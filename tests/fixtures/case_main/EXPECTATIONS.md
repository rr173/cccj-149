# case_main — human UTC expectations

All times below are verified UTC milliseconds. The manifest lists the files in
an order unrelated to the seq order on purpose.

## Session s1 (devA): enter 02:50 face, exit 02:10 face — the clock went back
* r-enter-s1 candidates: [1792889400000, 1792893000000] (00:50Z / 01:50Z)
* r-exit-s1  candidates: [1792887000000, 1792890600000] (00:10Z / 01:10Z)
* enter < exit only for enter=00:50Z (1792889400000) and exit=01:10Z (1792890600000).
  The fall-back must NOT be reported as a bad session. Both ends are DETERMINED
  and the session is exported with enter 1792889400000, exit 1792890600000.

## Session s2 (devB, stream D): determined exit, ambiguous enter
* r-enter-s2 label 02:40 face candidates: [1792888800000, 1792892400000] (00:40Z / 01:40Z).
* r-exit-s2 is a fixed utc_ms 1792893300000 (01:55Z), itself determined.
* Both enter candidates are strictly before 01:55Z and stream D has no other
  records, so nothing disambiguates the enter: it stays ambiguous and the
  whole session is quarantined (determined exit held back with its partner).

## Session s4 (devD, stream C): the required ambiguous-enter / pinned-exit session
* r-enter-s4 label 02:45 face candidates [1792889100000, 1792892700000] (00:45Z / 01:45Z).
* r-exit-s4 is a fixed utc_ms 1792893600000 (02:00Z),
  itself determined.
* Stream C has no ordering relation to streams A/B (different streams have no
  default order). Both enter candidates survive and both are strictly before
  02:00Z, so r-enter-s4 stays AMBIGUOUS. Neither end is exported: the
  determined exit gets decision held_back_partner_ambiguous, the enter gets
  quarantined_ambiguous, and the whole original session stays in the report.
  No fake unpaired session is manufactured.

## Session s3 (devC): independent, unambiguous
* r-enter-s3 04:00 face -> single candidate 1792897200000 (03:00Z)
* r-exit-s3  04:30 face -> single candidate 1792899000000 (03:30Z)
* Exported: enter 1792897200000, exit 1792899000000.

## Anchors (cross-file stream propagation, never exported)
* r-anchor-b1 at 1792884600000 (23:30Z), seq 5 in a
  separate anchor file that the manifest lists first; every B door record
  follows it by seq regardless of physical file order.
* r-anchor-a at 1792891800000 (01:30Z) sits after r-enter-s1 (00:50Z) and
  before r-enter-s3 (03:00Z) in stream A.
* The dedicated case_anchor fixture shows an anchor pinning a two-candidate
  label; case_equal_instant shows equality being allowed by stream order.
