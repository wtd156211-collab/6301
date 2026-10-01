"""分片键评估引擎：抽样、分片统计、倾斜判定与改进建议。

口径全部以 README.md 为准，阈值写死，不含任何随机源，同一份数据
重复运行得到逐字节一致的结果。
"""

import json
import math
import os

FNV_OFFSET_BASIS = 14695981039346656037
FNV_PRIME = 1099511628211
FNV_MASK = (1 << 64) - 1

EMPTY = "\u2205"  # ∅：缺失 / null / 空串统一记为空值
SAMPLE_LIMIT = 20000

HOT_VALUE_RATIO = 1.0 / 5.0  # 单值占样本的比例阈值


class BadRow(Exception):
    """输入行非法：空行、非法 JSON 或字段值不是字符串/null。"""

    def __init__(self, lineno):
        super().__init__(f"bad-row {lineno}")
        self.lineno = lineno


class KeySpec:
    """候选键：命令行名（如 ``a+b``）与字段顺序。"""

    __slots__ = ("name", "fields", "cli_index")

    def __init__(self, name, fields, cli_index):
        self.name = name
        self.fields = fields
        self.cli_index = cli_index


def parse_keys(keys_arg):
    """把 ``a+b,c`` 解析成候选键列表，逗号、加号只切一层。"""
    specs = []
    for index, raw in enumerate(keys_arg.split(",")):
        name = raw.strip()
        if not name:
            raise ValueError("empty key in --keys")
        fields = name.split("+")
        if any(part == "" for part in fields):
            raise ValueError(f"bad key: {name!r}")
        specs.append(KeySpec(name, fields, index))
    if not specs:
        raise ValueError("no candidate keys")
    return specs


def fnv1a64(text):
    """FNV-1a 64 位哈希，输入为文本，内部按 UTF-8 字节计算。"""
    h = FNV_OFFSET_BASIS
    for b in text.encode("utf-8"):
        h = ((h ^ b) * FNV_PRIME) & FNV_MASK
    return h


class BadRowValueType(Exception):
    pass


def data_string(row, fields):
    """按字段顺序取值并用 ``|`` 拼接，缺失字段视为 ∅。"""
    parts = []
    for field in fields:
        value = row.get(field, EMPTY)
        if value is None:
            parts.append(EMPTY)
        elif not isinstance(value, str):
            raise BadRowValueType
        elif value == "":
            parts.append(EMPTY)
        else:
            parts.append(value)
    return "|".join(parts)


def count_rows(path):
    """第一遍顺序读：只数行数。二进制迭代不会把结尾换行后的空串当一行。"""
    total = 0
    with open(path, "rb") as handle:
        for _ in handle:
            total += 1
    return total


def sample_indices(total, limit=SAMPLE_LIMIT):
    """返回 (是否全量, 步长)；采样规则见 README。

    N <= S 时全量；否则步长 L=ceil(N/S)，取 i mod L == 0 的行，
    再固定补最后一行（已在步长序列中则不重复）。
    """
    if total <= limit:
        return True, 1
    return False, math.ceil(total / limit)


class _Accumulator:
    __slots__ = ("spec", "counts", "values")

    def __init__(self, spec, shards):
        self.spec = spec
        self.counts = [0] * shards
        self.values = {}

    def add(self, text, shard):
        self.counts[shard] += 1
        self.values[text] = self.values.get(text, 0) + 1


def _scan(path, specs, shards):
    """读文件并统计。第一遍数行，第二遍校验全部行、只累计样本行。"""
    total = count_rows(path)
    full, step = sample_indices(total)

    accs = [_Accumulator(spec, shards) for spec in specs]
    sampled = 0
    with open(path, "rb") as handle:
        for lineno, raw in enumerate(handle):
            in_sample = full or lineno % step == 0 or lineno == total - 1
            text = raw.decode("utf-8")
            if text.endswith("\n"):
                text = text[:-1]
            if text.endswith("\r"):
                text = text[:-1]
            if text == "":
                raise BadRow(lineno)
            try:
                row = json.loads(text)
            except json.JSONDecodeError:
                raise BadRow(lineno)
            if not isinstance(row, dict):
                raise BadRow(lineno)
            for value in row.values():
                if value is not None and not isinstance(value, str):
                    raise BadRow(lineno)
            if not in_sample:
                continue
            sampled += 1
            for acc in accs:
                key_text = data_string(row, acc.spec.fields)
                shard = fnv1a64(key_text) % shards
                acc.add(key_text, shard)
    return total, sampled, accs


def _metrics(acc, shards, sampled):
    counts = acc.counts
    maximum = max(counts)
    minimum = min(counts)
    ratio = None if minimum == 0 else round(maximum / minimum, 2)
    mean = sampled / shards
    variance = sum((c - mean) ** 2 for c in counts) / shards
    stdev = round(math.sqrt(variance), 2)
    distinct = len(acc.values)
    skewed = distinct < shards or 2 * shards * maximum >= 3 * sampled
    overloaded = [2 * shards * c >= 3 * sampled for c in counts]
    return {
        "key": acc.spec.name,
        "cli_index": acc.spec.cli_index,
        "distinct": distinct,
        "max_min_ratio": ratio,
        "stdev": stdev,
        "skewed": skewed,
        "counts": counts,
        "overloaded": overloaded,
        "_values": acc.values,
    }


def _sort_key(result):
    ratio = result["max_min_ratio"]
    return (
        1 if ratio is None else 0,
        math.inf if ratio is None else ratio,
        result["stdev"],
        -result["distinct"],
        result["cli_index"],
    )


def _salt_pick(values, sampled):
    """挑占比最高的取值（>=1/5 才调用）；并列取 UTF-8 字节序最小者。"""
    hot = [(text, count) for text, count in values.items() if count * 5 >= sampled]
    hot.sort(key=lambda item: (-item[1], item[0].encode("utf-8")))
    return hot[0]


def _build_suggestions(results, shards, sampled):
    """建议规则按 README 的 1→4 顺序命中即停。results 已排序。"""
    for result in results:
        values = result.pop("_values")
        distinct = result["distinct"]
        if distinct < shards:
            target = next(
                (other["key"] for other in results if other["key"] != result["key"]),
                None,
            )
            result["suggestion"] = {"type": "replace", "target": target}
            continue
        hot_text = None
        if any(count * 5 >= sampled for count in values.values()):
            hot_text, hot_count = _salt_pick(values, sampled)
            salt_count = math.ceil(hot_count * shards / sampled)
            result["suggestion"] = {
                "type": "salt",
                "value": hot_text,
                "salt_count": salt_count,
            }
            continue
        if result["skewed"]:
            others = [
                other for other in results if other["key"] != result["key"]
            ]
            if others:
                target = max(
                    others,
                    key=lambda other: (other["distinct"], -other["cli_index"]),
                )["key"]
            else:
                target = None
            result["suggestion"] = {"type": "compose", "target": target}
            continue
        result["suggestion"] = {"type": "keep", "target": None}


def evaluate(path, specs, shards):
    """评估入口：返回包含全部候选结果（已排序）的报告字典。"""
    if shards <= 0:
        raise ValueError("shards must be positive")
    total, sampled, accs = _scan(path, specs, shards)
    results = [_metrics(acc, shards, sampled) for acc in accs]
    results.sort(key=_sort_key)
    _build_suggestions(results, shards, sampled)
    return {
        "shards": shards,
        "rows_total": total,
        "rows_sampled": sampled,
        "candidates": results,
    }


def format_ratio(ratio):
    return "inf" if ratio is None else f"{ratio:.2f}"


def suggestion_text(suggestion):
    kind = suggestion["type"]
    if kind == "keep":
        return "keep"
    if kind == "salt":
        return f"salt:{suggestion['value']}:{suggestion['salt_count']}"
    target = suggestion["target"]
    return f"{kind}:{target if target is not None else '-'}"


def render_stdout(report):
    """渲染验收用的制表符分隔 stdout 文本（LF 结尾）。"""
    lines = [
        f"SAMPLE\t{report['rows_sampled']}\t{report['rows_total']}\t{report['shards']}",
        "FIELDS\tkey\tdistinct\tmax_min_ratio\tstdev\tskewed\tsuggestion\tcounts",
    ]
    for result in report["candidates"]:
        lines.append(
            "\t".join(
                (
                    "KEY",
                    result["key"],
                    str(result["distinct"]),
                    format_ratio(result["max_min_ratio"]),
                    f"{result['stdev']:.2f}",
                    "yes" if result["skewed"] else "no",
                    suggestion_text(result["suggestion"]),
                    ",".join(str(c) for c in result["counts"]),
                )
            )
        )
    return "\n".join(lines) + "\n"


def write_web_data(report, web_dir):
    """把同一份引擎结果写成 web/data.json，页面不重算任何数字。"""
    os.makedirs(web_dir, exist_ok=True)
    candidates = []
    for result in report["candidates"]:
        candidates.append(
            {
                "key": result["key"],
                "distinct": result["distinct"],
                "max_min_ratio": result["max_min_ratio"],
                "stdev": result["stdev"],
                "skewed": result["skewed"],
                "counts": result["counts"],
                "overloaded": result["overloaded"],
                "suggestion": result["suggestion"],
            }
        )
    payload = {
        "shards": report["shards"],
        "rows_total": report["rows_total"],
        "rows_sampled": report["rows_sampled"],
        "candidates": candidates,
    }
    path = os.path.join(web_dir, "data.json")
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
        handle.write("\n")
    return path
