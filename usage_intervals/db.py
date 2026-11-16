"""PostgreSQL-backed staging, build orchestration and read/interpret APIs.

Design constraints honored here:

* Python never loads the whole device fact set.  Device ids are streamed via a
  server-side named cursor and every device is computed inside the database by
  ``ui_build_package`` / ``ui_build_device``.
* A published result is bound to the exact input hash of its package.  A later
  package with the same id but different input is rejected; new packages get
  new ids and old versions remain readable.
* Structural failure marks the package failed and rolls back every fact;
  business unknown/conflict are ordinary stored facts.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import psycopg2
import psycopg2.extras

from .model import InputPackage, StructuralError

SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "sql", "schema.sql")


class PackageError(RuntimeError):
    """Package identity / lifecycle problem (unknown id, hash mismatch...)."""


@dataclass
class Segment:
    seg_index: int
    lo: int
    hi: int
    state: str
    reasons: tuple[str, ...]
    lease_ids: tuple[str, ...]
    door_complete: tuple[str, ...]
    door_unpaired: tuple[str, ...]
    activity_ids: tuple[str, ...]
    session_conflicts: tuple[str, ...]
    activity_conflicts: tuple[str, ...]
    door_covered: bool
    telemetry_covered: bool

    def explain(self) -> dict:
        """Human/JSON-friendly interpretation of one fact segment."""
        return {
            "segment": [self.lo, self.hi],
            "state": self.state,
            "reasons": list(self.reasons),
            "candidate_leases": list(self.lease_ids),
            "door": {
                "complete_sessions": list(self.door_complete),
                "unpaired_sessions": list(self.door_unpaired),
                "conflicting_sessions": list(self.session_conflicts),
                "collection_complete": self.door_covered,
            },
            "telemetry": {
                "activities": list(self.activity_ids),
                "conflicting_activities": list(self.activity_conflicts),
                "collection_complete": self.telemetry_covered,
            },
        }


class UsageIntervalStore:
    def __init__(self, dsn=None, conn=None):
        if conn is not None:
            self._owns = False
            self.conn = conn
        else:
            self._owns = True
            self.conn = psycopg2.connect(dsn or os.environ.get("UI_DSN", "dbname=usage_dev user=postgres"))
        self.conn.autocommit = False

    # ----------------------------------------------------------- lifecycle
    def close(self):
        if self._owns:
            self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def install_schema(self):
        with open(SCHEMA_PATH, "r", encoding="utf-8") as fh:
            ddl = fh.read()
        with self.conn.cursor() as cur:
            cur.execute(ddl)
        self.conn.commit()

    # ------------------------------------------------------------- staging
    def stage_package(self, pkg: InputPackage):
        """Validate-then-stage one fixed bundle.

        Structural errors roll the staging back and record a failed package
        row in a separate transaction so the failure itself is inspectable.
        """
        try:
            with self.conn.cursor() as cur:
                cur.execute("SELECT input_hash, status FROM ui_packages WHERE package_id = %s",
                            (pkg.package_id,))
                row = cur.fetchone()
                if row is not None and row[0] != pkg.input_hash:
                    raise PackageError(
                        f"package {pkg.package_id!r} already bound to input {row[0][:12]}…; "
                        f"new input {pkg.input_hash[:12]}… needs a new package id"
                    )
                if row is not None and row[1] == "built":
                    # identical re-submission: nothing to do, already published
                    self.conn.commit()
                    return False

                cur.execute(
                    "INSERT INTO ui_packages(package_id, input_hash, window_lo, window_hi, status) "
                    "VALUES (%s, %s, %s, %s, 'staged') "
                    "ON CONFLICT (package_id) DO UPDATE SET status = 'staged', error = NULL",
                    (pkg.package_id, pkg.input_hash, pkg.window_lo, pkg.window_hi),
                )
                cur.execute("DELETE FROM ui_stg_batches WHERE package_id = %s", (pkg.package_id,))
                for idx, batch in enumerate(pkg.batches):
                    cur.execute(
                        "INSERT INTO ui_stg_batches(package_id, batch_idx, payload) VALUES (%s, %s, %s)",
                        (pkg.package_id, idx, psycopg2.extras.Json(batch)),
                    )
                cur.executemany(
                    "INSERT INTO ui_stg_leases(package_id, lease_id, device, start_ms, end_ms, pauses) "
                    "VALUES (%s,%s,%s,%s,%s,%s)",
                    [(pkg.package_id, l.lease_id, l.device, l.start, l.end,
                      psycopg2.extras.Json([list(p) for p in l.pauses])) for l in pkg.leases],
                )
                cur.executemany(
                    "INSERT INTO ui_stg_endpoints(package_id, device, session_id, kind, ts, content) "
                    "VALUES (%s,%s,%s,%s,%s,%s)",
                    [(pkg.package_id, e.device, e.session_id, e.kind, e.ts, e.content)
                     for e in pkg.endpoints],
                )
                cur.executemany(
                    "INSERT INTO ui_stg_activities(package_id, activity_id, device, lo, hi) "
                    "VALUES (%s,%s,%s,%s,%s)",
                    [(pkg.package_id, a.activity_id, a.device, a.lo, a.hi)
                     for a in pkg.activities],
                )
                cur.executemany(
                    "INSERT INTO ui_stg_coverage(package_id, source, lo, hi) VALUES (%s,%s,%s,%s)",
                    [(pkg.package_id, c.source, c.lo, c.hi) for c in pkg.coverage],
                )
            self.conn.commit()
            return True
        except StructuralError:
            self.conn.rollback()
            self._record_failure(pkg, "structural")
            raise

    def _record_failure(self, pkg, kind):
        try:
            with self.conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO ui_packages(package_id, input_hash, window_lo, window_hi, status, error) "
                    "VALUES (%s,%s,%s,%s,'failed',%s) "
                    "ON CONFLICT (package_id) DO UPDATE SET status='failed', error=EXCLUDED.error",
                    (pkg.package_id, pkg.input_hash, pkg.window_lo, pkg.window_hi, kind),
                )
            self.conn.commit()
        except Exception:  # pragma: no cover - best effort audit trail
            self.conn.rollback()

    # --------------------------------------------------------------- build
    def build(self, pkg: InputPackage):
        """Stage (if needed) and publish the interval facts for this package."""
        with self.conn.cursor() as cur:
            cur.execute("SELECT status, input_hash FROM ui_packages WHERE package_id = %s",
                        (pkg.package_id,))
            row = cur.fetchone()
            if row is None:
                self.stage_package(pkg)
            elif row[1] != pkg.input_hash:
                raise PackageError(f"package id {pkg.package_id!r} bound to different input")
            cur.execute("SELECT ui_build_package(%s)", (pkg.package_id,))
        self.conn.commit()

    # ---------------------------------------------------------------- reads
    def package_status(self, package_id: str) -> dict:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT package_id, input_hash, window_lo, window_hi, status, error, built_at "
                "FROM ui_packages WHERE package_id = %s",
                (package_id,),
            )
            row = cur.fetchone()
        if row is None:
            raise PackageError(f"unknown package {package_id!r}")
        return {
            "package_id": row[0], "input_hash": row[1],
            "window": [row[2], row[3]], "status": row[4],
            "error": row[5], "built_at": row[6].isoformat() if row[6] else None,
        }

    def list_devices(self, package_id: str) -> list[str]:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT device FROM ui_segments WHERE package_id = %s ORDER BY 1",
                (package_id,),
            )
            return [r[0] for r in cur.fetchall()]

    def stream_device_ids(self, package_id: str):
        """Yield device ids via a server-side cursor: no full set in Python."""
        cur = self.conn.cursor(name="ui_devices")
        try:
            cur.itersize = 1
            cur.execute(
                "SELECT DISTINCT device FROM ui_segments WHERE package_id = %s ORDER BY 1",
                (package_id,),
            )
            for (device,) in cur:
                yield device
        finally:
            cur.close()

    def read_segments(self, package_id: str, device: str,
                      window_lo: int | None = None, window_hi: int | None = None) -> list[Segment]:
        """Read stored facts, optionally restricted to a sub-window.

        Segments are clipped to the requested window rather than reclassified,
        so evidence identities stay stable.
        """
        with self.conn.cursor() as cur:
            cur.execute("SELECT window_lo, window_hi, status FROM ui_packages WHERE package_id=%s",
                        (package_id,))
            row = cur.fetchone()
            if row is None:
                raise PackageError(f"unknown package {package_id!r}")
            if row[2] != "built":
                raise PackageError(f"package {package_id!r} is not built (status={row[2]})")
            lo = row[0] if window_lo is None else max(window_lo, row[0])
            hi = row[1] if window_hi is None else min(window_hi, row[1])
            if lo >= hi:
                return []
            cur.execute(
                """
                SELECT seg_index, lower(range), upper(range), state, reasons,
                       lease_ids, door_complete, door_unpaired, activity_ids,
                       session_conflicts, activity_conflicts,
                       door_covered, telemetry_covered
                FROM ui_segments
                WHERE package_id = %s AND device = %s AND range && int8range(%s, %s, '[)')
                ORDER BY seg_index
                """,
                (package_id, device, lo, hi),
            )
            out = []
            for r in cur.fetchall():
                clo, chi = max(r[1], lo), min(r[2], hi)
                out.append(Segment(
                    r[0], clo, chi, r[3], tuple(r[4]), tuple(r[5]), tuple(r[6]),
                    tuple(r[7]), tuple(r[8]), tuple(r[9]), tuple(r[10]), r[11], r[12],
                ))
        return out

    def read_evidence(self, package_id: str, device: str) -> dict:
        """Read back the raw paired evidence actually stored for a device."""
        with self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT session_id, state, enter_ts, exit_ts, range FROM ui_sessions "
                "WHERE package_id=%s AND device=%s ORDER BY session_id",
                (package_id, device),
            )
            sessions = []
            for r in cur.fetchall():
                sessions.append({
                    "session_id": r["session_id"], "state": r["state"],
                    "enter_ts": r["enter_ts"], "exit_ts": r["exit_ts"],
                    "range": [r["range"].lower, r["range"].upper] if r["range"] else None,
                })
            cur.execute(
                "SELECT range, activity_ids FROM ui_activity_union "
                "WHERE package_id=%s AND device=%s ORDER BY range",
                (package_id, device),
            )
            activities = [{"range": [r["range"].lower, r["range"].upper],
                           "activity_ids": list(r["activity_ids"])} for r in cur.fetchall()]
            cur.execute(
                "SELECT source, range FROM ui_coverage "
                "WHERE package_id=%s AND device=%s ORDER BY source, range",
                (package_id, device),
            )
            coverage = [{"source": r["source"], "range": [r["range"].lower, r["range"].upper]}
                        for r in cur.fetchall()]
            cur.execute(
                "SELECT lease_id, range FROM ui_lease_avail "
                "WHERE package_id=%s AND device=%s ORDER BY lease_id, range",
                (package_id, device),
            )
            leases = [{"lease_id": r["lease_id"],
                       "range": [r["range"].lower, r["range"].upper]} for r in cur.fetchall()]
        return {"leases": leases, "sessions": sessions,
                "activities": activities, "coverage": coverage}

    def interpret(self, package_id: str, device: str,
                  window_lo: int | None = None, window_hi: int | None = None) -> dict:
        """Full interval interpretation API: partition + per-segment meaning."""
        status = self.package_status(package_id)
        segs = self.read_segments(package_id, device, window_lo, window_hi)
        return {
            "package_id": package_id,
            "input_hash": status["input_hash"],
            "device": device,
            "window": [window_lo if window_lo is not None else status["window"][0],
                       window_hi if window_hi is not None else status["window"][1]],
            "segments": [s.explain() for s in segs],
        }
