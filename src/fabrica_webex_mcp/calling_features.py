"""Audit and bulk-change one Calling feature across every people, workspace and virtual line in an org.

A feature is a row in FEATURES: one endpoint per subject type (plus an optional org-level one), the columns an
audit shows by default, and "related" settings that can defeat a change made through the main ones (e.g. voicemail
stored EXTERNAL still reaches email when "copy of message" is off). Adding a feature is one new row.

Everything is assembled server-side with limited concurrency; the client already retries 429s.
"""
import asyncio
import hashlib
import json
import operator
import re

import httpx

from . import output
from .client import WebexClient, WebexError
from .license_report import _items, _virtual_lines

PEOPLE, WORKSPACES, VIRTUAL_LINES, ORG = "people", "workspaces", "virtual_lines", "org"
SUBJECTS = (PEOPLE, WORKSPACES, VIRTUAL_LINES)
READ_CONCURRENCY = 10
WRITE_CONCURRENCY = 5
MAX_TARGETS = 200
LIST_CAP = 100  # entries kept in each side list (not applicable, errors, related) before "omitted" counts

FEATURES: dict[str, dict] = {
    "voicemail": {
        "paths": {PEOPLE: "/people/{id}/features/voicemail",
                  WORKSPACES: "/telephony/config/workspaces/{id}/voicemail",
                  VIRTUAL_LINES: "/telephony/config/virtualLines/{id}/voicemail"},
        "org_path": "/telephony/config/voicemail/settings",
        "fields": ["enabled", "emailCopyOfMessage.enabled", "emailCopyOfMessage.emailId",
                   "messageStorage.storageType"],
        # (path, op, value, why): flagged when outside the requested fields
        "related": [
            ("messageStorage.storageType", "==", "EXTERNAL", "voicemail is delivered to email (external storage) regardless of emailCopyOfMessage"),
            ("emailCopyOfMessage.enabled", "==", True, "a copy of each message is emailed"),
            ("notifications.enabled", "==", True, "voicemail notifications are sent to a destination"),
            ("faxMessage.enabled", "==", True, "voicemail fax delivery is on"),
        ],
        "org_related": [
            ("voiceMessageForwardingEnabled", "==", True,
             "org-level forwarding is on: it can send voicemail to email; turning it off switched a user's EXTERNAL storage to INTERNAL"),
        ],
        # Subjects whose PUT was seen accepting a partial body (people: reported by the user). Others rely on the canary.
        "partial_put_verified": {PEOPLE},
    },
    "doNotDisturb": {
        "paths": {PEOPLE: "/people/{id}/features/doNotDisturb",
                  WORKSPACES: "/telephony/config/workspaces/{id}/doNotDisturb",
                  VIRTUAL_LINES: "/telephony/config/virtualLines/{id}/doNotDisturb"},
        "fields": ["enabled", "ringSplashEnabled"],
        "related": [], "partial_put_verified": set(),
    },
    "callForwarding": {
        "paths": {PEOPLE: "/people/{id}/features/callForwarding",
                  WORKSPACES: "/workspaces/{id}/features/callForwarding",
                  VIRTUAL_LINES: "/telephony/config/virtualLines/{id}/callForwarding"},
        "fields": ["callForwarding.always.enabled", "callForwarding.always.destination",
                   "callForwarding.selective.enabled", "callForwarding.busy.enabled", "callForwarding.noAnswer.enabled"],
        "related": [], "partial_put_verified": set(),
    },
    "callRecording": {
        # No workspace route found (both /telephony/config/workspaces/{id}/callRecording and /workspaces/{id}/features/callRecording 404);
        # a subject type with no path is reported as not audited rather than guessed at.
        "paths": {PEOPLE: "/people/{id}/features/callRecording",
                  VIRTUAL_LINES: "/telephony/config/virtualLines/{id}/callRecording"},
        "fields": ["enabled", "record", "notification.enabled"],
        "related": [], "partial_put_verified": set(),
    },
}

_OPS = {"==": operator.eq, "!=": operator.ne, ">=": operator.ge, "<=": operator.le, ">": operator.gt, "<": operator.lt}
_COND = re.compile(r"^\s*([\w.]+)\s*(==|!=|>=|<=|>|<)\s*(.+?)\s*$")
_AND = re.compile(r"\s+and\s+|\s*&&\s*", re.I)


# ---------- small helpers ----------

def feature_spec(feature: str) -> dict:
    for name, spec in FEATURES.items():
        if name.lower() == feature.strip().lower():
            return {**spec, "name": name}
    raise ValueError(f"Unknown feature {feature!r}. Supported: {', '.join(FEATURES)}. "
                     "A new feature is one row in calling_features.FEATURES.")


def _subjects_arg(subjects) -> list[str]:
    wanted = list(subjects) if subjects else list(SUBJECTS)
    bad = [s for s in wanted if s not in SUBJECTS]
    if bad:
        raise ValueError(f"Unknown subject type(s) {bad}; use any of {list(SUBJECTS)}.")
    return wanted


def dig(obj, path: str):
    """Value at a dotted path, or None when any step is missing."""
    return output._pick(obj, path.split("."))


def _literal(text: str):
    t = text.strip()
    if t.lower() in ("true", "false", "null"):
        return {"true": True, "false": False, "null": None}[t.lower()]
    try:
        return json.loads(t)
    except ValueError:
        return t.strip("'\"")


def parse_filter(expr: str | None) -> list[tuple]:
    """"a.b == true and c != 'x'" -> [(path, op, value)]. Ops: == != >= <= > <."""
    if not expr or not expr.strip():
        return []
    out = []
    for part in _AND.split(expr):
        m = _COND.match(part)
        if not m:
            raise ValueError(f"Cannot parse filter condition {part!r}; use e.g. emailCopyOfMessage.enabled == true")
        out.append((m.group(1), m.group(2), _literal(m.group(3))))
    return out


def _test(body: dict, path: str, op: str, value) -> bool:
    try:
        return bool(_OPS[op](dig(body, path), value))
    except TypeError:
        return False


def matches(body: dict, conds: list[tuple]) -> bool:
    return all(_test(body, p, o, v) for p, o, v in conds)


def _flat(obj, prefix: str = "") -> dict:
    """Leaf paths of nested dicts (lists are leaves)."""
    if not isinstance(obj, dict):
        return {prefix: obj}
    out: dict = {}
    for k, v in obj.items():
        out.update(_flat(v, f"{prefix}.{k}" if prefix else k))
    return out


def _deep_merge(base: dict, change: dict) -> dict:
    out = dict(base)
    for k, v in change.items():
        out[k] = _deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def _cap(items: list, n: int = LIST_CAP) -> tuple[list, int]:
    return items[:n], max(len(items) - n, 0)


def _kind(e: Exception) -> str:
    """Where a failed per-subject call goes: not applicable (not a Calling user / wrong license) or a real error.

    A 404 "No static resource" means the ROUTE does not exist (a wrong endpoint mapping), never that the subject
    lacks the feature, so it must surface as an error instead of quietly shrinking the audit.
    """
    if isinstance(e, WebexError) and "No static resource" not in str(e) and (e.status in (404, 405) or "4003" in str(e)):
        return "not_applicable"
    return "error"


def _feature_path(spec: dict, subject: dict) -> str:
    return spec["paths"][subject["type"]].format(id=subject["id"]) if subject["type"] != ORG else spec["org_path"]


def _related(spec: dict, state: dict, skip: set[str]) -> list[str]:
    return [f"{p}={dig(state, p)}" for p, o, v, _ in spec.get("related", []) if p not in skip and _test(state, p, o, v)]


_NOISE = re.compile(r"\s*\(trackingId [^)]*\)|:\s*[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.I)
_LONG_ID = re.compile(r"[A-Za-z0-9+/=_-]{40,}")


def _group(entries: list[dict], key: str) -> list[dict]:
    """Collapse entries sharing a type, message and license into one with a name list (ids and tracking ids stripped)."""
    groups: dict[tuple, dict] = {}
    for e in entries:
        msg = _LONG_ID.sub("<id>", _NOISE.sub("", e[key]))
        g = groups.setdefault((e["type"], msg, e.get("license")), {"type": e["type"], key: msg,
                                                                   **({"license": e["license"]} if e.get("license") else {}), "names": []})
        g["names"].append(e["name"])
    out = []
    for g in groups.values():
        names, omitted = _cap(sorted(g.pop("names"), key=lambda n: (n or "").lower()), LIST_CAP)
        out.append({**g, "count": len(names) + omitted, "names": names, **({"omitted": omitted} if omitted else {})})
    return out


# ---------- listing and loading ----------

async def list_subjects(c: WebexClient, alias: str, wanted: list[str], location: str | None) -> tuple[list[dict], list[dict], dict]:
    """(queryable subjects, not-applicable entries, listed counts). Only Calling subjects are queryable."""
    loc_names = {l["id"]: l.get("name") for l in await _items(c, alias, "/locations")}
    if location and location.strip().lower() not in {(n or "").lower() for n in loc_names.values()}:
        raise ValueError(f"No location named {location!r} in org '{alias}'.")
    subjects: list[dict] = []
    skipped: list[dict] = []
    listed = {t: 0 for t in wanted}

    def keep(loc: str | None) -> bool:
        return not location or (loc or "").lower() == location.strip().lower()

    if PEOPLE in wanted:
        # callingData=true adds locationId/extension but does NOT drop non-Calling people; a missing locationId is the marker
        for p in await _items(c, alias, "/people", {"callingData": "true", "max": 1000}):
            loc = loc_names.get(p.get("locationId"))
            if not keep(loc):
                continue
            listed[PEOPLE] += 1
            row = {"type": PEOPLE, "id": p["id"], "name": p.get("displayName"), "extension": p.get("extension"),
                   "location": loc, "email": (p.get("emails") or [""])[0]}
            if p.get("locationId"):
                subjects.append(row)
            else:
                skipped.append({"type": PEOPLE, "name": row["name"], "reason": "no Calling location (not a Calling user)"})

    if WORKSPACES in wanted:
        try:
            lic = {l["id"]: l.get("name") for l in await _items(c, alias, "/licenses")}
        except WebexError:
            lic = {}
        try:  # workspaces carry no extension; the numbers list maps owner id -> extension
            ext = {n["owner"]["id"]: n.get("extension") for n in
                   await _items(c, alias, "/telephony/config/numbers", {"ownerType": "PLACE", "max": 1000})
                   if n.get("extension") and (n.get("owner") or {}).get("id")}
        except WebexError:
            ext = {}
        for w in await _items(c, alias, "/workspaces"):
            loc = loc_names.get(w.get("locationId"))
            if not keep(loc):
                continue
            listed[WORKSPACES] += 1
            calling = w.get("calling") or {}
            licenses = [lic.get(i, i) for i in (calling.get("webexCalling") or {}).get("licenses", [])]
            row = {"type": WORKSPACES, "id": w["id"], "name": w.get("displayName"), "extension": ext.get(w["id"]),
                   "location": loc, "license": ", ".join(licenses) or None}
            if calling.get("type") == "webexCalling":
                subjects.append(row)
            else:
                skipped.append({"type": WORKSPACES, "name": row["name"], "reason": f"calling type is {calling.get('type')!r}, not webexCalling"})

    if VIRTUAL_LINES in wanted:
        for v in await _virtual_lines(c, alias):
            loc = (v.get("location") or {}).get("name")
            if not keep(loc):
                continue
            listed[VIRTUAL_LINES] += 1
            subjects.append({"type": VIRTUAL_LINES, "id": v["id"], "name": f"{v.get('firstName', '')} {v.get('lastName', '')}".strip(),
                             "extension": (v.get("number") or {}).get("extension"), "location": loc})
    return subjects, skipped, listed


async def load_states(c: WebexClient, alias: str, spec: dict, subjects: list[dict]) -> None:
    """GET the feature for every subject in parallel; sets subject["state"], or ["na"] / ["error"] with the reason."""
    sem = asyncio.Semaphore(READ_CONCURRENCY)

    async def one(s: dict) -> None:
        async with sem:
            try:
                s["state"] = (await c.request(alias, "GET", _feature_path(spec, s))).get("data") or {}
            except (WebexError, httpx.HTTPError) as e:
                s["na" if _kind(e) == "not_applicable" else "error"] = str(e) or type(e).__name__

    await asyncio.gather(*(one(s) for s in subjects))


async def _org_setting(c: WebexClient, alias: str, spec: dict) -> dict | None:
    if not spec.get("org_path"):
        return None
    try:
        return (await c.request(alias, "GET", spec["org_path"])).get("data") or {}
    except (WebexError, httpx.HTTPError) as e:
        return {"error": str(e)}


# ---------- audit ----------

async def audit(c: WebexClient, alias: str, feature: str, subjects: list[str] | None = None,
                fields: list[str] | None = None, filter: str | None = None, location: str | None = None,
                max_rows: int = 300) -> dict:
    spec = feature_spec(feature)
    wanted = _subjects_arg(subjects)
    conds = parse_filter(filter)
    cols = list(fields or spec["fields"])
    cols += [p for p, _, _ in conds if p not in cols]  # show why a row matched
    skip = set(cols)

    found, skipped, listed = await list_subjects(c, alias, wanted, location)
    unmapped = [t for t in wanted if t not in spec["paths"]]
    warnings = [f"{t}: no {spec['name']} endpoint is mapped, so {listed[t]} {t} were NOT audited." for t in unmapped]
    found = [s for s in found if s["type"] in spec["paths"]]
    skipped = [e for e in skipped if e["type"] in spec["paths"]]
    await load_states(c, alias, spec, found)
    org_setting = await _org_setting(c, alias, spec)

    rows, flagged, na, errors = [], [], list(skipped), []
    counts = {t: {"listed": listed[t], "queried": 0, "matched": 0, "not_applicable": 0, "errors": 0} for t in wanted}
    for s in found:
        n = counts[s["type"]]
        if "na" in s:
            na.append({"type": s["type"], "name": s["name"], "reason": s["na"], **({"license": s["license"]} if s.get("license") else {})})
            continue
        if "error" in s:
            errors.append({"type": s["type"], "name": s["name"], "error": s["error"]})
            continue
        n["queried"] += 1
        flags = _related(spec, s["state"], skip)  # judged on every subject, not just matches: that is the point
        if flags:
            flagged.append({"type": s["type"], "name": s["name"], "flags": flags})
        if matches(s["state"], conds):
            n["matched"] += 1
            rows.append([s["type"], s["name"], s.get("extension"), s.get("location"), *(dig(s["state"], p) for p in cols)])
    for e in na:
        counts[e["type"]]["not_applicable"] += 1
    for e in errors:
        counts[e["type"]]["errors"] += 1

    rows.sort(key=lambda r: (SUBJECTS.index(r[0]), (r[1] or "").lower()))
    shown, omitted_rows = _cap(rows, max_rows)
    flagged_shown, flagged_omitted = _cap(flagged)
    out: dict = {"org": alias, "feature": spec["name"], "filter": filter, "location": location,
                 "columns": ["type", "name", "extension", "location", *cols],
                 "scanned": counts, "matched": len(rows), "items": shown}
    if omitted_rows:
        out["omitted_by_max_rows"] = omitted_rows
        out["truncated"] = True
    if org_setting is not None:
        out["org_setting"] = org_setting
        notes = [why for p, o, v, why in spec.get("org_related", []) if _test(org_setting, p, o, v)]
        if notes:
            out["org_notes"] = notes
    legend = {p: why for p, _, _, why in spec.get("related", []) if p not in skip}
    out["related_non_defaults"] = {"note": "Settings outside the requested fields that can change the outcome; judged on every scanned subject, not only matches.",
                                   "legend": legend, "subjects": flagged_shown, **({"omitted": flagged_omitted} if flagged_omitted else {})}
    out["not_applicable"] = _group(na, "reason")
    out["errors"] = _group(errors, "error")
    warnings += [f"All {counts[t]['listed']} {t} were skipped or failed (none queried); if that is unexpected the endpoint mapping may be wrong."
                 for t in wanted if t in spec["paths"] and counts[t]["listed"] and not counts[t]["queried"]]
    if warnings:
        out["warnings"] = warnings
    return out


# ---------- bulk update ----------

class PlanMismatch(ValueError):
    pass


def _resolve_targets(targets: list[str], subjects: list[dict], spec: dict) -> list[dict]:
    chosen, problems = [], []
    for t in targets:
        if t.strip().lower() == ORG:
            if not spec.get("org_path"):
                problems.append(f"{spec['name']} has no org-level setting")
            else:
                chosen.append({"type": ORG, "id": ORG, "name": "org-level settings", "extension": None, "location": None})
            continue
        key = t.strip().lower()
        hits = [s for s in subjects if s["id"] == t] or [
            s for s in subjects if key in ((s["name"] or "").lower(), (s.get("email") or "").lower(), str(s.get("extension") or ""))]
        if len(hits) != 1:
            problems.append(f"{t!r}: " + ("no Calling people/workspace/virtual line matches by id, name, email or extension" if not hits
                                         else "ambiguous, matches " + "; ".join(f"{h['type']} {h['name']} ({h.get('location')})" for h in hits)))
        else:
            chosen.append(hits[0])
    if problems:
        raise ValueError("Cannot resolve targets: " + " | ".join(problems))
    seen, unique = set(), []
    for s in chosen:
        if s["id"] not in seen:
            seen.add(s["id"])
            unique.append(s)
    return unique


def _plan_id(alias: str, spec: dict, targets: list[dict], change: dict) -> str:
    blob = {"org": alias, "feature": spec["name"], "change": change,
            "targets": [{"type": s["type"], "id": s["id"], "state": s.get("state"), "na": s.get("na"), "error": s.get("error")}
                        for s in targets]}
    return hashlib.sha256(json.dumps(blob, sort_keys=True, default=str).encode()).hexdigest()[:16]


async def build_plan(c: WebexClient, alias: str, feature: str, targets, change: dict, subjects: list[str] | None = None,
                     filter: str | None = None, location: str | None = None) -> tuple[dict, list[dict]]:
    """(public plan, target subjects with their current state). Reads only."""
    spec = feature_spec(feature)
    if not isinstance(change, dict) or not change:
        raise ValueError("`change` must be a non-empty partial body, e.g. {\"emailCopyOfMessage\": {\"enabled\": false}}.")
    leaves = _flat(change)
    wanted = _subjects_arg(subjects)
    found, _, listed = await list_subjects(c, alias, wanted, location)
    warnings = [f"{t}: no {spec['name']} endpoint is mapped, so {listed[t]} {t} cannot be targeted." for t in wanted if t not in spec["paths"]]
    found = [s for s in found if s["type"] in spec["paths"]]
    if targets == "from_audit":
        conds = parse_filter(filter)
        if not conds:
            raise ValueError("targets='from_audit' needs a `filter` (e.g. emailCopyOfMessage.enabled == true) so the scope is explicit.")
        await load_states(c, alias, spec, found)
        chosen = [s for s in found if "state" in s and matches(s["state"], conds)]
        warnings += [f"{s['type']} {s['name']}: not audited ({s.get('na') or s.get('error')})" for s in found if "state" not in s][:LIST_CAP]
    else:
        if isinstance(targets, str) or not targets:
            raise ValueError("`targets` must be a list of names/ids/emails/extensions (or 'org'), or 'from_audit'.")
        chosen = _resolve_targets(list(targets), found, spec)
        await load_states(c, alias, spec, [s for s in chosen if s["type"] != ORG])
        for s in chosen:
            if s["type"] == ORG:
                st = await _org_setting(c, alias, spec)
                s["state"] = st if "error" not in (st or {}) else None
                if s["state"] is None:
                    s["error"] = st["error"]
    if len(chosen) > MAX_TARGETS:
        raise ValueError(f"{len(chosen)} targets exceeds the {MAX_TARGETS} limit; narrow it with `location` or a filter.")
    chosen.sort(key=lambda s: (s["type"] != ORG, SUBJECTS.index(s["type"]) if s["type"] in SUBJECTS else -1, (s["name"] or "").lower()))

    rows, errors = [], []
    for s in chosen:
        if "state" not in s or s.get("state") is None:
            errors.append({"type": s["type"], "name": s["name"], "error": s.get("error") or s.get("na")})
            continue
        before = {p: dig(s["state"], p) for p in leaves}
        missing = [p for p in leaves if before[p] is None]
        if missing:
            warnings.append(f"{s['type']} {s['name']}: {missing} not present in the current state (the PUT may be rejected or ignored)")
        if s["type"] in SUBJECTS and s["type"] not in spec["partial_put_verified"]:
            warnings.append(f"{s['type']}: a partial PUT has not been verified for {spec['name']}; the canary update will show whether it works")
        row = {"type": s["type"], "name": s["name"], "extension": s.get("extension"), "location": s.get("location"),
               "before": before, "after": dict(leaves), "already_set": before == leaves}
        flags = _related(spec, s["state"], set(leaves))
        if flags:
            row["related"] = flags
        rows.append(row)
    warnings = list(dict.fromkeys(warnings))

    plan = {"org": alias, "feature": spec["name"], "change": change, "plan_id": _plan_id(alias, spec, chosen, change), "targets": rows,
            "will_change": sum(not r["already_set"] for r in rows), "already_set": sum(r["already_set"] for r in rows),
            "errors": errors, "warnings": warnings}
    return plan, chosen


async def _write_one(c: WebexClient, alias: str, spec: dict, s: dict, change: dict, leaves: dict) -> dict:
    row = {"type": s["type"], "name": s["name"], "extension": s.get("extension"), "location": s.get("location"),
           "before": {p: dig(s["state"], p) for p in leaves}, "after": None, "verified": False, "error": None, "side_effects": []}
    path = _feature_path(spec, s)
    # Org-level settings: send the whole body back with only the requested key changed. Subjects: the partial body.
    body = _deep_merge(s["state"], change) if s["type"] == ORG else change
    try:
        await c.request(alias, "PUT", path, body=body)
    except (WebexError, httpx.HTTPError) as e:
        row["error"] = f"PUT failed: {e}"
        return row
    try:
        after = (await c.request(alias, "GET", path)).get("data") or {}
    except (WebexError, httpx.HTTPError) as e:
        row["error"] = f"PUT sent but verification GET failed: {e}"
        return row
    row["after"] = {p: dig(after, p) for p in leaves}
    row["verified"] = row["after"] == leaves
    if not row["verified"]:
        row["error"] = "read-back does not match the requested change"
    b, a = _flat(s["state"]), _flat(after)
    row["side_effects"] = [{"path": p, "before": b.get(p), "after": a.get(p)}
                           for p in sorted(set(b) | set(a)) if p not in leaves and b.get(p) != a.get(p)][:10]
    return row


async def bulk_update(c: WebexClient, alias: str, feature: str, targets, change: dict, plan_id: str, canary_first: bool = True,
                      subjects: list[str] | None = None, filter: str | None = None, location: str | None = None) -> dict:
    """Apply a reviewed plan. The caller must already have passed the write gate (allow_write + writable org)."""
    spec = feature_spec(feature)
    plan, chosen = await build_plan(c, alias, feature, targets, change, subjects, filter, location)
    if not plan_id or plan["plan_id"] != plan_id:
        raise PlanMismatch(
            f"plan_id {plan_id!r} does not match the current state (now {plan['plan_id']}). The org, targets, change or the "
            "targets' current settings differ from when the plan was made. Nothing was written; run dry_run=true again and review.")
    leaves = _flat(change)
    todo = [s for s in chosen if s.get("state") is not None and {p: dig(s["state"], p) for p in leaves} != leaves]
    todo_ids = {id(s) for s in todo}
    results: list[dict] = []
    for s in chosen:
        if s.get("state") is None:
            results.append({"type": s["type"], "name": s["name"], "status": "error", "verified": False,
                            "error": s.get("error") or s.get("na")})
        elif id(s) not in todo_ids:
            results.append({"type": s["type"], "name": s["name"], "status": "unchanged", "verified": True, "error": None})

    async def run(s: dict) -> dict:
        r = await _write_one(c, alias, spec, s, change, leaves)
        r["status"] = "updated" if r["verified"] else "failed"
        return r

    first, rest = (todo[:1], todo[1:]) if canary_first and len(todo) > 1 else ([], todo)
    for s in first:
        r = await run(s)
        results.append({**r, "canary": True})
        if r["status"] != "updated":
            results += [{"type": x["type"], "name": x["name"], "status": "skipped", "verified": False,
                         "error": "canary update failed; nothing else was attempted"} for x in rest]
            rest = []
    sem = asyncio.Semaphore(WRITE_CONCURRENCY)

    async def limited(s: dict) -> dict:
        async with sem:
            return await run(s)

    results += await asyncio.gather(*(limited(s) for s in rest))
    effects = [{"type": r["type"], "name": r["name"], "changes": r["side_effects"]} for r in results if r.get("side_effects")]
    tally = {k: sum(r["status"] == k for r in results) for k in ("updated", "unchanged", "failed", "skipped", "error")}
    out = {"org": alias, "feature": spec["name"], "change": change, "plan_id": plan_id, "summary": tally, "results": results}
    if effects:
        out["side_effects"] = effects[:LIST_CAP]
    return out
