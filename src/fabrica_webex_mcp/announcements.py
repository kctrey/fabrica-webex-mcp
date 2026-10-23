"""Announcement Repository: audio check, upload, list, assign to a feature's greeting slot, guarded delete.

Webex features (call queues, auto attendants, ...) never embed audio. They point at an announcement in the
repository by ID with `greeting: "CUSTOM"`. So the workflow is always: upload to the repository (org or
location level), then assign the announcement ID to a slot on the feature.

Behaviours learned against a live org (all covered by tests):
- Upload is multipart: a `file` part plus a REQUIRED `name` form field. Direct uploads to a queue or auto
  attendant 404; the repository is the only upload path.
- A slot only takes a new file list while its greeting is CUSTOM. Setting it back to DEFAULT keeps the stored
  files (and their references); an empty list or null does nothing. References are only released when the
  slot is repointed at other files or the feature is deleted.
- A file with featureReferenceCount > 0 cannot be deleted (Webex error 6575).
"""
import asyncio
import copy
import struct
from dataclasses import dataclass

from .client import WebexClient

DEFAULT_MAX_AUDIO_KB = 9600  # what /announcements/usage reported; the live value is fetched on upload
_FORMATS = {1: "PCM", 3: "IEEE float", 6: "A-law", 7: "u-law", 0xFFFE: "extensible"}
_USABLE = {1, 6, 7}
_READONLY = {"isTransferNumberSet"}  # returned by GET but rejected/ignored on PUT


# ---------------------------------------------------------------- audio check

def inspect_wav(data: bytes, max_kb: int = DEFAULT_MAX_AUDIO_KB) -> dict:
    """Describe a WAV file and flag what Webex is likely to reject. Stdlib only; no conversion."""
    info: dict = {"size_kb": round(len(data) / 1024, 1), "problems": [], "warnings": []}
    problems, warnings = info["problems"], info["warnings"]
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        problems.append("Not a RIFF/WAVE file. Convert it to WAV first (e.g. with ffmpeg or the OS's own tools).")
        info["ok"] = False
        return info
    fmt = None
    data_size = None
    pos = 12
    while pos + 8 <= len(data):
        cid, size = data[pos:pos + 4], struct.unpack("<I", data[pos + 4:pos + 8])[0]
        body = data[pos + 8:pos + 8 + size]
        if cid == b"fmt " and len(body) >= 16:
            fmt = struct.unpack("<HHIIHH", body[:16])
        elif cid == b"data":
            data_size = min(size, len(data) - pos - 8)  # streamed WAVs may claim 0xFFFFFFFF
            break
        pos += 8 + size + (size & 1)
    if fmt is None or data_size is None:
        problems.append("WAV is missing its fmt or data chunk.")
        info["ok"] = False
        return info
    tag, channels, rate, byte_rate, _align, bits = fmt
    duration = data_size / byte_rate if byte_rate else 0
    info.update(format=_FORMATS.get(tag, f"unknown ({tag})"), channels=channels, sample_rate=rate,
                bits_per_sample=bits, duration_s=round(duration, 2))
    if tag not in _USABLE:
        warnings.append(f"Encoding is {info['format']}; Webex generally expects PCM, A-law or u-law.")
    if channels != 1:
        warnings.append("Not mono; mono is recommended for phone audio.")
    if rate not in (8000, 16000, 22050, 44100, 48000):
        warnings.append(f"Unusual sample rate {rate} Hz.")
    if tag == 1 and bits not in (8, 16):
        warnings.append(f"{bits}-bit PCM; 16-bit is the safe choice.")
    if duration <= 0:
        problems.append("The audio is empty.")
    if info["size_kb"] > max_kb:
        problems.append(f"File is {info['size_kb']} KB; the org's per-file audio limit is {max_kb} KB.")
    info["ok"] = not problems
    return info


# ---------------------------------------------------------------- slot table

@dataclass(frozen=True)
class Slot:
    """One greeting slot on a feature: where it lives in the feature's JSON and how it takes files."""
    path: tuple[str, ...]            # location of the greeting object inside the feature's GET/PUT body
    many: bool = True                # list of files (key audioAnnouncementFiles) or a single file
    max_files: int = 4
    verified: bool = True            # write path confirmed against a live org
    note: str = ""

    @property
    def files_key(self) -> str:
        return "audioAnnouncementFiles" if self.many else "audioAnnouncementFile"


@dataclass(frozen=True)
class Feature:
    list_path: str                   # org-wide list (items carry id, name, locationName[, locationId])
    detail_path: str                 # .format(location_id=, entity_id=)
    slots: dict[str, Slot]


QUEUE = Feature(
    list_path="/telephony/config/queues",
    detail_path="/telephony/config/locations/{location_id}/queues/{entity_id}",
    slots={
        "welcome": Slot(("queueSettings", "welcomeMessage")),
        "comfort": Slot(("queueSettings", "comfortMessage")),
        "moh": Slot(("queueSettings", "mohMessage", "normalSource"), note="Music on hold, normal source."),
        "moh_alternate": Slot(("queueSettings", "mohMessage", "alternateSource")),
        "whisper": Slot(("queueSettings", "whisperMessage")),
        "comfort_bypass": Slot(("queueSettings", "comfortMessageBypass")),
        "overflow": Slot(("queueSettings", "overflow"),
                         note="Only plays when overflow playOverflowGreetingEnabled is true."),
    },
)
AUTO_ATTENDANT = Feature(
    list_path="/telephony/config/autoAttendants",
    detail_path="/telephony/config/locations/{location_id}/autoAttendants/{entity_id}",
    slots={
        "business_hours": Slot(("businessHoursMenu",), many=False, max_files=1,
                               note="The main menu greeting during business hours."),
        "after_hours": Slot(("afterHoursMenu",), many=False, max_files=1),
    },
)
FEATURES: dict[str, Feature] = {"queue": QUEUE, "auto_attendant": AUTO_ATTENDANT}


def slot_table() -> list[dict]:
    """The slot catalogue, for tools and the skill."""
    return [{"feature": f, "slot": n, "file_type": "list" if s.many else "single", "max_files": s.max_files,
             "write_verified": s.verified, "note": s.note}
            for f, feat in FEATURES.items() for n, s in feat.slots.items()]


# ---------------------------------------------------------------- helpers

def _get(obj, path):
    for p in path:
        obj = obj[p]
    return obj


def _nest(path, value) -> dict:
    for p in reversed(path):
        value = {p: value}
    return value


def _ids(slot_obj: dict, slot: Slot) -> list[str]:
    files = slot_obj.get(slot.files_key) or []
    if isinstance(files, dict):
        files = [files]
    return [f["id"] for f in files if isinstance(f, dict) and "id" in f]


def _pick_one(kind: str, wanted: str, candidates: list[dict], ident: str = "id") -> dict:
    """Exact match on id or case-insensitive name; error lists the candidates when not exactly one."""
    hits = [c for c in candidates if c.get(ident) == wanted]
    if not hits:
        hits = [c for c in candidates if (c.get("name") or c.get("fileName") or "").lower() == wanted.lower()]
    if len(hits) == 1:
        return hits[0]
    shown = ", ".join(f"{c.get('name') or c.get('fileName')!r} ({c.get('locationName') or c.get('level') or ''})"
                      for c in (hits or candidates)[:15])
    raise ValueError(f"No unique {kind} matches {wanted!r}. " + ("Matches: " if hits else "Available: ") + shown)


async def location_id(client: WebexClient, alias: str, location: str) -> str:
    items = (await client.request(alias, "GET", "/telephony/config/locations", max_items=1000))["items"]
    return _pick_one("location", location, items)["id"]


# ---------------------------------------------------------------- repository operations

async def usage(client: WebexClient, alias: str) -> dict:
    return (await client.request(alias, "GET", "/telephony/config/announcements/usage"))["data"]


async def upload(client: WebexClient, alias: str, file: tuple[str, bytes, str], name: str,
                 location: str | None = None) -> dict:
    """Validate then upload a WAV to the org repository, or to a location's when `location` is given."""
    limits = await usage(client, alias)
    check = inspect_wav(file[1], int(limits.get("maxAudioFileSizeAllowedKB", DEFAULT_MAX_AUDIO_KB)))
    if not check["ok"]:
        raise ValueError("Audio check failed: " + " ".join(check["problems"]))
    base = "/telephony/config/announcements"
    loc = None
    if location:
        loc = await location_id(client, alias, location)
        base = f"/telephony/config/locations/{loc}/announcements"
    r = await client.request(alias, "POST", base, files={"file": file}, form={"name": name})
    new_id = r["data"]["id"]
    detail = (await client.request(alias, "GET", f"{base}/{new_id}"))["data"]
    return {"id": new_id, "level": "LOCATION" if loc else "ORGANIZATION", "detail": detail,
            "audio": check, "warnings": check["warnings"]}


async def list_announcements(client: WebexClient, alias: str, location: str | None = None,
                             include_references: bool = False) -> list[dict]:
    """Repository files; org-level, or a location's own when `location` is given.

    include_references adds featureReferenceCount per file (one extra GET each, run concurrently).
    """
    query = None
    loc = None
    if location:
        loc = await location_id(client, alias, location)
        query = {"locationId": loc}
    items = (await client.request(alias, "GET", "/telephony/config/announcements", query, max_items=1000))["items"]
    if include_references:
        base = f"/telephony/config/locations/{loc}/announcements" if loc else "/telephony/config/announcements"
        sem = asyncio.Semaphore(8)

        async def count(a):
            async with sem:
                try:
                    d = (await client.request(alias, "GET", f"{base}/{a['id']}"))["data"]
                    a["featureReferenceCount"] = d.get("featureReferenceCount")
                except Exception as e:  # one bad file must not hide the rest
                    a["featureReferenceCount"] = f"error: {e}"
        await asyncio.gather(*(count(a) for a in items))
    return items


async def _find_announcement(client, alias, wanted: str, loc_id: str | None) -> dict:
    """Resolve an announcement by id or name among org-level files plus the location's own."""
    pool = (await client.request(alias, "GET", "/telephony/config/announcements", max_items=1000))["items"]
    if loc_id:
        pool += (await client.request(alias, "GET", "/telephony/config/announcements", {"locationId": loc_id},
                                      max_items=1000))["items"]
    return _pick_one("announcement", wanted, pool)


async def delete(client: WebexClient, alias: str, announcement: str, location: str | None = None) -> dict:
    """Delete one repository file, but only when no feature references it."""
    loc = await location_id(client, alias, location) if location else None
    base = f"/telephony/config/locations/{loc}/announcements" if loc else "/telephony/config/announcements"
    pool = await list_announcements(client, alias, location)
    target = _pick_one("announcement", announcement, pool)
    detail = (await client.request(alias, "GET", f"{base}/{target['id']}"))["data"]
    refs = detail.get("featureReferenceCount") or 0
    if refs:
        raise ValueError(
            f"{detail.get('name')!r} is referenced by {refs} feature(s) and Webex will not delete it. References "
            "cannot be cleared through the API: repoint each slot at another file (assign_announcement), or "
            "delete the feature that uses it.")
    await client.request(alias, "DELETE", f"{base}/{target['id']}")
    return {"deleted": {"id": target["id"], "name": detail.get("name"), "fileName": detail.get("fileName")}}


# ---------------------------------------------------------------- assign

async def resolve_entity(client: WebexClient, alias: str, feature: str, entity: str,
                         location: str | None) -> tuple[str, str, str]:
    """Return (entity_id, location_id, display_name) for a queue/auto attendant given as id or name."""
    feat = FEATURES[feature]
    items = (await client.request(alias, "GET", feat.list_path, max_items=2000))["items"]
    if location:
        items = [i for i in items if (i.get("locationName") or "").lower() == location.lower()
                 or i.get("locationId") == location]
    hit = _pick_one(feature.replace("_", " "), entity, items)
    loc_id = hit.get("locationId") or await location_id(client, alias, hit["locationName"])
    return hit["id"], loc_id, hit.get("name", entity)


async def assign(client: WebexClient, alias: str, feature: str, entity: str, slot: str,
                 announcements: list[str], location: str | None = None, dry_run: bool = False) -> dict:
    """Point a feature's greeting slot at repository announcements (CUSTOM), or back to DEFAULT when the list is empty.

    Returns the slot before and after so the change can be reversed, and warnings for things that make the
    change inaudible or leave stale links behind.
    """
    if feature not in FEATURES:
        raise ValueError(f"Unknown feature {feature!r}. Known: {sorted(FEATURES)}")
    feat = FEATURES[feature]
    if slot not in feat.slots:
        raise ValueError(f"Unknown {feature} slot {slot!r}. Known: {sorted(feat.slots)}")
    spec = feat.slots[slot]
    if len(announcements) > spec.max_files:
        raise ValueError(f"{feature} slot {slot!r} takes at most {spec.max_files} file(s), got {len(announcements)}.")
    entity_id, loc_id, display = await resolve_entity(client, alias, feature, entity, location)
    detail_path = feat.detail_path.format(location_id=loc_id, entity_id=entity_id)
    chosen = [(await _find_announcement(client, alias, a, loc_id)) for a in announcements]

    current = (await client.request(alias, "GET", detail_path))["data"]
    before = copy.deepcopy(_get(current, spec.path))
    new = {k: v for k, v in copy.deepcopy(before).items() if k not in _READONLY}
    warnings: list[str] = []
    if chosen:
        new["greeting"] = "CUSTOM"
        refs = [{"id": c["id"]} for c in chosen]
        new[spec.files_key] = refs if spec.many else refs[0]
        # Webex only swaps the stored file list while the slot is CUSTOM, so the write is a single PUT.
    else:
        new["greeting"] = "DEFAULT"
        if _ids(before, spec):
            warnings.append("Stored file links are kept when a slot returns to DEFAULT (the API cannot clear them), "
                            "so earlier announcements stay referenced and cannot be deleted.")
    if not spec.verified:
        warnings.append(f"{feature} slot {slot!r} write path is not yet verified against a live org; "
                        "the read-back below is the source of truth.")
    if before.get("enabled") is False:
        warnings.append("This slot is disabled (enabled=false), so callers will not hear it until it is enabled.")
    if slot == "overflow" and not before.get("playOverflowGreetingEnabled"):
        warnings.append("playOverflowGreetingEnabled is false, so the overflow greeting will not play.")
    old_ids = _ids(before, spec)
    replaced = [i for i in old_ids if i not in {c["id"] for c in chosen}]
    if chosen and replaced:
        warnings.append(f"Replaced {len(replaced)} previously linked file(s); they remain in the repository.")
    body = _nest(spec.path, new)
    out = {"feature": feature, "entity": display, "entity_id": entity_id, "slot": slot, "before": before,
           "request_body": body, "warnings": warnings}
    if dry_run:
        return {**out, "dry_run": True}
    await client.request(alias, "PUT", detail_path, body=body)
    after = _get((await client.request(alias, "GET", detail_path))["data"], spec.path)
    want = [c["id"] for c in chosen]
    out["after"] = after
    out["verified"] = after.get("greeting") == ("CUSTOM" if chosen else "DEFAULT") and (
        not chosen or _ids(after, spec) == want)
    if not out["verified"]:
        out["warnings"].append("Read-back does not match what was requested; check the slot in Control Hub.")
    return out


# ---------------------------------------------------------------- voicemail greetings (entity-level upload)

GREETING_KINDS = {"no_answer": ("uploadNoAnswerGreeting", "sendUnansweredCalls"),
                  "busy": ("uploadBusyGreeting", "sendBusyCalls")}


async def set_voicemail_greeting(client: WebexClient, alias: str, person: str, kind: str,
                                 file: tuple[str, bytes, str]) -> dict:
    """Upload a custom voicemail greeting for a person (by email or exact display name) and read it back."""
    if kind not in GREETING_KINDS:
        raise ValueError(f"kind must be one of {sorted(GREETING_KINDS)}")
    check = inspect_wav(file[1])
    if not check["ok"]:
        raise ValueError("Audio check failed: " + " ".join(check["problems"]))
    action, section = GREETING_KINDS[kind]
    query = {"email": person} if "@" in person else {"displayName": person}
    people = (await client.request(alias, "GET", "/people", query, max_items=50))["items"]
    if "@" not in person:
        people = [p for p in people if p.get("displayName", "").lower() == person.lower()]
    who = _pick_one("person", people[0]["id"] if len(people) == 1 else person,
                    [{"id": p["id"], "name": p.get("displayName")} for p in people])
    base = f"/people/{who['id']}/features/voicemail"
    await client.request(alias, "POST", f"{base}/actions/{action}/invoke", files={"file": file})
    after = (await client.request(alias, "GET", base))["data"][section]
    return {"person": who["name"], "kind": kind, "greeting": after.get("greeting"),
            "greetingUploaded": after.get("greetingUploaded"), "audio": check, "warnings": check["warnings"]}
