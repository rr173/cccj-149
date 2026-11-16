"""Input package model and structural validation.

A *package* is one fixed, immutable bundle of input batches together with the
finite UTC millisecond query window it belongs to.  Structural problems
(bad endpoints, malformed ranges, unknown fields, ...) raise
:class:`StructuralError`; such a package must never be published.  Business
uncertainty (unknown / conflict) is not structural and is handled later by the
builder.

All time values are integer UTC milliseconds.  All intervals are half-open
[lo, hi) with lo < hi and must fit into the PostgreSQL bigint range.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field

INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1

DOOR_KINDS = ("enter", "exit")
COVERAGE_KINDS = ("door", "telemetry")

# top-level batch key -> accepted record-level keys
_RECORD_FIELDS = {
    "leases": ("lease_id", "device", "start", "end", "pauses"),
    "endpoints": ("device", "session_id", "kind", "ts", "content"),
    "activities": ("activity_id", "device", "lo", "hi"),
    "coverage": ("source", "lo", "hi"),
}
_BATCH_KEYS = set(_RECORD_FIELDS)


class StructuralError(ValueError):
    """The package itself is malformed.  The whole package fails, no facts."""


def _is_int(v) -> bool:
    # bool is a subclass of int -- timestamps of True/False make no sense.
    return isinstance(v, int) and not isinstance(v, bool)


def _check_range(lo, hi, what: str) -> None:
    if not (_is_int(lo) and _is_int(hi)):
        raise StructuralError(f"{what}: bounds must be integers, got {lo!r}, {hi!r}")
    if not (INT64_MIN <= lo <= INT64_MAX and INT64_MIN <= hi <= INT64_MAX):
        raise StructuralError(f"{what}: bounds outside int64 range")
    if lo >= hi:
        raise StructuralError(f"{what}: empty or reversed range [{lo}, {hi})")


def _check_required(rec: dict, keys, what: str) -> None:
    extra = set(rec) - set(keys)
    if extra:
        raise StructuralError(f"{what}: unknown field(s) {sorted(extra)} in {rec!r}")
    for k in keys:
        if k not in rec:
            raise StructuralError(f"{what}: missing field {k!r} in {rec!r}")


@dataclass(frozen=True)
class Lease:
    lease_id: str
    device: str
    start: int
    end: int
    pauses: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class Endpoint:
    device: str
    session_id: str
    kind: str
    ts: int
    content: str


@dataclass(frozen=True)
class Activity:
    activity_id: str
    device: str
    lo: int
    hi: int


@dataclass(frozen=True)
class Coverage:
    source: str
    lo: int
    hi: int


@dataclass
class InputPackage:
    """One validated, immutable input bundle fixed to one window."""

    package_id: str
    window_lo: int
    window_hi: int
    batches: tuple[dict, ...]
    leases: tuple[Lease, ...] = field(default_factory=tuple)
    endpoints: tuple[Endpoint, ...] = field(default_factory=tuple)
    activities: tuple[Activity, ...] = field(default_factory=tuple)
    coverage: tuple[Coverage, ...] = field(default_factory=tuple)
    input_hash: str = ""

    def devices(self) -> list[str]:
        ids: set[str] = set()
        for coll in (self.leases, self.endpoints, self.activities):
            for rec in coll:
                ids.add(rec.device)
        return sorted(ids)


def _validate_window(win: dict) -> tuple[int, int]:
    if not isinstance(win, dict) or set(win) != {"lo", "hi"}:
        raise StructuralError("window must be an object with exactly lo and hi")
    lo, hi = win["lo"], win["hi"]
    _check_range(lo, hi, "window")
    return lo, hi


def _validate_leases(batches):
    out, seen = [], set()
    for b in batches:
        for rec in b.get("leases", []):
            what = f"lease {rec.get('lease_id', '?')!r}"
            if not isinstance(rec, dict):
                raise StructuralError("lease record must be an object")
            _check_required(rec, _RECORD_FIELDS["leases"], "lease")
            lid, dev = rec["lease_id"], rec["device"]
            if not (isinstance(lid, str) and lid and isinstance(dev, str) and dev):
                raise StructuralError(f"{what}: lease_id/device must be non-empty strings")
            start, end = rec["start"], rec["end"]
            _check_range(start, end, what)
            pauses = rec["pauses"]
            if not isinstance(pauses, list):
                raise StructuralError(f"{what}: pauses must be a list")
            norm_pauses = []
            for p in pauses:
                if not isinstance(p, (list, tuple)) or len(p) != 2:
                    raise StructuralError(f"{what}: pause must be [lo, hi]")
                plo, phi = p
                _check_range(plo, phi, f"{what} pause")
                if not (start <= plo and phi <= end):
                    raise StructuralError(
                        f"{what}: pause [{plo}, {phi}) not inside lease [{start}, {end})"
                    )
                norm_pauses.append((int(plo), int(phi)))
            key = (lid, dev)
            if key in seen:
                raise StructuralError(f"{what}: duplicate lease for device {dev!r}")
            seen.add(key)
            out.append(Lease(lid, dev, int(start), int(end), tuple(norm_pauses)))
    return tuple(out)


def _validate_endpoints(batches):
    out, seen = [], set()
    for b in batches:
        for rec in b.get("endpoints", []):
            what = f"endpoint {rec.get('session_id', '?')!r}"
            if not isinstance(rec, dict):
                raise StructuralError("endpoint record must be an object")
            _check_required(rec, _RECORD_FIELDS["endpoints"], "endpoint")
            dev, sid, kind, ts, content = (
                rec["device"],
                rec["session_id"],
                rec["kind"],
                rec["ts"],
                rec["content"],
            )
            if not (isinstance(dev, str) and dev and isinstance(sid, str) and sid):
                raise StructuralError(f"{what}: device/session_id must be non-empty strings")
            if kind not in DOOR_KINDS:
                raise StructuralError(f"{what}: kind must be one of {DOOR_KINDS}")
            if not _is_int(ts) or not (INT64_MIN <= ts <= INT64_MAX):
                raise StructuralError(f"{what}: ts must be an int64 millisecond timestamp")
            if not isinstance(content, str):
                raise StructuralError(f"{what}: content must be a string")
            key = (dev, sid, kind, ts, content)
            if key in seen:
                # identical re-send, possibly in another batch: fold.
                continue
            seen.add(key)
            out.append(Endpoint(dev, sid, kind, int(ts), content))
    return tuple(out)


def _validate_activities(batches):
    out, seen = [], set()
    for b in batches:
        for rec in b.get("activities", []):
            what = f"activity {rec.get('activity_id', '?')!r}"
            if not isinstance(rec, dict):
                raise StructuralError("activity record must be an object")
            _check_required(rec, _RECORD_FIELDS["activities"], "activity")
            aid, dev, lo, hi = rec["activity_id"], rec["device"], rec["lo"], rec["hi"]
            if not (isinstance(aid, str) and aid and isinstance(dev, str) and dev):
                raise StructuralError(f"{what}: activity_id/device must be non-empty strings")
            _check_range(lo, hi, what)
            key = (aid, dev, lo, hi)
            if key in seen:
                continue
            seen.add(key)
            out.append(Activity(aid, dev, int(lo), int(hi)))
    return tuple(out)


def _validate_coverage(batches):
    out, seen = [], set()
    for b in batches:
        for rec in b.get("coverage", []):
            what = "coverage"
            if not isinstance(rec, dict):
                raise StructuralError("coverage record must be an object")
            _check_required(rec, _RECORD_FIELDS["coverage"], what)
            source, lo, hi = rec["source"], rec["lo"], rec["hi"]
            if source not in COVERAGE_KINDS:
                raise StructuralError(f"coverage: source must be one of {COVERAGE_KINDS}")
            _check_range(lo, hi, what)
            key = (source, lo, hi)
            if key in seen:
                continue
            seen.add(key)
            out.append(Coverage(source, int(lo), int(hi)))
    return tuple(out)


def canonical_input_hash(window_lo: int, window_hi: int, batches) -> str:
    """Order-independent hash of the raw accepted input.

    Re-sends that are exact duplicates and appear in different batches hash the
    same; batches are a set for hashing purposes so ingestion order can never
    change the published facts.
    """
    canon = {"window": [window_lo, window_hi], "batches": sorted(
        (json.dumps(b, sort_keys=True, separators=(",", ":")) for b in batches)
    )}
    blob = json.dumps(canon, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def load_package(package_id: str, window: dict, batches) -> InputPackage:
    """Validate one fixed bundle and return its immutable model.

    ``batches`` is a sequence of dicts, each may contain ``leases`` /
    ``endpoints`` / ``activities`` / ``coverage`` lists.  Endpoints of one
    session are allowed to arrive in different batches; pairing happens later.
    Raises :class:`StructuralError` for any structural problem.
    """
    if not isinstance(package_id, str) or not package_id:
        raise StructuralError("package_id must be a non-empty string")
    if not isinstance(batches, (list, tuple)) or not batches:
        raise StructuralError("batches must be a non-empty list")
    norm_batches = []
    for i, b in enumerate(batches):
        if not isinstance(b, dict):
            raise StructuralError(f"batch {i}: must be an object")
        extra = set(b) - _BATCH_KEYS
        if extra:
            raise StructuralError(f"batch {i}: unknown key(s) {sorted(extra)}")
        norm_batches.append(dict(b))

    wlo, whi = _validate_window(window)
    leases = _validate_leases(norm_batches)
    endpoints = _validate_endpoints(norm_batches)
    activities = _validate_activities(norm_batches)
    coverage = _validate_coverage(norm_batches)

    return InputPackage(
        package_id=package_id,
        window_lo=wlo,
        window_hi=whi,
        batches=tuple(norm_batches),
        leases=leases,
        endpoints=endpoints,
        activities=activities,
        coverage=coverage,
        input_hash=canonical_input_hash(wlo, whi, norm_batches),
    )


def load_package_files(package_id: str, window: dict, paths) -> InputPackage:
    """Read a bundle from JSON files on disk (one batch per file)."""
    batches = []
    for p in paths:
        # Files are untrusted: open by absolute path, never import from them.
        with open(os.fspath(p), "r", encoding="utf-8") as fh:
            try:
                batches.append(json.load(fh))
            except json.JSONDecodeError as exc:
                raise StructuralError(f"batch file {p!r} is not valid JSON: {exc}") from exc
    return load_package(package_id, window, batches)
