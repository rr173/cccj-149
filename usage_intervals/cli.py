"""Command line interface for the offline usage interval processor.

Examples:
    python3 -m usage_intervals.cli validate --window 0 100 batch1.json batch2.json
    python3 -m usage_intervals.cli build    pkg1 --window 0 100 batch1.json batch2.json
    python3 -m usage_intervals.cli read     pkg1 --device dev1
    python3 -m usage_intervals.cli read     pkg1 --device dev1 --window 45 70
    python3 -m usage_intervals.cli evidence pkg1 --device dev1
    python3 -m usage_intervals.cli status   pkg1
    python3 -m usage_intervals.cli devices  pkg1
"""

from __future__ import annotations

import argparse
import json
import sys

from .engine import Processor
from .model import StructuralError


def _window(text):
    lo, hi = text
    return {"lo": int(lo), "hi": int(hi)}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="usage_intervals")
    ap.add_argument("--dsn", default=None,
                    help="libpq DSN (defaults to $UI_DSN)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("validate", help="structural input check only")
    v.add_argument("files", nargs="+")
    v.add_argument("--window", nargs=2, type=int, required=True, metavar=("LO", "HI"))
    v.add_argument("--package-id", default="adhoc")

    b = sub.add_parser("build", help="stage and publish one fixed package")
    b.add_argument("package_id")
    b.add_argument("files", nargs="+")
    b.add_argument("--window", nargs=2, type=int, required=True, metavar=("LO", "HI"))

    r = sub.add_parser("read", help="read the interval partition for a device")
    r.add_argument("package_id")
    r.add_argument("--device", required=True)
    r.add_argument("--window", nargs=2, type=int, metavar=("LO", "HI"))

    e = sub.add_parser("evidence", help="read back paired sessions/leases/coverage")
    e.add_argument("package_id")
    e.add_argument("--device", required=True)

    s = sub.add_parser("status", help="package publication status / input binding")
    s.add_argument("package_id")

    d = sub.add_parser("devices", help="list devices with published facts")
    d.add_argument("package_id")

    args = ap.parse_args(argv)
    with Processor(dsn=args.dsn) as proc:
        proc.install()
        if args.cmd == "validate":
            try:
                pkg = proc.validate_files(args.package_id, _window(args.window), args.files)
            except StructuralError as exc:
                print(json.dumps({"ok": False, "structural_error": str(exc)}))
                return 2
            print(json.dumps({"ok": True, "input_hash": pkg.input_hash,
                              "devices": pkg.devices()}, indent=2))
            return 0

        if args.cmd == "build":
            try:
                pkg = proc.validate_files(args.package_id, _window(args.window), args.files)
                meta = proc.process(pkg)
            except StructuralError as exc:
                print(json.dumps({"ok": False, "structural_error": str(exc)}))
                return 2
            print(json.dumps(meta, indent=2))
            return 0

        if args.cmd == "read":
            wlo, whi = (args.window if args.window else (None, None))
            print(json.dumps(proc.interpret(args.package_id, args.device, wlo, whi),
                             indent=2, default=str))
            return 0

        if args.cmd == "evidence":
            print(json.dumps(proc.evidence(args.package_id, args.device), indent=2,
                             default=str))
            return 0

        if args.cmd == "status":
            print(json.dumps(proc.status(args.package_id), indent=2))
            return 0

        if args.cmd == "devices":
            print(json.dumps(proc.devices(args.package_id), indent=2))
            return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
