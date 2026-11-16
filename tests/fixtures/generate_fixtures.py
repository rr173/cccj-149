#!/usr/bin/env python3 -I
"""Generate the small, committable clock-cleaning fixtures.

Every fixture directory contains one ``manifest.json`` plus the JSONL files it
lists and an ``EXPECTATIONS.md`` stating the human UTC predictions.  The
generator only uses the standard library (zoneinfo) to turn labels into the
documented expected millisecond values; it never imports the formal solver.

Run:  python3 -I tests/fixtures/generate_fixtures.py
"""
import json
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
UTC = ZoneInfo("UTC")


def ms(label_utc):
    return int(datetime.strptime(label_utc, "%Y-%m-%dT%H:%M:%S.%f")
               .replace(tzinfo=UTC).timestamp() * 1000)


def write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def write_manifest(directory, streams, files):
    with open(os.path.join(directory, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump({"streams": streams, "files": files}, fh, indent=2)
        fh.write("\n")


def door(rid, stream, seq, device, sid, kind, content, **timefield):
    return {"record_id": rid, "stream": stream, "seq": seq, "type": "door",
            "device": device, "session_id": sid, "kind": kind,
            "content": content, **timefield}


def anchor(rid, stream, seq, utc_ms):
    return {"record_id": rid, "stream": stream, "seq": seq,
            "type": "anchor", "utc_ms": utc_ms}


# ----------------------------------------------------------------- numbers --
# Europe/Berlin fall-back 2026-10-25 (CEST +02:00 -> CET +01:00 at 03:00 face)
B_0250 = ("2026-10-25T02:50:00.000",
          [ms("2026-10-25T00:50:00.000"), ms("2026-10-25T01:50:00.000")])
B_0210 = ("2026-10-25T02:10:00.000",
          [ms("2026-10-25T00:10:00.000"), ms("2026-10-25T01:10:00.000")])
B_0205 = ("2026-10-25T02:05:00.000",
          [ms("2026-10-25T00:05:00.000"), ms("2026-10-25T01:05:00.000")])
B_0230 = ("2026-10-25T02:30:00.000",
          [ms("2026-10-25T00:30:00.000"), ms("2026-10-25T01:30:00.000")])
B_0400 = ("2026-10-25T04:00:00.000", [ms("2026-10-25T03:00:00.000")])
B_0430 = ("2026-10-25T04:30:00.000", [ms("2026-10-25T03:30:00.000")])
B_0120 = ("2026-10-25T01:20:00.000", [ms("2026-10-25T23:20:00.000")])  # prior day UTC
B_0220 = ("2026-10-25T02:20:00.000",
          [ms("2026-10-25T00:20:00.000"), ms("2026-10-25T01:20:00.000")])
B_0240 = ("2026-10-25T02:40:00.000",
          [ms("2026-10-25T00:40:00.000"), ms("2026-10-25T01:40:00.000")])
B_0245 = ("2026-10-25T02:45:00.000",
          [ms("2026-10-25T00:45:00.000"), ms("2026-10-25T01:45:00.000")])
ANCHOR_0120Z = ms("2026-10-25T01:20:00.000")
ANCHOR_0130Z = ms("2026-10-25T01:30:00.000")
EXIT_0130Z = ms("2026-10-25T01:30:00.000")
EXIT_0155Z = ms("2026-10-25T01:55:00.000")
# spring gap: 02:30 does not exist on 2026-03-29 in Berlin
SPRING_GAP_LABEL = "2026-03-29T02:30:00.000"
# Lord Howe fall-back 2026-04-05 (+11:00 -> +10:30; only a 30-minute repeat)
LH_0140 = ("2026-04-05T01:40:00.000",
           [ms("2026-04-04T14:40:00.000"), ms("2026-04-04T15:10:00.000")])
LH_0150 = ("2026-04-05T01:50:00.000",
           [ms("2026-04-04T14:50:00.000"), ms("2026-04-04T15:20:00.000")])
LH_0145 = ("2026-04-05T01:45:00.000",
           [ms("2026-04-04T14:45:00.000"), ms("2026-04-04T15:15:00.000")])


def case_main():
    """Headline walk-through: fallback ordering, ambiguity, determined, anchors."""
    d = os.path.join(HERE, "case_main")
    os.makedirs(d, exist_ok=True)
    # stream A: session s1 enter 02:50 (two candidates), anchor, session s3 enter
    write_jsonl(os.path.join(d, "stream_a.jsonl"), [
        door("r-enter-s1", "A", 10, "devA", "s1", "enter", "in-1", local=B_0250[0]),
        anchor("r-anchor-a", "A", 20, ANCHOR_0130Z),
        door("r-enter-s3", "A", 30, "devC", "s3", "enter", "in-3", local=B_0400[0]),
    ])
    # stream B: anchors first (in a separate file), then s1 exit and s3 exit
    write_jsonl(os.path.join(d, "stream_b_anchors.jsonl"), [
        anchor("r-anchor-b1", "B", 5, ms("2026-10-24T23:30:00.000")),
    ])
    write_jsonl(os.path.join(d, "stream_b.jsonl"), [
        door("r-exit-s1", "B", 10, "devA", "s1", "exit", "out-1", local=B_0210[0]),
        door("r-exit-s3", "B", 40, "devC", "s3", "exit", "out-3", local=B_0430[0]),
    ])
    # stream D: the determined-enter / pinned-exit pair whose enter keeps two
    # candidates under its own session relation alone.
    write_jsonl(os.path.join(d, "stream_d.jsonl"), [
        door("r-enter-s2", "D", 1, "devB", "s2", "enter", "in-2", local=B_0240[0]),
        door("r-exit-s2", "D", 2, "devB", "s2", "exit", "out-2", utc_ms=EXIT_0155Z),
    ])
    # stream C: only the ambiguous-enter / pinned-exit session; no stream-order
    # relation to the others, so both enter candidates stay alive.
    write_jsonl(os.path.join(d, "stream_c.jsonl"), [
        door("r-enter-s4", "C", 1, "devD", "s4", "enter", "in-4", local=B_0245[0]),
        door("r-exit-s4", "C", 2, "devD", "s4", "exit", "out-4",
             utc_ms=ms("2026-10-25T02:00:00.000")),
    ])
    write_manifest(d, {"A": "Europe/Berlin", "B": "Europe/Berlin",
                       "C": "Europe/Berlin", "D": "Europe/Berlin"},
                   ["stream_b.jsonl", "stream_b_anchors.jsonl",
                    "stream_a.jsonl", "stream_c.jsonl", "stream_d.jsonl"])
    with open(os.path.join(d, "EXPECTATIONS.md"), "w", encoding="utf-8") as fh:
        fh.write(f"""# case_main — human UTC expectations

All times below are verified UTC milliseconds. The manifest lists the files in
an order unrelated to the seq order on purpose.

## Session s1 (devA): enter 02:50 face, exit 02:10 face — the clock went back
* r-enter-s1 candidates: {B_0250[1]} (00:50Z / 01:50Z)
* r-exit-s1  candidates: {B_0210[1]} (00:10Z / 01:10Z)
* enter < exit only for enter=00:50Z ({B_0250[1][0]}) and exit=01:10Z ({B_0210[1][1]}).
  The fall-back must NOT be reported as a bad session. Both ends are DETERMINED
  and the session is exported with enter {B_0250[1][0]}, exit {B_0210[1][1]}.

## Session s2 (devB, stream D): determined exit, ambiguous enter
* r-enter-s2 label 02:40 face candidates: {B_0240[1]} (00:40Z / 01:40Z).
* r-exit-s2 is a fixed utc_ms {EXIT_0155Z} (01:55Z), itself determined.
* Both enter candidates are strictly before 01:55Z and stream D has no other
  records, so nothing disambiguates the enter: it stays ambiguous and the
  whole session is quarantined (determined exit held back with its partner).

## Session s4 (devD, stream C): the required ambiguous-enter / pinned-exit session
* r-enter-s4 label 02:45 face candidates {B_0245[1]} (00:45Z / 01:45Z).
* r-exit-s4 is a fixed utc_ms {ms('2026-10-25T02:00:00.000')} (02:00Z),
  itself determined.
* Stream C has no ordering relation to streams A/B (different streams have no
  default order). Both enter candidates survive and both are strictly before
  02:00Z, so r-enter-s4 stays AMBIGUOUS. Neither end is exported: the
  determined exit gets decision held_back_partner_ambiguous, the enter gets
  quarantined_ambiguous, and the whole original session stays in the report.
  No fake unpaired session is manufactured.

## Session s3 (devC): independent, unambiguous
* r-enter-s3 04:00 face -> single candidate {B_0400[1][0]} (03:00Z)
* r-exit-s3  04:30 face -> single candidate {B_0430[1][0]} (03:30Z)
* Exported: enter {B_0400[1][0]}, exit {B_0430[1][0]}.

## Anchors (cross-file stream propagation, never exported)
* r-anchor-b1 at {ms('2026-10-24T23:30:00.000')} (23:30Z), seq 5 in a
  separate anchor file that the manifest lists first; every B door record
  follows it by seq regardless of physical file order.
* r-anchor-a at {ANCHOR_0130Z} (01:30Z) sits after r-enter-s1 (00:50Z) and
  before r-enter-s3 (03:00Z) in stream A.
* The dedicated case_anchor fixture shows an anchor pinning a two-candidate
  label; case_equal_instant shows equality being allowed by stream order.
""")


def case_anchor_propagation():
    """Cross-file anchor pins the two-candidate labels around it in one stream."""
    d = os.path.join(HERE, "case_anchor")
    os.makedirs(d, exist_ok=True)
    write_jsonl(os.path.join(d, "part1.jsonl"), [
        door("a-1", "S", 1, "dev", "sa", "enter", "c1", local=B_0220[0]),
        anchor("a-anchor", "S", 2, ANCHOR_0120Z),   # 01:20Z
    ])
    write_jsonl(os.path.join(d, "part2.jsonl"), [
        anchor("a-anchor-2", "S", 3, ANCHOR_0130Z),  # 01:30Z
        door("a-2", "S", 4, "dev", "sa", "exit", "c2", local=B_0240[0]),
    ])
    write_manifest(d, {"S": "Europe/Berlin"}, ["part2.jsonl", "part1.jsonl"])
    with open(os.path.join(d, "EXPECTATIONS.md"), "w", encoding="utf-8") as fh:
        fh.write(f"""# case_anchor — anchor propagation across files

Manifest lists part2 before part1 on purpose: file order must not matter,
only seq does.

* a-1 label {B_0220[0]} candidates {B_0220[1]} (00:20Z / 01:20Z).
  seq1 <= anchor seq2 at 01:20Z: both 00:20Z and 01:20Z are <= 01:20Z (the
  stream relation is NON-decreasing, equality allowed).
* anchor seq3 at 01:30Z <= a-2 seq4; a-2 label {B_0240[0]} candidates
  {B_0240[1]} (00:40Z / 01:40Z). 00:40Z would precede 01:30Z, so the anchor
  forces a-2 to its second candidate 01:40Z ({B_0240[1][1]}).
* The enter<exit session relation leaves a-1 with both 00:20Z and 01:20Z
  feasible (both precede 01:40Z), so the session stays quarantined while a-2
  is determined-but-held-back.
* Both anchors participate in ordering and are never exported.
""")


def case_equal_instant():
    """Same UTC instant is allowed for stream order, forbidden for enter<exit."""
    d = os.path.join(HERE, "case_equal_instant")
    os.makedirs(d, exist_ok=True)
    # two labels that share the 01:30Z instant: the second fold of 02:30 == 01:30Z
    write_jsonl(os.path.join(d, "equal.jsonl"), [
        anchor("e-anchor", "E", 1, EXIT_0130Z),
        door("e-1", "E", 2, "dev", "se", "enter", "c", local=B_0230[0]),
    ])
    write_manifest(d, {"E": "Europe/Berlin"}, ["equal.jsonl"])
    with open(os.path.join(d, "EXPECTATIONS.md"), "w", encoding="utf-8") as fh:
        fh.write(f"""# case_equal_instant — non-decreasing allows equality

* e-anchor at exactly {EXIT_0130Z} (01:30Z), e-1 label {B_0230[0]} with
  candidates {B_0230[1]} (00:30Z / 01:30Z).
* Stream order is non-decreasing, so e-1 = 01:30Z (equal to the anchor) stays
  feasible; the 00:30Z candidate is pruned. e-1 is determined to {B_0230[1][1]}.
* This is an enter-only session, so once determined it IS exported (no exit to
  wait for).
* The companion case_strict_equal shows the same instant is NOT allowed when it
  is an enter/exit pair: session order is strictly increasing.
""")


def case_strict_equal():
    """An enter and exit resolving to the same instant must fail (not a session)."""
    d = os.path.join(HERE, "case_strict_equal")
    os.makedirs(d, exist_ok=True)
    write_jsonl(os.path.join(d, "strict.jsonl"), [
        # anchor exactly at 01:30Z immediately before the enter: non-decreasing
        # order allows the enter's 01:30Z candidate but prunes its 00:30Z one
        anchor("t-pre", "T", 1, EXIT_0130Z),
        door("t-enter", "T", 2, "dev", "st", "enter", "c", local=B_0230[0]),
        door("t-exit", "T", 3, "dev", "st", "exit", "c", utc_ms=EXIT_0130Z),
    ])
    write_manifest(d, {"T": "Europe/Berlin"}, ["strict.jsonl"])
    with open(os.path.join(d, "EXPECTATIONS.md"), "w", encoding="utf-8") as fh:
        fh.write(f"""# case_strict_equal — equality is illegal for enter<exit

* t-pre anchor and t-exit are both pinned to {EXIT_0130Z} (01:30Z).
* t-enter label {B_0230[0]} candidates {B_0230[1]} (00:30Z / 01:30Z). The
  seq-1 anchor forces t-enter (non-decreasing) onto its 01:30Z candidate;
  00:30Z would precede the anchor and is pruned.
* Stream non-decreasing accepts the equal instant anchor == enter == exit, but
  the session enter<exit relation is STRICT: enter == exit is impossible. The
  import FAILS (exit 2) naming t-enter/t-exit and the session relation; no
  output directory is created.
""")


def case_contradiction():
    """Fully contradictory session: exit pinned before every enter candidate."""
    d = os.path.join(HERE, "case_contradiction")
    os.makedirs(d, exist_ok=True)
    early_exit = ms("2026-10-24T23:00:00.000")  # before both 02:50 candidates
    write_jsonl(os.path.join(d, "bad.jsonl"), [
        door("x-enter", "X", 1, "dev", "sx", "enter", "c", local=B_0250[0]),
        door("x-exit", "X", 2, "dev", "sx", "exit", "c", utc_ms=early_exit),
    ])
    write_manifest(d, {"X": "Europe/Berlin"}, ["bad.jsonl"])
    with open(os.path.join(d, "EXPECTATIONS.md"), "w", encoding="utf-8") as fh:
        fh.write(f"""# case_contradiction — no joint interpretation

* x-enter candidates {B_0250[1]} (00:50Z / 01:50Z on 2026-10-25).
* x-exit is pinned to {early_exit} (2026-10-24T23:00Z), strictly before both
  enter candidates, so enter < exit can never hold.
* The whole import FAILS (exit code 2) naming x-enter/x-exit and the session
  relation. No output directory is created; rows must not be dropped to force
  feasibility.
""")


def case_spring_gap():
    """A non-existent spring-forward label fails the import outright."""
    d = os.path.join(HERE, "case_spring_gap")
    os.makedirs(d, exist_ok=True)
    write_jsonl(os.path.join(d, "gap.jsonl"), [
        door("g-1", "G", 1, "dev", "sg", "enter", "c", local=SPRING_GAP_LABEL),
    ])
    write_manifest(d, {"G": "Europe/Berlin"}, ["gap.jsonl"])
    with open(os.path.join(d, "EXPECTATIONS.md"), "w", encoding="utf-8") as fh:
        fh.write(f"""# case_spring_gap — label does not exist

* {SPRING_GAP_LABEL} falls in the Berlin spring-forward gap (clocks jump
  02:00 -> 03:00 on 2026-03-29); it has ZERO UTC candidates.
* Import FAILS (exit code 2). The tool must not move the label to 03:30 or any
  other hour and must not quarantine it as ordinary ambiguity.
""")


def case_lord_howe():
    """Half-hour fall-back: the repeated window is only 30 minutes wide."""
    d = os.path.join(HERE, "case_lord_howe")
    os.makedirs(d, exist_ok=True)
    write_jsonl(os.path.join(d, "lh.jsonl"), [
        door("lh-enter", "LH", 1, "dev", "slh", "enter", "c", local=LH_0140[0]),
        anchor("lh-anchor", "LH", 2, ms("2026-04-04T15:15:00.000")),
        door("lh-exit", "LH", 3, "dev", "slh", "exit", "c", local=LH_0150[0]),
    ])
    write_manifest(d, {"LH": "Australia/Lord_Howe"}, ["lh.jsonl"])
    with open(os.path.join(d, "EXPECTATIONS.md"), "w", encoding="utf-8") as fh:
        fh.write(f"""# case_lord_howe — 30-minute fall-back (not one hour)

Lord Howe moves +11:00 -> +10:30 on 2026-04-05; only face times 01:30-02:00
repeat, and the two readings of one label are 30 minutes apart.

* lh-enter {LH_0140[0]} candidates {LH_0140[1]} (14:40Z / 15:10Z on 2026-04-04)
* lh-exit  {LH_0150[0]} candidates {LH_0150[1]} (14:50Z / 15:20Z)
* lh-anchor pinned at {ms('2026-04-04T15:15:00.000')} (15:15Z), between them
  in seq order. lh-enter must be <= 15:15Z: both 14:40Z and 15:10Z qualify, so
  lh-enter keeps BOTH candidates. lh-exit must be >= 15:15Z and strictly after
  the enter: only 15:20Z ({LH_0150[1][1]}) survives.
* lh-exit is determined-but-held-back (lh-enter still ambiguous); the whole
  session is quarantined. The 30-minute candidate spacing proves no one-hour
  assumption is baked in.
""")


def case_resend():
    """File reorder + exact re-send: identical business result, full provenance."""
    d = os.path.join(HERE, "case_resend")
    os.makedirs(d, exist_ok=True)
    # same logical rows as a small determined session, split and duplicated
    write_jsonl(os.path.join(d, "f1.jsonl"), [
        door("rs-enter", "R", 5, "dev", "srs", "enter", "in", local=B_0400[0]),
    ])
    write_jsonl(os.path.join(d, "f2.jsonl"), [
        door("rs-exit", "R", 9, "dev", "srs", "exit", "out", local=B_0430[0]),
        door("rs-enter", "R", 5, "dev", "srs", "enter", "in", local=B_0400[0]),
    ])
    write_manifest(d, {"R": "Europe/Berlin"}, ["f2.jsonl", "f1.jsonl"])
    with open(os.path.join(d, "EXPECTATIONS.md"), "w", encoding="utf-8") as fh:
        fh.write(f"""# case_resend — reorder and re-send invariance

* rs-enter appears twice (f1 line 1 and f2 line 2) as an exact duplicate: it
  folds to one logical record but the source map must list BOTH locations.
* The manifest reverses file order; seq (5 < 9) still gives the same order.
* Determined session exported: enter {B_0400[1][0]}, exit {B_0430[1][0]}.
* Re-running into a fresh directory yields byte-identical business output.
""")


def case_bad_records():
    """One directory per hard structural failure, exercised via the public CLI."""
    base = os.path.join(HERE, "case_bad")
    scenarios = {
        "unknown_stream": (
            {"Z": "Europe/Berlin"}, ["d.jsonl"],
            [door("b1", "NOPE", 1, "d", "s", "enter", "c", local=B_0400[0])],
            "stream not registered in manifest",
        ),
        "bad_timezone": (
            {"Z": "Europe/NotAZone"}, ["d.jsonl"],
            [door("b1", "Z", 1, "d", "s", "enter", "c", local=B_0400[0])],
            "invalid IANA timezone",
        ),
        "bool_integer": (
            {"Z": "Europe/Berlin"}, ["d.jsonl"],
            [anchor("b1", "Z", 1, True)],
            "booleans rejected where int64 is required",
        ),
        "seq_reused": (
            {"Z": "Europe/Berlin"}, ["d.jsonl"],
            [anchor("b1", "Z", 3, 1), anchor("b2", "Z", 3, 2)],
            "same stream/seq occupied by two record ids",
        ),
        "id_content_change": (
            {"Z": "Europe/Berlin"}, ["d.jsonl"],
            [door("b1", "Z", 1, "d", "s", "enter", "v1", local=B_0400[0]),
             door("b1", "Z", 2, "d", "s", "enter", "v2", local=B_0400[0])],
            "same record id with changed content",
        ),
        "session_kind_clash": (
            {"Z": "Europe/Berlin"}, ["d.jsonl"],
            [door("b1", "Z", 1, "d", "s", "enter", "v", local=B_0400[0]),
             door("b2", "Z", 2, "d", "s", "enter", "w", local=B_0430[0])],
            "same (device, session_id, kind) with different record ids",
        ),
        "bad_label_shape": (
            {"Z": "Europe/Berlin"}, ["d.jsonl"],
            [door("b1", "Z", 1, "d", "s", "enter", "v",
                  local="2026-10-25 04:00:00.000")],
            "label with space / offset shape is rejected",
        ),
        "illegal_calendar_date": (
            {"Z": "Europe/Berlin"}, ["d.jsonl"],
            [door("b1", "Z", 1, "d", "s", "enter", "v",
                  local="2026-02-30T04:00:00.000")],
            "2026-02-30 is not a calendar date",
        ),
        "tail_bad_line": (
            {"Z": "Europe/Berlin"}, ["d.jsonl"],
            [door("b1", "Z", 1, "d", "s", "enter", "v", local=B_0400[0]),
             "{not json"],
            "malformed trailing line fails the whole import",
        ),
        "both_time_fields": (
            {"Z": "Europe/Berlin"}, ["d.jsonl"],
            [door("b1", "Z", 1, "d", "s", "enter", "v",
                  local=B_0400[0], utc_ms=1)],
            "exactly one time field allowed",
        ),
        "unknown_field": (
            {"Z": "Europe/Berlin"}, ["d.jsonl"],
            [{"record_id": "b1", "stream": "Z", "seq": 1, "type": "anchor",
              "utc_ms": 1, "extra": 7}],
            "unknown field on an anchor record",
        ),
        "negative_seq": (
            {"Z": "Europe/Berlin"}, ["d.jsonl"],
            [anchor("b1", "Z", -1, 1)],
            "seq must be non-negative",
        ),
        "utc_ms_overflow": (
            {"Z": "Europe/Berlin"}, ["d.jsonl"],
            [anchor("b1", "Z", 1, 2**63)],
            "utc_ms outside signed int64 fails, it is not quarantine material",
        ),
        "resend_seq_collision": (
            # b2 occupies (Z,2) before the folded re-send of b1 is re-checked;
            # the post-fold global check must still catch the collision.
            {"Z": "Europe/Berlin"}, ["d.jsonl"],
            [door("b1", "Z", 1, "d", "s", "enter", "v", local=B_0400[0]),
             door("b1", "Z", 1, "d", "s", "enter", "v", local=B_0400[0]),
             anchor("b2", "Z", 1, 5)],
            "stream/seq occupied by two record ids",
        ),
        "resend_session_collision": (
            # b1 folds as an exact re-send before b2 claims the same
            # (device, session, kind); global re-check must still reject.
            {"Z": "Europe/Berlin"}, ["d.jsonl"],
            [door("b1", "Z", 1, "d", "s", "enter", "v", local=B_0400[0]),
             door("b1", "Z", 1, "d", "s", "enter", "v", local=B_0400[0]),
             door("b2", "Z", 2, "d", "s", "enter", "w", local=B_0430[0])],
            "(device, session_id, kind) claimed by two record ids",
        ),
    }
    for name, (streams, files, rows, why) in scenarios.items():
        d = os.path.join(base, name)
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, files[0])
        with open(path, "w", encoding="utf-8") as fh:
            for row in rows:
                if isinstance(row, str):
                    fh.write(row + "\n")
                else:
                    fh.write(json.dumps(row) + "\n")
        write_manifest(d, streams, files)
        with open(os.path.join(d, "EXPECTATIONS.md"), "w", encoding="utf-8") as fh:
            fh.write(f"# {name}\n\nExpected: import FAILS, exit code 2. Reason: {why}.\n")


def main():
    case_main()
    case_anchor_propagation()
    case_equal_instant()
    case_strict_equal()
    case_contradiction()
    case_spring_gap()
    case_lord_howe()
    case_resend()
    case_bad_records()
    print("fixtures written under", HERE)


if __name__ == "__main__":
    main()
