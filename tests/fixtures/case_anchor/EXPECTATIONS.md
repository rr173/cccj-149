# case_anchor — anchor propagation across files

Manifest lists part2 before part1 on purpose: file order must not matter,
only seq does.

* a-1 label 2026-10-25T02:20:00.000 candidates [1792887600000, 1792891200000] (00:20Z / 01:20Z).
  seq1 <= anchor seq2 at 01:20Z: both 00:20Z and 01:20Z are <= 01:20Z (the
  stream relation is NON-decreasing, equality allowed).
* anchor seq3 at 01:30Z <= a-2 seq4; a-2 label 2026-10-25T02:40:00.000 candidates
  [1792888800000, 1792892400000] (00:40Z / 01:40Z). 00:40Z would precede 01:30Z, so the anchor
  forces a-2 to its second candidate 01:40Z (1792892400000).
* The enter<exit session relation leaves a-1 with both 00:20Z and 01:20Z
  feasible (both precede 01:40Z), so the session stays quarantined while a-2
  is determined-but-held-back.
* Both anchors participate in ordering and are never exported.
