"""Clock-cleaning facade: raw manifest -> decided UTC endpoints + quarantine.

This is *raw time cleaning only*: it feeds the existing ``load_package``
endpoint format and never invents usage states or rewrites interval results.

Pipeline stages (all run fully in memory before anything is published):

1. structural validation of manifest + JSONL (see :mod:`raw`);
2. per-record UTC candidates from the IANA zone (see :mod:`timezones`) -- a
   spring-gap label with no candidate fails the whole import;
3. global 2-SAT disambiguation over stream (seq, non-decreasing) and session
   (enter < exit, cross-stream allowed) relations (see :mod:`solver`);
4. export/quarantine decisions at the original-session level;
5. batch file in the existing endpoint format, source map and full report,
   delivered atomically (an existing output directory is never overwritten).
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass

from .raw import ANCHOR, DOOR, ClockImportError, load_manifest
from .solver import build_and_solve, explain_candidate
from .timezones import ClockLabelError, local_candidates, utc_ms_candidate

EXPORTED = "exported"
QUARANTINED_AMBIGUOUS = "quarantined_ambiguous"
HELD_BACK_PARTNER = "held_back_partner_ambiguous"
ANCHOR_DECISION = "anchor"


@dataclass
class CleanResult:
    manifest_path: str
    loaded: object
    domains: dict               # record_id -> tuple of candidate ms
    solved: object
    decisions: dict             # record_id -> decision string
    decision_reasons: dict      # record_id -> human reason
    sessions: dict              # (device, session_id) -> session summary dict
    endpoints: tuple            # exported endpoint dicts (existing format only)
    sources: dict               # exported record_id -> provenance dict


def clean_manifest(manifest_path) -> CleanResult:
    """Run stages 1-4 without touching the filesystem output."""
    loaded = load_manifest(manifest_path)

    # --- stage 2: per-record real UTC candidates ---------------------------
    domains: dict[str, tuple] = {}
    for rec in loaded.records:
        if rec.type == ANCHOR:
            try:
                domains[rec.record_id] = (utc_ms_candidate(rec.utc_ms),)
            except ClockLabelError as exc:
                raise ClockImportError(
                    f"record {rec.record_id!r} ({rec.sources[0].file}:"
                    f"{rec.sources[0].line}): {exc}"
                ) from exc
            continue
        if rec.local is not None:
            try:
                cands = local_candidates(rec.local, loaded.zones[rec.stream])
            except ClockLabelError as exc:
                raise ClockImportError(
                    f"record {rec.record_id!r} ({rec.sources[0].file}:"
                    f"{rec.sources[0].line}): {exc}"
                ) from exc
            if not cands:
                raise ClockImportError(
                    f"record {rec.record_id!r} ({rec.sources[0].file}:"
                    f"{rec.sources[0].line}): local label {rec.local!r} does not "
                    f"exist in timezone {loaded.streams[rec.stream]!r} (spring gap); "
                    "the import fails, the clock is not moved to another hour"
                )
            domains[rec.record_id] = cands
        else:
            try:
                domains[rec.record_id] = (utc_ms_candidate(rec.utc_ms),)
            except ClockLabelError as exc:
                raise ClockImportError(
                    f"record {rec.record_id!r} ({rec.sources[0].file}:"
                    f"{rec.sources[0].line}): {exc}"
                ) from exc

    # --- stage 3: global joint disambiguation -------------------------------
    solved = build_and_solve(loaded.records, domains)

    # --- stage 4: session-level export / quarantine decisions ---------------
    sessions: dict[tuple, dict] = {}
    for rec in loaded.records:
        if rec.type != DOOR:
            continue
        key = (rec.device, rec.session_id)
        sess = sessions.setdefault(key, {"device": rec.device,
                                         "session_id": rec.session_id,
                                         "ends": {}})
        sess["ends"][rec.kind] = rec.record_id

    decisions, reasons = {}, {}
    for rec in loaded.records:
        if rec.type == ANCHOR:
            decisions[rec.record_id] = ANCHOR_DECISION
            reasons[rec.record_id] = "anchor clock-check record never produces door endpoints"

    endpoints = []
    sources = {}
    for key, sess in sorted(sessions.items()):
        ends = sess["ends"]
        rids = list(ends.values())
        complete = "enter" in ends and "exit" in ends
        determined = {rid: solved.variables[rid].determined for rid in rids}
        if complete and all(determined.values()):
            sess["decision"] = EXPORTED
            sess["reason"] = "both enter and exit uniquely determined"
        elif not complete and all(determined.values()):
            sess["decision"] = EXPORTED
            sess["reason"] = f"single-end session; the {next(iter(ends))} end is determined"
        else:
            sess["decision"] = "quarantined"
            ambiguous = [rid for rid in rids if not determined[rid]]
            sess["reason"] = ("ambiguous end(s) " + ", ".join(ambiguous)
                              + "; the whole original session stays with the data team")
        for kind, rid in ends.items():
            rec = loaded.by_id[rid]
            if sess["decision"] == EXPORTED:
                decisions[rid] = EXPORTED
                reasons[rid] = sess["reason"]
                endpoints.append({
                    "device": rec.device,
                    "session_id": rec.session_id,
                    "kind": rec.kind,
                    "ts": solved.variables[rid].value,
                    "content": rec.content,
                })
                sources[rid] = _provenance(loaded, rec)
            elif solved.variables[rid].determined:
                # determined itself, but not exported because a partner is ambiguous
                decisions[rid] = HELD_BACK_PARTNER
                reasons[rid] = ("determined but held back: " + sess["reason"])
            else:
                decisions[rid] = QUARANTINED_AMBIGUOUS
                reasons[rid] = sess["reason"]

    endpoints.sort(key=lambda e: (e["device"], e["session_id"], e["kind"]))
    return CleanResult(
        manifest_path=os.path.abspath(os.fspath(manifest_path)),
        loaded=loaded, domains=domains, solved=solved,
        decisions=decisions, decision_reasons=reasons,
        sessions=sessions, endpoints=tuple(endpoints), sources=sources,
    )


def _provenance(loaded, rec) -> dict:
    return {
        "record_id": rec.record_id,
        "stream": rec.stream,
        "seq": rec.seq,
        "type": rec.type,
        "locations": [
            {"file": loc.file, "line": loc.line}
            for loc in loaded.locations_by_id[rec.record_id]
        ],
    }


# ----------------------------------------------------------------- reporting --

def build_report(result: CleanResult) -> dict:
    loaded, solved = result.loaded, result.solved
    records_out = {}
    for rec in loaded.records:
        sv = solved.variables[rec.record_id]
        raw = {"local": rec.local} if rec.local is not None else {"utc_ms": rec.utc_ms}
        door = None
        if rec.type == DOOR:
            door = {"device": rec.device, "session_id": rec.session_id,
                    "kind": rec.kind, "content": rec.content}
        records_out[rec.record_id] = {
            "stream": rec.stream,
            "seq": rec.seq,
            "type": rec.type,
            "timezone": loaded.streams[rec.stream],
            "raw": raw,
            "initial_candidates": list(sv.candidates),
            "feasible_candidates": list(sv.feasible),
            "determined": sv.determined,
            "determined_utc_ms": sv.value,
            "door": door,
            "decision": result.decisions.get(rec.record_id),
            "decision_reason": result.decision_reasons.get(rec.record_id),
            "sources": [
                {"file": loc.file, "line": loc.line}
                for loc in loaded.locations_by_id[rec.record_id]
            ],
        }

    relations_out = [
        {"kind": rel.kind, "left": rel.left_id, "right": rel.right_id,
         "detail": rel.detail}
        for rel in solved.relations
    ]
    sessions_out = {
        f"{key[0]}|{key[1]}": {
            "device": s["device"], "session_id": s["session_id"],
            "ends": dict(s["ends"]),
            "complete": ("enter" in s["ends"] and "exit" in s["ends"]),
            "decision": s["decision"], "reason": s["reason"],
        }
        for key, s in sorted(result.sessions.items())
    }
    n_quar = sum(1 for d in result.decisions.values()
                 if d in (QUARANTINED_AMBIGUOUS, HELD_BACK_PARTNER))
    return {
        "manifest": result.manifest_path,
        "streams": dict(loaded.streams),
        "files": list(loaded.files),
        "summary": {
            "logical_records": len(loaded.records),
            "door_records": sum(1 for r in loaded.records if r.type == DOOR),
            "anchor_records": sum(1 for r in loaded.records if r.type == ANCHOR),
            "exported_endpoints": len(result.endpoints),
            "quarantined_records": n_quar,
            "ambiguous_records": sum(
                1 for rid in solved.variables
                if not solved.variables[rid].determined),
            "sessions_total": len(result.sessions),
            "sessions_exported": sum(1 for s in result.sessions.values()
                                     if s["decision"] == EXPORTED),
            "sessions_quarantined": sum(1 for s in result.sessions.values()
                                         if s["decision"] == "quarantined"),
        },
        "records": records_out,
        "relations": relations_out,
        "sessions": sessions_out,
    }


def build_source_map(result: CleanResult) -> dict:
    """Provenance for every exported endpoint, including all re-send locations."""
    out = {}
    for ep in result.endpoints:
        # find the record behind this endpoint via session/kind
        rid = next(iter(
            r for r in result.loaded.records
            if r.type == DOOR and r.device == ep["device"]
            and r.session_id == ep["session_id"] and r.kind == ep["kind"]
        )).record_id
        prov = dict(result.sources[rid])
        prov["endpoint"] = dict(ep)
        out[rid] = prov
    return out


def explain_record(result: CleanResult, record_id: str) -> dict:
    """Show candidates, connected constraints and a joint instance per option.

    For every still-feasible candidate, the returned ``instance`` assigns a UTC
    time to *all* records in the relation connected component simultaneously,
    so the explanation can never combine mutually contradictory local choices.
    """
    if record_id not in result.solved.variables:
        raise KeyError(f"unknown record_id {record_id!r}")
    loaded, solved = result.loaded, result.solved
    sv = solved.variables[record_id]
    rec = loaded.by_id[record_id]
    touching = [
        {"kind": rel.kind, "left": rel.left_id, "right": rel.right_id,
         "detail": rel.detail}
        for rel in solved.relations
        if record_id in (rel.left_id, rel.right_id)
    ]
    cid = solved.connected[record_id]
    component = list(solved.comp_members[cid])
    candidates = []
    for ms in sv.candidates:
        feasible = ms in sv.feasible
        instance = None
        if feasible:
            instance = explain_candidate(solved, solved.solver, record_id, ms)
        candidates.append({
            "utc_ms": ms,
            "feasible": feasible,
            "joint_instance": instance,
        })
    return {
        "record_id": record_id,
        "stream": rec.stream,
        "seq": rec.seq,
        "type": rec.type,
        "raw": {"local": rec.local} if rec.local is not None else {"utc_ms": rec.utc_ms},
        "initial_candidates": list(sv.candidates),
        "feasible_candidates": list(sv.feasible),
        "determined": sv.determined,
        "decision": result.decisions.get(record_id),
        "decision_reason": result.decision_reasons.get(record_id),
        "connected_relations": touching,
        "connected_component": component,
        "candidates_explained": candidates,
    }


# ------------------------------------------------------------- file delivery --

BATCH_FILE = "endpoints_batch.json"
REPORT_FILE = "report.json"
SOURCES_FILE = "sources.json"


def run_import(manifest_path, out_dir) -> CleanResult:
    """Validate + analyze fully, then deliver batch/report/sources atomically.

    An existing ``out_dir`` is refused; on failure no directory that could be
    mistaken for a successful result is left behind.
    """
    out_dir = os.path.abspath(os.fspath(out_dir))
    if os.path.exists(out_dir):
        raise ClockImportError(f"output directory already exists, refusing to overwrite: {out_dir}")

    # full validation and analysis happen before any directory is created
    result = clean_manifest(manifest_path)
    payload = {
        BATCH_FILE: {"endpoints": list(result.endpoints)},
        REPORT_FILE: build_report(result),
        SOURCES_FILE: build_source_map(result),
    }

    parent = os.path.dirname(out_dir) or "."
    if not os.path.isdir(parent):
        raise ClockImportError(f"output parent directory does not exist: {parent}")
    tmp = tempfile.mkdtemp(prefix=".clockclean-", dir=parent)
    try:
        for name, obj in payload.items():
            with open(os.path.join(tmp, name), "w", encoding="utf-8") as fh:
                json.dump(obj, fh, indent=2, ensure_ascii=False)
                fh.write("\n")
        os.replace(tmp, out_dir)
    except BaseException:
        _safe_rmtree(tmp)
        raise
    return result


def _safe_rmtree(path):
    import shutil
    shutil.rmtree(path, ignore_errors=True)
