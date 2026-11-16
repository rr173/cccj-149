"""Manifest / JSONL loading and structural validation for raw clock cleaning.

A manifest is one JSON object::

    {"streams": {"<stream>": "<IANA zone>", ...},
     "files":   ["<jsonl path relative to the manifest dir>", ...]}

Each JSONL line carries the common fields ``record_id`` / ``stream`` / ``seq``
/ ``type``; ``type=door`` adds ``device`` / ``session_id`` / ``kind`` /
``content`` and exactly one time field (``local`` or ``utc_ms``);
``type=anchor`` adds only ``utc_ms``.

Structural problems raise :class:`ClockImportError` and fail the *whole*
import -- ambiguous clocks are data-team business output, never a reason to
relax structural checks.  Physical line positions are kept for every read so
that folded re-sends still list every source location.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from .timezones import TimezoneError, get_zone, is_int64

DOOR = "door"
ANCHOR = "anchor"
DOOR_KINDS = ("enter", "exit")
INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1

_COMMON_FIELDS = ("record_id", "stream", "seq", "type")
_DOOR_FIELDS = _COMMON_FIELDS + ("device", "session_id", "kind", "content")
_TIME_FIELDS = ("local", "utc_ms")


class ClockImportError(ValueError):
    """The raw import is structurally invalid; nothing may be published."""


@dataclass(frozen=True)
class SourceLocation:
    file: str          # path as listed in the manifest
    line: int          # 1-based physical line inside that file


@dataclass
class RawRecord:
    record_id: str
    stream: str
    seq: int
    type: str
    sources: list[SourceLocation] = field(default_factory=list)
    # door-only
    device: str | None = None
    session_id: str | None = None
    kind: str | None = None
    content: str | None = None
    # exactly one of the two is set
    local: str | None = None
    utc_ms: int | None = None

    def identity_key(self):
        """Content compared when the same record_id is seen again."""
        return (
            self.record_id, self.stream, self.seq, self.type, self.device,
            self.session_id, self.kind, self.content, self.local, self.utc_ms,
        )


@dataclass
class LoadedManifest:
    streams: dict                       # stream name -> IANA zone name
    zones: dict                         # stream name -> ZoneInfo
    files: tuple                        # file paths as listed
    records: tuple                      # tuple[RawRecord], one logical row per id
    by_id: dict
    locations_by_id: dict              # record_id -> list[SourceLocation] (all sends)


def _is_nonempty_str(v) -> bool:
    return isinstance(v, str) and bool(v)


def load_manifest(manifest_path) -> LoadedManifest:
    manifest_path = os.path.abspath(os.fspath(manifest_path))
    base_dir = os.path.dirname(manifest_path)
    try:
        with open(manifest_path, "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
    except FileNotFoundError as exc:
        raise ClockImportError(f"manifest not found: {manifest_path}") from exc
    except json.JSONDecodeError as exc:
        raise ClockImportError(f"manifest {manifest_path} is not valid JSON: {exc}") from exc

    errors: list[str] = []
    if not isinstance(manifest, dict):
        raise ClockImportError("manifest must be a JSON object")
    extra = set(manifest) - {"streams", "files"}
    if extra:
        errors.append(f"manifest: unknown top-level field(s) {sorted(extra)}")
    if missing := {"streams", "files"} - set(manifest):
        errors.append(f"manifest: missing top-level field(s) {sorted(missing)}")

    streams_raw = manifest.get("streams")
    files_raw = manifest.get("files")
    if not isinstance(streams_raw, dict) or not streams_raw:
        errors.append("manifest: 'streams' must be a non-empty object mapping stream -> IANA zone")
        streams_raw = {}
    if not isinstance(files_raw, list) or not files_raw:
        errors.append("manifest: 'files' must be a non-empty list of JSONL paths")
        files_raw = []

    streams, zones = {}, {}
    for name, tzname in streams_raw.items():
        if not _is_nonempty_str(name):
            errors.append(f"manifest: stream name must be a non-empty string, got {name!r}")
            continue
        if not _is_nonempty_str(tzname):
            errors.append(f"manifest: stream {name!r}: timezone must be a non-empty string")
            continue
        streams[name] = tzname
        try:
            zones[name] = get_zone(tzname)
        except TimezoneError as exc:
            # invalid zones are hard failures, never quarantine material
            errors.append(str(exc))

    norm_files = []
    for f in files_raw:
        if not _is_nonempty_str(f):
            errors.append(f"manifest: file path must be a non-empty string, got {f!r}")
            continue
        norm_files.append(f)

    records, by_id, locations = [], {}, {}
    seen_stream_seq: dict[tuple[str, int], str] = {}
    seen_session_kind: dict[tuple, str] = {}

    for rel in norm_files:
        path = os.path.abspath(os.path.join(base_dir, rel))
        if os.path.commonpath([base_dir, path]) != base_dir:
            errors.append(f"manifest: file {rel!r} escapes the manifest directory")
            continue
        try:
            fh = open(path, "r", encoding="utf-8")
        except OSError as exc:
            errors.append(f"cannot open data file {rel!r}: {exc}")
            continue
        with fh:
            for lineno, line in enumerate(fh, start=1):
                if not line.strip():
                    errors.append(f"{rel}:{lineno}: blank or empty line")
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError as exc:
                    errors.append(f"{rel}:{lineno}: malformed JSON line: {exc}")
                    continue
                loc = SourceLocation(rel, lineno)
                rec_errs = _validate_row(obj, streams, loc)
                if rec_errs:
                    errors.extend(rec_errs)
                    continue
                rec = _build_record(obj, loc)
                if _index_record(rec, loc, records, by_id, locations,
                                 seen_stream_seq, seen_session_kind, errors):
                    records.append(rec)

    if errors:
        # Report all structural problems at once; the import fails as a whole.
        detail = "\n  ".join(errors)
        raise ClockImportError(f"raw import failed with {len(errors)} structural problem(s):\n  {detail}")

    # Re-sends fold, but EVERY occurrence of a record_id must still respect the
    # stream/seq and session/kind ownership rules (the fold above only checked
    # the first occurrence).
    for rid, rid_locations in locations.items():
        if len(rid_locations) <= 1:
            continue
        rec = by_id[rid]
        for loc in rid_locations[1:]:
            owner = seen_stream_seq.get((rec.stream, rec.seq))
            if owner is not None and owner != rid:
                errors.append(
                    f"{loc.file}:{loc.line}: re-sent record {rid!r} collides with stream "
                    f"{rec.stream!r} seq {rec.seq} owned by {owner!r}"
                )
            if rec.type == DOOR:
                skey = (rec.device, rec.session_id, rec.kind)
                owner = seen_session_kind.get(skey)
                if owner is not None and owner != rid:
                    errors.append(
                        f"{loc.file}:{loc.line}: re-sent record {rid!r} collides with "
                        f"(device, session_id, kind)={skey} owned by {owner!r}"
                    )
    if errors:
        detail = "\n  ".join(errors)
        raise ClockImportError(f"raw import failed with {len(errors)} structural problem(s):\n  {detail}")

    return LoadedManifest(
        streams=streams, zones=zones, files=tuple(norm_files),
        records=tuple(records), by_id=by_id, locations_by_id=locations,
    )


def _validate_row(obj, streams, loc) -> list[str]:
    errs = []
    where = f"{loc.file}:{loc.line}"
    if not isinstance(obj, dict):
        return [f"{where}: record must be a JSON object"]
    rtype = obj.get("type")
    allowed = set(_DOOR_FIELDS) | set(_TIME_FIELDS) if rtype == DOOR else \
        set(_COMMON_FIELDS) | {"utc_ms"}
    if unknown := set(obj) - allowed:
        errs.append(f"{where}: unknown field(s) {sorted(unknown)} for type={rtype!r}")
    for key in _COMMON_FIELDS:
        if key not in obj:
            errs.append(f"{where}: missing common field {key!r}")
    rid = obj.get("record_id")
    if "record_id" in obj and not _is_nonempty_str(rid):
        errs.append(f"{where}: record_id must be a non-empty string")
    stream = obj.get("stream")
    if "stream" in obj and not _is_nonempty_str(stream):
        errs.append(f"{where}: stream must be a non-empty string")
    elif "stream" in obj and stream not in streams:
        errs.append(f"{where}: unknown stream {stream!r} (not registered in manifest)")
    seq = obj.get("seq")
    if "seq" in obj and (not is_int64(seq) or seq < 0):
        errs.append(f"{where}: seq must be a non-negative integer")
    if rtype not in (DOOR, ANCHOR):
        errs.append(f"{where}: type must be one of {DOOR!r}/{ANCHOR!r}, got {rtype!r}")
        return errs

    if rtype == DOOR:
        for key in ("device", "session_id"):
            if key not in obj:
                errs.append(f"{where}: door record missing {key!r}")
            elif not _is_nonempty_str(obj[key]):
                errs.append(f"{where}: {key} must be a non-empty string")
        if "kind" not in obj:
            errs.append(f"{where}: door record missing 'kind'")
        elif obj["kind"] not in DOOR_KINDS:
            errs.append(f"{where}: kind must be one of {DOOR_KINDS}, got {obj['kind']!r}")
        if "content" not in obj:
            errs.append(f"{where}: door record missing 'content'")
        elif not isinstance(obj["content"], str):
            errs.append(f"{where}: content must be a string")
        present = [k for k in _TIME_FIELDS if k in obj]
        if len(present) != 1:
            errs.append(f"{where}: door record must have exactly one time field local/utc_ms, "
                        f"found {present}")
        elif "utc_ms" in obj and (not is_int64(obj["utc_ms"])
                                  or not (INT64_MIN <= obj["utc_ms"] <= INT64_MAX)):
            errs.append(f"{where}: utc_ms must be a signed int64 integer (booleans rejected)")
        elif "local" in obj and not isinstance(obj["local"], str):
            errs.append(f"{where}: local must be a string label")
    else:  # anchor
        unknown_anchor = set(obj) - set(_COMMON_FIELDS) - {"utc_ms"}
        if unknown_anchor:
            errs.append(f"{where}: anchor record may only carry common fields and utc_ms, "
                        f"got {sorted(unknown_anchor)}")
        if "utc_ms" not in obj:
            errs.append(f"{where}: anchor record requires utc_ms")
        elif not is_int64(obj["utc_ms"]) or not (INT64_MIN <= obj["utc_ms"] <= INT64_MAX):
            errs.append(f"{where}: utc_ms must be a signed int64 integer (booleans rejected)")
    return errs


def _build_record(obj, loc) -> RawRecord:
    common = dict(record_id=obj["record_id"], stream=obj["stream"],
                  seq=obj["seq"], type=obj["type"], sources=[loc])
    if obj["type"] == DOOR:
        common.update(device=obj["device"], session_id=obj["session_id"],
                      kind=obj["kind"], content=obj["content"],
                      local=obj.get("local"), utc_ms=obj.get("utc_ms"))
    else:
        common.update(utc_ms=obj["utc_ms"], local=None)
    return RawRecord(**common)


def _index_record(rec, loc, records, by_id, locations, seen_stream_seq,
                  seen_session_kind, errors) -> bool:
    """Index one validated row.  Returns True if it is a new logical record."""
    where = f"{loc.file}:{loc.line}"
    locations.setdefault(rec.record_id, []).append(loc)

    prior = by_id.get(rec.record_id)
    if prior is not None:
        if prior.identity_key() != rec.identity_key():
            errors.append(
                f"{where}: record_id {rec.record_id!r} re-sent with different content "
                f"(first seen at {prior.sources[0].file}:{prior.sources[0].line})"
            )
            return False
        # exact re-send: fold, keep every physical source location
        prior.sources.append(loc)
        return False

    seq_owner = seen_stream_seq.get((rec.stream, rec.seq))
    if seq_owner is not None and seq_owner != rec.record_id:
        errors.append(
            f"{where}: stream {rec.stream!r} seq {rec.seq} already occupied by "
            f"record_id {seq_owner!r}"
        )
        return False

    if rec.type == DOOR:
        skey = (rec.device, rec.session_id, rec.kind)
        owner = seen_session_kind.get(skey)
        if owner is not None and owner != rec.record_id:
            errors.append(
                f"{where}: (device, session_id, kind)={skey} already claimed by "
                f"record_id {owner!r}"
            )
            return False
        seen_session_kind[skey] = rec.record_id

    seen_stream_seq[(rec.stream, rec.seq)] = rec.record_id
    by_id[rec.record_id] = rec
    return True
