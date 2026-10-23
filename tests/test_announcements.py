import io
import json
import time
import wave

import httpx
import pytest

from fabrica_webex_mcp import announcements as ann
from fabrica_webex_mcp import client, config, server, store

L1, L2 = "LOC_MW", "LOC_EC"
Q1 = "Q_RECEPTION"
ORG_A, LOC_A = "ANN_ORG", "ANN_LOC"


def seed(writable=True):
    store.set_client("cid", "secret")
    store.set_tokens("demo", store.Tokens("A1", "R1", time.time() + 3600, time.time() + 9e6))
    reg = config.load()
    reg["orgs"]["demo"] = {"org_id": "o1", "name": "Demo", "writable": writable}
    reg["default"] = "demo"
    config.save(reg)


def mk(handler):
    return client.WebexClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def wav_bytes(rate=8000, channels=1, seconds=1.0, width=2) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(b"\x00" * int(rate * seconds) * channels * width)
    return buf.getvalue()


FILE = ("greeting.wav", wav_bytes(), "audio/wav")


class FakeWebex:
    """Just enough of the Calling API to exercise the announcement workflow, with state."""

    def __init__(self):
        self.calls = []
        self.queue = {"name": "Receptionist", "queueSettings": {
            "welcomeMessage": {"enabled": True, "alwaysEnabled": True, "greeting": "DEFAULT"},
            "overflow": {"action": "TRANSFER_TO_PHONE_NUMBER", "greeting": "DEFAULT", "isTransferNumberSet": True,
                         "playOverflowGreetingEnabled": False},
            "comfortMessage": {"enabled": False, "greeting": "DEFAULT"}}}
        self.org_files = {ORG_A: {"id": ORG_A, "name": "Welcome", "fileName": "w.wav", "level": "ORGANIZATION",
                                  "featureReferenceCount": 0}}
        self.loc_files = {LOC_A: {"id": LOC_A, "name": "Local", "fileName": "l.wav", "level": "LOCATION",
                                  "featureReferenceCount": 0}}
        self.ref_count = {}

    def __call__(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path.replace("/v1", "", 1)
        q = dict(req.url.params)
        m = req.method
        self.calls.append((m, path, q, req.content))
        J = lambda data, code=200: httpx.Response(code, json=data)
        if path == "/telephony/config/announcements/usage":
            return J({"maxAudioFileSizeAllowedKB": 9600, "totalFileSizeUsedKB": 1})
        if path == "/telephony/config/locations":
            return J({"locations": [{"id": L1, "name": "Midwest"}, {"id": L2, "name": "East Coast"}]})
        if path == "/telephony/config/queues":
            return J({"queues": [{"id": Q1, "name": "Receptionist", "locationName": "Midwest"},
                                 {"id": "Q_EC", "name": "Receptionist", "locationName": "East Coast"},
                                 {"id": "Q_HD", "name": "Help Desk", "locationName": "East Coast"}]})
        if path == "/telephony/config/announcements" and m == "GET":
            files = list(self.loc_files.values()) if q.get("locationId") else list(self.org_files.values())
            return J({"announcements": [{k: v for k, v in f.items() if k != "featureReferenceCount"} for f in files]})
        if path == "/telephony/config/announcements" and m == "POST":
            assert b'name="file"' in req.content and b'name="name"' in req.content
            self.org_files["NEW_ORG"] = {"id": "NEW_ORG", "name": "uploaded", "level": "ORGANIZATION",
                                         "featureReferenceCount": 0}
            return J({"id": "NEW_ORG"}, 201)
        if path == f"/telephony/config/locations/{L1}/announcements" and m == "POST":
            self.loc_files["NEW_LOC"] = {"id": "NEW_LOC", "name": "uploaded", "level": "LOCATION",
                                         "featureReferenceCount": 0}
            return J({"id": "NEW_LOC"}, 201)
        for files, prefix in ((self.org_files, "/telephony/config/announcements/"),
                              (self.loc_files, f"/telephony/config/locations/{L1}/announcements/")):
            if path.startswith(prefix):
                fid = path[len(prefix):]
                if fid not in files:
                    return J({"message": "not found"}, 404)
                if m == "DELETE":
                    if files[fid]["featureReferenceCount"]:
                        return J({"message": "[Error 6575] cannot be deleted"}, 400)
                    del files[fid]
                    return httpx.Response(204)
                return J(files[fid])
        if path == f"/telephony/config/locations/{L1}/queues/{Q1}":
            if m == "PUT":
                self._merge(self.queue, json.loads(req.content))
                return httpx.Response(204)
            return J(self.queue)
        if path == "/people":
            email = q.get("email")
            return J({"items": [{"id": "P1", "displayName": "Jimmy Carr"}] if email == "jc@x.com" else []})
        if path == "/people/P1/features/voicemail/actions/uploadNoAnswerGreeting/invoke":
            assert b'name="file"' in req.content
            return httpx.Response(201)
        if path == "/people/P1/features/voicemail":
            return J({"sendUnansweredCalls": {"greeting": "CUSTOM", "greetingUploaded": True},
                      "sendBusyCalls": {"greeting": "DEFAULT"}})
        return J({"message": f"unexpected {m} {path}"}, 404)

    def _merge(self, dst, src):
        for k, v in src.items():
            if isinstance(v, dict) and isinstance(dst.get(k), dict):
                self._merge(dst[k], v)
            else:
                dst[k] = v


# ------------------------------------------------------------------ audio check

def test_inspect_good_wav():
    r = ann.inspect_wav(wav_bytes())
    assert r["ok"] and r["format"] == "PCM" and r["sample_rate"] == 8000 and r["channels"] == 1
    assert r["duration_s"] == 1.0 and not r["warnings"]


def test_inspect_warns_but_accepts_stereo_and_odd_rate():
    r = ann.inspect_wav(wav_bytes(rate=11025, channels=2))
    assert r["ok"] and len(r["warnings"]) == 2


def test_inspect_rejects_non_wav_empty_and_oversize():
    assert not ann.inspect_wav(b"ID3\x03\x00 not a wav")["ok"]
    assert "empty" in " ".join(ann.inspect_wav(wav_bytes(seconds=0))["problems"])
    big = ann.inspect_wav(wav_bytes(seconds=2), max_kb=1)
    assert not big["ok"] and "per-file audio limit" in big["problems"][0]


def test_inspect_tolerates_streamed_data_size():
    data = bytearray(wav_bytes())
    data[40:44] = b"\xff\xff\xff\xff"  # data chunk length unknown (streamed)
    r = ann.inspect_wav(bytes(data))
    assert r["ok"] and r["duration_s"] == 1.0


def test_slot_table_covers_every_feature():
    rows = ann.slot_table()
    assert {r["feature"] for r in rows} == {"queue", "auto_attendant"}
    assert {"welcome", "comfort", "moh", "moh_alternate", "whisper", "comfort_bypass", "overflow"} <= {
        r["slot"] for r in rows if r["feature"] == "queue"}


# ------------------------------------------------------------------ upload / list / delete

async def test_upload_org_level_sends_file_and_name():
    seed()
    fake = FakeWebex()
    r = await ann.upload(mk(fake), "demo", FILE, "My Greeting")
    assert r["id"] == "NEW_ORG" and r["level"] == "ORGANIZATION"
    post = [c for c in fake.calls if c[0] == "POST"][0]
    assert b"My Greeting" in post[3]


async def test_upload_location_level_resolves_location_name():
    seed()
    fake = FakeWebex()
    r = await ann.upload(mk(fake), "demo", FILE, "Local one", location="midwest")
    assert r["id"] == "NEW_LOC" and r["level"] == "LOCATION"


async def test_upload_rejects_bad_audio_before_posting():
    seed()
    fake = FakeWebex()
    with pytest.raises(ValueError, match="Audio check failed"):
        await ann.upload(mk(fake), "demo", ("x.wav", b"not audio", "audio/wav"), "Bad")
    assert not [c for c in fake.calls if c[0] == "POST"]


async def test_list_with_references_and_location_filter():
    seed()
    fake = FakeWebex()
    fake.loc_files[LOC_A]["featureReferenceCount"] = 3
    items = await ann.list_announcements(mk(fake), "demo", "Midwest", include_references=True)
    assert items[0]["id"] == LOC_A and items[0]["featureReferenceCount"] == 3


async def test_delete_refuses_referenced_file_and_deletes_unreferenced():
    seed()
    fake = FakeWebex()
    fake.org_files[ORG_A]["featureReferenceCount"] = 1
    with pytest.raises(ValueError, match="cannot be cleared through the API"):
        await ann.delete(mk(fake), "demo", "Welcome")
    assert not [c for c in fake.calls if c[0] == "DELETE"]  # never even attempted
    fake.org_files[ORG_A]["featureReferenceCount"] = 0
    r = await ann.delete(mk(fake), "demo", "welcome")
    assert r["deleted"]["id"] == ORG_A and ORG_A not in fake.org_files


# ------------------------------------------------------------------ assign

async def test_assign_dry_run_changes_nothing():
    seed()
    fake = FakeWebex()
    r = await ann.assign(mk(fake), "demo", "queue", "Receptionist", "welcome", ["Welcome"], "Midwest", dry_run=True)
    assert r["dry_run"] and r["request_body"]["queueSettings"]["welcomeMessage"]["greeting"] == "CUSTOM"
    assert not [c for c in fake.calls if c[0] == "PUT"]


async def test_assign_sets_custom_with_ids_and_verifies():
    seed()
    fake = FakeWebex()
    r = await ann.assign(mk(fake), "demo", "queue", "Receptionist", "welcome", ["Welcome", LOC_A], "Midwest")
    wm = fake.queue["queueSettings"]["welcomeMessage"]
    assert wm["greeting"] == "CUSTOM" and wm["audioAnnouncementFiles"] == [{"id": ORG_A}, {"id": LOC_A}]
    assert wm["alwaysEnabled"] is True  # other fields on the slot are preserved
    assert r["verified"] is True and r["before"]["greeting"] == "DEFAULT"


class StickyFake(FakeWebex):
    """Like Webex: a slot keeps its stored files unless the new greeting is CUSTOM."""

    def _merge(self, dst, src):
        if src.get("greeting") == "DEFAULT" and "audioAnnouncementFiles" in dst:
            src = {k: v for k, v in src.items() if k != "audioAnnouncementFiles"}
        super()._merge(dst, src)


async def test_assign_default_warns_that_links_stay():
    seed()
    fake = StickyFake()
    c = mk(fake)
    await ann.assign(c, "demo", "queue", "Receptionist", "comfort", ["Welcome"], "Midwest")
    r = await ann.assign(c, "demo", "queue", "Receptionist", "comfort", [], "Midwest")
    assert r["after"]["greeting"] == "DEFAULT" and r["verified"] is True
    assert any("cannot clear" in w for w in r["warnings"])


async def test_assign_strips_read_only_fields_and_warns_when_inaudible():
    seed()
    fake = FakeWebex()
    r = await ann.assign(mk(fake), "demo", "queue", "Receptionist", "overflow", ["Welcome"], "Midwest")
    assert "isTransferNumberSet" not in r["request_body"]["queueSettings"]["overflow"]
    assert any("playOverflowGreetingEnabled" in w for w in r["warnings"])
    r2 = await ann.assign(mk(fake), "demo", "queue", "Receptionist", "comfort", ["Welcome"], "Midwest")
    assert any("disabled" in w for w in r2["warnings"])


async def test_assign_reports_replaced_files():
    seed()
    fake = FakeWebex()
    c = mk(fake)
    await ann.assign(c, "demo", "queue", "Receptionist", "welcome", ["Welcome"], "Midwest")
    r = await ann.assign(c, "demo", "queue", "Receptionist", "welcome", [LOC_A], "Midwest")
    assert any("Replaced 1 previously linked" in w for w in r["warnings"])


async def test_assign_validation_errors():
    seed()
    c = mk(FakeWebex())
    with pytest.raises(ValueError, match="Unknown feature"):
        await ann.assign(c, "demo", "hunt_group", "x", "welcome", [])
    with pytest.raises(ValueError, match="Unknown queue slot"):
        await ann.assign(c, "demo", "queue", "Receptionist", "nope", [], "Midwest")
    with pytest.raises(ValueError, match="at most 4"):
        await ann.assign(c, "demo", "queue", "Receptionist", "welcome", ["a"] * 5, "Midwest")
    with pytest.raises(ValueError, match="No unique queue"):  # two queues share the name; needs a location
        await ann.assign(c, "demo", "queue", "Receptionist", "welcome", [ORG_A])
    with pytest.raises(ValueError, match="No unique announcement"):
        await ann.assign(c, "demo", "queue", "Receptionist", "welcome", ["does not exist"], "Midwest")


# ------------------------------------------------------------------ voicemail greeting

async def test_set_voicemail_greeting_uploads_and_reads_back():
    seed()
    r = await ann.set_voicemail_greeting(mk(FakeWebex()), "demo", "jc@x.com", "no_answer", FILE)
    assert r["person"] == "Jimmy Carr" and r["greeting"] == "CUSTOM" and r["greetingUploaded"] is True


async def test_set_voicemail_greeting_unknown_person_and_kind():
    seed()
    c = mk(FakeWebex())
    with pytest.raises(ValueError, match="No unique person"):
        await ann.set_voicemail_greeting(c, "demo", "nobody@x.com", "no_answer", FILE)
    with pytest.raises(ValueError, match="kind must be"):
        await ann.set_voicemail_greeting(c, "demo", "jc@x.com", "weird", FILE)


# ------------------------------------------------------------------ tool guards

async def test_new_write_tools_need_allow_write_and_writable_org(tmp_path):
    seed(writable=False)
    f = tmp_path / "g.wav"
    f.write_bytes(wav_bytes())
    with pytest.raises(PermissionError, match="allow_write"):
        await server.upload_announcement(str(f), "n")
    with pytest.raises(PermissionError, match="read-only"):
        await server.upload_announcement(str(f), "n", allow_write=True)
    with pytest.raises(PermissionError, match="allow_write"):
        await server.assign_announcement("queue", "q", "welcome", ["a"])
    with pytest.raises(PermissionError, match="allow_write"):
        await server.delete_announcement("a")
    with pytest.raises(PermissionError, match="allow_write"):
        await server.set_voicemail_greeting("a@x.com", str(f))


def test_inspect_audio_tool(tmp_path):
    f = tmp_path / "g.wav"
    f.write_bytes(wav_bytes())
    assert json.loads(server.inspect_audio(str(f)))["ok"] is True


def test_upload_dir_restriction(tmp_path, monkeypatch):
    inside, outside = tmp_path / "ok", tmp_path / "no"
    inside.mkdir()
    outside.mkdir()
    (inside / "a.wav").write_bytes(wav_bytes())
    (outside / "b.wav").write_bytes(wav_bytes())
    monkeypatch.setenv(server.UPLOAD_DIR_ENV, str(inside))
    assert server._load_upload(str(inside / "a.wav"))[0] == "a.wav"
    with pytest.raises(ValueError, match="restricted"):
        server._load_upload(str(outside / "b.wav"))
