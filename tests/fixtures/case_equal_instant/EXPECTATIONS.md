# case_equal_instant — non-decreasing allows equality

* e-anchor at exactly 1792891800000 (01:30Z), e-1 label 2026-10-25T02:30:00.000 with
  candidates [1792888200000, 1792891800000] (00:30Z / 01:30Z).
* Stream order is non-decreasing, so e-1 = 01:30Z (equal to the anchor) stays
  feasible; the 00:30Z candidate is pruned. e-1 is determined to 1792891800000.
* This is an enter-only session, so once determined it IS exported (no exit to
  wait for).
* The companion case_strict_equal shows the same instant is NOT allowed when it
  is an enter/exit pair: session order is strictly increasing.
