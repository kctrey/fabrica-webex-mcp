"""Per-org access-token lifecycle: use cached token, refresh when near expiry, persist rotation."""
import asyncio
import time

import httpx

from . import store

TOKEN_URL = "https://webexapis.com/v1/access_token"
SKEW = 120  # refresh this many seconds before expiry

_locks: dict[str, asyncio.Lock] = {}


class AuthError(Exception):
    pass


def _lock(alias: str) -> asyncio.Lock:
    return _locks.setdefault(alias, asyncio.Lock())


async def refresh(alias: str, refresh_token: str, http: httpx.AsyncClient) -> store.Tokens:
    """Exchange a refresh token and persist the new pair. Raises AuthError on rejection."""
    client = store.get_client()
    if not client:
        raise AuthError("Service App client credentials not stored. Run `fabrica-webex-mcp setup`.")
    cid, secret = client
    r = await http.post(
        TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "client_id": cid,
            "client_secret": secret,
            "refresh_token": refresh_token,
        },
    )
    if r.status_code != 200:
        raise AuthError(
            f"Token refresh failed for org '{alias}' (HTTP {r.status_code}): {r.text[:300]}. "
            f"If the refresh token expired, run `fabrica-webex-mcp add-org {alias} --replace` to log in again."
        )
    return _save(alias, r.json(), refresh_token)


def _save(alias: str, d: dict, fallback_refresh: str = "") -> store.Tokens:
    now = time.time()
    t = store.Tokens(
        access=d["access_token"],
        refresh=d.get("refresh_token", fallback_refresh),  # keep old one if not rotated
        access_expires_at=now + int(d.get("expires_in", 0)),
        refresh_expires_at=now + int(d.get("refresh_token_expires_in", 0)),
    )
    store.set_tokens(alias, t)
    return t


async def exchange_code(alias: str, code: str, redirect_uri: str, http: httpx.AsyncClient) -> store.Tokens:
    """Authorization-code exchange (Integration login); persists the token pair."""
    client = store.get_client()
    if not client:
        raise AuthError("Integration client credentials not stored. Run `fabrica-webex-mcp setup`.")
    cid, secret = client
    r = await http.post(
        TOKEN_URL,
        data={"grant_type": "authorization_code", "client_id": cid, "client_secret": secret,
              "code": code, "redirect_uri": redirect_uri},
    )
    if r.status_code != 200:
        raise AuthError(f"Code exchange failed (HTTP {r.status_code}): {r.text[:300]}")
    return _save(alias, r.json())


async def get_access_token(alias: str, http: httpx.AsyncClient, force: bool = False) -> str:
    async with _lock(alias):
        t = store.get_tokens(alias)
        if not t:
            raise AuthError(f"No tokens stored for org '{alias}'. Run `fabrica-webex-mcp add-org {alias}`.")
        if not force and t.access and t.access_expires_at - time.time() > SKEW:
            return t.access
        return (await refresh(alias, t.refresh, http)).access
