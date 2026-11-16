# case_contradiction — no joint interpretation

* x-enter candidates [1792889400000, 1792893000000] (00:50Z / 01:50Z on 2026-10-25).
* x-exit is pinned to 1792882800000 (2026-10-24T23:00Z), strictly before both
  enter candidates, so enter < exit can never hold.
* The whole import FAILS (exit code 2) naming x-enter/x-exit and the session
  relation. No output directory is created; rows must not be dropped to force
  feasibility.
