"""Rendering: stdout table and web/data.json. No recomputation happens here."""

from __future__ import annotations

import json
import math

from .engine import CandidateReport, Report


def format_ratio(ratio: float) -> str:
    return "inf" if math.isinf(ratio) else f"{ratio:.2f}"


def format_suggestion(suggestion: dict) -> str:
    kind = suggestion["type"]
    if kind == "keep":
        return "keep"
    if kind == "salt":
        return f"salt:{suggestion['value']}:{suggestion['salt_count']}"
    target = suggestion["target"]
    return f"{kind}:{target if target is not None else '-'}"


def render_stdout(report: Report) -> str:
    lines = [
        f"SAMPLE\t{report.rows_sampled}\t{report.rows_total}\t{report.shards}",
        "FIELDS\tkey\tdistinct\tmax_min_ratio\tstdev\tskewed\tsuggestion\tcounts",
    ]
    for cand in report.candidates:
        counts = ",".join(str(c) for c in cand.counts)
        lines.append(
            "KEY\t{key}\t{distinct}\t{ratio}\t{stdev:.2f}\t{skewed}\t{suggestion}\t{counts}".format(
                key=cand.key,
                distinct=cand.distinct,
                ratio=format_ratio(cand.max_min_ratio),
                stdev=cand.stdev,
                skewed="yes" if cand.skewed else "no",
                suggestion=format_suggestion(cand.suggestion),
                counts=counts,
            )
        )
    return "\n".join(lines) + "\n"


def _candidate_to_json(cand: CandidateReport) -> dict:
    ratio = None if math.isinf(cand.max_min_ratio) else cand.max_min_ratio
    suggestion = dict(cand.suggestion)
    if suggestion["type"] in ("compose", "replace") and suggestion.get("target") is None:
        suggestion["target"] = None
    return {
        "key": cand.key,
        "distinct": cand.distinct,
        "max_min_ratio": ratio,
        "stdev": cand.stdev,
        "skewed": cand.skewed,
        "counts": cand.counts,
        "overloaded": cand.overloaded,
        "suggestion": suggestion,
    }


def render_data_json(report: Report) -> str:
    payload = {
        "shards": report.shards,
        "rows_total": report.rows_total,
        "rows_sampled": report.rows_sampled,
        "candidates": [_candidate_to_json(c) for c in report.candidates],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
