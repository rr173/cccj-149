"""Raw local-clock cleaning for legacy gateway exports.

Public entry points (all work offline, no database needed):

  * clean_manifest        -- validate, disambiguate and decide, in memory
  * run_import            -- clean_manifest + atomic batch/report/sources output
  * build_report          -- full per-record / relation / decision report
  * explain_record        -- candidates, connected constraints and joint
                             satisfying instances for one record_id
  * load_manifest         -- structural validation only
  * ClockImportError / UnsatisfiableClockError / ClockLabelError / TimezoneError

The cleaned door output uses the existing endpoint format and is meant to be
fed straight into :func:`usage_intervals.load_package`; no usage-state
algorithm lives here.
"""

from .pipeline import (
    ANCHOR_DECISION,
    EXPORTED,
    HELD_BACK_PARTNER,
    QUARANTINED_AMBIGUOUS,
    CleanResult,
    build_report,
    build_source_map,
    clean_manifest,
    explain_record,
    run_import,
)
from .raw import ClockImportError, LoadedManifest, RawRecord, load_manifest
from .solver import UnsatisfiableClockError
from .timezones import ClockLabelError, TimezoneError, local_candidates

__all__ = [
    "clean_manifest",
    "run_import",
    "build_report",
    "build_source_map",
    "explain_record",
    "load_manifest",
    "CleanResult",
    "LoadedManifest",
    "RawRecord",
    "ClockImportError",
    "UnsatisfiableClockError",
    "ClockLabelError",
    "TimezoneError",
    "local_candidates",
    "EXPORTED",
    "QUARANTINED_AMBIGUOUS",
    "HELD_BACK_PARTNER",
    "ANCHOR_DECISION",
]
