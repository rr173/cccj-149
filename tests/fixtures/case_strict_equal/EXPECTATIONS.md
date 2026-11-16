# case_strict_equal — equality is illegal for enter<exit

* t-pre anchor and t-exit are both pinned to 1792891800000 (01:30Z).
* t-enter label 2026-10-25T02:30:00.000 candidates [1792888200000, 1792891800000] (00:30Z / 01:30Z). The
  seq-1 anchor forces t-enter (non-decreasing) onto its 01:30Z candidate;
  00:30Z would precede the anchor and is pruned.
* Stream non-decreasing accepts the equal instant anchor == enter == exit, but
  the session enter<exit relation is STRICT: enter == exit is impossible. The
  import FAILS (exit 2) naming t-enter/t-exit and the session relation; no
  output directory is created.
