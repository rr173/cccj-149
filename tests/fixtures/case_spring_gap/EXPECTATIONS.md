# case_spring_gap — label does not exist

* 2026-03-29T02:30:00.000 falls in the Berlin spring-forward gap (clocks jump
  02:00 -> 03:00 on 2026-03-29); it has ZERO UTC candidates.
* Import FAILS (exit code 2). The tool must not move the label to 03:30 or any
  other hour and must not quarantine it as ordinary ambiguity.
