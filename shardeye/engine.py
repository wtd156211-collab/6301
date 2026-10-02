"""Core evaluation engine for shard-key skew analysis.

Everything here is deterministic: no built-in hash(), no randomness.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field

EMPTY_VALUE = "∅"  # U+2205, normalized form of missing / null / "" values
SAMPLE_LIMIT = 20000

FNV_OFFSET_BASIS = 14695981039346656037
FNV_PRIME = 1099511628211
MASK64 = (1 << 64) - 1


class BadRowError(Exception):
    """Raised when an input row violates the JSONL contract."""

    def __init__(self, line_no: int, reason: str) -> None:
        super().__init__(f"line {line_no}: {reason}")
        self.line_no = line_no
        self.reason = reason


def fnv1a64(data: bytes) -> int:
    h = FNV_OFFSET_BASIS
    for b in data:
        h = ((h ^ b) * FNV_PRIME) & MASK64
    return h


def shard_of(value: str, shards: int) -> int:
    return fnv1a64(value.encode("utf-8")) % shards


def normalize(value: object) -> str:
    """Missing / null / empty-string all collapse to the empty value ∅."""
    if value is None or value == "":
        return EMPTY_VALUE
    return value  # type: ignore[return-value]


def data_string(record: dict, fields: tuple[str, ...]) -> str:
    return "|".join(normalize(record.get(name)) for name in fields)


def parse_key_spec(spec: str) -> tuple[str, ...]:
    """'a+b' -> ('a', 'b'); fields are split on '+' one level only."""
    return tuple(spec.split("+"))


def count_rows(path: str) -> int:
    """First pass: count lines. A final line without '\\n' still counts."""
    total = 0
    with open(path, "rb") as fh:
        for _ in fh:
            total += 1
    return total


def sample_stride(total: int, limit: int = SAMPLE_LIMIT) -> int:
    """Stride L so that rows i % L == 0 are sampled (plus the last row)."""
    if total <= limit:
        return 1
    return math.ceil(total / limit)


def parse_row(raw: bytes, line_no: int) -> dict:
    if raw.strip() == b"":
        raise BadRowError(line_no, "empty line")
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise BadRowError(line_no, "invalid JSON") from exc
    if not isinstance(obj, dict):
        raise BadRowError(line_no, "row is not a JSON object")
    for value in obj.values():
        if value is not None and not isinstance(value, str):
            raise BadRowError(line_no, "field value is not a string or null")
    return obj


@dataclass
class _Accumulator:
    """Per-candidate running stats over the sampled rows."""

    fields: tuple[str, ...]
    shards: int
    counts: list[int] = field(init=False)
    values: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.counts = [0] * self.shards

    def add(self, record: dict) -> None:
        value = data_string(record, self.fields)
        self.counts[shard_of(value, self.shards)] += 1
        self.values[value] = self.values.get(value, 0) + 1


@dataclass
class CandidateReport:
    key: str
    distinct: int
    max_min_ratio: float  # math.inf when the emptiest shard has 0 rows
    stdev: float
    skewed: bool
    counts: list[int]
    overloaded: list[bool]
    suggestion: dict  # {"type": ..., ...} per README contract


@dataclass
class Report:
    shards: int
    rows_total: int
    rows_sampled: int
    candidates: list[CandidateReport]


def _build_candidate(key: str, acc: _Accumulator, m: int, k: int) -> CandidateReport:
    counts = acc.counts
    distinct = len(acc.values)
    mean = m / k
    ratio = math.inf if min(counts) == 0 else round(max(counts) / min(counts), 2)
    stdev = round(math.sqrt(sum((c - mean) ** 2 for c in counts) / k), 2)
    skewed = distinct < k or 2 * k * max(counts) >= 3 * m
    overloaded = [2 * k * c >= 3 * m for c in counts]
    return CandidateReport(
        key=key,
        distinct=distinct,
        max_min_ratio=ratio,
        stdev=stdev,
        skewed=skewed,
        counts=list(counts),
        overloaded=overloaded,
        suggestion={},
    )


def _sort_key(cand: CandidateReport) -> tuple:
    # inf sorts last naturally; CLI order is the final tiebreaker via stable sort.
    return (cand.max_min_ratio, cand.stdev, -cand.distinct)


def _suggest(
    cand: CandidateReport,
    acc: _Accumulator,
    m: int,
    k: int,
    ranked: list[CandidateReport],
    cli_order: list[str],
) -> dict:
    if cand.distinct < k:
        target = next((c.key for c in ranked if c.key != cand.key), None)
        return {"type": "replace", "target": target}

    if cand.skewed:
        # Hottest value first; ties broken by UTF-8 byte order.
        hot_value, hot_count = min(
            acc.values.items(), key=lambda item: (-item[1], item[0].encode("utf-8"))
        )
        if 5 * hot_count >= m:
            return {
                "type": "salt",
                "value": hot_value,
                "salt_count": math.ceil(hot_count * k / m),
            }
        others = [c for c in ranked if c.key != cand.key]
        if others:
            best_distinct = max(c.distinct for c in others)
            for name in cli_order:
                if name == cand.key:
                    continue
                match = next(c for c in others if c.key == name)
                if match.distinct == best_distinct:
                    return {"type": "compose", "target": name}
        return {"type": "compose", "target": None}

    return {"type": "keep"}


def evaluate(path: str, key_specs: list[str], shards: int) -> Report:
    """Evaluate candidate shard keys over a JSONL file.

    Reads the file twice: once to count rows, once to sample and validate.
    Memory stays proportional to the sample size, never to rows_total.
    """
    keys = [spec.strip() for spec in key_specs]
    fields_per_key = [parse_key_spec(spec) for spec in keys]

    total = count_rows(path)
    stride = sample_stride(total)

    # Every row is parsed and validated; only sampled rows feed the stats.
    # 32M rows parse in ~25s, well inside the time budget, and memory stays
    # proportional to the sample because raw rows are never retained.
    accumulators = [_Accumulator(fields, shards) for fields in fields_per_key]
    sampled = 0
    with open(path, "rb") as fh:
        for index, raw in enumerate(fh):
            record = parse_row(raw, index + 1)
            if index % stride != 0 and index != total - 1:
                continue
            sampled += 1
            for acc in accumulators:
                acc.add(record)

    m = sampled
    candidates = [
        _build_candidate(key, acc, m, shards)
        for key, acc in zip(keys, accumulators)
    ]
    ranked = sorted(candidates, key=_sort_key)
    for cand, acc in zip(candidates, accumulators):
        cand.suggestion = _suggest(cand, acc, m, shards, ranked, keys)

    return Report(
        shards=shards,
        rows_total=total,
        rows_sampled=m,
        candidates=ranked,
    )
