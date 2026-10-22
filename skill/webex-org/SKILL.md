---
name: webex-org
description: Query, report on, and (with confirmation) change the user's Webex demo orgs through the `fabrica-webex` MCP server. Use for anything about Webex people, licenses, workspaces, devices, Calling (locations, queues, numbers, CDR), audit events, or org health reports; also when the user mentions a specific Webex org alias.
---

# Webex org management

Use the `fabrica-webex` MCP tools (`webex_request`, `list_orgs`, `people_with_licenses`, `workspace_inventory`, `license_assignment_by_location`, `user_registrations`). Auth, token refresh, pagination, and 429 retry are handled by the server; never ask the user for tokens.

## Orgs
- Call `list_orgs` first when the org is unclear. Every tool takes `org` (an alias); omitted means the default org. `org="all"` runs a **read** across every org and groups results by alias.
- Say which org a result came from whenever more than one is registered.
- Tokens: a refresh token that goes unused ~90 days expires. If a call fails with a refresh error, tell the user to run `fabrica-webex-mcp add-org <alias> --replace`. If `refresh_token_days_left` in `list_orgs` is under ~14, mention it.

## Reading data (default)
- Prefer curated tools when they fit: `people_with_licenses` (license IDs already resolved to names) and `workspace_inventory` (location names resolved).
- For everything else use `webex_request(method="GET", path=...)`.
  - **Always pass `fields`** (comma list, dotted paths like `owner.type`) and a sensible `max_items`; raw Webex objects are large and IDs are long base64 strings.
  - Results with `"truncated": true` mean more exist. Say so rather than presenting a partial list as complete; raise `max_items` or filter if the user needs everything.
  - Paths are relative to `https://webexapis.com/v1`. Calling responses may wrap the list under another key (`locations`, `queues`, `phoneNumbers`); the server normalizes these to `items` and adds `"collection": "<key>"`.
- Counts: use the length of `items` only if not truncated.

## Writing (POST/PUT/PATCH/DELETE)
1. Describe the exact change (org, endpoint, body, what it affects) and get the user's explicit yes in chat first. Never batch destructive changes under one yes without listing them.
2. Call `webex_request` with `allow_write=true`. The server also refuses unless the org is marked writable (`fabrica-webex-mcp set-writable <alias> on`, run by the user). Never try to bypass this.
3. Writes cannot use `org="all"`.
4. Verify afterward with a GET and report the result.
These are real orgs: prefer one item first, then the rest.

## Endpoint cheat sheet (verified against a live org unless marked *)
| Need | Path | Useful `fields` |
|---|---|---|
| License totals/usage | `/licenses` | `name,totalUnits,consumedUnits` |
| People | `/people` (`max` up to 1000; filters `email`, `displayName`) | `displayName,emails,status,licenses,roles` |
| Workspaces | `/workspaces`, `/workspaceLocations` | `displayName,type,calling,workspaceLocationId` |
| Devices | `/devices` | `displayName,product,connectionStatus` |
| Calling locations | `/telephony/config/locations` | `id,name` |
| Call queues | `/telephony/config/queues` | `id,name,locationName` |
| Numbers | `/telephony/config/numbers` | `phoneNumber,state,owner.type` |
| Admin/audit events | `/adminAudit/events`* (needs `orgId`, `from`, `to`) | |
| Org events | `/events` | `resource,type,created` |
| CDR | `https://analytics.webexapis.com/v1/cdr_feed` (full URL, different host) | |
| Me / org id | `/people/me` | `orgId,emails` |

Other Calling config follows the same pattern (`/telephony/config/huntGroups`, `autoAttendants`, `virtualLines`, ...)*; check developer.webex.com if a path 404s rather than guessing repeatedly.

## Gotchas
- **CDR**: window must start more than ~5 minutes ago and within ~30 days, and each request covers a limited span (about 12 hours)*; an empty `items` for a recent window is normal. Loop over windows for longer ranges, using `fields` to keep output small.
- IDs in `licenses` arrays are opaque base64; resolve through `/licenses` (the curated tool does this).
- The token acts as the admin who logged in: actions are attributed to them and limited by their roles. Compliance scopes need the Compliance Officer role. A 403 usually means a missing role or an unregistered scope, not a bad path.
- Meetings and analytics APIs may need extra parameters (`hostEmail`, `from`/`to`)*.
- Rate limits are handled automatically; if a long loop is slow, that is Retry-After, not a hang.

## Report recipes
**Org health snapshot** (read-only, all orgs unless told otherwise):
1. `list_orgs`, then per org: `/licenses` (flag any with `consumedUnits/totalUnits` > 90% or unused > 50%).
2. `people_with_licenses`: count people, and list those with no paid license or status `unknown`.
3. `workspace_inventory`: counts by type and location; flag workspaces without calling.
4. `/devices`: count by `connectionStatus`; list the disconnected ones.
5. `/telephony/config/numbers`: counts by `state` and `owner.type`; flag unassigned numbers.
Present a short summary table per org, then only the exceptions. Offer a follow-up rather than dumping every row.

**License utilization**: `/licenses` sorted by percent consumed, per org.

**Who has X license**: `people_with_licenses`, filter by license name client-side.

**User registrations** (live SIP registrations: which phones/clients are registered, MAC, public/private IP, expiry). Use the `user_registrations` tool; this data is only visible through Xsi-Actions, not the REST APIs, so `webex_request` cannot answer it. Pass `entries` as comma-separated **names or emails**: a name can be a user, a workspace or a virtual line (all three work), and the result labels which it was. If a name matches more than one thing the entry's `error` lists the candidates; ask the user which, or retry with an email or fuller name. Each entry succeeds or fails on its own, and a 404 means the subject is not a Webex Calling user. Present one row per registration (type, model/version, MAC, public IP, private IP, expiry) under each subject, and say so when a subject has none (offline). Needs `spark-admin:xsi`, which Cisco must enable per org, so it may be unavailable: a 401/403 from this tool usually means the org was logged in without it (`add-org --no-xsi`), not a bad name. Xsi-Events (live subscriptions) is deliberately not supported. Registration output is a point-in-time snapshot.

**License assignment by location** (read-only; per org). Counts Webex Calling Professional, Workspace (Common Area) and Outbound Calling Plan (OCP) licenses at every Calling location, plus how many of the OCPs belong to Virtual Lines.
Use the `license_assignment_by_location` tool: it does the whole scan server-side and returns per-location counts, totals and `warnings` (users without a location, OCP lookups that were not applicable, workspaces with no recognised calling license). Pass `location="<name>"` to also get the licensed people/workspaces/virtual lines at one location (drill-down), and `include_ocp=false` for a fast Professional/Workspace-only report on a large org (OCP is one lookup per licensed item). Present one table per org with the Totals row, then only the warnings; `virtual_lines` is a breakout of `ocp`, not additional to it.
If the tool is not available (older server), assemble it by hand; no single endpoint answers this:
1. `/licenses` (`fields=id,name`): store the IDs named `Webex Calling - Professional` and `Webex Calling - Workspaces`. People/Workspaces list licenses as bare IDs, so this is the only way to interpret them. Any ID not in these two is not a calling license.
2. `/locations` (`fields=id,name`): location ID to name. These are Locations, not `/workspaceLocations`.
3. People: `/people?callingData=true` (`fields=id,displayName,licenses,locationId`, `max_items` high). `callingData=true` is what fills `locationId`. Keep users whose `licenses` contains a calling license ID. Use this org-wide call; per-location people queries drop the `licenses` field.
4. Workspaces: `/workspaces` with `fields=id,displayName,locationId,calling` (the licenses are at `calling.webexCalling.licenses`, not top level, and only present for Calling workspaces). A workspace can hold either Professional or Workspace license, so check which ID it carries; don't assume. If none of the calling workspaces match a known license ID, say so rather than reporting zero.
5. Virtual Lines: `/telephony/config/virtualLines`. They hold no calling license but can consume an OCP, so leave them out and OCP comes up short. The response is under `data.virtualLines` (the `fields` filter does not apply; omit it), each with `location.name` inline and `billingPlan` (e.g. `BCOCP1`) present when an OCP is assigned. Treat a line with `billingPlan` as having an OCP.
6. OCP for People and Workspaces: there is no bulk form. For each licensed item, `GET /telephony/config/people/{personId}/outboundBillingPlan` or `/telephony/config/workspaces/{workspaceId}/outboundBillingPlan` and read `enabled`. Only ask about items that already hold a calling license. An error (item not in a Cisco Calling Plan location) means "not applicable", not zero; report it separately. If the org has more than ~50 licensed items, tell the user this is one call each, and offer a licenses-only report (Professional/Workspace exact, OCP omitted) first. Calls in one response can be issued in parallel.
7. Count by location name. Every location starts at zero so empty ones still appear; items with no `locationId` go under "No Location". Report per location: Professional, Workspace, OCP, and Virtual Line OCPs (a breakout of OCP, not additional to it).
Output: one table per org with a Totals row, sorted by location name; mention items needing attention (users with no location, N/A OCP lookups). Offer drill-down (the licensed users/workspaces behind any location row) rather than listing everyone. This is a point-in-time snapshot; say so if reusing it for later follow-ups.

Keep user-facing output concise: tables for lists, a sentence for the takeaway, and the org alias on every table.
