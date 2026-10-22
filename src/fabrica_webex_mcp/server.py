"""MCP server (stdio). Every tool takes `org` (alias); GET-only tools also accept "all"."""
import time

import httpx
from mcp.server.fastmcp import FastMCP

from . import config, license_report, output, store, xsi
from .client import WebexClient

mcp = FastMCP("fabrica-webex")
_client: WebexClient | None = None

READ = {"GET", "HEAD"}
WRITE = {"POST", "PUT", "PATCH", "DELETE"}


def client() -> WebexClient:
    global _client
    if _client is None:
        _client = WebexClient(httpx.AsyncClient(timeout=60))
    return _client


def _targets(org: str | None) -> list[str]:
    if org == "all":
        aliases = config.all_aliases()
        if not aliases:
            raise config.ConfigError("No orgs registered. Run `fabrica-webex-mcp add-org`.")
        return aliases
    return [config.resolve(org)]


def _csv(s: str | None) -> list[str] | None:
    return [f.strip() for f in s.split(",") if f.strip()] if s else None


@mcp.tool()
def list_orgs() -> str:
    """List registered Webex orgs with default, write permission, and token expiry."""
    reg = config.load()
    now = time.time()
    rows = []
    for alias, o in sorted(reg["orgs"].items()):
        t = store.get_tokens(alias)
        rows.append({
            "alias": alias, "name": o.get("name"), "org_id": o.get("org_id"),
            "writable": o.get("writable", False), "default": alias == reg.get("default"),
            "has_tokens": bool(t),
            "refresh_token_days_left": round((t.refresh_expires_at - now) / 86400, 1) if t and t.refresh_expires_at else None,
        })
    return output.dumps({"orgs": rows})


@mcp.tool()
async def webex_request(
    method: str,
    path: str,
    org: str | None = None,
    query: dict | None = None,
    body: dict | list | None = None,
    fields: str | None = None,
    max_items: int = 200,
    max_chars: int = 40000,
    allow_write: bool = False,
) -> str:
    """Call any Webex REST endpoint (https://webexapis.com/v1) as the chosen org.

    path: e.g. "/people" or "/telephony/config/locations"; for CDR use the full URL
    "https://analytics.webexapis.com/v1/cdr_feed". GET list responses follow
    pagination automatically up to max_items. `fields` is a comma list of dotted keys to keep
    per item (e.g. "id,displayName,emails"). Use org="all" for GET across every org.
    POST/PUT/PATCH/DELETE require allow_write=true AND the org to be marked writable.
    """
    method = method.upper()
    if method not in READ | WRITE:
        raise ValueError(f"Unsupported method {method}")
    if method in WRITE:
        if org == "all":
            raise ValueError("Writes cannot target org='all'.")
        alias = config.resolve(org)
        if not allow_write:
            raise PermissionError("Write blocked: pass allow_write=true to confirm this change.")
        if not config.get_org(alias).get("writable", False):
            raise PermissionError(
                f"Org '{alias}' is read-only. Run `fabrica-webex-mcp set-writable {alias} on` to enable writes."
            )
    keep = _csv(fields)
    results = {}
    for alias in _targets(org):
        r = await client().request(alias, method, path, query, body, max_items=max_items if method == "GET" else None)
        if "items" in r:
            r["items"] = output.select(r["items"], keep)
        elif "data" in r:
            r["data"] = output.select(r["data"], keep)
        results[alias] = r
    if org != "all":
        return output.fit(results[alias], max_chars)
    return output.fit({"by_org": results}, max_chars)


async def _fetch(alias: str, path: str, query: dict | None = None, cap: int = 5000) -> list:
    return (await client().request(alias, "GET", path, query, max_items=cap))["items"]


@mcp.tool()
async def people_with_licenses(org: str | None = None, email_contains: str | None = None, max_items: int = 500) -> str:
    """People with license names resolved (id -> name), plus display name/email/status. org may be "all"."""
    out = {}
    for alias in _targets(org):
        names = {l["id"]: l.get("name", l["id"]) for l in await _fetch(alias, "/licenses")}
        rows = []
        for p in await _fetch(alias, "/people", {"max": 1000}):
            email = (p.get("emails") or [""])[0]
            if email_contains and email_contains.lower() not in email.lower():
                continue
            rows.append({
                "name": p.get("displayName"), "email": email, "status": p.get("status"),
                "licenses": [names.get(i, i) for i in p.get("licenses", [])],
            })
        out[alias] = {"count": len(rows), "people": rows[:max_items], "truncated": len(rows) > max_items}
    return output.fit({"by_org": out}, 60000)


@mcp.tool()
async def workspace_inventory(org: str | None = None) -> str:
    """Workspaces with location names and calling type. org may be "all"."""
    out = {}
    for alias in _targets(org):
        try:
            locs = {l["id"]: l.get("displayName") for l in await _fetch(alias, "/workspaceLocations")}
        except Exception:
            locs = {}
        rows = [{
            "name": w.get("displayName"), "type": w.get("type"), "capacity": w.get("capacity"),
            "location": locs.get(w.get("workspaceLocationId"), w.get("workspaceLocationId")),
            "calling": (w.get("calling") or {}).get("type"),
        } for w in await _fetch(alias, "/workspaces")]
        out[alias] = {"count": len(rows), "workspaces": rows}
    return output.fit({"by_org": out}, 60000)


@mcp.tool()
async def license_assignment_by_location(org: str | None = None, include_ocp: bool = True, location: str | None = None) -> str:
    """Webex Calling Professional, Workspace and Outbound Calling Plan (OCP) license counts per location.

    Does the whole scan server-side and returns only counts, totals and warnings. OCP has no bulk API
    (one lookup per licensed person/workspace), so on a large org pass include_ocp=false for a fast
    Professional/Workspace-only report. `location` (name, case-insensitive) adds the licensed people,
    workspaces and virtual lines at that location. "virtual_lines" is the share of "ocp" that belongs
    to virtual lines, not additional to it. org may be "all".
    """
    out = {}
    for alias in _targets(org):
        report = await license_report.collect(client(), alias, include_ocp)
        if location:
            entities = license_report.detail(report, location)
            if entities is None:
                raise ValueError(f"No location named {location!r} in org '{alias}'.")
        report.pop("_entities")
        out[alias] = {**report, "entities": entities} if location else report
    return output.fit(out[alias] if org != "all" else {"by_org": out}, 60000)


@mcp.tool()
async def user_registrations(entries: str, org: str | None = None) -> str:
    """Live SIP registrations (device/client, MAC, public/private IP, expiry) via Xsi-Actions, which is the only
    place they are visible.

    entries: comma-separated names, emails, Common Identity UUIDs or base64 Webex IDs. A name can be a user, a
    workspace or a virtual line (all three work); if a name matches more than one, the error lists them. Each
    entry succeeds or fails on its own. Subjects must be Webex Calling users; needs the spark-admin:xsi scope.
    """
    alias = config.resolve(org)
    return output.fit({"org": alias, "results": await xsi.lookup(client(), alias, entries)}, 60000)


def run() -> None:
    mcp.run(transport="stdio")
