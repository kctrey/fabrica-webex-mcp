import json
import time

import httpx
import pytest

from fabrica_webex_mcp import calling_features as cf
from fabrica_webex_mcp import client, config, server, store

LOC_A, LOC_B = "LOC_A", "LOC_B"
VM = "voicemail"


def seed(writable=True):
    store.set_client("cid", "secret")
    store.set_tokens("demo", store.Tokens("A1", "R1", time.time() + 3600, time.time() + 9e6))
    reg = config.load()
    reg["orgs"]["demo"] = {"org_id": "o1", "name": "Demo", "writable": writable}
    reg["default"] = "demo"
    config.save(reg)


def mk(handler):
    return client.WebexClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def vm(copy=False, storage="INTERNAL", notif=False, fax=False):
    return {"enabled": True, "emailCopyOfMessage": {"enabled": copy, "emailId": "x@y.com"},
            "messageStorage": {"mwiEnabled": True, "storageType": storage},
            "notifications": {"enabled": notif}, "faxMessage": {"enabled": fax}}


class Fake:
    """A small org: 3 people (one with no Calling location), 3 workspaces, 2 virtual lines, with mutable voicemail state."""

    def __init__(self):
        self.state = {
            "P1": vm(copy=True), "P2": vm(copy=True), "P3": vm(storage="EXTERNAL"),   # P3 copy off but EXTERNAL
            "W1": vm(copy=True), "VL1": vm(copy=True),
        }
        self.org = {"messageExpiryEnabled": True, "voiceMessageForwardingEnabled": True, "voicePortalAccessVMDepositEnabled": True}
        self.calls, self.puts = [], []
        self.fail_get = {}            # id -> (status, message)
        self.fail_put = {}            # id -> (status, message)
        self.put_side_effect = None   # callable(id, body, state)
        self.rate_limit_once = set()
        self.people_paged = False

    def __call__(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path.replace("/v1", "", 1)
        m, q = req.method, dict(req.url.params)
        self.calls.append((m, path))
        J = lambda d, code=200, h=None: httpx.Response(code, json=d, headers=h or {})
        if path == "/locations":
            return J({"items": [{"id": LOC_A, "name": "Midwest"}, {"id": LOC_B, "name": "East Coast"}]})
        if path == "/people":
            people = [{"id": "P1", "displayName": "Ann", "locationId": LOC_A, "extension": "1001", "emails": ["a@x.com"]},
                      {"id": "P2", "displayName": "Bob", "locationId": LOC_B, "extension": "1002"},
                      {"id": "P3", "displayName": "Cy", "locationId": LOC_A, "extension": "1003"},
                      {"id": "PX", "displayName": "No Calling"}]
            if self.people_paged:
                if "page2" in q:
                    return J({"items": people[2:]})
                return J({"items": people[:2]}, h={"Link": '<https://webexapis.com/v1/people?page2=1&callingData=true>; rel="next"'})
            return J({"items": people})
        if path == "/licenses":
            return J({"items": [{"id": "LP", "name": "Webex Calling - Professional"}, {"id": "LW", "name": "Webex Calling - Workspaces"}]})
        if path == "/telephony/config/numbers":
            return J({"phoneNumbers": [{"extension": "2001", "owner": {"id": "W1", "type": "PLACE"}}]})
        if path == "/workspaces":
            return J({"items": [
                {"id": "W1", "displayName": "Lobby", "locationId": LOC_A, "calling": {"type": "webexCalling", "webexCalling": {"licenses": ["LP"]}}},
                {"id": "W2", "displayName": "Speaker", "locationId": LOC_A, "calling": {"type": "webexCalling", "webexCalling": {"licenses": ["LW"]}}},
                {"id": "W3", "displayName": "Meeting only", "locationId": LOC_B, "calling": {"type": "freeCalling"}}]})
        if path == "/telephony/config/virtualLines":
            return J({"virtualLines": [{"id": "VL1", "firstName": "Vi", "lastName": "One", "location": {"id": LOC_A, "name": "Midwest"},
                                        "number": {"extension": "3001"}},
                                       {"id": "VL2", "firstName": "Vi", "lastName": "Two", "location": {"id": LOC_B, "name": "East Coast"},
                                        "number": {"extension": "3002"}}],
                      "customer": {"id": "c"}})
        if path == "/telephony/config/voicemail/settings":
            if m == "PUT":
                self.puts.append(("org", json.loads(req.content)))
                self.org = json.loads(req.content)
                return httpx.Response(204)
            return J(self.org)
        for prefix in ("/people/", "/telephony/config/workspaces/", "/telephony/config/virtualLines/"):
            if path.startswith(prefix) and path.endswith(("/features/voicemail", "/voicemail")):
                sid = path[len(prefix):].split("/")[0]
                return self.subject(m, sid, req)
        return J({"message": "No static resource hydra/v1" + path}, 404)

    def subject(self, m, sid, req):
        if m == "GET":
            if sid in self.rate_limit_once:
                self.rate_limit_once.discard(sid)
                return httpx.Response(429, headers={"Retry-After": "2"})
            if sid in self.fail_get:
                code, msg = self.fail_get[sid]
                return httpx.Response(code, json={"message": msg})
            if sid == "W2":
                return httpx.Response(405, json={"message": f"Invalid Professional Place: {'0' * 8}-0000-0000-0000-{'0' * 12}"})
            if sid == "VL2":
                return httpx.Response(500, json={"message": "boom"})
            return httpx.Response(200, json=self.state[sid])
        body = json.loads(req.content)
        self.puts.append((sid, body))
        if sid in self.fail_put:
            code, msg = self.fail_put[sid]
            return httpx.Response(code, json={"message": msg})
        cf_merge(self.state[sid], body)
        if self.put_side_effect:
            self.put_side_effect(sid, body, self.state[sid])
        return httpx.Response(204)


def cf_merge(dst, src):
    for k, v in src.items():
        if isinstance(v, dict):
            cf_merge(dst.setdefault(k, {}), v)
        else:
            dst[k] = v


@pytest.fixture
def fake(monkeypatch):
    seed()
    f = Fake()
    monkeypatch.setattr(server, "_client", mk(f))
    return f


async def audit(**kw):
    return json.loads(await server.calling_feature_audit(kw.pop("feature", VM), "demo", **kw))


async def update(**kw):
    kw.setdefault("feature", VM)
    kw.setdefault("org", "demo")
    return json.loads(await server.calling_feature_bulk_update(**kw))


EMAIL_OFF = {"emailCopyOfMessage": {"enabled": False}}


# ---------- audit ----------

async def test_audit_covers_all_subject_types_with_buckets(fake):
    r = await audit()
    sc = r["scanned"]
    assert sc["people"] == {"listed": 4, "queried": 3, "matched": 3, "not_applicable": 1, "errors": 0}
    assert sc["workspaces"]["listed"] == 3 and sc["workspaces"]["queried"] == 1 and sc["workspaces"]["not_applicable"] == 2
    assert sc["virtual_lines"] == {"listed": 2, "queried": 1, "matched": 1, "not_applicable": 0, "errors": 1}
    names = {row[1] for row in r["items"]}
    assert names == {"Ann", "Bob", "Cy", "Lobby", "Vi One"}
    assert r["columns"][:4] == ["type", "name", "extension", "location"]
    lobby = next(row for row in r["items"] if row[1] == "Lobby")
    assert lobby[2] == "2001"  # workspace extension comes from the numbers list
    # buckets are separate from the table: the 405 and the no-location person are not applicable; the 500 is an error
    na = {(g["type"], g["names"][0]) for g in r["not_applicable"]}
    assert ("people", "No Calling") in na and ("workspaces", "Speaker") in na and ("workspaces", "Meeting only") in na
    assert [(g["type"], g["names"]) for g in r["errors"]] == [("virtual_lines", ["Vi Two"])]
    assert "Vi Two" not in names and "Speaker" not in names
    speaker = next(g for g in r["not_applicable"] if g["names"] == ["Speaker"])
    assert speaker["reason"] == "HTTP 405: Invalid Professional Place" and speaker["license"] == "Webex Calling - Workspaces"
    assert r["org_setting"]["voiceMessageForwardingEnabled"] is True and r["org_notes"]


async def test_audit_filter_and_related_flags_cover_every_subject(fake):
    r = await audit(filter="emailCopyOfMessage.enabled == true", fields=["enabled"])
    assert {row[1] for row in r["items"]} == {"Ann", "Bob", "Lobby", "Vi One"}
    assert r["columns"][-2:] == ["enabled", "emailCopyOfMessage.enabled"]  # the filter field is shown
    # Cy has CC off so the filter skips them, but EXTERNAL storage still sends voicemail to email: flagged anyway
    assert {s["name"]: s["flags"] for s in r["related_non_defaults"]["subjects"]} == {"Cy": ["messageStorage.storageType=EXTERNAL"]}
    assert "messageStorage.storageType" in r["related_non_defaults"]["legend"]
    # when storageType is itself a requested column it is shown, not flagged as "related"
    r = await audit(filter="emailCopyOfMessage.enabled == true", fields=["messageStorage.storageType"])
    assert r["related_non_defaults"]["subjects"] == []


async def test_audit_subject_and_location_scoping(fake):
    r = await audit(subjects=["workspaces"], location="midwest")
    assert {row[1] for row in r["items"]} == {"Lobby"} and set(r["scanned"]) == {"workspaces"}
    with pytest.raises(ValueError, match="No location"):
        await audit(location="Nowhere")
    with pytest.raises(ValueError, match="Unknown subject"):
        await audit(subjects=["devices"])
    with pytest.raises(ValueError, match="Unknown feature"):
        await audit(feature="nope")


async def test_audit_pagination_and_429_retry(fake, monkeypatch):
    fake.people_paged = True
    fake.rate_limit_once = {"P1", "VL1"}
    slept = []

    async def fake_sleep(s):
        slept.append(s)
    monkeypatch.setattr(client.asyncio, "sleep", fake_sleep)
    r = await audit(subjects=["people", "virtual_lines"])
    assert {row[1] for row in r["items"] if row[0] == "people"} == {"Ann", "Bob", "Cy"}  # Cy is on page 2
    assert r["scanned"]["people"]["queried"] == 3
    assert any(row[1] == "Vi One" for row in r["items"])
    assert slept == [2.0, 2.0]  # each rate-limited call waited Retry-After and then succeeded


async def test_audit_route_404_is_an_error_not_applicable_never(fake):
    fake.fail_get["P1"] = (404, "No static resource hydra/v1/people/P1/features/voicemail")
    fake.fail_get["P2"] = (403, "Insufficient scope")
    fake.fail_get["P3"] = (400, "4003: not a Calling user")
    r = await audit(subjects=["people"])
    assert {g["names"][0]: g["count"] for g in r["errors"]} == {"Ann": 1, "Bob": 1}
    assert any(g["names"] == ["Cy"] for g in r["not_applicable"])  # 4003 is the one real not-applicable
    assert r["scanned"]["people"]["errors"] == 2 and r["scanned"]["people"]["queried"] == 0
    assert any("none queried" in w for w in r["warnings"])  # nothing succeeded: the mapping may be wrong


async def test_audit_warns_when_nothing_was_queried_and_for_unmapped_types(fake):
    for pid in ("P1", "P2", "P3"):
        fake.fail_get[pid] = (404, "No static resource x")
    r = await audit(subjects=["people"])
    assert any("none queried" in w for w in r["warnings"])
    r = await audit(feature="callRecording", subjects=["workspaces"])  # no workspace route mapped
    assert r["scanned"]["workspaces"]["queried"] == 0 and any("NOT audited" in w for w in r["warnings"])


async def test_audit_marks_truncated_rows(fake):
    r = await audit(max_rows=2)
    assert len(r["items"]) == 2 and r["truncated"] is True and r["omitted_by_max_rows"] == r["matched"] - 2


def test_filter_parsing():
    assert cf.parse_filter("a.b == true and c != 'x' and n >= 3") == [("a.b", "==", True), ("c", "!=", "x"), ("n", ">=", 3)]
    assert cf.parse_filter(None) == [] and cf.parse_filter("  ") == []
    with pytest.raises(ValueError, match="Cannot parse"):
        cf.parse_filter("just words")
    assert cf.matches({"a": {"b": True}}, [("a.b", "==", True)]) and not cf.matches({}, [("a.b", "==", True)])
    assert not cf.matches({"n": "x"}, [("n", ">", 3)])  # incomparable types do not raise


# ---------- bulk update: planning ----------

async def test_dry_run_plans_without_writing(fake):
    plan = await update(targets="from_audit", filter="emailCopyOfMessage.enabled == true", change=EMAIL_OFF)
    assert {t["name"] for t in plan["targets"]} == {"Ann", "Bob", "Lobby", "Vi One"}
    ann = next(t for t in plan["targets"] if t["name"] == "Ann")
    assert ann["before"] == {"emailCopyOfMessage.enabled": True} and ann["after"] == {"emailCopyOfMessage.enabled": False}
    assert plan["will_change"] == 4 and plan["writable"] is True and len(plan["plan_id"]) == 16
    assert plan["plan_id"] in plan["next_step"]
    assert any("workspaces" in w and "partial PUT" in w for w in plan["warnings"])  # not verified for workspaces
    assert not [c for c in fake.calls if c[0] in ("PUT", "POST", "DELETE")]
    # the plan is stable while nothing changes, and different when the change differs
    assert (await update(targets="from_audit", filter="emailCopyOfMessage.enabled == true", change=EMAIL_OFF))["plan_id"] == plan["plan_id"]
    assert (await update(targets="from_audit", filter="emailCopyOfMessage.enabled == true",
                         change={"emailCopyOfMessage": {"enabled": True}}))["plan_id"] != plan["plan_id"]


async def test_plan_flags_related_settings_and_resolves_names(fake):
    plan = await update(targets=["Cy", "bob", "1001"], change=EMAIL_OFF)
    assert {t["name"] for t in plan["targets"]} == {"Cy", "Bob", "Ann"}
    cy = next(t for t in plan["targets"] if t["name"] == "Cy")
    assert cy["already_set"] is True and cy["related"] == ["messageStorage.storageType=EXTERNAL"]
    with pytest.raises(ValueError, match="Cannot resolve"):
        await update(targets=["nobody"], change=EMAIL_OFF)
    with pytest.raises(ValueError, match="needs a `filter`"):
        await update(targets="from_audit", change=EMAIL_OFF)
    with pytest.raises(ValueError, match="non-empty"):
        await update(targets=["Ann"], change={})


async def test_plan_reports_unreadable_targets_as_errors(fake):
    fake.fail_get["P1"] = (500, "boom")
    plan = await update(targets=["Ann", "Bob"], change=EMAIL_OFF)
    assert [e["name"] for e in plan["errors"]] == ["Ann"] and [t["name"] for t in plan["targets"]] == ["Bob"]


# ---------- bulk update: gating ----------

async def test_write_gate_is_never_bypassed(fake):
    args = dict(targets=["Ann"], change=EMAIL_OFF, dry_run=False, plan_id="x")
    with pytest.raises(PermissionError, match="allow_write"):
        await update(**args)
    with pytest.raises(ValueError, match="org='all'"):
        await update(**{**args, "org": "all", "allow_write": True})
    assert not [c for c in fake.calls if c[0] == "PUT"]
    reg = config.load()
    reg["orgs"]["demo"]["writable"] = False
    config.save(reg)
    with pytest.raises(PermissionError, match="read-only"):
        await update(**args, allow_write=True)
    plan = await update(targets=["Ann"], change=EMAIL_OFF)  # planning stays available and says writes are blocked
    assert plan["writable"] is False
    assert not [c for c in fake.calls if c[0] == "PUT"]


async def test_apply_needs_matching_plan_id(fake):
    args = dict(targets=["Ann", "Bob"], change=EMAIL_OFF, dry_run=False, allow_write=True)
    with pytest.raises(cf.PlanMismatch, match="does not match"):
        await update(**args)  # missing
    with pytest.raises(cf.PlanMismatch):
        await update(**args, plan_id="0123456789abcdef")  # wrong
    assert not fake.puts
    plan = await update(targets=["Ann", "Bob"], change=EMAIL_OFF)
    fake.state["P2"]["messageStorage"]["storageType"] = "EXTERNAL"  # someone changed a setting after the plan
    with pytest.raises(cf.PlanMismatch, match="current state"):
        await update(**args, plan_id=plan["plan_id"])
    with pytest.raises(cf.PlanMismatch):  # same plan, different targets
        await update(**{**args, "targets": ["Ann"]}, plan_id=plan["plan_id"])
    assert not fake.puts


# ---------- bulk update: execution ----------

async def test_apply_canary_then_rest_verifies_every_target(fake):
    plan = await update(targets="from_audit", filter="emailCopyOfMessage.enabled == true", change=EMAIL_OFF)
    r = await update(targets="from_audit", filter="emailCopyOfMessage.enabled == true", change=EMAIL_OFF,
                     dry_run=False, plan_id=plan["plan_id"], allow_write=True)
    assert r["summary"] == {"updated": 4, "unchanged": 0, "failed": 0, "skipped": 0, "error": 0}
    assert sum(1 for x in r["results"] if x.get("canary")) == 1 and r["results"][0]["canary"]
    assert all(x["verified"] and x["after"] == {"emailCopyOfMessage.enabled": False} for x in r["results"])
    assert [p for p in fake.puts if p[0] == "P1"][0][1] == EMAIL_OFF  # partial body, not the whole object
    for sid in ("P1", "P2", "W1", "VL1"):
        assert fake.state[sid]["emailCopyOfMessage"]["enabled"] is False
    assert "side_effects" not in r
    # the canary is read back before anything else is written
    seq = [c for c in fake.calls if c[0] == "PUT" or (c[0] == "GET" and "voicemail" in c[1])]
    first_put = next(i for i, c in enumerate(seq) if c[0] == "PUT")
    assert seq[first_put + 1] == ("GET", seq[first_put][1]) and sum(c[0] == "PUT" for c in seq[:first_put + 2]) == 1


async def test_canary_failure_stops_everything(fake):
    fake.fail_put["P1"] = (400, "bad body")
    args = dict(targets=["Ann", "Bob", "Lobby"], change=EMAIL_OFF)
    plan = await update(**args)
    r = await update(**args, dry_run=False, plan_id=plan["plan_id"], allow_write=True)
    assert r["summary"] == {"updated": 0, "unchanged": 0, "failed": 1, "skipped": 2, "error": 0}
    assert r["results"][0]["canary"] and "PUT failed" in r["results"][0]["error"]
    assert [p[0] for p in fake.puts] == ["P1"]  # nothing else was attempted
    assert fake.state["P2"]["emailCopyOfMessage"]["enabled"] is True


async def test_canary_that_writes_but_does_not_stick_also_stops(fake):
    fake.put_side_effect = lambda sid, body, st: st["emailCopyOfMessage"].update(enabled=True)  # server ignores the change
    args = dict(targets=["Ann", "Bob"], change=EMAIL_OFF)
    plan = await update(**args)
    r = await update(**args, dry_run=False, plan_id=plan["plan_id"], allow_write=True)
    assert r["results"][0]["status"] == "failed" and r["results"][0]["error"].startswith("read-back does not match")
    assert r["summary"]["skipped"] == 1 and len(fake.puts) == 1


async def test_without_canary_the_rest_go_in_parallel_and_failures_are_isolated(fake):
    fake.fail_put["P2"] = (500, "boom")
    args = dict(targets=["Ann", "Bob", "Lobby"], change=EMAIL_OFF, canary_first=False)
    plan = await update(**args)
    r = await update(**args, dry_run=False, plan_id=plan["plan_id"], allow_write=True)
    assert r["summary"]["updated"] == 2 and r["summary"]["failed"] == 1 and not any(x.get("canary") for x in r["results"])
    assert next(x for x in r["results"] if x["name"] == "Bob")["verified"] is False


async def test_already_set_targets_are_not_written(fake):
    args = dict(targets=["Cy", "Ann"], change=EMAIL_OFF)
    plan = await update(**args)
    assert plan["already_set"] == 1 and plan["will_change"] == 1
    r = await update(**args, dry_run=False, plan_id=plan["plan_id"], allow_write=True)
    assert r["summary"]["unchanged"] == 1 and r["summary"]["updated"] == 1 and [p[0] for p in fake.puts] == ["P1"]


async def test_side_effects_in_other_fields_are_reported(fake):
    def flip(sid, body, st):
        if sid == "P1":
            st["messageStorage"]["storageType"] = "INTERNAL"
    fake.state["P1"]["messageStorage"]["storageType"] = "EXTERNAL"
    fake.put_side_effect = flip
    plan = await update(targets=["Ann"], change=EMAIL_OFF)
    assert plan["targets"][0]["related"] == ["messageStorage.storageType=EXTERNAL"]
    r = await update(targets=["Ann"], change=EMAIL_OFF, dry_run=False, plan_id=plan["plan_id"], allow_write=True)
    assert r["side_effects"] == [{"type": "people", "name": "Ann", "changes": [
        {"path": "messageStorage.storageType", "before": "EXTERNAL", "after": "INTERNAL"}]}]


async def test_org_level_setting_gets_full_body_back_with_only_the_key_changed(fake):
    change = {"voiceMessageForwardingEnabled": False}
    plan = await update(targets=["org"], change=change)
    assert plan["targets"][0]["type"] == "org" and plan["targets"][0]["before"] == {"voiceMessageForwardingEnabled": True}
    r = await update(targets=["org"], change=change, dry_run=False, plan_id=plan["plan_id"], allow_write=True)
    assert fake.puts == [("org", {"messageExpiryEnabled": True, "voiceMessageForwardingEnabled": False,
                                  "voicePortalAccessVMDepositEnabled": True})]
    assert r["summary"]["updated"] == 1 and r["results"][0]["verified"] and fake.org["voiceMessageForwardingEnabled"] is False


async def test_org_target_is_applied_before_subjects_and_is_the_canary(fake):
    change = {"voiceMessageForwardingEnabled": False}
    args = dict(targets=["org", "Ann"], change=change)
    plan = await update(**args)
    assert [t["type"] for t in plan["targets"]] == ["org", "people"]
    r = await update(**args, dry_run=False, plan_id=plan["plan_id"], allow_write=True)
    assert r["results"][0]["type"] == "org" and r["results"][0]["canary"]


async def test_feature_without_org_setting_rejects_org_target(fake):
    with pytest.raises(ValueError, match="no org-level setting"):
        await update(feature="doNotDisturb", targets=["org"], change={"enabled": True})


# ---------- webex_request projection (checked, not assumed) ----------

async def test_fields_projection_applies_to_single_object_responses(fake):
    out = json.loads(await server.webex_request("GET", "/people/P1/features/voicemail", org="demo",
                                                fields="enabled,emailCopyOfMessage.enabled"))
    assert out["data"] == {"enabled": True, "emailCopyOfMessage": {"enabled": True}}
