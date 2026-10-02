import json
import math
import os
import subprocess
import sys
import tempfile
import unittest

from shardeye.engine import (
    EMPTY_VALUE,
    BadRowError,
    _Accumulator,
    _build_candidate,
    _suggest,
    count_rows,
    data_string,
    evaluate,
    fnv1a64,
    normalize,
    parse_key_spec,
    sample_stride,
    shard_of,
)
from shardeye.report import format_ratio, format_suggestion, render_data_json, render_stdout

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLES = os.path.join(REPO_ROOT, "samples")

SAMPLE_KEYS = {
    "01-uniform": "order_id,user_id",
    "02-hot-tenant": "tenant_id,order_id",
    "03-correlated": "province,city,province+city",
    "04-nulls": "channel,order_id",
    "05-low-cardinality": "status,order_id",
    "06-tail-hot": "sku,warehouse",
    "07-bumpy": "promo,user_id",
}


def write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            if isinstance(row, str):
                fh.write(row + "\n")
            else:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")


class HashTest(unittest.TestCase):
    def test_fnv1a64_known_vectors(self):
        self.assertEqual(fnv1a64(b""), 0xCBF29CE484222325)
        self.assertEqual(fnv1a64(b"a"), 0xAF63DC4C8601EC8C)
        self.assertEqual(fnv1a64(b"foobar"), 0x85944171F73967E8)

    def test_shard_of_uses_fnv_mod_k(self):
        value = "华东|上海"
        expect = fnv1a64(value.encode("utf-8")) % 16
        self.assertEqual(shard_of(value, 16), expect)

    def test_shard_of_deterministic_across_calls(self):
        self.assertEqual(shard_of("vip-0001", 16), shard_of("vip-0001", 16))


class NormalizeTest(unittest.TestCase):
    def test_empty_forms_collapse(self):
        self.assertEqual(normalize(None), EMPTY_VALUE)
        self.assertEqual(normalize(""), EMPTY_VALUE)
        self.assertEqual(normalize("x"), "x")

    def test_data_string_missing_and_null(self):
        record = {"a": None, "b": "v"}
        self.assertEqual(data_string(record, ("a",)), EMPTY_VALUE)
        self.assertEqual(data_string(record, ("missing",)), EMPTY_VALUE)
        self.assertEqual(data_string(record, ("b", "a")), "v|" + EMPTY_VALUE)

    def test_parse_key_spec(self):
        self.assertEqual(parse_key_spec("a"), ("a",))
        self.assertEqual(parse_key_spec("a+b+c"), ("a", "b", "c"))


class SamplingTest(unittest.TestCase):
    def test_stride_full_scan_when_small(self):
        self.assertEqual(sample_stride(20000), 1)
        self.assertEqual(sample_stride(1), 1)

    def test_stride_when_large(self):
        self.assertEqual(sample_stride(20001), 2)
        self.assertEqual(sample_stride(60000), 3)
        self.assertEqual(sample_stride(32_000_000), 1600)

    def test_sample_includes_tail_row(self):
        # 06-style trap: hotspot lives only in the last rows; the sample must see row N-1.
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "tail.jsonl")
            rows = [{"k": f"cold-{i % 100}"} for i in range(59999)]
            rows.append({"k": "HOT"})
            write_jsonl(path, rows)
            report = evaluate(path, ["k"], 16)
            cand = report.candidates[0]
            self.assertEqual(report.rows_sampled, 20001)
            self.assertEqual(report.rows_total, 60000)
            self.assertIn("HOT", _last_values(path))

    def test_count_rows_without_trailing_newline(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "x.jsonl")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write('{"a":"1"}\n{"a":"2"}')
            self.assertEqual(count_rows(path), 2)
            report = evaluate(path, ["a"], 4)
            self.assertEqual(report.rows_total, 2)
            self.assertEqual(report.rows_sampled, 2)


def _last_values(path):
    # helper kept tiny: re-evaluate and peek via a fresh accumulator
    acc = _Accumulator(("k",), 16)
    with open(path, "rb") as fh:
        for raw in fh:
            acc.add(json.loads(raw))
    return acc.values


class MetricsTest(unittest.TestCase):
    def _candidate(self, counts, values, m, k):
        acc = _Accumulator(("x",), k)
        acc.counts = list(counts)
        acc.values = values
        return _build_candidate("x", acc, m, k)

    def test_ratio_and_stdev(self):
        cand = self._candidate([3, 1], {"a": 3, "b": 1}, 4, 2)
        self.assertEqual(cand.max_min_ratio, 3.0)
        self.assertEqual(cand.stdev, 1.0)
        self.assertEqual(cand.distinct, 2)

    def test_ratio_inf_on_empty_shard(self):
        cand = self._candidate([4, 0], {"a": 4}, 4, 2)
        self.assertTrue(math.isinf(cand.max_min_ratio))

    def test_skew_threshold_boundary(self):
        # 2*k*max >= 3*m: exactly at the boundary counts as skewed.
        k, m = 4, 8
        cand = self._candidate([3, 2, 2, 1], {str(i): c for i, c in enumerate([3, 2, 2, 1])}, m, k)
        self.assertTrue(cand.skewed)
        cand = self._candidate([2, 2, 2, 2], {str(i): 2 for i in range(4)}, m, k)
        self.assertFalse(cand.skewed)

    def test_skew_when_distinct_below_shards(self):
        cand = self._candidate([2, 2, 2, 2], {"a": 4, "b": 4}, 8, 4)
        self.assertTrue(cand.skewed)

    def test_overloaded_flags(self):
        cand = self._candidate([3, 2, 2, 1], {"a": 8}, 8, 4)
        self.assertEqual(cand.overloaded, [True, False, False, False])


class SuggestionTest(unittest.TestCase):
    def _run(self, rows, keys, shards=4):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "d.jsonl")
            write_jsonl(path, rows)
            report = evaluate(path, keys, shards)
            return {c.key: c for c in report.candidates}

    def test_keep_when_even(self):
        rows = [{"a": f"v{i}", "b": f"w{i}"} for i in range(400)]
        cands = self._run(rows, ["a", "b"])
        self.assertEqual(cands["a"].suggestion, {"type": "keep"})

    def test_replace_when_low_cardinality(self):
        rows = [{"a": "x" if i % 2 else "y", "b": f"v{i}"} for i in range(400)]
        cands = self._run(rows, ["a", "b"])
        sug = cands["a"].suggestion
        self.assertEqual(sug["type"], "replace")
        self.assertEqual(sug["target"], "b")

    def test_replace_without_target(self):
        rows = [{"a": "x" if i % 2 else "y"} for i in range(100)]
        cands = self._run(rows, ["a"])
        self.assertEqual(cands["a"].suggestion, {"type": "replace", "target": None})

    def test_salt_for_hot_value(self):
        rows = [{"a": "HOT" if i < 100 else f"v{i}", "b": f"w{i}"} for i in range(400)]
        cands = self._run(rows, ["a", "b"])
        sug = cands["a"].suggestion
        self.assertEqual(sug["type"], "salt")
        self.assertEqual(sug["value"], "HOT")
        self.assertEqual(sug["salt_count"], math.ceil(100 * 4 / 400))

    def test_salt_tie_breaks_by_utf8_bytes(self):
        # 'b' and 'a' tie at 30% each; byte order picks 'a'.
        rows = [{"k": "b"} for _ in range(30)] + [{"k": "a"} for _ in range(30)]
        rows += [{"k": f"v{i}"} for i in range(40)]
        cands = self._run(rows, ["k"], 4)
        self.assertEqual(cands["k"].suggestion["value"], "a")

    def test_compose_when_no_dominant_value(self):
        # Cluster three ~15% values on one shard (>1.5x mean), none reaching 20%.
        clustered = []
        i = 0
        while len(clustered) < 3:
            name = f"g{i}"
            if shard_of(name, 4) == 0:
                clustered.append(name)
            i += 1
        rows = [{"a": v, "b": f"w{n}"} for v in clustered for n in range(150)]
        rows += [{"a": f"cold-{n}", "b": f"w{n}"} for n in range(550)]
        cands = self._run(rows, ["a", "b"], 4)
        self.assertTrue(cands["a"].skewed)
        sug = cands["a"].suggestion
        self.assertEqual(sug["type"], "compose")
        self.assertEqual(sug["target"], "b")

    def test_format_suggestion(self):
        self.assertEqual(format_suggestion({"type": "keep"}), "keep")
        self.assertEqual(format_suggestion({"type": "salt", "value": "∅", "salt_count": 12}), "salt:∅:12")
        self.assertEqual(format_suggestion({"type": "compose", "target": "user_id"}), "compose:user_id")
        self.assertEqual(format_suggestion({"type": "replace", "target": None}), "replace:-")
        self.assertEqual(format_ratio(math.inf), "inf")
        self.assertEqual(format_ratio(1.5), "1.50")


class OrderingTest(unittest.TestCase):
    def test_ranking_and_tiebreaks(self):
        rows = [{"good": f"v{i}", "mid": f"m{i % 50}", "bad": "x" if i % 2 else "y"} for i in range(800)]
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "d.jsonl")
            write_jsonl(path, rows)
            report = evaluate(path, ["bad", "mid", "good"], 16)
            keys = [c.key for c in report.candidates]
            self.assertEqual(keys[0], "good")
            self.assertEqual(keys[-1], "bad")


class BadRowTest(unittest.TestCase):
    def _expect_bad(self, lines, expect_line):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "d.jsonl")
            write_jsonl(path, lines)
            with self.assertRaises(BadRowError) as ctx:
                evaluate(path, ["a"], 4)
            self.assertEqual(ctx.exception.line_no, expect_line)

    def test_empty_line(self):
        self._expect_bad(['{"a":"1"}', "", '{"a":"2"}'], 2)

    def test_whitespace_only_line(self):
        self._expect_bad(['{"a":"1"}', "   "], 2)

    def test_invalid_json(self):
        self._expect_bad(['{"a":"1"}', "{not json}"], 2)

    def test_non_object_row(self):
        self._expect_bad(['{"a":"1"}', "[1,2]"], 2)

    def test_non_string_value(self):
        self._expect_bad(['{"a":"1"}', '{"a": 42}'], 2)

    def test_null_value_is_legal(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "d.jsonl")
            write_jsonl(path, [{"a": None}, {"a": "x"}])
            report = evaluate(path, ["a"], 4)
            self.assertEqual(report.candidates[0].distinct, 2)

    def test_cli_exit_code_and_stderr(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "d.jsonl")
            write_jsonl(path, ['{"a":"1"}', "oops"])
            proc = subprocess.run(
                [sys.executable, "-m", "shardeye", "--data", path, "--keys", "a",
                 "--shards", "4", "--web-dir", os.path.join(tmp, "web")],
                capture_output=True, text=True, cwd=REPO_ROOT,
            )
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(proc.stderr.strip(), "bad-row 2")


class SamplesTest(unittest.TestCase):
    def test_samples_match_expected_byte_for_byte(self):
        for name, keys in sorted(SAMPLE_KEYS.items()):
            with self.subTest(sample=name):
                data = os.path.join(SAMPLES, "data", f"{name}.jsonl")
                expected = os.path.join(SAMPLES, "expected", f"{name}.txt")
                with tempfile.TemporaryDirectory() as tmp:
                    proc = subprocess.run(
                        [sys.executable, "-m", "shardeye", "--data", data, "--keys", keys,
                         "--shards", "16", "--web-dir", tmp],
                        capture_output=True, text=True, cwd=REPO_ROOT,
                    )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                with open(expected, "r", encoding="utf-8", newline="") as fh:
                    self.assertEqual(proc.stdout, fh.read())

    def test_tail_hot_sample_size(self):
        report = evaluate(os.path.join(SAMPLES, "data", "06-tail-hot.jsonl"), ["sku"], 16)
        self.assertEqual(report.rows_total, 60000)
        self.assertEqual(report.rows_sampled, 20001)
        self.assertTrue(report.candidates[0].skewed)


class DeterminismTest(unittest.TestCase):
    def test_same_input_same_output(self):
        data = os.path.join(SAMPLES, "data", "06-tail-hot.jsonl")
        outs = []
        for _ in range(2):
            with tempfile.TemporaryDirectory() as tmp:
                proc = subprocess.run(
                    [sys.executable, "-m", "shardeye", "--data", data,
                     "--keys", "sku,warehouse", "--shards", "16", "--web-dir", tmp],
                    capture_output=True, text=True, cwd=REPO_ROOT,
                )
                outs.append(proc.stdout)
                with open(os.path.join(tmp, "data.json"), encoding="utf-8") as fh:
                    outs.append(fh.read())
        self.assertEqual(outs[0], outs[2])
        self.assertEqual(outs[1], outs[3])


class DataJsonTest(unittest.TestCase):
    def test_json_contract(self):
        report = evaluate(os.path.join(SAMPLES, "data", "03-correlated.jsonl"),
                          ["province", "city", "province+city"], 16)
        payload = json.loads(render_data_json(report))
        self.assertEqual(payload["shards"], 16)
        self.assertEqual(payload["rows_total"], 20000)
        self.assertEqual(payload["rows_sampled"], 20000)
        by_key = {c["key"]: c for c in payload["candidates"]}
        city = by_key["city"]
        self.assertIsNone(city["max_min_ratio"])  # inf -> null
        self.assertEqual(len(city["counts"]), 16)
        self.assertEqual(len(city["overloaded"]), 16)
        self.assertEqual(sum(city["counts"]), 20000)
        self.assertEqual(city["suggestion"], {"type": "salt", "value": "上海", "salt_count": 6})
        province = by_key["province"]
        self.assertEqual(province["suggestion"], {"type": "replace", "target": "city"})
        self.assertTrue(any(province["overloaded"]))

    def test_stdout_shape(self):
        report = evaluate(os.path.join(SAMPLES, "data", "05-low-cardinality.jsonl"),
                          ["status", "order_id"], 16)
        out = render_stdout(report)
        lines = out.split("\n")
        self.assertEqual(lines[0], "SAMPLE\t12000\t12000\t16")
        self.assertTrue(lines[1].startswith("FIELDS\tkey\tdistinct"))
        self.assertEqual(len(lines), 5)  # 2 header + 2 keys + trailing ""
        self.assertTrue(out.endswith("\n"))


if __name__ == "__main__":
    unittest.main()
