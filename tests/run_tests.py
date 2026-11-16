"""End-to-end tests for the offline usage interval processor.

Run directly (no pytest needed):

    python3 -I tests/run_tests.py

Every test:
  * hand-lists the expected full partition,
  * compares the database-backed result against the pure-Python reference,
  * verifies every millisecond tick on the small discrete domain,
  * reads stored intervals *and* paired evidence back from PostgreSQL.
"""

import sys
import os

sys.path.insert(0, os.path.expanduser("~/.local/lib/python3.11/site-packages"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from usage_intervals import (  # noqa: E402
    Processor, StructuralError, load_package, build_reference, verify_discrete,
)

DSN = os.environ.get("UI_DSN", "host=/tmp port=55432 user=postgres dbname=usage_dev")
WINDOW = {"lo": 0, "hi": 100}

PASS, FAIL = 0, 0


def check(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        raise AssertionError(msg)


def base_batch(**over):
    b = {
        "leases": [
            {"lease_id": "L1", "device": "dev1", "start": 0, "end": 50, "pauses": []},
            {"lease_id": "L2", "device": "dev1", "start": 50, "end": 100, "pauses": []},
        ],
        "endpoints": [
            {"device": "dev1", "session_id": "s", "kind": "enter", "ts": 40, "content": "in"},
            {"device": "dev1", "session_id": "s", "kind": "exit", "ts": 70, "content": "out"},
        ],
        "activities": [{"activity_id": "a", "device": "dev1", "lo": 45, "hi": 65}],
        "coverage": [
            {"source": "door", "lo": 0, "hi": 100},
            {"source": "telemetry", "lo": 0, "hi": 100},
        ],
    }
    b.update(over)
    return b


def expected_segments(spec):
    """spec: list of (lo, hi, state, lease_ids, dcomp, dunp, aids, dcov, tcov)."""
    return spec


def assert_partition(proc, pkg, device, expected):
    """Compare DB partition to hand-listed expectation, reference and ticks."""
    got = proc.segments(pkg.package_id, device)
    check(len(got) == len(expected),
          f"{pkg.package_id}: {len(got)} segments, expected {len(expected)}: "
          f"{[(g.lo, g.hi, g.state) for g in got]}")
    for seg, (lo, hi, state, lids, dc, du, aids, dcov, tcov) in zip(got, expected):
        check((seg.lo, seg.hi, seg.state) == (lo, hi, state),
              f"{pkg.package_id}: segment [{lo},{hi}) {state} != "
              f"[{seg.lo},{seg.hi}) {seg.state}")
        check(set(seg.lease_ids) == set(lids), f"{pkg.package_id}: leases {seg.lease_ids}")
        check(set(seg.door_complete) == set(dc), f"{pkg.package_id}: door_complete {seg.door_complete}")
        check(set(seg.door_unpaired) == set(du), f"{pkg.package_id}: door_unpaired {seg.door_unpaired}")
        check(set(seg.activity_ids) == set(aids), f"{pkg.package_id}: activities {seg.activity_ids}")
        check(seg.door_covered == dcov, f"{pkg.package_id}: door_covered {seg.door_covered}")
        check(seg.telemetry_covered == tcov, f"{pkg.package_id}: telemetry_covered {seg.telemetry_covered}")

    # reference implementation must produce the identical classification
    ref = build_reference(pkg, device)
    check([(s.lo, s.hi, s.state) for s in ref.segments]
          == [(g.lo, g.hi, g.state) for g in got],
          f"{pkg.package_id}: reference states disagree")
    for rs, gs in zip(ref.segments, got):
        check(set(rs.evidence.lease_ids) == set(gs.lease_ids), "ref lease evidence")
        check(set(rs.evidence.door_complete) == set(gs.door_complete), "ref door evidence")
        check(set(rs.evidence.door_unpaired) == set(gs.door_unpaired), "ref unpaired evidence")
        check(set(rs.evidence.activity_ids) == set(gs.activity_ids), "ref activity evidence")
        check(rs.evidence.door_covered == gs.door_covered, "ref door coverage")
        check(rs.evidence.telemetry_covered == gs.telemetry_covered, "ref telemetry coverage")
        check(rs.reasons == gs.reasons, f"ref reasons {rs.reasons} != {gs.reasons}")

    # discrete small-domain verification, tick by tick
    problems = verify_discrete(pkg, device, ref)
    check(not problems, f"{pkg.package_id}: discrete mismatch: {problems[:3]}")

    # partition exhausts the window without overlap
    check(got[0].lo == pkg.window_lo and got[-1].hi == pkg.window_hi,
          "partition must cover the full window")
    check(all(x.hi == y.lo for x, y in zip(got, got[1:])), "partition must be contiguous")
    return got


def main():
    global PASS, FAIL
    with Processor(dsn=DSN) as proc:
        proc.install()

        # ------------------------------------------------ pack A: base ----
        pkgA = load_package("walkA", WINDOW, [base_batch()])
        proc.process(pkgA)
        assert_partition(proc, pkgA, "dev1", expected_segments([
            (0, 40, "idle",      ["L1"], [], [], [], True, True),
            (40, 45, "unknown",  ["L1"], ["s"], [], [], True, True),
            (45, 50, "confirmed", ["L1"], ["s"], [], ["a"], True, True),
            (50, 65, "confirmed", ["L2"], ["s"], [], ["a"], True, True),
            (65, 70, "unknown",  ["L2"], ["s"], [], [], True, True),
            (70, 100, "idle",     ["L2"], [], [], [], True, True),
        ]))

        # evidence is really stored and read back, including the paired door
        ev = proc.evidence("walkA", "dev1")
        sess = {s["session_id"]: s for s in ev["sessions"]}
        check(sess["s"]["state"] == "complete" and sess["s"]["range"] == [40, 70],
              f"paired session readback: {sess['s']}")
        check([l["range"] for l in ev["leases"]] == [[0, 50], [50, 100]],
              f"lease avail readback: {ev['leases']}")
        check(any(a["activity_ids"] == ["a"] for a in ev["activities"]),
              "activity union readback")

        # ----------------------------- pack B: L2 pause => conflict --------
        b = base_batch()
        b["leases"][1]["pauses"] = [[55, 60]]
        pkgB = load_package("walkB", WINDOW, [b])
        proc.process(pkgB)
        assert_partition(proc, pkgB, "dev1", expected_segments([
            (0, 40, "idle",      ["L1"], [], [], [], True, True),
            (40, 45, "unknown",  ["L1"], ["s"], [], [], True, True),
            (45, 50, "confirmed", ["L1"], ["s"], [], ["a"], True, True),
            (50, 55, "confirmed", ["L2"], ["s"], [], ["a"], True, True),
            (55, 60, "conflict", [], ["s"], [], ["a"], True, True),   # evidence retained
            (60, 65, "confirmed", ["L2"], ["s"], [], ["a"], True, True),
            (65, 70, "unknown",  ["L2"], ["s"], [], [], True, True),
            (70, 100, "idle",    ["L2"], [], [], [], True, True),
        ]))
        paused = [g for g in proc.segments("walkB", "dev1") if g.lo == 55][0]
        check("usage_without_lease" in paused.reasons, "pause evidence must not be filtered")

        # -------------------- pack C: drop telemetry coverage [70,80) -----
        b = base_batch()
        b["coverage"] = [
            {"source": "door", "lo": 0, "hi": 100},
            {"source": "telemetry", "lo": 0, "hi": 70},
            {"source": "telemetry", "lo": 80, "hi": 100},
        ]
        pkgC = load_package("walkC", WINDOW, [b])
        proc.process(pkgC)
        assert_partition(proc, pkgC, "dev1", expected_segments([
            (0, 40, "idle",      ["L1"], [], [], [], True, True),
            (40, 45, "unknown",  ["L1"], ["s"], [], [], True, True),
            (45, 50, "confirmed", ["L1"], ["s"], [], ["a"], True, True),
            (50, 65, "confirmed", ["L2"], ["s"], [], ["a"], True, True),
            (65, 70, "unknown",  ["L2"], ["s"], [], [], True, True),
            (70, 80, "unknown",  ["L2"], [], [], [], True, False),  # gap: never idle
            (80, 100, "idle",    ["L2"], [], [], [], True, True),
        ]))

        # ------------------- pack D: unpaired enter @20, conflict wins -----
        b = base_batch()
        b["leases"][1]["pauses"] = [[55, 60]]
        b["endpoints"].append(
            {"device": "dev1", "session_id": "u", "kind": "enter", "ts": 20, "content": "x"})
        pkgD = load_package("walkD", WINDOW, [b])
        proc.process(pkgD)
        # unpaired enter's possible range covers the window: nothing inside it
        # can be called confirmed, even where other evidence looks complete.
        assert_partition(proc, pkgD, "dev1", expected_segments([
            (0, 20, "idle",     ["L1"], [], [], [], True, True),
            (20, 40, "unknown", ["L1"], [], ["u"], [], True, True),
            (40, 45, "unknown", ["L1"], ["s"], ["u"], [], True, True),
            (45, 50, "unknown", ["L1"], ["s"], ["u"], ["a"], True, True),
            (50, 55, "unknown", ["L2"], ["s"], ["u"], ["a"], True, True),
            (55, 60, "conflict", [], ["s"], ["u"], ["a"], True, True),
            (60, 65, "unknown", ["L2"], ["s"], ["u"], ["a"], True, True),
            (65, 70, "unknown", ["L2"], ["s"], ["u"], [], True, True),
            (70, 100, "unknown",   ["L2"], [], ["u"], [], True, True),
        ]))

        # ----------------------- cross-file pairing, order invariant -------
        f1 = {"endpoints": [
            {"device": "dev1", "session_id": "s2", "kind": "exit", "ts": 90, "content": "o"}]}
        f2 = {"endpoints": [
            {"device": "dev1", "session_id": "s2", "kind": "enter", "ts": 80, "content": "i"}]}
        common = {
            "leases": [{"lease_id": "L1", "device": "dev1", "start": 0, "end": 100, "pauses": []}],
            "activities": [{"activity_id": "a", "device": "dev1", "lo": 80, "hi": 90}],
            "coverage": [
                {"source": "door", "lo": 0, "hi": 100},
                {"source": "telemetry", "lo": 0, "hi": 100}],
        }
        p1 = load_package("cross1", WINDOW, [f1, f2, common])
        p2 = load_package("cross2", WINDOW, [common, f2, f1])
        check(p1.input_hash == p2.input_hash, "batch order must not change input hash")
        proc.process(p1)
        proc.process(p2)
        g1 = [(s.lo, s.hi, s.state) for s in proc.segments("cross1", "dev1")]
        g2 = [(s.lo, s.hi, s.state) for s in proc.segments("cross2", "dev1")]
        check(g1 == g2, f"cross-file order must not change facts: {g1} vs {g2}")
        check(g1 == [(0, 80, "idle"), (80, 90, "confirmed"), (90, 100, "idle")],
              f"cross-file pairing partition: {g1}")
        ev2 = proc.evidence("cross1", "dev1")
        check(any(s["session_id"] == "s2" and s["state"] == "complete" for s in ev2["sessions"]),
              "exit-before-enter file order still pairs")

        # -------------------------- same-instant boundaries ----------------
        b = {
            "leases": [{"lease_id": "L0", "device": "d", "start": 0, "end": 100, "pauses": []},
                       {"lease_id": "L1", "device": "d", "start": 10, "end": 90, "pauses": []}],
            "endpoints": [
                {"device": "d", "session_id": "s", "kind": "enter", "ts": 10, "content": "i"},
                {"device": "d", "session_id": "s", "kind": "exit", "ts": 90, "content": "o"}],
            "activities": [{"activity_id": "a", "device": "d", "lo": 10, "hi": 90}],
            "coverage": [
                {"source": "door", "lo": 0, "hi": 100},
                {"source": "telemetry", "lo": 0, "hi": 100}],
        }
        pkg = load_package("boundary", WINDOW, [b])
        proc.process(pkg)
        assert_partition(proc, pkg, "d", expected_segments([
            (0, 10, "idle", ["L0"], [], [], [], True, True),
            (10, 90, "conflict", ["L0", "L1"], ["s"], [], ["a"], True, True),
            (90, 100, "idle", ["L0"], [], [], [], True, True),
        ]))

        # ------------------------ two usable leases -> conflict ------------
        b = {
            "leases": [
                {"lease_id": "L1", "device": "d", "start": 0, "end": 60, "pauses": []},
                {"lease_id": "L2", "device": "d", "start": 40, "end": 100, "pauses": []}],
            "endpoints": [],
            "activities": [],
            "coverage": [
                {"source": "door", "lo": 0, "hi": 100},
                {"source": "telemetry", "lo": 0, "hi": 100}],
        }
        pkg = load_package("duallease", WINDOW, [b])
        proc.process(pkg)
        got = proc.segments("duallease", "d")
        check([(g.lo, g.hi, g.state) for g in got]
              == [(0, 40, "idle"), (40, 60, "conflict"), (60, 100, "idle")],
              f"dual lease: {[(g.lo, g.hi, g.state) for g in got]}")
        check("multiple_usable_leases" in got[1].reasons, "conflict reason recorded")

        # --------------------- overlapping complete doors -> conflict -----
        b = {
            "leases": [{"lease_id": "L1", "device": "d", "start": 0, "end": 100, "pauses": []}],
            "endpoints": [
                {"device": "d", "session_id": "s1", "kind": "enter", "ts": 10, "content": "i1"},
                {"device": "d", "session_id": "s1", "kind": "exit", "ts": 40, "content": "o1"},
                {"device": "d", "session_id": "s2", "kind": "enter", "ts": 30, "content": "i2"},
                {"device": "d", "session_id": "s2", "kind": "exit", "ts": 50, "content": "o2"}],
            "activities": [{"activity_id": "a", "device": "d", "lo": 10, "hi": 50}],
            "coverage": [
                {"source": "door", "lo": 0, "hi": 100},
                {"source": "telemetry", "lo": 0, "hi": 100}],
        }
        pkg = load_package("overlapdoor", WINDOW, [b])
        proc.process(pkg)
        got = proc.segments("overlapdoor", "d")
        check(any(g.state == "conflict" and g.lo == 30 and g.hi == 40 for g in got),
              f"overlapping doors must conflict: {[(g.lo, g.hi, g.state) for g in got]}")

        # --------------------- duplicate activities fold into union --------
        b = {
            "leases": [{"lease_id": "L1", "device": "d", "start": 0, "end": 100, "pauses": []}],
            "endpoints": [
                {"device": "d", "session_id": "s", "kind": "enter", "ts": 10, "content": "i"},
                {"device": "d", "session_id": "s", "kind": "exit", "ts": 60, "content": "o"}],
            "activities": [
                {"activity_id": "a1", "device": "d", "lo": 10, "hi": 35},
                {"activity_id": "a1", "device": "d", "lo": 10, "hi": 35},  # exact re-send folds
                {"activity_id": "a2", "device": "d", "lo": 30, "hi": 60}],
            "coverage": [
                {"source": "door", "lo": 0, "hi": 100},
                {"source": "telemetry", "lo": 0, "hi": 100}],
        }
        pkg = load_package("dupact", WINDOW, [b])
        proc.process(pkg)
        got = proc.segments("dupact", "d")
        check([(g.lo, g.hi, g.state) for g in got]
              == [(0, 10, "idle"), (10, 60, "confirmed"), (60, 100, "idle")],
              f"union activity partition: {[(g.lo, g.hi, g.state) for g in got]}")
        ev = proc.evidence("dupact", "d")
        check(len(ev["activities"]) == 1 and ev["activities"][0]["range"] == [10, 60],
              f"overlapping activities unioned, not summed: {ev['activities']}")

        # same activity id with different range is a business conflict
        b2 = dict(b)
        b2["activities"] = [
            {"activity_id": "a1", "device": "d", "lo": 10, "hi": 30},
            {"activity_id": "a1", "device": "d", "lo": 20, "hi": 60}]
        pkg = load_package("actconf", WINDOW, [b2])
        proc.process(pkg)
        got = proc.segments("actconf", "d")
        check(any(g.state == "conflict" for g in got),
              "same-id different-range activity must be conflict")

        # --------------- endpoint content change is conflict, re-send folds -
        b = base_batch()
        b["endpoints"].append(
            {"device": "dev1", "session_id": "s", "kind": "enter", "ts": 40, "content": "TAMPERED"})
        pkg = load_package("contentconf", WINDOW, [b])
        proc.process(pkg)
        got = proc.segments("contentconf", "dev1")
        check(any(g.state == "conflict" and set(g.session_conflicts) == {"s"} for g in got),
              f"content conflict must mark session: {[(g.lo, g.hi, g.state) for g in got]}")
        # identical re-send in another batch must not conflict
        b = base_batch()
        again = {"endpoints": [
            {"device": "dev1", "session_id": "s", "kind": "enter", "ts": 40, "content": "in"},
            {"device": "dev1", "session_id": "s", "kind": "exit", "ts": 70, "content": "out"}]}
        pkg = load_package("resend", WINDOW, [b, again])
        proc.process(pkg)
        check(all(g.state != "conflict" for g in proc.segments("resend", "dev1")),
              "identical re-send must fold, not conflict")

        # ------------------ endpoints outside the window get clipped -------
        b = {
            "leases": [{"lease_id": "L1", "device": "d", "start": 0, "end": 100, "pauses": []}],
            "endpoints": [
                {"device": "d", "session_id": "s", "kind": "enter", "ts": -10, "content": "i"},
                {"device": "d", "session_id": "s", "kind": "exit", "ts": 30, "content": "o"}],
            "activities": [{"activity_id": "a", "device": "d", "lo": 0, "hi": 30}],
            "coverage": [
                {"source": "door", "lo": 0, "hi": 100},
                {"source": "telemetry", "lo": 0, "hi": 100}],
        }
        pkg = load_package("clip", WINDOW, [b])
        proc.process(pkg)
        got = proc.segments("clip", "d")
        check(got[0].lo == 0 and got[0].state == "confirmed",
              f"enter outside window clips to window start: {[(g.lo, g.hi, g.state) for g in got]}")
        check(tuple(got[0].door_complete) == ("s",),
              "paired session still evidence after clipping")

        # unpaired exit: [window start, exit)
        b = {
            "leases": [{"lease_id": "L1", "device": "d", "start": 0, "end": 100, "pauses": []}],
            "endpoints": [
                {"device": "d", "session_id": "u", "kind": "exit", "ts": 25, "content": "o"}],
            "activities": [],
            "coverage": [
                {"source": "door", "lo": 0, "hi": 100},
                {"source": "telemetry", "lo": 0, "hi": 100}],
        }
        pkg = load_package("unpairedexit", WINDOW, [b])
        proc.process(pkg)
        got = proc.segments("unpairedexit", "d")
        check([(g.lo, g.hi, g.state) for g in got]
              == [(0, 25, "unknown"), (25, 100, "idle")],
              f"unpaired exit range: {[(g.lo, g.hi, g.state) for g in got]}")

        # ----------------------------- sub-window reads --------------------
        sub = proc.segments("walkA", "dev1", 47, 72)
        check(sub[0].lo == 47 and sub[-1].hi == 72, "sub-window clipping endpoints")
        check([s.state for s in sub] == ["confirmed", "confirmed", "unknown", "idle"],
              f"sub-window states: {[s.state for s in sub]}")

        # ------------------------- version binding / old readable ----------
        check(proc.status("walkA")["status"] == "built", "walkA stays published")
        try:
            proc.process(load_package("walkA", WINDOW,
                                      [base_batch(activities=[
                                          {"activity_id": "a", "device": "dev1",
                                           "lo": 45, "hi": 66}])]))
            check(False, "same package id with different input must be rejected")
        except Exception as exc:
            check("bound to different input" in str(exc), f"hash mismatch error: {exc}")
        # old version still readable
        check(proc.segments("walkA", "dev1")[2].hi == 50, "old package version remains readable")

        # --------------------------- structural failures -------------------
        bad_cases = [
            ("bad window", {"lo": 50, "hi": 50}, [base_batch()]),
            ("non-int ts", WINDOW, [base_batch(endpoints=[
                {"device": "d", "session_id": "s", "kind": "enter",
                 "ts": 1.5, "content": "x"}])]),
            ("bad kind", WINDOW, [base_batch(endpoints=[
                {"device": "d", "session_id": "s", "kind": "arrive",
                 "ts": 1, "content": "x"}])]),
            ("reversed activity", WINDOW, [base_batch(activities=[
                {"activity_id": "a", "device": "dev1", "lo": 80, "hi": 40}])]),
            ("bad coverage source", WINDOW, [base_batch(coverage=[
                {"source": "wifi", "lo": 0, "hi": 100}])]),
            ("pause outside lease", WINDOW, [dict(base_batch(), leases=[
                {"lease_id": "L1", "device": "dev1", "start": 0, "end": 50,
                 "pauses": [[40, 60]]},
                {"lease_id": "L2", "device": "dev1", "start": 50, "end": 100,
                 "pauses": []}])]),
            ("unknown field", WINDOW, [dict(base_batch(), leases=[
                {"lease_id": "L9", "device": "d", "start": 0, "end": 10,
                 "pauses": [], "price": 1}])]),
            ("unknown batch key", WINDOW, [{"events": []}]),
        ]
        for name, win, batches in bad_cases:
            try:
                load_package("structfail_" + name.replace(" ", "_"), win, batches)
                check(False, f"{name}: must raise StructuralError")
            except StructuralError:
                PASS += 1

        # a structurally bad package fails as a whole and stores nothing
        bad_pkg_id = "tailbroken"
        try:
            load_package(bad_pkg_id, WINDOW, [base_batch(activities=[
                {"activity_id": "a", "device": "dev1", "lo": 90, "hi": 80}])])
            check(False, "tail structural error must fail the package")
        except StructuralError:
            PASS += 1

        # ----------------- streaming device ids never materializes set -----
        ids = list(proc.store.stream_device_ids("walkA"))
        check(ids == ["dev1"], f"server-side cursor device stream: {ids}")

        # ------------------- interpretation API shape ----------------------
        interp = proc.interpret("walkA", "dev1")
        check(interp["segments"][0]["door"]["collection_complete"] is True,
              "interpret exposes coverage per segment")
        check(all(set(s) == {"segment", "state", "reasons", "candidate_leases",
                             "door", "telemetry"} for s in interp["segments"]),
              "interpret segment schema stable")

    print(f"\n{PASS} checks passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
