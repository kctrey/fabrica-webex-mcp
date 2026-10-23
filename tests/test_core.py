import time

import httpx
import pytest

from fabrica_webex_mcp import auth, client, config, output, server, store


def seed(alias="demo", access="A1", expires_in=3600, writable=False):
    store.set_client("cid", "secret")
    store.set_tokens(alias, store.Tokens(access, "R1", time.time() + expires_in, time.time() + 9e6))
    reg = config.load()
    reg["orgs"][alias] = {"org_id": "o1", "name": "Demo", "writable": writable}
    reg["default"] = alias
    config.save(reg)


def mk(handler):
    return client.WebexClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def test_build_url():
    assert client.build_url("/people", {"max": 5})[0] == "https://webexapis.com/v1/people"
    assert client.build_url("/v1/people?a=1")[1] == {"a": "1"}
    assert client.build_url("https://webexapis.com/v1/people")[0].endswith("/v1/people")
    assert client.build_url("https://analytics.webexapis.com/v1/cdr_feed", {"max": 1}) == (
        "https://analytics.webexapis.com/v1/cdr_feed", {"max": 1})
    for bad in ["https://evil.com/v1/x", "https://webexapis.com.evil.com/v1/x", "http://webexapis.com/v1/x", "/a/../b", "/"]:
        with pytest.raises(ValueError):
            client.build_url(bad)


async def test_refresh_rotates_and_persists():
    seed(expires_in=10)  # inside skew -> refresh

    def h(req):
        if req.url.path.endswith("access_token"):
            return httpx.Response(200, json={"access_token": "A2", "refresh_token": "R2", "expires_in": 100, "refresh_token_expires_in": 200})
        assert req.headers["authorization"] == "Bearer A2"
        return httpx.Response(200, json={"id": 1})
    c = mk(h)
    r = await c.request("demo", "GET", "/people/me")
    assert r["data"] == {"id": 1}
    t = store.get_tokens("demo")
    assert (t.access, t.refresh) == ("A2", "R2")


async def test_401_triggers_one_refresh():
    seed()
    calls = []

    def h(req):
        if req.url.path.endswith("access_token"):
            return httpx.Response(200, json={"access_token": "A2", "expires_in": 100})
        calls.append(req.headers["authorization"])
        return httpx.Response(401 if len(calls) == 1 else 200, json={"ok": 1})
    r = await mk(h).request("demo", "GET", "/people/me")
    assert calls == ["Bearer A1", "Bearer A2"] and r["status"] == 200
    assert store.get_tokens("demo").refresh == "R1"  # not rotated -> kept


async def test_pagination_and_429(monkeypatch):
    seed()
    slept = []

    async def fake_sleep(s):
        slept.append(s)
    monkeypatch.setattr(client.asyncio, "sleep", fake_sleep)
    state = {"n": 0}

    def h(req):
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "3"})
        if "page2" not in str(req.url):
            return httpx.Response(200, json={"items": [1, 2]}, headers={"Link": '<https://webexapis.com/v1/people?page2=1>; rel="next"'})
        return httpx.Response(200, json={"items": [3]})
    r = await mk(h).request("demo", "GET", "/people")
    assert r["items"] == [1, 2, 3] and r["pages"] == 2 and slept == [3.0]


async def test_wrapped_collection_key():
    seed()
    h = lambda req: httpx.Response(200, json={"locations": [{"id": 1}, {"id": 2}]})
    r = await mk(h).request("demo", "GET", "/telephony/config/locations")
    assert r["items"] == [{"id": 1}, {"id": 2}] and r["collection"] == "locations"
    # a single object that merely contains lists is not a collection
    h = lambda req: httpx.Response(200, json={"id": "x", "emails": ["a"]})
    assert "data" in await mk(h).request("demo", "GET", "/people/me")


async def test_max_items_truncates():
    seed()
    h = lambda req: httpx.Response(200, json={"items": [1, 2, 3, 4]})
    r = await mk(h).request("demo", "GET", "/people", max_items=2)
    assert r["items"] == [1, 2] and r["truncated"]


async def test_refresh_failure_message():
    seed(expires_in=0)
    h = lambda req: httpx.Response(400, json={"error": "invalid_grant"})
    with pytest.raises(auth.AuthError, match="add-org demo --replace"):
        await mk(h).request("demo", "GET", "/people")


async def test_write_guard():
    seed()
    with pytest.raises(PermissionError, match="allow_write"):
        await server.webex_request("POST", "/people", body={})
    with pytest.raises(PermissionError, match="read-only"):
        await server.webex_request("POST", "/people", body={}, allow_write=True)
    with pytest.raises(ValueError):
        await server.webex_request("DELETE", "/people/x", org="all", allow_write=True)


async def test_multipart_upload_sent_with_file_and_form():
    seed()
    seen = {}

    def h(req):
        seen["ctype"] = req.headers["content-type"]
        seen["body"] = req.content
        return httpx.Response(204)
    r = await mk(h).request("demo", "POST", "/people/p1/features/voicemail/actions/uploadNoAnswerGreeting/invoke",
                            files={"file": ("g.wav", b"RIFFdata", "audio/wav")}, form={"note": "x"})
    assert r == {"status": 204}
    assert seen["ctype"].startswith("multipart/form-data; boundary=")
    assert b'name="file"; filename="g.wav"' in seen["body"] and b"RIFFdata" in seen["body"]
    assert b'name="note"' in seen["body"]


async def test_multipart_and_json_body_are_exclusive():
    seed()
    with pytest.raises(ValueError, match="not both"):
        await mk(lambda req: httpx.Response(204)).request("demo", "POST", "/x", body={}, files={"file": ("a", b"b", "x/y")})


async def test_upload_tool_guards(tmp_path):
    seed(writable=True)
    f = tmp_path / "g.wav"
    f.write_bytes(b"abc")
    with pytest.raises(PermissionError, match="allow_write"):  # uploads are writes, so the same guard applies
        await server.webex_request("POST", "/x", file_path=str(f))
    with pytest.raises(ValueError, match="only supported for POST"):
        await server.webex_request("GET", "/x", file_path=str(f))
    with pytest.raises(ValueError, match="not both"):
        await server.webex_request("POST", "/x", body={}, file_path=str(f), allow_write=True)
    with pytest.raises(ValueError, match="not found"):
        await server.webex_request("POST", "/x", file_path=str(tmp_path / "nope.wav"), allow_write=True)
    big = tmp_path / "big.wav"
    big.write_bytes(b"0" * (server.MAX_UPLOAD_BYTES + 1))
    with pytest.raises(ValueError, match="larger than"):
        await server.webex_request("POST", "/x", file_path=str(big), allow_write=True)


def test_load_upload_guesses_content_type(tmp_path):
    f = tmp_path / "greeting.wav"
    f.write_bytes(b"abc")
    name, data, ctype = server._load_upload(str(f))
    assert (name, data) == ("greeting.wav", b"abc")
    assert ctype in {"audio/wav", "audio/x-wav", "audio/vnd.wave"}  # varies by platform mime table


async def test_exchange_code_saves_tokens():
    store.set_client("cid", "secret")
    seen = {}

    def h(req):
        seen.update(dict(p.split("=") for p in req.content.decode().split("&")))
        return httpx.Response(200, json={"access_token": "AX", "refresh_token": "RX", "expires_in": 100, "refresh_token_expires_in": 200})
    async with httpx.AsyncClient(transport=httpx.MockTransport(h)) as http:
        await auth.exchange_code("new", "CODE", "http://localhost:8765/callback", http)
    assert seen["grant_type"] == "authorization_code" and seen["code"] == "CODE"
    assert store.get_tokens("new").refresh == "RX"


def test_login_scopes_and_url(tmp_path):
    from fabrica_webex_mcp import login
    login.scopes_path().write_text("# c\nspark:kms\nspark:kms\nspark-admin:people_read\n")
    assert login.load_scopes() == "spark:kms spark-admin:people_read"
    login.scopes_path().write_text("spark:kms\nspark-admin:xsi\nspark-admin:people_read\n")
    assert login.load_scopes() == "spark:kms spark-admin:people_read"  # xsi listed in the file is ignored by default
    assert login.load_scopes(with_xsi=True) == "spark:kms spark-admin:people_read spark-admin:xsi"
    login.scopes_path().write_text("spark:kms\n")
    assert login.load_scopes(with_xsi=True) == "spark:kms spark-admin:xsi"  # added even if the file omits it
    u = login.authorize_url("cid", "http://localhost:8765/callback", "a b", "st")
    assert "redirect_uri=http%3A%2F%2Flocalhost%3A8765%2Fcallback" in u and "state=st" in u


def test_callback_rejects_bad_state():
    import threading, urllib.request, urllib.error
    from fabrica_webex_mcp import login
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("code", login.wait_for_code(8791, "good", timeout=5)))
    t.start()
    import time; time.sleep(0.5)
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen("http://127.0.0.1:8791/callback?code=evil&state=bad")
    urllib.request.urlopen("http://127.0.0.1:8791/callback?code=ok&state=good")
    t.join()
    assert out["code"] == "ok"


def test_select_and_fit():
    items = [{"id": i, "a": {"b": i, "c": 0}, "big": "x" * 100} for i in range(50)]
    sel = output.select(items, ["id", "a.b"])
    assert sel[3] == {"id": 3, "a": {"b": 3}}
    s = output.fit({"items": items}, 1000)
    assert len(s) <= 1000 and '"truncated":true' in s


def _license_handler(ocp_errors=()):
    def h(req):
        p = req.url.path
        if p.endswith("/licenses"):
            return httpx.Response(200, json={"items": [{"id": "LP", "name": "Webex Calling - Professional"},
                                                       {"id": "LW", "name": "Webex Calling - Workspaces"},
                                                       {"id": "LH", "name": "Webex Calling - Hot desk only"}]})
        if p.endswith("/locations"):
            return httpx.Response(200, json={"items": [{"id": "L1", "name": "East"}, {"id": "L2", "name": "West"},
                                                       {"id": "L3", "name": "Empty"}]})
        if p.endswith("/people"):
            assert req.url.params["callingData"] == "true"
            return httpx.Response(200, json={"items": [
                {"id": "p1", "displayName": "Ann", "licenses": ["LP"], "locationId": "L1"},
                {"id": "p2", "displayName": "Bob", "licenses": ["LP"], "locationId": "L2"},
                {"id": "p3", "displayName": "Cy", "licenses": ["LP"]},          # no location
                {"id": "p4", "displayName": "Di", "licenses": ["other"], "locationId": "L1"}]})  # not licensed
        if p.endswith("/workspaces"):
            return httpx.Response(200, json={"items": [
                {"id": "w1", "displayName": "Lobby", "locationId": "L1", "calling": {"type": "webexCalling", "webexCalling": {"licenses": ["LW"]}}},
                {"id": "w2", "displayName": "Desk", "locationId": "L1", "calling": {"type": "webexCalling", "webexCalling": {"licenses": ["LP"]}}},
                {"id": "w3", "displayName": "HotOnly", "locationId": "L2", "calling": {"type": "webexCalling", "webexCalling": {"licenses": ["LH"]}}},
                {"id": "w4", "displayName": "Free", "calling": {"type": "freeCalling"}}]})
        if p.endswith("/virtualLines"):  # sibling "customer" key keeps it out of the items normalisation
            return httpx.Response(200, json={"virtualLines": [
                {"id": "v1", "firstName": "VL", "lastName": "One", "location": {"id": "L2", "name": "West"}, "billingPlan": "BCOCP1"},
                {"id": "v2", "firstName": "VL", "lastName": "Two", "location": {"id": "L2", "name": "West"}}],
                "customer": {"id": "c"}})
        if p.endswith("/outboundBillingPlan"):
            eid = p.split("/")[-2]
            if eid in ocp_errors:
                return httpx.Response(400, json={"message": "not a Cisco Calling Plan location"})
            return httpx.Response(200, json={"enabled": eid in {"p1", "w1", "w3"}})
        raise AssertionError(p)
    return h


async def test_license_report_counts():
    from fabrica_webex_mcp import license_report
    seed()
    r = await license_report.collect(mk(_license_handler(ocp_errors={"p2"})), "demo")
    rows = {x["location"]: x for x in r["locations"]}
    assert rows["East"] == {"location": "East", "professional": 2, "workspace": 1, "ocp": 2, "virtual_lines": 0}
    # Bob's OCP lookup errored (n/a, not zero); the hot-desk-only workspace still gets its OCP; v2 has no plan
    assert rows["West"] == {"location": "West", "professional": 1, "workspace": 0, "ocp": 2, "virtual_lines": 1}
    assert rows["Empty"]["professional"] == 0 and rows["No Location"]["professional"] == 1
    assert [x["location"] for x in r["locations"]][-1] == "No Location"
    assert r["totals"] == {"professional": 4, "workspace": 1, "ocp": 4, "virtual_lines": 1}
    assert r["warnings"]["ocp_not_applicable"] == ["Bob"]
    assert r["warnings"]["licensed_without_location"] == ["Cy"]
    assert r["warnings"]["workspaces_without_professional_or_workspace_license"] == ["HotOnly"]


async def test_license_report_without_ocp_and_detail():
    from fabrica_webex_mcp import license_report
    seed()
    r = await license_report.collect(mk(_license_handler()), "demo", include_ocp=False)
    assert r["totals"]["ocp"] is None and all(x["ocp"] is None for x in r["locations"])
    east = license_report.detail(r, "east")
    assert [e["name"] for e in east] == ["Ann", "Desk", "Lobby"]
    assert license_report.detail(r, "nowhere") is None


def _wid(resource, u):
    import base64
    return base64.urlsafe_b64encode(f"ciscospark://us/{resource}/{u}".encode()).decode().rstrip("=")


U1, U2, U3 = ("11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222",
              "33333333-3333-3333-3333-333333333333")
U4 = "44444444-4444-4444-4444-444444444444"
XSI = "https://api-us.bcld.webex.com/com.broadsoft.xsi-actions"


def _xsi_handler(calls):
    def h(req):
        p, host = req.url.path, req.url.host
        calls.append((host, p))
        if host == "api-us.bcld.webex.com":
            assert req.headers["x-broadworks-protocol-version"] == "25.0" and req.headers["authorization"] == "Bearer A1"
            if p.endswith("/profile"):
                return httpx.Response(200, json={"Profile": {"details": {
                    "firstName": {"$": "Front Desk"}, "lastName": {"$": "."}, "externalIdType": {"$": "Place"}}}})
            if U3 in p:
                return httpx.Response(404, text="<ErrorInfo><summary>user not found</summary></ErrorInfo>")
            one = {"endpointType": {"$": "Mobility"}, "expiration": {"$": "soon"},
                   "userAgent": {"$": "Cisco-CP8851/12.0.1_AABBCCDDEEFF_x"},
                   "uri": {"$": "sip:u@192.168.1.5:5060"}, "pathHeader": {"$": '<sip:x>;x-bw-nat="9.9.9.9:5060"'}}
            return httpx.Response(200, json={"Registrations": {"registration": one}})  # a single one is an object
        if p.endswith("/organizations/o1"):
            return httpx.Response(200, json={"xsiActionsEndpoint": XSI + "/"})
        if p.endswith("/people"):
            q = req.url.params
            if q.get("email") == "ann@x.com":
                return httpx.Response(200, json={"items": [{"id": _wid("PEOPLE", U1), "displayName": "Ann"}]})
            if q.get("displayName"):
                return httpx.Response(200, json={"items": [{"id": _wid("PEOPLE", U1), "displayName": "Front Desk Ann", "emails": ["a@x.com"]}]})
            return httpx.Response(200, json={"items": []})
        if p.endswith("/workspaces"):
            return httpx.Response(200, json={"items": [{"id": _wid("PLACE", U2), "displayName": "Front Desk"}]})
        if p.endswith("/virtualLines"):
            return httpx.Response(200, json={"virtualLines": [{"id": _wid("VIRTUAL_LINE", U4), "firstName": "Lobby", "lastName": "Line"}],
                                             "customer": {}})
        raise AssertionError(p)
    return h


async def test_xsi_registrations_by_name_email_and_id():
    from fabrica_webex_mcp import xsi
    xsi._endpoints.clear()
    seed()
    calls = []
    c = mk(_xsi_handler(calls))
    res = await xsi.lookup(c, "demo", f"Front Desk, ann@x.com, {_wid('PLACE', U2)}, Lobby Line, nobody, {U3}")
    by = {r["entry"].split(",")[0][:12]: r for r in res}
    front = res[0]  # exact name match wins over the partial "Front Desk Ann": a workspace
    assert front["subject"] == "Front Desk (Workspace)" and front["registrations"] == [{
        "type": "Webex Go", "model": "Cisco-CP8851", "version": "12.0.1", "mac": "AA:BB:CC:DD:EE:FF", "expiration": "soon",
        "public_ip": "9.9.9.9", "private_ip": "192.168.1.5"}]
    assert res[1]["entry"] == "ann@x.com" and res[1]["registrations"]
    assert res[2]["registrations"]  # base64 workspace ID
    assert "error" not in res[3] and res[3]["registrations"]  # a virtual line, by name
    assert res[3]["subject"].endswith("(Virtual Line)")  # the Webex-side type, not the profile's (mock says "Place")
    assert res[1]["subject"].endswith("(User)")
    assert "named 'nobody'" in res[4]["error"]
    assert "not a Webex Calling user" in res[5]["error"] and "user not found" in res[5]["error"]  # 404 guidance
    assert {h for h, _ in calls if h != "webexapis.com"} == {"api-us.bcld.webex.com"}


async def test_xsi_ambiguous_name_lists_candidates():
    from fabrica_webex_mcp import xsi
    xsi._endpoints.clear()
    seed()
    res = await xsi.lookup(mk(_xsi_handler([])), "demo", "Front")
    assert "more than one" in res[0]["error"] and "Front Desk (Workspace)" in res[0]["error"] \
        and "Front Desk Ann (User, a@x.com)" in res[0]["error"]


async def test_token_only_goes_to_webex_hosts():
    seed()
    c = mk(lambda req: httpx.Response(200, json={}))
    for bad in ["https://evil.com/x", "https://webex.com.evil.com/x", "http://api-us.bcld.webex.com/x"]:
        with pytest.raises(ValueError):
            await c.get_external("demo", bad, ".webex.com")


def test_xsi_parsing_shapes():
    from fabrica_webex_mcp import xsi
    assert xsi.parse_registrations({"Registrations": {}}) == []
    rows = xsi.parse_registrations({"Registrations": {"registration": [
        {"userAgent": {"$": "Cisco-DP-9861/5.0.1_845a3ec378eb_x"}, "uri": {"$": "sip:a@10.0.0.2:5061;endpoint=10.0.0.9"},
         "pathHeader": {"$": ""}},
        {"publicIP": {"$": "1.2.3.4"}, "privateIP": {"$": "5.6.7.8"}}]}})
    assert rows[0]["mac"] == "84:5A:3E:C3:78:EB" and rows[0]["private_ip"] == "10.0.0.9" and rows[0]["public_ip"] == "Unknown"
    assert rows[1]["public_ip"] == "1.2.3.4" and rows[1]["private_ip"] == "5.6.7.8" and rows[1]["model"] == "Unknown"


def test_home_falls_back_to_xdg_config_off_windows(tmp_path, monkeypatch):
    from fabrica_webex_mcp import config
    monkeypatch.delenv("FABRICA_WEBEX_MCP_HOME")
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert config.home() == tmp_path / "xdg" / "fabrica-webex-mcp"
    monkeypatch.delenv("XDG_CONFIG_HOME")
    monkeypatch.setattr(config.Path, "home", lambda: tmp_path / "h")
    assert config.home() == tmp_path / "h" / ".config" / "fabrica-webex-mcp"


def test_home_prefers_override_then_appdata(tmp_path, monkeypatch):
    from fabrica_webex_mcp import config
    monkeypatch.setenv("APPDATA", str(tmp_path / "ad"))
    assert config.home() == tmp_path  # FABRICA_WEBEX_MCP_HOME from conftest wins
    monkeypatch.delenv("FABRICA_WEBEX_MCP_HOME")
    assert config.home() == tmp_path / "ad" / "fabrica-webex-mcp"


def test_rejected_scope_error_names_xsi():
    import threading, urllib.request
    from fabrica_webex_mcp import login
    out = {}

    def run():
        try:
            login.wait_for_code(8792, "good", timeout=5)
        except TimeoutError as e:
            out["msg"] = str(e)
    t = threading.Thread(target=run)
    t.start()
    import time; time.sleep(0.5)
    urllib.request.urlopen("http://127.0.0.1:8792/callback?error=invalid_scope&state=good")
    t.join()
    assert "invalid_scope" in out["msg"] and "--with-xsi" in out["msg"]
