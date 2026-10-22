"""User registrations via Xsi-Actions.

Xsi is the BroadWorks Xtended Services Interface under Webex Calling, and the only place live SIP registrations
are visible. Only Xsi-Actions (plain request/response) is used here; Xsi-Events is a long-lived channel and is
deliberately out of scope.

* Endpoint: the org record's `xsiActionsEndpoint` (needs callingData=true), so no SRV lookups.
* Auth: the ordinary access token as a bearer; `spark-admin:xsi` to read other users.
* Subject ID: the Cisco Common Identity UUID, i.e. the UUID at the end of a base64-decoded Webex ID. Users,
  workspaces and virtual lines all share that identity space, so the same Registrations call works for all three.
* JSON is requested; Xsi translates its XML BadgerFish-style, with element text under "$".
"""
import asyncio
import base64
import re
import uuid
from urllib.parse import quote

from . import config
from .client import WebexClient, WebexError

PROTOCOL_VERSION = "25.0"  # the only version Webex Calling accepts
HOST_SUFFIX = ".webex.com"
CONCURRENCY = 5
_endpoints: dict[str, str] = {}


class XsiError(Exception):
    """Written to be shown to a user."""


# -- BadgerFish ------------------------------------------------------------

def text(node, default: str = "") -> str:
    if node is None:
        return default
    value = node.get("$", default) if isinstance(node, dict) else node
    return str(value).strip() if value is not None else default


def as_list(node) -> list:
    """One repeated element arrives as an object, several as an array."""
    if node is None:
        return []
    return node if isinstance(node, list) else [node]


# -- Registration parsing --------------------------------------------------

# publicIP/privateIP are documented but not always present; they are recoverable from the Path header and URI
_NAT_IP = re.compile(r'x-bw-nat="(\d{1,3}(?:\.\d{1,3}){3})')
_FIREWALL_IP = re.compile(r"firewall=(\d{1,3}(?:\.\d{1,3}){3})")
_ENDPOINT_IP = re.compile(r"endpoint=(\d{1,3}(?:\.\d{1,3}){3})")
_HOST_IP = re.compile(r"@(\d{1,3}(?:\.\d{1,3}){3})")
# MACs come colon-separated in the User-Agent, or bare inside an underscore-delimited token
_COLON_MAC = re.compile(r"(?:[A-F0-9]{2}:){5}[A-F0-9]{2}")
_BARE_MAC = re.compile(r"_([A-F0-9]{12})_")
_SUMMARY = re.compile(r"<summary[^>]*>(.*?)</summary>", re.IGNORECASE | re.DOTALL)


def _public_ip(entry: dict, path_header: str, uri: str) -> str:
    if explicit := text(entry.get("publicIP")):
        return explicit
    for pattern, haystack in ((_NAT_IP, path_header), (_FIREWALL_IP, uri)):
        if haystack and (found := pattern.search(haystack)):
            return found.group(1)
    return ""


def _private_ip(entry: dict, uri: str) -> str:
    if explicit := text(entry.get("privateIP")):
        return explicit
    if not uri:
        return ""
    # a device behind NAT puts its own address in endpoint=; otherwise the URI host is the private address
    found = (_ENDPOINT_IP if "endpoint=" in uri else _HOST_IP).search(uri)
    return found.group(1) if found else ""


def _mac(user_agent: str) -> str:
    upper = (user_agent or "").upper()
    if found := _COLON_MAC.search(upper):
        return found.group()
    if found := _BARE_MAC.search(upper):
        raw = found.group(1)
        return ":".join(raw[i:i + 2] for i in range(0, 12, 2))
    return ""


def _model_and_version(user_agent: str) -> tuple[str, str]:
    """Best-effort split of a free-form SIP User-Agent; never raises."""
    if not user_agent:
        return "Unknown", ""
    parts = user_agent.split("/")
    model = parts[0].strip()
    if len(parts) > 1:
        version = re.split(r"[_/]", parts[1])[0].strip()
    else:
        words = model.split()
        if len(words) > 1 and re.fullmatch(r"\d[\d.]*", words[-1]):
            model, version = " ".join(words[:-1]), words[-1]
        else:
            version = ""
    lowered = user_agent.lower()
    if "spark" in lowered:  # the Webex app reports a platform rather than a model
        if "windows" in lowered:
            model = "Webex for Windows"
        elif "macos" in lowered:
            model = "Webex for MacOS"
        version = version.split(" ")[0]
    return model or "Unknown", version


def _registration(entry: dict) -> dict:
    user_agent, uri = text(entry.get("userAgent")), text(entry.get("uri"))
    endpoint_type = text(entry.get("endpointType")) or "Unknown"
    if endpoint_type.lower() == "mobility":  # Webex Go registers as Mobility
        endpoint_type = "Webex Go"
    model, version = _model_and_version(user_agent)
    row = {
        "type": endpoint_type, "model": model, "version": version, "mac": _mac(user_agent),
        "expiration": text(entry.get("expiration")) or "NA",
        "public_ip": _public_ip(entry, text(entry.get("pathHeader")), uri) or "Unknown",
        "public_port": text(entry.get("publicPort")),
        "private_ip": _private_ip(entry, uri), "private_port": text(entry.get("privatePort")),
        "line_port": text(entry.get("linePort")),
    }
    return {k: v for k, v in row.items() if v != ""}


def parse_registrations(payload: dict) -> list[dict]:
    return [_registration(e) for e in as_list((payload.get("Registrations") or {}).get("registration"))]


# -- Profile / labels ------------------------------------------------------

_PLACEHOLDER_NAMES = {".", "-", "_"}  # a workspace's whole name is in firstName; lastName is a placeholder
_SUBJECT_TYPES = {"Place": "Workspace", "VirtualLine": "Virtual Line"}


def _name_part(value: str | None) -> str:
    value = (value or "").strip()
    return "" if value in _PLACEHOLDER_NAMES else value


def label_from_profile(payload: dict, fallback: str) -> tuple[str, str]:
    """(name, type) from a profile payload; externalIdType is the only place the subject's kind is visible."""
    details = (payload.get("Profile") or {}).get("details") or {}
    if not isinstance(details, dict):
        return fallback, ""
    details = {k: text(v) for k, v in details.items() if not k.startswith("@")}
    name = " ".join(p for p in (_name_part(details.get("firstName")), _name_part(details.get("lastName"))) if p)
    kind = (details.get("externalIdType") or "").strip()
    return name or details.get("userId", "") or fallback, _SUBJECT_TYPES.get(kind, kind)


# -- Subject resolution ----------------------------------------------------

_UUID = re.compile(r"^[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")
_RESOURCE = {"PEOPLE": "User", "PLACE": "Workspace", "VIRTUAL_LINE": "Virtual Line"}


def decode_webex_id(value: str) -> tuple[str, str] | None:
    """A base64 Webex ID (ciscospark://us/<RESOURCE>/<uuid>) as (resource, uuid), or None."""
    try:
        decoded = base64.b64decode(value + "=" * (-len(value) % 4)).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    if not decoded.startswith("ciscospark://"):
        return None
    parts = decoded.rstrip("/").split("/")
    if len(parts) < 2 or not _UUID.match(parts[-1]):
        return None
    return parts[-2], parts[-1]


def split_entries(raw: str) -> list[str]:
    return [p.strip() for p in (raw or "").split(",") if p.strip()]


async def _list(c: WebexClient, alias: str, path: str, query: dict | None = None) -> list:
    return (await c.request(alias, "GET", path, query, max_items=1000))["items"]


async def _virtual_lines(c: WebexClient, alias: str) -> list:
    r = await c.request(alias, "GET", "/telephony/config/virtualLines", {"max": 1000})
    return r["items"] if "items" in r else (r.get("data") or {}).get("virtualLines", [])


async def _find_by_name(c: WebexClient, alias: str, name: str) -> list[dict]:
    """People, workspaces and virtual lines whose name matches; exact (case-insensitive) matches win over partial."""
    people, workspaces, lines = await asyncio.gather(
        _list(c, alias, "/people", {"displayName": name, "max": 100}),
        _list(c, alias, "/workspaces", {"displayName": name}),
        _virtual_lines(c, alias))
    found = [{"id": p["id"], "name": p.get("displayName", ""), "type": "User",
              "detail": (p.get("emails") or [""])[0]} for p in people]
    found += [{"id": w["id"], "name": w.get("displayName", ""), "type": "Workspace", "detail": ""} for w in workspaces]
    for v in lines:
        full = " ".join(p for p in (_name_part(v.get("firstName")), _name_part(v.get("lastName"))) if p)
        found.append({"id": v["id"], "name": full, "type": "Virtual Line",
                      "detail": (v.get("number") or {}).get("extension", "")})
    wanted = name.lower()
    found = [f for f in found if wanted in f["name"].lower()]
    return [f for f in found if f["name"].lower() == wanted] or found


async def resolve_subject(c: WebexClient, alias: str, entry: str) -> tuple[str, str, str]:
    """(xsi_user_id, label, type) for an email, a name, a bare UUID or a base64 Webex ID.

    type is "" for a bare UUID. It is worth knowing from the Webex side because Xsi's own externalIdType calls a
    virtual line a "User".

    A name may belong to a user, a workspace or a virtual line; if more than one matches, the error lists them
    so the caller can pick (by email, UUID or a fuller name).
    """
    if _UUID.match(entry):
        return entry, entry, ""
    if decoded := decode_webex_id(entry):
        kind = _RESOURCE.get(decoded[0], decoded[0])
        return decoded[1], f"{kind} {decoded[1]}", kind
    if "@" in entry:
        people = await _list(c, alias, "/people", {"email": entry})
        if not people:
            raise XsiError(f"No user in this organization has the email address {entry}.")
        matches = [{"id": people[0]["id"], "name": people[0].get("displayName") or entry, "type": "User"}]
    else:
        matches = await _find_by_name(c, alias, entry)
    if not matches:
        raise XsiError(f"Nothing in this organization (user, workspace or virtual line) is named '{entry}'.")
    if len(matches) > 1:
        options = "; ".join(f"{m['name']} ({m['type']}{', ' + m['detail'] if m.get('detail') else ''})"
                            for m in matches[:8])
        more = f" and {len(matches) - 8} more" if len(matches) > 8 else ""
        raise XsiError(f"'{entry}' matches more than one: {options}{more}. Use an email address or a fuller name.")
    decoded = decode_webex_id(matches[0]["id"])
    if not decoded:
        raise XsiError(f"Could not work out an Xsi user ID for {matches[0]['name']}.")
    return decoded[1], matches[0]["name"], matches[0]["type"]


# -- Xsi-Actions client ----------------------------------------------------

def _error_message(response) -> str:
    """Xsi returns ErrorInfo as XML even when JSON was asked for; 404 almost always means 'not a Calling user'."""
    detail = ""
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        for key in ("ErrorInfo", "errorInfo"):
            if isinstance(body.get(key), dict):
                detail = text(body[key].get("summary")) or text(body[key].get("summaryEnglish"))
                break
    if not detail and response.text and (found := _SUMMARY.search(response.text)):
        detail = found.group(1).strip()
    guidance = {
        401: "Xsi rejected the access token; it needs the 'spark-admin:xsi' scope, and tokens issued before it was "
             "added do not have it. Cisco must enable that scope for your org; it is only requested when you log in "
             "with `add-org <alias> --replace --with-xsi`.",
        403: "Xsi refused this request; reading another user's registrations needs the 'spark-admin:xsi' scope "
             "(which Cisco must approve for your org, and log in with --with-xsi) and an administrator token.",
        404: "Xsi does not know this subject. That usually means it is not a Webex Calling user, not that the "
             "address is wrong.",
    }.get(response.status_code, f"Xsi-Actions returned HTTP {response.status_code}.")
    return f"{guidance} {detail}".strip() if detail else guidance


async def endpoint(c: WebexClient, alias: str) -> str:
    """The org's Xsi-Actions base URL, discovered once per org."""
    if alias not in _endpoints:
        # callingData=true is required: without it the org omits xsiActionsEndpoint rather than erroring
        org = (await c.request(alias, "GET", f"/organizations/{config.get_org(alias)['org_id']}",
                               {"callingData": "true"}))["data"]
        url = (org.get("xsiActionsEndpoint") or "").rstrip("/")
        if not url:
            raise XsiError("This organization does not publish an Xsi-Actions endpoint, so Xsi is not available.")
        _endpoints[alias] = url
    return _endpoints[alias]


async def xsi_get(c: WebexClient, alias: str, path: str) -> dict:
    tracking_id = f"WebexMCP_{uuid.uuid4()}"
    url = f"{await endpoint(c, alias)}/v2.0/{path.lstrip('/')}"
    r = await c.get_external(alias, url, HOST_SUFFIX, {
        "Accept": "application/json", "X-BroadWorks-Protocol-Version": PROTOCOL_VERSION, "trackingId": tracking_id})
    if r.status_code >= 400:
        raise XsiError(f"{_error_message(r)} (trackingId {tracking_id})")
    if not r.content:
        return {}
    try:
        return r.json()
    except ValueError as e:
        raise XsiError("Xsi-Actions returned a response that could not be read as JSON.") from e


async def lookup(c: WebexClient, alias: str, raw: str) -> list[dict]:
    """Registrations for each comma-separated entry. A failure is recorded against its entry, not raised, so
    one bad entry does not lose the others."""
    sem = asyncio.Semaphore(CONCURRENCY)

    async def one(entry: str) -> dict:
        result = {"entry": entry, "registrations": []}
        async with sem:
            try:
                user_id, label, kind = await resolve_subject(c, alias, entry)
                result["subject"] = f"{label} ({kind})" if kind and kind not in label else label
                try:  # names the subject and says what kind it is, but is not needed for the registrations
                    name, xsi_kind = label_from_profile(await xsi_get(c, alias, f"user/{quote(user_id)}/profile"), label)
                    kind = kind or xsi_kind
                    result["subject"] = f"{name} ({kind})" if kind else name
                except (XsiError, WebexError):
                    pass
                payload = await xsi_get(c, alias, f"user/{quote(user_id)}/profile/Registrations")
                result["registrations"] = parse_registrations(payload)
            except (XsiError, WebexError) as e:
                result["error"] = str(e)
        return result

    return await asyncio.gather(*(one(e) for e in split_entries(raw)))
