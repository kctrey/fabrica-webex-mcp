"""License Assignment by Location: Webex Calling Professional, Workspace and Outbound Calling Plan (OCP)
counts per location.

No single Webex endpoint answers this, so the report is assembled from licenses, locations, people,
workspaces and virtual lines, plus one outboundBillingPlan lookup per licensed person/workspace
(there is no bulk form), run concurrently.
"""
import asyncio

from .client import WebexClient, WebexError

PROFESSIONAL = "Webex Calling - Professional"
WORKSPACE = "Webex Calling - Workspaces"
NO_LOCATION = "No Location"
UNKNOWN_LOCATION = "Unknown Location"
CONCURRENCY = 10
OCP_PATH = {"person": "/telephony/config/people/{}/outboundBillingPlan",
            "workspace": "/telephony/config/workspaces/{}/outboundBillingPlan"}


async def _items(c: WebexClient, alias: str, path: str, query: dict | None = None) -> list:
    return (await c.request(alias, "GET", path, query, max_items=10000))["items"]


async def _virtual_lines(c: WebexClient, alias: str) -> list:
    """The virtualLines response carries a sibling "customer" key, so the client leaves it under "data"
    (and does not follow pagination); `max` keeps it to one page for any realistic org."""
    r = await c.request(alias, "GET", "/telephony/config/virtualLines", {"max": 1000})
    return r["items"] if "items" in r else (r.get("data") or {}).get("virtualLines", [])


def _license_types(licenses: list) -> dict[str, str]:
    kinds = {PROFESSIONAL: "professional", WORKSPACE: "workspace"}
    return {l["id"]: kinds[l["name"]] for l in licenses if l.get("name") in kinds}


def _held(ids: list | None, types: dict[str, str]) -> set[str]:
    return {types[i] for i in ids or [] if i in types}


def _location_name(location_id: str | None, names: dict[str, str], inline: str | None = None) -> str:
    if not location_id:
        return NO_LOCATION
    return names.get(location_id) or inline or UNKNOWN_LOCATION


async def _lookup_ocp(c: WebexClient, alias: str, entities: list[dict]) -> None:
    """Sets entity["ocp"] to True/False, or None when Webex has no answer (e.g. not a Cisco Calling Plan location)."""
    sem = asyncio.Semaphore(CONCURRENCY)

    async def one(e: dict) -> None:
        async with sem:
            try:
                r = await c.request(alias, "GET", OCP_PATH[e["kind"]].format(e["id"]))
            except WebexError:
                e["ocp"] = None
                return
        e["ocp"] = bool((r.get("data") or {}).get("enabled"))

    await asyncio.gather(*(one(e) for e in entities))


async def collect(c: WebexClient, alias: str, include_ocp: bool = True) -> dict:
    types = _license_types(await _items(c, alias, "/licenses"))
    names = {l["id"]: l.get("name") for l in await _items(c, alias, "/locations")}
    entities: list[dict] = []
    warnings: dict = {}

    # callingData=true is what populates locationId on People
    for p in await _items(c, alias, "/people", {"callingData": "true", "max": 1000}):
        held = _held(p.get("licenses"), types)
        if held:
            entities.append({"id": p["id"], "name": p.get("displayName"), "kind": "person", "held": held,
                             "location": _location_name(p.get("locationId"), names), "ocp": None})

    unclassified, calling_workspaces = [], 0
    for w in await _items(c, alias, "/workspaces"):
        calling = w.get("calling") or {}
        if calling.get("type") != "webexCalling":
            continue
        calling_workspaces += 1
        held = _held((calling.get("webexCalling") or {}).get("licenses"), types)
        if not held:
            # Still asked about for OCP: not recognising its license is no reason to skip its plan
            unclassified.append(w.get("displayName"))
        entities.append({"id": w["id"], "name": w.get("displayName"), "kind": "workspace", "held": held,
                         "location": _location_name(w.get("locationId"), names), "ocp": None})

    if calling_workspaces and len(unclassified) == calling_workspaces:
        warnings["workspace_licenses_unidentified"] = (
            "No calling workspace reported a Professional/Workspace license this report could identify; "
            "the Workspace column will read zero.")
    if unclassified:
        warnings["workspaces_without_professional_or_workspace_license"] = sorted(unclassified)

    lines = []
    if include_ocp:
        await _lookup_ocp(c, alias, entities)
        # A virtual line holds no license and carries its plan inline; it only appears if it has one
        for v in await _virtual_lines(c, alias):
            if v.get("billingPlan"):
                loc = v.get("location") or {}
                lines.append({"id": v["id"], "name": f"{v.get('firstName', '')} {v.get('lastName', '')}".strip(),
                              "kind": "virtual_line", "held": set(), "ocp": True,
                              "location": _location_name(loc.get("id"), names, loc.get("name"))})
        entities += lines
        not_applicable = [e["name"] for e in entities if e["kind"] != "virtual_line" and e["ocp"] is None]
        if not_applicable:
            warnings["ocp_not_applicable"] = sorted(not_applicable)

    no_location = sorted(e["name"] for e in entities if e["location"] == NO_LOCATION)
    if no_location:
        warnings["licensed_without_location"] = no_location

    counts = {name: {"location": name, "professional": 0, "workspace": 0,
                     "ocp": 0 if include_ocp else None, "virtual_lines": 0 if include_ocp else None}
              for name in names.values()}
    for e in entities:
        row = counts.setdefault(e["location"], {"location": e["location"], "professional": 0, "workspace": 0,
                                                "ocp": 0 if include_ocp else None,
                                                "virtual_lines": 0 if include_ocp else None})
        for kind in e["held"]:
            row[kind] += 1
        if e["ocp"]:
            row["ocp"] += 1
            if e["kind"] == "virtual_line":
                row["virtual_lines"] += 1  # a breakout of ocp, not additional to it

    rows = sorted(counts.values(), key=lambda r: (r["location"] == NO_LOCATION, (r["location"] or "").lower()))
    totals = {k: (sum(r[k] for r in rows) if include_ocp or k in ("professional", "workspace") else None)
              for k in ("professional", "workspace", "ocp", "virtual_lines")}
    return {"locations": rows, "totals": totals, "warnings": warnings, "ocp_included": include_ocp,
            "_entities": entities}


def detail(report: dict, location: str) -> list[dict] | None:
    """The licensed people/workspaces/virtual lines at one location (case-insensitive), or None if no such location."""
    wanted = location.strip().lower()
    if wanted not in {r["location"].lower() for r in report["locations"]}:
        return None
    return sorted(({"name": e["name"], "kind": e["kind"],
                    "licenses": sorted(e["held"] | ({"ocp"} if e["ocp"] else set()))}
                   for e in report["_entities"] if e["location"].lower() == wanted),
                  key=lambda e: ((e["name"] or "").lower(), e["kind"]))
