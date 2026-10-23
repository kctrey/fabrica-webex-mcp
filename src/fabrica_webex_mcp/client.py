"""HTTP layer: URL safety, auth, 401 re-refresh, 429 retry, Link-header pagination."""
import asyncio
import re
from urllib.parse import parse_qsl, urlsplit

import httpx

from . import auth

BASE = "https://webexapis.com/v1/"
HOST = "webexapis.com"
ALLOWED_HOSTS = {HOST, "analytics.webexapis.com"}  # CDR / analytics live on the second host
MAX_RETRIES = 5
MAX_SLEEP = 60
_NEXT = re.compile(r'<([^>]+)>;\s*rel="next"')


class WebexError(Exception):
    def __init__(self, status: int, message: str, tracking_id: str | None = None):
        self.status = status
        super().__init__(f"HTTP {status}: {message}" + (f" (trackingId {tracking_id})" if tracking_id else ""))


def _collection_key(data) -> str | None:
    """Webex lists use "items", but Calling endpoints wrap them as {"locations": [...]} etc."""
    if not isinstance(data, dict):
        return None
    if isinstance(data.get("items"), list):
        return "items"
    if len(data) == 1:
        (k, v), = data.items()
        return k if isinstance(v, list) else None
    return None


def build_url(path: str, query: dict | None = None) -> tuple[str, dict]:
    """Normalize a relative path or webexapis.com URL; reject any other host (token safety)."""
    parts = urlsplit(path)
    if parts.scheme or parts.netloc:
        if parts.scheme != "https" or parts.hostname not in ALLOWED_HOSTS:
            raise ValueError(f"Only https URLs on {sorted(ALLOWED_HOSTS)} are allowed, got: {path}")
        if parts.hostname != HOST:  # e.g. analytics.webexapis.com/v1/cdr_feed: keep host and path as given
            q = dict(parse_qsl(parts.query))
            q.update({k: v for k, v in (query or {}).items() if v is not None})
            return f"https://{parts.hostname}{parts.path}", q
    rel = parts.path
    rel = rel.lstrip("/")
    if rel.startswith("v1/"):
        rel = rel[3:]
    if not rel or ".." in rel.split("/"):
        raise ValueError(f"Invalid path: {path!r}")
    q = dict(parse_qsl(parts.query))
    q.update({k: v for k, v in (query or {}).items() if v is not None})
    return BASE + rel, q


class WebexClient:
    def __init__(self, http: httpx.AsyncClient):
        self.http = http

    async def _send(self, alias: str, method: str, url: str, params, body,
                    headers: dict | None = None, files: dict | None = None,
                    form: dict | None = None) -> httpx.Response:
        token = await auth.get_access_token(alias, self.http)
        refreshed = False
        for attempt in range(MAX_RETRIES + 1):
            r = await self.http.request(
                method, url, params=params, json=body, files=files, data=form,
                headers={**(headers or {}), "Authorization": f"Bearer {token}"},
            )
            if r.status_code == 401 and not refreshed:
                token = await auth.get_access_token(alias, self.http, force=True)
                refreshed = True
                continue
            if r.status_code == 429 and attempt < MAX_RETRIES:
                try:
                    wait = float(r.headers.get("Retry-After", "5"))
                except ValueError:
                    wait = 5.0
                await asyncio.sleep(min(max(wait, 1.0), MAX_SLEEP))
                continue
            return r
        return r

    async def get_external(self, alias: str, url: str, host_suffix: str, headers: dict | None = None) -> httpx.Response:
        """GET a service on a host outside ALLOWED_HOSTS (e.g. the org's Xsi-Actions endpoint) with the org's token.

        The caller has discovered the URL from the org record, not from user input, but the token still only
        goes to https hosts under `host_suffix` (e.g. ".webex.com"). Returns the raw response; errors are the caller's.
        """
        parts = urlsplit(url)
        if parts.scheme != "https" or not (parts.hostname or "").endswith(host_suffix):
            raise ValueError(f"Refusing to send a token to {url!r}: only https hosts under {host_suffix} are allowed.")
        return await self._send(alias, "GET", url, None, None, headers)

    async def request(
        self, alias: str, method: str, path: str,
        query: dict | None = None, body: dict | list | None = None,
        max_items: int | None = None,
        files: dict | None = None, form: dict | None = None,
    ) -> dict:
        """Returns {"status", "data"|"items", "pages", "truncated"}. GET follows rel="next".

        `files` ({field: (filename, bytes, content_type)}) and `form` send a multipart/form-data body
        instead of JSON; they are mutually exclusive with `body`.
        """
        method = method.upper()
        if (files or form) and body is not None:
            raise ValueError("Pass either a JSON body or file/form fields, not both.")
        url, params = build_url(path, query)
        items: list = []
        collection = None
        pages = 0
        truncated = False
        while True:
            r = await self._send(alias, method, url, params, body, files=files, form=form)
            pages += 1
            if r.status_code >= 400:
                try:
                    j = r.json()
                    msg = j.get("message") or j.get("errors") or r.text
                except ValueError:
                    msg = r.text
                raise WebexError(r.status_code, str(msg)[:500], r.headers.get("TrackingID"))
            if not r.content:
                return {"status": r.status_code}
            data = r.json()
            key = _collection_key(data) if method == "GET" else None
            if not key:
                return {"status": r.status_code, "data": data}
            collection = collection or key
            items.extend(data[key])
            if max_items is not None and len(items) >= max_items:
                truncated = len(items) > max_items or bool(_NEXT.search(r.headers.get("Link", "")))
                items = items[:max_items]
                break
            m = _NEXT.search(r.headers.get("Link", ""))
            if not m:
                break
            url, params = build_url(m.group(1))  # next URL already carries its query
        out = {"status": 200, "items": items, "pages": pages, "truncated": truncated}
        if collection != "items":
            out["collection"] = collection  # e.g. "locations" for /telephony/config/locations
        return out
