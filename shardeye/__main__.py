"""命令行入口：python -m shardeye --data <jsonl> --keys a,b+c --shards 16"""

import argparse
import sys

from .core import BadRow, evaluate, parse_keys, render_stdout, write_web_data


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="shardeye",
        description="分片键评估：统计候选键的分片分布、判定倾斜并给出建议。",
    )
    parser.add_argument("--data", required=True, help="UTF-8 JSONL 数据文件")
    parser.add_argument(
        "--keys",
        required=True,
        help="候选键，逗号分隔；组合键用 + 连接，如 user_id,province+city",
    )
    parser.add_argument("--shards", type=int, required=True, help="分片数 k")
    parser.add_argument(
        "--web-dir",
        default="web",
        help="页面数据输出目录（写 data.json），默认 web",
    )
    args = parser.parse_args(argv)

    try:
        specs = parse_keys(args.keys)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.shards <= 0:
        print("shards must be positive", file=sys.stderr)
        return 2

    try:
        report = evaluate(args.data, specs, args.shards)
    except BadRow as exc:
        print(f"bad-row {exc.lineno}", file=sys.stderr)
        return 2
    except (OSError, UnicodeDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    write_web_data(report, args.web_dir)
    sys.stdout.write(render_stdout(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
