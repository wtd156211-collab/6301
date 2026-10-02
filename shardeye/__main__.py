"""CLI entry: python -m shardeye --data FILE --keys a,b+c --shards 16 [--web-dir web]"""

from __future__ import annotations

import argparse
import os
import sys

from .engine import BadRowError, evaluate
from .report import render_data_json, render_stdout


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="shardeye",
        description="Evaluate candidate shard keys against a JSONL dataset.",
    )
    parser.add_argument("--data", required=True, help="path to the JSONL input file")
    parser.add_argument("--keys", required=True, help="comma-separated candidate keys; composite keys use '+'")
    parser.add_argument("--shards", type=int, required=True, help="number of shards (k)")
    parser.add_argument("--web-dir", default="web", help="directory for data.json (created if missing)")
    args = parser.parse_args(argv)

    if args.shards < 1:
        print("shards must be >= 1", file=sys.stderr)
        return 2

    key_specs = [spec.strip() for spec in args.keys.split(",") if spec.strip()]
    if not key_specs:
        print("no candidate keys given", file=sys.stderr)
        return 2

    try:
        report = evaluate(args.data, key_specs, args.shards)
    except BadRowError as exc:
        print(f"bad-row {exc.line_no}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"cannot read {args.data}: {exc}", file=sys.stderr)
        return 2

    os.makedirs(args.web_dir, exist_ok=True)
    with open(os.path.join(args.web_dir, "data.json"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(render_data_json(report))

    sys.stdout.write(render_stdout(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
