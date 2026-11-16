"""Command line entry for local-clock cleaning (fully offline, no database).

Examples:
    python3 -m usage_intervals.clock_cli import manifest.json -o out/
    python3 -m usage_intervals.clock_cli explain manifest.json -r rec-42

Exit codes: 0 success; 2 structural / unsatisfiable / spring-gap / output-dir
error; 1 unexpected internal error.
"""

from __future__ import annotations

import argparse
import json
import sys

from .clock_clean import (
    ClockImportError,
    UnsatisfiableClockError,
    build_report,
    clean_manifest,
    explain_record,
    run_import,
)


def _fail(exc, extra=None):
    payload = {"ok": False, "error": str(exc)}
    if isinstance(exc, UnsatisfiableClockError):
        payload["error_type"] = "unsatisfiable"
        payload["records"] = exc.records
        payload["relations"] = [
            {"kind": r.kind, "left": r.left_id, "right": r.right_id,
             "detail": r.detail} for r in exc.relations
        ]
    else:
        payload["error_type"] = "structural"
    if extra:
        payload.update(extra)
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 2


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="usage_intervals.clock_clean",
        description="Offline raw local-clock cleaning for gateway JSONL exports.",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    imp = sub.add_parser("import", help="validate, disambiguate and write outputs")
    imp.add_argument("manifest")
    imp.add_argument("-o", "--out", required=True, help="output directory (must not exist)")

    ex = sub.add_parser("explain", help="explain one record's candidates and constraints")
    ex.add_argument("manifest")
    ex.add_argument("-r", "--record-id", required=True)
    ex.add_argument("--report", action="store_true",
                    help="also print the full report alongside the explanation")

    args = ap.parse_args(argv)
    try:
        if args.cmd == "import":
            result = run_import(args.manifest, args.out)
            report = build_report(result)
            print(json.dumps({"ok": True, "out_dir": args.out,
                              "summary": report["summary"]}, indent=2, ensure_ascii=False))
            return 0

        if args.cmd == "explain":
            # explain only needs in-memory analysis; never writes an output dir
            result = clean_manifest(args.manifest)
            try:
                explanation = explain_record(result, args.record_id)
            except KeyError as exc:
                return _fail(ClockImportError(str(exc)))
            payload = {"ok": True, "explanation": explanation}
            if args.report:
                payload["report"] = build_report(result)
            print(json.dumps(payload, indent=2, ensure_ascii=False))
            return 0
    except UnsatisfiableClockError as exc:
        return _fail(exc)
    except ClockImportError as exc:
        return _fail(exc)
    return 1


if __name__ == "__main__":
    sys.exit(main())
