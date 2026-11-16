# case_lord_howe — 30-minute fall-back (not one hour)

Lord Howe moves +11:00 -> +10:30 on 2026-04-05; only face times 01:30-02:00
repeat, and the two readings of one label are 30 minutes apart.

* lh-enter 2026-04-05T01:40:00.000 candidates [1775313600000, 1775315400000] (14:40Z / 15:10Z on 2026-04-04)
* lh-exit  2026-04-05T01:50:00.000 candidates [1775314200000, 1775316000000] (14:50Z / 15:20Z)
* lh-anchor pinned at 1775315700000 (15:15Z), between them
  in seq order. lh-enter must be <= 15:15Z: both 14:40Z and 15:10Z qualify, so
  lh-enter keeps BOTH candidates. lh-exit must be >= 15:15Z and strictly after
  the enter: only 15:20Z (1775316000000) survives.
* lh-exit is determined-but-held-back (lh-enter still ambiguous); the whole
  session is quarantined. The 30-minute candidate spacing proves no one-hour
  assumption is baked in.
