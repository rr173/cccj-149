"""Tests for the offline local-clock cleaning feature.

Run directly (no pytest needed)::

    python3 -I tests/run_clock_tests.py

Layers:
  1. hand-computed UTC expectations for every documented fixture;
  2. an INDEPENDENT brute-force checker (tests/clock_clean_bruteforce.py) that
     never imports the formal solver, comparing feasible sets, decisions,
     satisfiability and joint explanation instances;
  3. randomized small manifests cross-validated the same way;
  4. a 120 independent two-candidate-session scale case proving the formal
     implementation does not enumerate 2^N interpretations;
  5. load_package acceptance of a cleaned batch and a real PostgreSQL build /
     read-back of session UTC endpoints.
"""

import json
import os
import random
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.expanduser("~/.local/lib/python3.11/site-packages"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from usage_intervals import Processor, load_package  # noqa: E402
from usage_intervals.clock_clean import (  # noqa: E402
    ClockImportError,
    UnsatisfiableClockError,
    build_report,
    clean_manifest,
    explain_record,
    run_import,
)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from clock_clean_bruteforce import BruteForce  # noqa: E402

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
DSN = os.environ.get("UI_DSN", "host=/tmp port=55432 user=postgres dbname=usage_dev")
PASS, FAIL = 0, 0


def check(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        raise AssertionError(msg)


def ms(y, mo, d, h, mi, s=0):
    from datetime import datetime, timezone
    return int(datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc).timestamp() * 1000)


# ------------------------------------------------------- 1. fixture checks --

def expect(case, rid, feasible=None, determined=None, decision=None):
    res = clean_manifest(os.path.join(FIX, case, "manifest.json"))
    rep = build_report(res)
    rec = rep["records"][rid]
    if feasible is not None:
        check(rec["feasible_candidates"] == feasible,
              f"{case}/{rid}: feasible {rec['feasible_candidates']} != {feasible}")
    if determined is not None:
        check(rec["determined"] is determined, f"{case}/{rid}: determined flag")
    if decision is not None:
        check(rec["decision"] == decision,
              f"{case}/{rid}: decision {rec['decision']} != {decision}")
    return res


def fixture_expectations():
    # ---- headline case ----------------------------------------------------
    enter_s1, exit_s1 = ms(2026, 10, 25, 0, 50), ms(2026, 10, 25, 1, 10)
    expect("case_main", "r-enter-s1", [enter_s1], True, "exported")
    expect("case_main", "r-exit-s1", [exit_s1], True, "exported")
    expect("case_main", "r-enter-s2",
           [ms(2026, 10, 25, 0, 40), ms(2026, 10, 25, 1, 40)],
           False, "quarantined_ambiguous")
    expect("case_main", "r-exit-s2", [ms(2026, 10, 25, 1, 55)],
           True, "held_back_partner_ambiguous")
    expect("case_main", "r-enter-s4",
           [ms(2026, 10, 25, 0, 45), ms(2026, 10, 25, 1, 45)],
           False, "quarantined_ambiguous")
    expect("case_main", "r-exit-s4", [ms(2026, 10, 25, 2, 0)],
           True, "held_back_partner_ambiguous")
    expect("case_main", "r-enter-s3", [ms(2026, 10, 25, 3, 0)], True, "exported")
    expect("case_main", "r-exit-s3", [ms(2026, 10, 25, 3, 30)], True, "exported")
    expect("case_main", "r-anchor-a", [ms(2026, 10, 25, 1, 30)], True, "anchor")
    res = clean_manifest(os.path.join(FIX, "case_main", "manifest.json"))
    batch_sessions = {(e["device"], e["session_id"]) for e in res.endpoints}
    check(batch_sessions == {("devA", "s1"), ("devC", "s3")},
          f"only fully determined sessions exported: {batch_sessions}")

    # ---- anchor propagation ----------------------------------------------
    expect("case_anchor", "a-2", [ms(2026, 10, 25, 1, 40)],
           True, "held_back_partner_ambiguous")
    expect("case_anchor", "a-1",
           [ms(2026, 10, 25, 0, 20), ms(2026, 10, 25, 1, 20)],
           False, "quarantined_ambiguous")

    # ---- equality allowed by stream order --------------------------------
    expect("case_equal_instant", "e-1", [ms(2026, 10, 25, 1, 30)],
           True, "exported")

    # ---- Lord Howe 30-minute fall-back -----------------------------------
    expect("case_lord_howe", "lh-exit",
           [ms(2026, 4, 4, 15, 20)], True, "held_back_partner_ambiguous")
    expect("case_lord_howe", "lh-enter",
           [ms(2026, 4, 4, 14, 40), ms(2026, 4, 4, 15, 10)],
           False, "quarantined_ambiguous")
    # spacing of the two candidates is exactly 30 minutes
    check(ms(2026, 4, 4, 15, 10) - ms(2026, 4, 4, 14, 40) == 30 * 60 * 1000,
          "Lord Howe candidate spacing must be 30 minutes")

    # ---- resend: folded record keeps both source locations ---------------
    res = expect("case_resend", "rs-enter", [ms(2026, 10, 25, 3, 0)],
                 True, "exported")
    locs = res.loaded.locations_by_id["rs-enter"]
    check({(l.file, l.line) for l in locs}
          == {("f1.jsonl", 1), ("f2.jsonl", 2)},
          f"re-send provenance must list both locations: {locs}")


def hard_failure_cases():
    global PASS
    for name in sorted(os.listdir(os.path.join(FIX, "case_bad"))):
        try:
            clean_manifest(os.path.join(FIX, "case_bad", name, "manifest.json"))
            check(False, f"{name}: must fail structurally")
        except ClockImportError:
            PASS += 1
    from usage_intervals.clock_clean.solver import LT
    for name in ("case_contradiction", "case_spring_gap", "case_strict_equal"):
        try:
            clean_manifest(os.path.join(FIX, name, "manifest.json"))
            check(False, f"{name}: must fail")
        except ClockImportError:
            PASS += 1
        except UnsatisfiableClockError as exc:
            PASS += 1
            if name == "case_strict_equal":
                check({"t-enter", "t-exit"}.issubset(set(exc.records)),
                      "strict-equal names the enter/exit records")
                check(any(r.kind == LT for r in exc.relations),
                      "strict-equal cites the strictly-before session relation")


# ------------------------------------------------- 2. brute-force agreement --

def cross_check_case(case):
    path = os.path.join(FIX, case, "manifest.json")
    bf = BruteForce(path)
    check(not bf.gap_labels, f"{case}: brute force found unexpected spring gap")
    bf_feasible, bf_decisions, bf_exported, n_solutions = bf.decisions()
    res = clean_manifest(path)
    # feasible sets
    for rid in bf.order:
        formal = set(res.solved.variables[rid].feasible)
        check(formal == bf_feasible[rid],
              f"{case}/{rid}: formal feasible {sorted(formal)} != "
              f"brute {sorted(bf_feasible[rid])} over {n_solutions} solutions")
    # decisions
    for rid in bf.order:
        check(res.decisions[rid] == bf_decisions[rid],
              f"{case}/{rid}: decision {res.decisions[rid]} != {bf_decisions[rid]}")
    # exported endpoints identical
    check(list(res.endpoints) == bf_exported,
          f"{case}: exported endpoints differ from brute force")
    # explain: every feasible candidate gets a joint instance that really
    # satisfies ALL relations; infeasible candidates return None
    for rid in bf.order:
        explanation = explain_record(res, rid)
        bf_component = bf.component_of(rid)
        check(set(explanation["connected_component"]) == bf_component,
              f"{case}/{rid}: connected component mismatch")
        for item in explanation["candidates_explained"]:
            inst = bf.instance_for(rid, item["utc_ms"])
            if item["feasible"]:
                check(inst is not None, f"{case}/{rid}: feasible {item['utc_ms']} "
                                         "has no brute-force solution")
                check(isinstance(item["joint_instance"], dict)
                      and item["joint_instance"], "missing joint instance")
                check(all(isinstance(v, int) for v in item["joint_instance"].values()),
                      "joint instance values must be integer UTC ms")
                # the formal joint instance covers exactly the component and
                # must satisfy every relation inside it simultaneously
                check(set(item["joint_instance"]) == bf_component,
                      f"{case}/{rid}: joint instance scope differs from component")
                check(bf._valid(item["joint_instance"]),
                      f"{case}/{rid}: formal joint instance violates a relation")
                check(item["joint_instance"][rid] == item["utc_ms"],
                      "joint instance must take the explained candidate")
                # the instance's component choices must be extendable to a
                # global brute-force solution with the same component values
                # (the brute instance need not match outside the component)
                extends = any(
                    all(sol[k] == v for k, v in item["joint_instance"].items())
                    for sol in bf.all_solutions()
                )
                check(extends, f"{case}/{rid}: component choices not jointly extendable")
            else:
                check(inst is None, f"{case}/{rid}: infeasible candidate has a solution")
                check(item["joint_instance"] is None, "infeasible candidate got an instance")


def cross_check_all_fixtures():
    for case in ("case_main", "case_anchor", "case_equal_instant",
                 "case_lord_howe", "case_resend"):
        cross_check_case(case)


# ----------------------------------------------------------- 3. random fuzz --

def _write_tmp_manifest(tmp, streams, files_rows):
    for fname, rows in files_rows.items():
        with open(os.path.join(tmp, fname), "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")
    with open(os.path.join(tmp, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump({"streams": streams,
                   "files": sorted(files_rows)}, fh)
    return os.path.join(tmp, "manifest.json")


def fuzz_cross_check(seed, n_records=8):
    """Random small two-stream manifest; compare formal vs brute force."""
    rng = random.Random(seed)
    rows = {f"f{i}.jsonl": [] for i in range(2)}
    streams = {"S1": "Europe/Berlin", "S2": "Europe/Berlin"}
    labels = ["2026-10-25T02:%02d:00.000" % m for m in (5, 15, 25, 35, 45, 55)]
    n_sessions = max(1, n_records // 3)
    recno = 0
    for s in range(n_sessions):
        stream = rng.choice(["S1", "S2"])
        f = rng.choice(["f0.jsonl", "f1.jsonl"])
        rows[f].append({
            "record_id": f"r{recno}", "stream": stream, "seq": recno,
            "type": "door", "device": "dev", "session_id": f"sess{s}",
            "kind": "enter", "content": "c",
            "local": rng.choice(labels),
        })
        recno += 1
        if rng.random() < 0.8:
            stream2 = rng.choice(["S1", "S2"])
            f = rng.choice(["f0.jsonl", "f1.jsonl"])
            if rng.random() < 0.4:
                timefield = {"utc_ms": rng.choice(
                    [ms(2026, 10, 24, 23, m) for m in range(60)]
                    + [ms(2026, 10, 25, h, m) for h in (0, 1, 2) for m in range(60)])}
            else:
                timefield = {"local": rng.choice(labels)}
            rows[f].append({
                "record_id": f"r{recno}", "stream": stream2, "seq": recno,
                "type": "door", "device": "dev", "session_id": f"sess{s}",
                "kind": "exit", "content": "c", **timefield,
            })
            recno += 1
    # seq must be unique per stream; renumber per stream preserving insertion
    seen = {}
    for rows_list in rows.values():
        for row in rows_list:
            k = row["stream"]
            row["seq"] = seen.get(k, 0) * 10
            seen[k] = seen.get(k, 0) + 1
    tmp = tempfile.mkdtemp(prefix="ccfuzz-")
    try:
        path = _write_tmp_manifest(tmp, streams, rows)
        bf = BruteForce(path)
        if bf.gap_labels:
            return  # fuzz used only fall-back labels; defensive
        bf_feasible, bf_decisions, bf_exported, _ = bf.decisions()
        try:
            res = clean_manifest(path)
        except UnsatisfiableClockError:
            # formal says unsat: brute force must have zero solutions too
            _, count = bf.feasible_sets()
            check(count == 0, f"fuzz seed {seed}: formal unsat but brute force "
                              f"found {count} solutions")
            return
        for rid in bf.order:
            check(set(res.solved.variables[rid].feasible) == bf_feasible[rid],
                  f"fuzz {seed}/{rid}: feasible set mismatch")
            check(res.decisions[rid] == bf_decisions[rid],
                  f"fuzz {seed}/{rid}: decision mismatch")
        check(list(res.endpoints) == bf_exported, f"fuzz {seed}: endpoints mismatch")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------- 4. scale: no enumeration --

def scale_independent_sessions(n=120):
    """120 independent two-candidate sessions => 2^120 interpretations exist.

    Each session lives on its own stream so the only relation binding its two
    records is the session enter<exit edge; the components are mutually
    independent.  The formal 2-SAT path must return in well under a second with
    every record's full feasible set while never materializing combinations.
    """
    tmp = tempfile.mkdtemp(prefix="ccscale-")
    try:
        rows = []
        streams = {}
        for i in range(n):
            sname = f"S{i}"
            streams[sname] = "Europe/Berlin"
            rows.append({"record_id": f"e{i}", "stream": sname, "seq": 1,
                         "type": "door", "device": f"d{i}", "session_id": f"s{i}",
                         "kind": "enter", "content": "c",
                         "local": "2026-10-25T02:50:00.000"})
            rows.append({"record_id": f"x{i}", "stream": sname, "seq": 2,
                         "type": "door", "device": f"d{i}", "session_id": f"s{i}",
                         "kind": "exit", "content": "c",
                         "utc_ms": ms(2026, 10, 25, 5, 0)})
        with open(os.path.join(tmp, "d.jsonl"), "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")
        with open(os.path.join(tmp, "manifest.json"), "w", encoding="utf-8") as fh:
            json.dump({"streams": streams, "files": ["d.jsonl"]}, fh)
        t0 = time.perf_counter()
        res = clean_manifest(os.path.join(tmp, "manifest.json"))
        elapsed = time.perf_counter() - t0
        check(elapsed < 5.0, f"scale case took {elapsed:.2f}s; combinations were enumerated?")
        # every enter keeps both candidates (both < the 05:00Z exit), every
        # exit is pinned; hence every complete session is quarantined
        for i in range(n):
            check(len(res.solved.variables[f"e{i}"].feasible) == 2,
                  f"scale e{i} should keep two candidates")
            check(res.decisions[f"x{i}"] == "held_back_partner_ambiguous",
                  f"scale x{i} should be held back")
            check(len(res.solved.connected) == 2 * n, "all records indexed")
        check(len(set(res.solved.connected.values())) == n,
              "120 independent relation components expected")
        check(len(res.endpoints) == 0, "ambiguous sessions must not be exported")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------- 5. load_package + PostgreSQL --

def postgres_end_to_end():
    """Cleaned, unambiguous batch -> existing load_package -> build -> read."""
    tmp = tempfile.mkdtemp(prefix="ccpg-")
    try:
        rows = [
            {"record_id": "p1", "stream": "S", "seq": 1, "type": "door",
             "device": "dev1", "session_id": "s1", "kind": "enter",
             "content": "in", "local": "2026-10-25T04:00:00.000"},
            {"record_id": "p2", "stream": "S", "seq": 2, "type": "door",
             "device": "dev1", "session_id": "s1", "kind": "exit",
             "content": "out", "local": "2026-10-25T04:30:00.000"},
        ]
        with open(os.path.join(tmp, "d.jsonl"), "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")
        with open(os.path.join(tmp, "manifest.json"), "w", encoding="utf-8") as fh:
            json.dump({"streams": {"S": "Europe/Berlin"}, "files": ["d.jsonl"]}, fh)
        out = os.path.join(tmp, "out")
        result = run_import(os.path.join(tmp, "manifest.json"), out)
        check(len(result.endpoints) == 2, "cleaned batch must have two endpoints")

        # the batch file must pass the EXISTING validator with no extra fields
        batch = json.load(open(os.path.join(out, "endpoints_batch.json")))
        check(set(batch) == {"endpoints"}, "batch file holds endpoints only")
        for ep in batch["endpoints"]:
            check(set(ep) == {"device", "session_id", "kind", "ts", "content"},
                  f"endpoint carries unknown fields: {set(ep)}")
        window = {"lo": ms(2026, 10, 25, 0, 0), "hi": ms(2026, 10, 25, 6, 0)}
        pkg = load_package("clockclean_pg", window, [batch])

        # small lease + activity so a confirmed segment is actually built
        lease = {"leases": [{"lease_id": "L1", "device": "dev1",
                             "start": window["lo"], "end": window["hi"],
                             "pauses": []}],
                 "activities": [{"activity_id": "a1", "device": "dev1",
                                 "lo": ms(2026, 10, 25, 3, 0),
                                 "hi": ms(2026, 10, 25, 3, 30)}],
                 "coverage": [
                     {"source": "door", "lo": window["lo"], "hi": window["hi"]},
                     {"source": "telemetry", "lo": window["lo"], "hi": window["hi"]}]}
        pkg2 = load_package("clockclean_pg", window, [batch, lease])
        check(pkg.endpoints == pkg2.endpoints,
              "the cleaned endpoints survive the existing validator unchanged")
        with Processor(dsn=DSN) as proc:
            proc.install()
            proc.process(pkg2)
            evidence = proc.evidence("clockclean_pg", "dev1")
        sess = next(s for s in evidence["sessions"] if s["session_id"] == "s1")
        check(sess["state"] == "complete", f"session state: {sess}")
        check(sess["enter_ts"] == ms(2026, 10, 25, 3, 0)
              and sess["exit_ts"] == ms(2026, 10, 25, 3, 30),
              f"UTC endpoints read back from PostgreSQL: {sess}")
        check(sess["range"] == [ms(2026, 10, 25, 3, 0), ms(2026, 10, 25, 3, 30)],
              "session range in UTC ms")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def edge_cases():
    tmp = tempfile.mkdtemp(prefix="ccedge-")
    try:
        # seq gaps are allowed; anchors and doors interleave; a genuinely
        # single-ended ambiguous session quarantines its only record
        rows = [
            {"record_id": "k1", "stream": "S", "seq": 100, "type": "anchor",
             "utc_ms": ms(2026, 10, 24, 20, 0)},
            {"record_id": "k2", "stream": "S", "seq": 250, "type": "door",
             "device": "d", "session_id": "only", "kind": "enter",
             "content": "c", "local": "2026-10-25T02:05:00.000"},
            {"record_id": "k3", "stream": "S", "seq": 900, "type": "anchor",
             "utc_ms": ms(2026, 10, 25, 6, 0)},
        ]
        with open(os.path.join(tmp, "d.jsonl"), "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")
        with open(os.path.join(tmp, "manifest.json"), "w", encoding="utf-8") as fh:
            json.dump({"streams": {"S": "Europe/Berlin"},
                       "files": ["d.jsonl"]}, fh)
        res = clean_manifest(os.path.join(tmp, "manifest.json"))
        check(len(res.solved.relations) == 2, "two adjacent stream relations despite seq gaps")
        check(not res.solved.variables["k2"].determined,
              "single-ended ambiguous enter must stay ambiguous")
        check(res.decisions["k2"] == "quarantined_ambiguous",
              "single-ended ambiguous record is quarantined, not exported")

        # a determined single-ended session IS exported (no partner to wait for)
        rows2 = [r for r in rows if r["record_id"] != "k2"]
        rows2.insert(1, {"record_id": "k2", "stream": "S", "seq": 250,
                         "type": "door", "device": "d", "session_id": "only",
                         "kind": "enter", "content": "c",
                         "local": "2026-10-25T04:45:00.000"})
        with open(os.path.join(tmp, "d.jsonl"), "w", encoding="utf-8") as fh:
            for row in rows2:
                fh.write(json.dumps(row) + "\n")
        res2 = clean_manifest(os.path.join(tmp, "manifest.json"))
        check(res2.decisions["k2"] == "exported" and len(res2.endpoints) == 1,
              "determined single-ended session exports its one endpoint")

        # report carries relation provenance and per-record source locations
        report = build_report(res)
        check(all({"kind", "left", "right", "detail"} == set(rel)
                  for rel in report["relations"]), "relation provenance shape")
        check(report["records"]["k2"]["sources"] == [{"file": "d.jsonl", "line": 2}],
              "physical line source kept in report")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def rerun_invariance():
    """Reordered manifest / fresh output dir: identical business artifacts."""
    out1 = tempfile.mkdtemp(prefix="ccinv1-")
    out2 = tempfile.mkdtemp(prefix="ccinv2-")
    shutil.rmtree(out1); shutil.rmtree(out2)
    try:
        run_import(os.path.join(FIX, "case_resend", "manifest.json"), out1)
        run_import(os.path.join(FIX, "case_resend", "manifest.json"), out2)
        for name in ("endpoints_batch.json", "report.json", "sources.json"):
            a = open(os.path.join(out1, name), encoding="utf-8").read()
            b = open(os.path.join(out2, name), encoding="utf-8").read()
            check(a == b, f"{name} must be stable across re-runs")
    finally:
        shutil.rmtree(out1, ignore_errors=True)
        shutil.rmtree(out2, ignore_errors=True)


def explain_on_unsat_and_unknown():
    global PASS
    # explaining an unknown id raises a key error the CLI maps to exit 2
    res = clean_manifest(os.path.join(FIX, "case_main", "manifest.json"))
    try:
        explain_record(res, "nope")
        check(False, "unknown record id must raise")
    except KeyError:
        PASS += 1
    # an unsatisfiable import never reaches explain
    try:
        clean_manifest(os.path.join(FIX, "case_contradiction", "manifest.json"))
        check(False, "contradiction must raise")
    except UnsatisfiableClockError as exc:
        check(set(exc.records) == {"x-enter", "x-exit"}, "unsat names the records")
        check(all(rel.detail for rel in exc.relations), "unsat names the relations")
        PASS += 1


def indirect_cross_stream_propagation():
    """A two-candidate record is pinned only through another stream's session."""
    tmp = tempfile.mkdtemp(prefix="ccprop-")
    try:
        rows = [
            {"record_id": "a1", "stream": "A", "seq": 1, "type": "door",
             "device": "d", "session_id": "s", "kind": "enter", "content": "c",
             "local": "2026-10-25T02:10:00.000"},
            {"record_id": "a2", "stream": "A", "seq": 2, "type": "anchor",
             "utc_ms": ms(2026, 10, 25, 2, 0)},
            {"record_id": "b1", "stream": "B", "seq": 1, "type": "door",
             "device": "d", "session_id": "s", "kind": "exit", "content": "c",
             "utc_ms": ms(2026, 10, 25, 0, 20)},
        ]
        with open(os.path.join(tmp, "d.jsonl"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(json.dumps(r) for r in rows))
        with open(os.path.join(tmp, "m.json"), "w", encoding="utf-8") as fh:
            json.dump({"streams": {"A": "Europe/Berlin", "B": "Europe/Berlin"},
                       "files": ["d.jsonl"]}, fh)
        full = clean_manifest(os.path.join(tmp, "m.json"))
        check(full.solved.variables["a1"].determined
              and full.solved.variables["a1"].value == ms(2026, 10, 25, 0, 10),
              "cross-stream session must pin the ambiguous enter to 00:10Z")
        # same stream without the other stream's exit: both candidates survive
        with open(os.path.join(tmp, "d.jsonl"), "w", encoding="utf-8") as fh:
            fh.write(json.dumps(rows[0]) + "\n" + json.dumps(rows[1]))
        alone = clean_manifest(os.path.join(tmp, "m.json"))
        check(not alone.solved.variables["a1"].determined
              and len(alone.solved.variables["a1"].feasible) == 2,
              "without the cross-stream session the enter stays ambiguous")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    fixture_expectations()
    hard_failure_cases()
    cross_check_all_fixtures()
    for seed in range(60):
        fuzz_cross_check(seed)
    scale_independent_sessions(120)
    edge_cases()
    indirect_cross_stream_propagation()
    rerun_invariance()
    explain_on_unsat_and_unknown()
    postgres_end_to_end()
    print(f"\n{PASS} checks passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
