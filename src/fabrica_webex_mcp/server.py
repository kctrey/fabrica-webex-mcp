"""MCP server (stdio). Every tool takes `org` (alias); GET-only tools also accept "all"."""
import mimetypes
import os
import time
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP

from . import announcements, calling_features, config, license_report, output, store, xsi
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


def _write_alias(org: str | None, allow_write: bool) -> str:
    """Resolve the org for a write and enforce both guards (allow_write on the call, org marked writable)."""
    if org == "all":
        raise ValueError("Writes cannot target org='all'.")
    alias = config.resolve(org)
    if not allow_write:
        raise PermissionError("Write blocked: pass allow_write=true to confirm this change.")
    if not config.get_org(alias).get("writable", False):
        raise PermissionError(
            f"Org '{alias}' is read-only. Run `fabrica-webex-mcp set-writable {alias} on` to enable writes."
        )
    return alias


MAX_UPLOAD_BYTES = 10 * 1024 * 1024
UPLOAD_DIR_ENV = "WEBEX_MCP_UPLOAD_DIR"  # optional: if set, uploads may only come from inside this folder


def _load_upload(file_path: str) -> tuple[str, bytes, str]:
    """Read a local file for a multipart upload: (filename, bytes, content_type)."""
    p = Path(file_path).expanduser()
    if not p.is_file():
        raise ValueError(f"Upload file not found: {file_path}")
    allowed = os.environ.get(UPLOAD_DIR_ENV)
    if allowed and not p.resolve().is_relative_to(Path(allowed).expanduser().resolve()):
        raise ValueError(f"Uploads are restricted to {allowed} ({UPLOAD_DIR_ENV}); {file_path} is outside it.")
    if p.stat().st_size > MAX_UPLOAD_BYTES:
        raise ValueError(f"Upload file is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB: {file_path}")
    ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
    return p.name, p.read_bytes(), ctype


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
    file_path: str | None = None,
    file_field: str = "file",
    form: dict | None = None,
) -> str:
    """Call any Webex REST endpoint (https://webexapis.com/v1) as the chosen org.

    path: e.g. "/people" or "/telephony/config/locations"; for CDR use the full URL
    "https://analytics.webexapis.com/v1/cdr_feed". GET list responses follow
    pagination automatically up to max_items. `fields` is a comma list of dotted keys to keep
    per item (e.g. "id,displayName,emails"). Use org="all" for GET across every org.
    POST/PUT/PATCH/DELETE require allow_write=true AND the org to be marked writable.

    File upload (POST/PUT/PATCH only): pass `file_path` (a local file, max 10 MB) to send a
    multipart/form-data request, e.g. a WAV voicemail greeting. `file_field` is the form field name
    (default "file"); `form` adds extra text fields. Cannot be combined with `body`.
    """
    method = method.upper()
    if method not in READ | WRITE:
        raise ValueError(f"Unsupported method {method}")
    files = None
    if file_path or form:
        if method not in {"POST", "PUT", "PATCH"}:
            raise ValueError("File/form uploads are only supported for POST, PUT and PATCH.")
        if body is not None:
            raise ValueError("Pass either `body` (JSON) or `file_path`/`form` (multipart), not both.")
    if file_path:
        files = {file_field: _load_upload(file_path)}
    if method in WRITE:
        _write_alias(org, allow_write)
    keep = _csv(fields)
    results = {}
    for alias in _targets(org):
        r = await client().request(alias, method, path, query, body,
                                   max_items=max_items if method == "GET" else None, files=files, form=form)
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
async def calling_feature_audit(feature: str, org: str | None = None, subjects: list[str] | None = None,
                                fields: list[str] | str | None = None, filter: str | None = None,
                                location: str | None = None, max_rows: int = 300) -> str:
    """Read one Calling feature for EVERY person, workspace and virtual line in an org and return a compact table.

    feature: voicemail, doNotDisturb, callForwarding, callRecording (see calling_features.FEATURES; a new feature
    is one table row). subjects: any of people, workspaces, virtual_lines (default all three). fields: dotted paths
    to show, e.g. ["emailCopyOfMessage.enabled","messageStorage.storageType"] (default per feature). filter: e.g.
    "emailCopyOfMessage.enabled == true" (ops == != >= <= > <, join with "and"); judged on the full response, and the
    filter field is added to the columns. location: location name. Returns counts per subject type, the org-level
    setting (voicemail: /telephony/config/voicemail/settings), the matching rows, `related_non_defaults` (settings
    outside your fields that change the outcome, e.g. voicemail storageType=EXTERNAL, judged on ALL scanned subjects,
    not only matches), and separate not_applicable / errors lists (e.g. a workspace on a Workspace license has no
    voicemail). People without a Calling location are listed as not applicable. `truncated` is set if rows were cut.
    Read-only; org may be "all".
    """
    if isinstance(fields, str):
        fields = _csv(fields)
    out = {}
    for alias in _targets(org):
        out[alias] = await calling_features.audit(client(), alias, feature, subjects, fields, filter, location, max_rows)
    return output.fit(out[alias] if org != "all" else {"by_org": out}, 60000)


@mcp.tool()
async def calling_feature_bulk_update(feature: str, targets: list[str] | str, change: dict, org: str | None = None,
                                      dry_run: bool = True, plan_id: str | None = None, canary_first: bool = True,
                                      subjects: list[str] | None = None, filter: str | None = None,
                                      location: str | None = None, allow_write: bool = False) -> str:
    """Change one Calling feature on many people, workspaces and virtual lines (and/or the org-level setting).

    targets: names, emails, extensions or ids, "org" for the org-level setting (e.g. voicemail
    /telephony/config/voicemail/settings: the full body is sent back with only your key changed), or "from_audit"
    with the same `filter` (and optional `subjects`, `location`) as calling_feature_audit. change: partial body,
    e.g. {"emailCopyOfMessage":{"enabled":false}}. A single org only; never "all".

    dry_run=true (default) reads current state and returns a before/after plan, warnings, related non-default
    settings and a plan_id; nothing is written. To apply: show the plan to the user, get an explicit yes, then call
    again with dry_run=false, the SAME arguments, plan_id from the plan, and allow_write=true. The call is refused,
    with nothing written, if plan_id is missing or the current state or arguments differ from the plan. With
    canary_first (default) the first target is written and read back before the rest; a failure stops everything.
    Every target is verified by a GET; the result has one row each (before/after/verified/error) plus side effects
    seen in other fields. Requires allow_write=true (not for dry_run) and a writable org.
    """
    if org == "all":
        raise ValueError("Writes cannot target org='all'.")
    if dry_run:
        alias = config.resolve(org)
        plan, _ = await calling_features.build_plan(client(), alias, feature, targets, change, subjects, filter, location)
        plan["writable"] = bool(config.get_org(alias).get("writable", False))
        plan["next_step"] = ("Review, get an explicit yes, then repeat this call with dry_run=false, the same arguments, "
                             f"plan_id={plan['plan_id']} and allow_write=true.")
        return output.fit(plan, 60000)
    alias = _write_alias(org, allow_write)
    return output.fit(await calling_features.bulk_update(
        client(), alias, feature, targets, change, plan_id or "", canary_first, subjects, filter, location), 60000)


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


@mcp.tool()
def inspect_audio(file_path: str) -> str:
    """Check a local WAV before uploading it: format, channels, sample rate, duration, size, and anything
    Webex is likely to reject. Read-only, standard library only; it never converts or modifies the file."""
    _, data, _ = _load_upload(file_path)
    return output.dumps(announcements.inspect_wav(data))


@mcp.tool()
def list_announcement_slots() -> str:
    """The greeting slots assign_announcement can set, per feature (call queue, auto attendant), with how many
    files each takes and whether the write path has been verified against a live org."""
    return output.dumps({"slots": announcements.slot_table()})


@mcp.tool()
async def list_announcements(location: str | None = None, include_references: bool = False,
                             org: str | None = None) -> str:
    """Announcement Repository files: org-level, or one location's own when `location` (name or id) is given.

    include_references adds featureReferenceCount per file (one extra call each); a file with references cannot
    be deleted. Also returns the org's storage usage and per-file size limit.
    """
    alias = config.resolve(org)
    items = await announcements.list_announcements(client(), alias, location, include_references)
    rows = [{k: a.get(k) for k in ("id", "name", "fileName", "fileSize", "mediaFileType", "level", "lastUpdated",
                                   "isTextToSpeech", "featureReferenceCount") if k in a} for a in items]
    return output.fit({"org": alias, "count": len(rows), "usage": await announcements.usage(client(), alias),
                       "items": rows}, 40000)


@mcp.tool()
async def upload_announcement(file_path: str, name: str, location: str | None = None, org: str | None = None,
                              allow_write: bool = False) -> str:
    """Upload a local WAV to the Announcement Repository (org level, or a location's when `location` is given).

    The file is checked first (format, size against the org's limit). `name` is the announcement's display
    name (required by Webex). Afterwards use assign_announcement to point a queue or auto attendant at it.
    Requires allow_write=true and a writable org.
    """
    alias = _write_alias(org, allow_write)
    return output.dumps({"org": alias, **await announcements.upload(client(), alias, _load_upload(file_path), name, location)})


@mcp.tool()
async def assign_announcement(feature: str, entity: str, slot: str, announcements_to_play: list[str] | None = None,
                              location: str | None = None, dry_run: bool = False, org: str | None = None,
                              allow_write: bool = False) -> str:
    """Point a feature's greeting slot at repository announcements (greeting becomes CUSTOM).

    feature: "queue" or "auto_attendant" (see list_announcement_slots for slots such as welcome, comfort, moh,
    whisper, overflow, business_hours, after_hours). entity: the queue/auto attendant name or id; add `location`
    if the name is not unique. announcements_to_play: names or ids from the repository, in play order (queues
    take up to 4); leave empty to return the slot to DEFAULT. Returns the slot before and after (keep `before` to
    undo) plus warnings. dry_run=true shows the exact request without changing anything. NOTE: Webex keeps
    old file links when a slot returns to DEFAULT, so avoid attaching test files to live features.
    Requires allow_write=true (not for dry_run) and a writable org.
    """
    alias = config.resolve(org) if dry_run else _write_alias(org, allow_write)
    return output.fit({"org": alias, **await announcements.assign(
        client(), alias, feature, entity, slot, announcements_to_play or [], location, dry_run)}, 40000)


@mcp.tool()
async def delete_announcement(announcement: str, location: str | None = None, org: str | None = None,
                              allow_write: bool = False) -> str:
    """Delete one Announcement Repository file (by name or id; add `location` for a location-level file).

    Refused up front when any feature still references the file, with an explanation of how to free it.
    Requires allow_write=true and a writable org.
    """
    alias = _write_alias(org, allow_write)
    return output.dumps({"org": alias, **await announcements.delete(client(), alias, announcement, location)})


@mcp.tool()
async def set_voicemail_greeting(person: str, file_path: str, kind: str = "no_answer", org: str | None = None,
                                 allow_write: bool = False) -> str:
    """Upload a custom voicemail greeting WAV for a person (email or exact display name) and read it back.

    kind: "no_answer" (unanswered calls) or "busy". Requires allow_write=true and a writable org.
    """
    alias = _write_alias(org, allow_write)
    return output.dumps({"org": alias, **await announcements.set_voicemail_greeting(
        client(), alias, person, kind, _load_upload(file_path))})


def run() -> None:
    mcp.run(transport="stdio")
