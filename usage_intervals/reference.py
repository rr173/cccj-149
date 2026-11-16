"""Pure-Python reference implementation of the interval semantics.

This module is intentionally independent of PostgreSQL: it re-derives the full
partition from an :class:`~usage_intervals.model.InputPackage` and is used to
cross-check the database-backed builder point-by-point.  It also contains a
discrete small-domain verifier that classifies every millisecond tick
individually and confirms the continuous partition agrees with it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .model import InputPackage

CONFLICT = "conflict"
CONFIRMED = "confirmed"
IDLE = "idle"
UNKNOWN = "unknown"
STATES = (CONFLICT, CONFIRMED, IDLE, UNKNOWN)


def _clip(lo, hi, wlo, whi):
    lo, hi = max(lo, wlo), min(hi, whi)
    return (lo, hi) if lo < hi else None


def _merge_ranges(ranges):
    """Union of [lo, hi) ranges -- never adds durations of overlaps."""
    merged = []
    for lo, hi in sorted(ranges):
        if not merged or lo > merged[-1][1]:
            merged.append([lo, hi])
        elif hi > merged[-1][1]:
            merged[-1][1] = hi
    return [(lo, hi) for lo, hi in merged]


@dataclass
class RefEvidence:
    lease_ids: tuple[str, ...]
    door_complete: tuple[str, ...]
    door_unpaired: tuple[str, ...]
    activity_ids: tuple[str, ...]
    door_covered: bool
    telemetry_covered: bool
    session_conflicts: tuple[str, ...]
    activity_conflicts: tuple[str, ...]


@dataclass
class RefSegment:
    lo: int
    hi: int
    state: str
    reasons: tuple[str, ...]
    evidence: RefEvidence


@dataclass
class RefResult:
    package_id: str
    window_lo: int
    window_hi: int
    device: str
    segments: list[RefSegment] = field(default_factory=list)


@dataclass
class _Session:
    session_id: str
    device: str
    enter_versions: tuple  # list of (ts, content)
    exit_versions: tuple
    conflict: bool

    @property
    def conflict_range(self):
        ts = [t for t, _ in self.enter_versions + self.exit_versions]
        return min(ts), max(ts)


def _build_sessions(pkg: InputPackage, device: str):
    """Pair endpoints across batches; ingestion order cannot matter here.

    Identity of an endpoint record is (session, kind, ts, content).  Re-sends
    of the same identity fold; more than one distinct content for the same
    (session, kind) is a content conflict.
    """
    groups: dict[str, dict] = {}
    for ep in pkg.endpoints:
        if ep.device != device:
            continue
        g = groups.setdefault(ep.session_id, {"enter": {}, "exit": {}})
        g[ep.kind].setdefault(ep.ts, set()).add(ep.content)

    sessions = []
    for sid, g in groups.items():
        conflict = any(len(c) > 1 for c in g["enter"].values()) or any(
            len(c) > 1 for c in g["exit"].values()
        )
        sessions.append(
            _Session(
                session_id=sid,
                device=device,
                enter_versions=tuple(sorted((ts, next(iter(c))) for ts, c in g["enter"].items())),
                exit_versions=tuple(sorted((ts, next(iter(c))) for ts, c in g["exit"].items())),
                conflict=conflict,
            )
        )
    return sessions


def build_reference(pkg: InputPackage, device: str) -> RefResult:
    wlo, whi = pkg.window_lo, pkg.window_hi

    # --- leases with pauses deducted, clipped to the window ----------------
    usable = []  # (lease_id, range)
    for le in pkg.leases:
        if le.device != device:
            continue
        rng = _clip(le.start, le.end, wlo, whi)
        if rng is None:
            continue
        lo, hi = rng
        avail = [(lo, hi)]
        for plo, phi in le.pauses:
            cut = _clip(plo, phi, lo, hi)
            if cut is None:
                continue
            clo, chi = cut
            new = []
            for a, b in avail:
                if chi <= a or clo >= b:
                    new.append((a, b))
                else:
                    if clo > a:
                        new.append((a, clo))
                    if chi < b:
                        new.append((chi, b))
            avail = new
        for r in avail:
            usable.append((le.lease_id, r))

    # --- door sessions ------------------------------------------------------
    sessions = _build_sessions(pkg, device)
    complete, unpaired, session_conflict_ranges = [], [], []
    for s in sessions:
        if s.conflict:
            r = _clip(*s.conflict_range, wlo, whi)
            if r:
                session_conflict_ranges.append((s.session_id, r))
            continue
        enters = sorted(ts for ts, _ in s.enter_versions)
        exits = sorted(ts for ts, _ in s.exit_versions)
        if enters and exits:
            r = _clip(enters[0], exits[0], wlo, whi)
            # full pair only counts as usage when enter < exit (after clip)
            if r is not None:
                complete.append((s.session_id, r))
            # an out-of-window / inverted pair still carries raw evidence ids
        elif enters:
            r = _clip(enters[0], whi, wlo, whi)
            if r is not None:
                unpaired.append((s.session_id, r, "enter_only"))
        elif exits:
            r = _clip(wlo, exits[0], wlo, whi)
            if r is not None:
                unpaired.append((s.session_id, r, "exit_only"))

    # --- activities: union, and same-id range conflicts --------------------
    raw_act = {}
    activity_conflicts = set()
    for a in pkg.activities:
        if a.device != device:
            continue
        prev = raw_act.get(a.activity_id)
        if prev is not None and prev != (a.lo, a.hi):
            activity_conflicts.add(a.activity_id)
        raw_act.setdefault(a.activity_id, (a.lo, a.hi))
    act_ranges = []
    for aid, (lo, hi) in raw_act.items():
        r = _clip(lo, hi, wlo, whi)
        if r:
            act_ranges.append((aid, r))
    act_union = _merge_ranges([r for _, r in act_ranges])

    # --- coverage -----------------------------------------------------------
    cov = {"door": [], "telemetry": []}
    for c in pkg.coverage:
        r = _clip(c.lo, c.hi, wlo, whi)
        if r:
            cov[c.source].append(r)
    cov = {k: _merge_ranges(v) for k, v in cov.items()}

    # --- conflict ranges ----------------------------------------------------
    # overlapping complete door segments
    door_overlap_conflicts = []
    for i, (id1, (a, b)) in enumerate(complete):
        for id2, (c, d) in complete[i + 1 :]:
            lo, hi = max(a, c), min(b, d)
            if lo < hi:
                door_overlap_conflicts.append((lo, hi))
    conflict_ranges = _merge_ranges(
        [r for _, r in session_conflict_ranges] + door_overlap_conflicts
    )

    # --- atomic partition ---------------------------------------------------
    bounds = {wlo, whi}
    for _, (lo, hi) in usable + complete + act_ranges:
        bounds.add(lo)
        bounds.add(hi)
    for _, (lo, hi), _ in unpaired:
        bounds.add(lo)
        bounds.add(hi)
    for k in cov:
        for lo, hi in cov[k]:
            bounds.add(lo)
            bounds.add(hi)
    for lo, hi in conflict_ranges:
        bounds.add(lo)
        bounds.add(hi)
    bounds = sorted(bounds)

    def covered_at(kind, t):
        for lo, hi in cov[kind]:
            if lo <= t < hi:
                return True
        return False

    segments = []
    for lo, hi in zip(bounds, bounds[1:]):
        t = lo  # membership is constant inside an atomic segment
        leases = tuple(sorted(lid for lid, (a, b) in usable if a <= t < b))
        dcomp = tuple(sorted(sid for sid, (a, b) in complete if a <= t < b))
        dunp = tuple(sorted(sid for sid, (a, b), _ in unpaired if a <= t < b))
        aids = tuple(sorted(aid for aid, (a, b) in act_ranges if a <= t < b))
        dcov, tcov = covered_at("door", t), covered_at("telemetry", t)
        sconf = tuple(sorted(sid for sid, (a, b) in session_conflict_ranges if a <= t < b))
        acon = tuple(sorted(aid for aid in activity_conflicts if any(
            a <= t < b for x, (a, b) in act_ranges if x == aid
        )))
        in_conflict_range = any(a <= t < b for a, b in conflict_ranges)

        positive_door = bool(dcomp or dunp)
        positive_any = positive_door or bool(aids)
        ev = RefEvidence(leases, dcomp, dunp, aids, dcov, tcov, sconf, acon)

        reasons = []
        if in_conflict_range or sconf or acon:
            reasons.append("door_or_activity_conflict")
        if len(leases) >= 2:
            reasons.append("multiple_usable_leases")
        if (dcomp or aids) and not leases:
            reasons.append("usage_without_lease")
        if reasons:
            state = CONFLICT
        elif dunp:
            state = UNKNOWN
            reasons.append("unpaired_door_endpoint")
        elif len(leases) == 1 and dcomp and aids:
            state = CONFIRMED
            reasons.append("confirmed_lease_door_and_telemetry")
        elif not positive_any and dcov is True and tcov is True:
            state = IDLE
            reasons.append("no_evidence_both_collections_complete")
        else:
            state = UNKNOWN
            if not positive_any:
                reasons.append("incomplete_or_partial_evidence")
            else:
                reasons.append("incomplete_or_partial_evidence")
        segments.append(RefSegment(lo, hi, state, tuple(reasons), ev))

    # --- merge neighbours identical in state, evidence, leases, coverage ----
    merged = []
    for seg in segments:
        if not merged:
            merged.append(seg)
            continue
        prev = merged[-1]
        same = (
            prev.hi == seg.lo
            and prev.state == seg.state
            and prev.reasons == seg.reasons
            and prev.evidence == seg.evidence
        )
        if same:
            prev.hi = seg.hi
        else:
            merged.append(seg)

    return RefResult(pkg.package_id, wlo, whi, device, merged)


def verify_discrete(pkg: InputPackage, device: str, result: RefResult) -> list[str]:
    """Tick every millisecond of the (small) window and compare.

    Returns a list of human-readable mismatches (empty == perfect match).
    """
    wlo, whi = pkg.window_lo, pkg.window_hi
    if whi - wlo > 20000:
        raise ValueError("discrete verification is only for small windows")
    problems = []

    # partition coverage and non-overlap
    if not result.segments:
        problems.append("partition is empty")
    cursor = wlo
    for seg in result.segments:
        if seg.lo != cursor:
            problems.append(f"gap/overlap at {cursor} -> [{seg.lo},{seg.hi})")
        cursor = seg.hi
    if cursor != whi:
        problems.append(f"partition ends at {cursor}, expected {whi}")

    def seg_at(t):
        for seg in result.segments:
            if seg.lo <= t < seg.hi:
                return seg
        return None

    for t in range(wlo, whi):
        seg = seg_at(t)
        if seg is None:
            problems.append(f"tick {t} not covered")
            continue
        # rebuild the classification for this single tick
        tick_pkg = InputPackage(
            pkg.package_id, t, t + 1, pkg.batches, pkg.leases, pkg.endpoints,
            pkg.activities, pkg.coverage, pkg.input_hash,
        )
        tick_res = build_reference(tick_pkg, device)
        if len(tick_res.segments) != 1:
            problems.append(f"tick {t}: expected one segment, got {len(tick_res.segments)}")
            continue
        ts = tick_res.segments[0]
        if ts.state != seg.state:
            problems.append(f"tick {t}: state {seg.state} != discrete {ts.state}")
        if ts.evidence != seg.evidence:
            problems.append(f"tick {t}: evidence mismatch {seg.evidence} != {ts.evidence}")
        if ts.reasons != seg.reasons:
            problems.append(f"tick {t}: reasons mismatch {seg.reasons} != {ts.reasons}")
    return problems
