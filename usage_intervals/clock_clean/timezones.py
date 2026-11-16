"""Local wall-clock labels -> real UTC candidates.

The legacy gateways only persist the wall-clock face (``YYYY-MM-DDTHH:MM:SS.mmm``,
no offset).  A label may denote:

* exactly one UTC instant on ordinary days and outside a transition window;
* two UTC instants during a fall-back (the repeated hour / half-hour);
* *zero* instants during a spring-forward gap -- such a label makes the whole
  import fail, the clock is never silently moved to the next valid hour.

The mapping is derived from the IANA database via :mod:`zoneinfo` and every
candidate is verified by converting it back to the same local label: merely
setting ``fold=1`` is not treated as proof that the label exists, and no
one-hour assumption is made (e.g. ``Australia/Lord_Howe`` repeats only 30 min).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1

# Strict shape: exactly three fractional digits, no offset, no 'Z'.
_LABEL_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})\.(\d{3})$")

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class ClockLabelError(ValueError):
    """A local label is malformed or does not exist in its timezone."""


class TimezoneError(ValueError):
    """The timezone name is not a known IANA zone."""


def is_int64(value) -> bool:
    # bool is an int subclass; a boolean timestamp makes no sense.
    return isinstance(value, int) and not isinstance(value, bool)


def parse_local_label(label: str) -> datetime:
    """Parse the strict local-label shape into a naive datetime."""
    if not isinstance(label, str):
        raise ClockLabelError(f"local label must be a string, got {type(label).__name__}")
    m = _LABEL_RE.match(label)
    if m is None:
        raise ClockLabelError(
            f"local label {label!r} must look like YYYY-MM-DDTHH:MM:SS.mmm with no offset"
        )
    y, mo, d, hh, mm, ss, ms = (int(g) for g in m.groups())
    try:
        # fromisoformat would accept much more; validate the calendar explicitly.
        return datetime(y, mo, d, hh, mm, ss, ms * 1000)
    except ValueError as exc:
        raise ClockLabelError(f"local label {label!r} is not a legal calendar date/time: {exc}") from exc


def get_zone(tzname: str) -> ZoneInfo:
    if not isinstance(tzname, str) or not tzname:
        raise TimezoneError(f"timezone name must be a non-empty string, got {tzname!r}")
    try:
        return ZoneInfo(tzname)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise TimezoneError(f"unknown or invalid IANA timezone {tzname!r}: {exc}") from exc


def _to_epoch_ms(aware: datetime) -> int:
    return int((aware.astimezone(timezone.utc) - _EPOCH) // timedelta(milliseconds=1))


def _from_epoch_ms(ms: int, zone: ZoneInfo) -> datetime:
    # Exact integer arithmetic; no float seconds, no platform gmtime limits.
    return (_EPOCH + timedelta(milliseconds=ms)).astimezone(zone)


def local_candidates(label: str, zone: ZoneInfo) -> tuple[int, ...]:
    """All real UTC millisecond instants denoted by a local label in ``zone``.

    Returns one candidate for ordinary labels, two distinct candidates for a
    fall-back repeated label and an empty tuple for a non-existent spring-gap
    label.  Every returned instant converts back to the identical label with
    the offset that produced it.
    """
    naive = parse_local_label(label)
    found: set[int] = set()
    for fold in (0, 1):
        aware = naive.replace(tzinfo=zone, fold=fold)
        ms = _to_epoch_ms(aware)
        if not (INT64_MIN <= ms <= INT64_MAX):
            raise ClockLabelError(f"local label {label!r} resolves outside the int64 millisecond range")
        back = _from_epoch_ms(ms, zone)
        # Round-trip: same wall-clock fields AND same offset as the candidate
        # that produced it.  A fold guess that the zone rules move off a gap
        # fails this check instead of inventing a non-existent instant.
        if back.replace(tzinfo=None) == naive and back.utcoffset() == aware.utcoffset():
            found.add(ms)
    return tuple(sorted(found))


def utc_ms_candidate(ms: int) -> int:
    """Validate an explicit UTC millisecond record's single candidate."""
    if not is_int64(ms):
        raise ClockLabelError("utc_ms must be an integer (booleans are rejected)")
    if not (INT64_MIN <= ms <= INT64_MAX):
        raise ClockLabelError("utc_ms outside signed int64 range")
    return int(ms)
