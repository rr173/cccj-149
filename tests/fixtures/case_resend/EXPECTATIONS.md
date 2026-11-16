# case_resend — reorder and re-send invariance

* rs-enter appears twice (f1 line 1 and f2 line 2) as an exact duplicate: it
  folds to one logical record but the source map must list BOTH locations.
* The manifest reverses file order; seq (5 < 9) still gives the same order.
* Determined session exported: enter 1792897200000, exit 1792899000000.
* Re-running into a fresh directory yields byte-identical business output.
