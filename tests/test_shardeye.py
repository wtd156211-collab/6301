import json
import math
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shardeye import core
from shardeye.__main__ import main

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SAMPLE_CASES = {
    "01-uniform": "order_id,user_id",
    "02-hot-tenant": "tenant_id,order_id",
    "03-correlated": "province,city,province+city",
    "04-nulls": "channel,order_id",
    "05-low-cardinality": "status,order_id",
    "06-tail-hot": "sku,warehouse",
    "07-bumpy": "promo,user_id",
}


def write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            if isinstance(row, str):
                handle.write(row + "\n")
            else:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")


class HashTest(unittest.TestCase):
    def test_fnv1a64_known_vectors(self):
        self.assertEqual(core.fnv1a64(""), 14695981039346656037)
        self.assertEqual(core.fnv1a64("a"), 12638187200555641996)
        self.assertEqual(core.fnv1a64("foobar"), 0x85944171F73967E8)

    def test_fnv1a64_utf8_and_deterministic(self):
        self.assertEqual(core.fnv1a64("华东|上海"), core.fnv1a64("华东|上海"))
        self.assertNotEqual(core.fnv1a64("a|b"), core.fnv1a64("a|b "))


class NormalizeTest(unittest.TestCase):
    def test_missing_null_empty_become_empty_mark(self):
        row = {"a": None, "b": "", "c": "x"}
        self.assertEqual(core.data_string(row, ["a"]), core.EMPTY)
        self.assertEqual(core.data_string(row, ["b"]), core.EMPTY)
        self.assertEqual(core.data_string(row, ["zzz"]), core.EMPTY)
        self.assertEqual(core.data_string(row, ["c"]), "x")

    def test_composite_joins_with_pipe_in_field_order(self):
        row = {"a": "1", "b": "2"}
        self.assertEqual(core.data_string(row, ["a", "b"]), "1|2")
        self.assertEqual(core.data_string(row, ["b", "a"]), "2|1")
        self.assertEqual(core.data_string(row, ["a", "zzz"]), "1|" + core.EMPTY)

    def test_parse_keys(self):
        specs = core.parse_keys(" user_id , province+city ")
        self.assertEqual([s.name for s in specs], ["user_id", "province+city"])
        self.assertEqual(specs[1].fields, ["province", "city"])
        with self.assertRaises(ValueError):
            core.parse_keys("a++b")
        with self.assertRaises(ValueError):
            core.parse_keys("")


class SamplingTest(unittest.TestCase):
    def test_full_scan_at_limit(self):
        self.assertEqual(core.sample_indices(20000), (True, 1))
        self.assertEqual(core.sample_indices(1), (True, 1))

    def test_step_and_tail_row(self):
        full, step = core.sample_indices(60000)
        self.assertFalse(full)
        self.assertEqual(step, 3)
        picked = [i for i in range(60000) if i % step == 0 or i == 59999]
        self.assertEqual(len(picked), 20001)
        self.assertEqual(picked[-1], 59999)

    def test_tail_row_not_duplicated(self):
        full, step = core.sample_indices(20001)
        self.assertEqual(step, 2)
        picked = [i for i in range(20001) if i % step == 0 or i == 20000]
        self.assertEqual(len(picked), 10001)


class MetricsTest(unittest.TestCase):
    def _result(self, counts, values, sampled):
        spec = core.KeySpec("k", ["k"], 0)
        acc = core._Accumulator(spec, len(counts))
        acc.counts = list(counts)
        acc.values = values
        return core._metrics(acc, len(counts), sampled)

    def test_ratio_and_stdev(self):
        result = self._result([3, 1], {"a": 2, "b": 2}, 4)
        self.assertEqual(result["max_min_ratio"], 3.0)
        self.assertEqual(result["stdev"], 1.0)
        self.assertEqual(result["distinct"], 2)

    def test_empty_shard_gives_inf_and_overload_flags(self):
        result = self._result([4, 0], {"a": 4}, 4)
        self.assertIsNone(result["max_min_ratio"])
        self.assertTrue(result["skewed"])  # distinct < k
        self.assertEqual(result["overloaded"], [True, False])

    def test_skew_threshold_boundary(self):
        # 2*k*max == 3*m 恰好命中阈值即判倾斜：16*3 == 3*16
        values = {str(i): c for i, c in enumerate([3, 3, 2, 2, 2, 2, 1, 1])}
        result = self._result([3, 3, 2, 2, 2, 2, 1, 1], values, 16)
        self.assertTrue(result["skewed"])
        result = self._result([2] * 8, {str(i): 2 for i in range(8)}, 16)
        self.assertFalse(result["skewed"])


class SortTest(unittest.TestCase):
    def test_inf_ratio_sorts_last_then_stdev_then_distinct_then_cli(self):
        def entry(ratio, stdev, distinct, cli):
            return {
                "key": f"k{cli}",
                "cli_index": cli,
                "distinct": distinct,
                "max_min_ratio": ratio,
                "stdev": stdev,
            }

        items = [
            entry(None, 9.0, 5, 0),
            entry(2.0, 1.0, 5, 1),
            entry(None, 1.0, 5, 2),
            entry(1.5, 1.0, 3, 3),
            entry(1.5, 1.0, 9, 4),
        ]
        ordered = sorted(items, key=core._sort_key)
        self.assertEqual([x["cli_index"] for x in ordered], [4, 3, 1, 2, 0])


class SuggestionTest(unittest.TestCase):
    def _evaluate(self, rows, keys, shards=4):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "data.jsonl")
            write_jsonl(path, rows)
            specs = core.parse_keys(keys)
            return core.evaluate(path, specs, shards)

    def test_keep_when_uniform(self):
        rows = [{"k": f"v{i}", "u": f"w{i}"} for i in range(400)]
        report = self._evaluate(rows, "k,u")
        self.assertEqual(
            [c["suggestion"]["type"] for c in report["candidates"]],
            ["keep", "keep"],
        )

    def test_salt_picks_hottest_value_and_ceil_salt_count(self):
        rows = [{"k": "hot"}] * 5 + [{"k": f"c{i}"} for i in range(15)]
        report = self._evaluate(rows, "k")
        cand = report["candidates"][0]
        self.assertEqual(
            cand["suggestion"], {"type": "salt", "value": "hot", "salt_count": 1}
        )

    def test_salt_tie_breaks_by_utf8_byte_order(self):
        rows = [{"k": "b"}] * 3 + [{"k": "a"}] * 3 + [{"k": f"c{i}"} for i in range(4)]
        report = self._evaluate(rows, "k")
        cand = report["candidates"][0]
        self.assertEqual(cand["suggestion"]["type"], "salt")
        self.assertEqual(cand["suggestion"]["value"], "a")
        self.assertEqual(cand["suggestion"]["salt_count"], math.ceil(3 * 4 / 10))

    def test_replace_targets_first_other_candidate_in_sorted_order(self):
        rows = [{"low": "x", "high": f"h{i % 100}"} for i in range(400)]
        report = self._evaluate(rows, "low,high", shards=4)
        by_key = {c["key"]: c for c in report["candidates"]}
        self.assertEqual(by_key["low"]["suggestion"], {"type": "replace", "target": "high"})
        self.assertEqual(report["candidates"][0]["key"], "high")

    def test_replace_without_target_uses_dash(self):
        rows = [{"low": "x"} for _ in range(10)]
        report = self._evaluate(rows, "low", shards=4)
        cand = report["candidates"][0]
        self.assertEqual(cand["suggestion"], {"type": "replace", "target": None})
        self.assertEqual(core.suggestion_text(cand["suggestion"]), "replace:-")

    def test_compose_targets_max_distinct_other(self):
        # 直接构造结果：倾斜、无占比 >=1/5 的取值 -> compose，取 distinct 最大者
        def entry(name, cli, distinct, stdev, skewed, values):
            return {
                "key": name, "cli_index": cli, "distinct": distinct,
                "max_min_ratio": 2.0, "stdev": stdev, "skewed": skewed,
                "counts": [0] * 4, "overloaded": [skewed] + [False] * 3,
                "_values": values,
            }

        values = {f"v{i}": 11 for i in range(5)}  # 11/60 < 1/5，无热点取值
        results = [
            entry("g", 0, 5, 9.0, True, values),
            entry("u2", 1, 6, 1.0, False, {}),
            entry("u1", 2, 50, 1.0, False, {}),
        ]
        core._build_suggestions(results, 4, 60)
        self.assertEqual(results[0]["suggestion"],
                         {"type": "compose", "target": "u1"})

    def test_compose_tie_target_by_cli_order(self):
        def entry(name, cli, distinct):
            return {
                "key": name, "cli_index": cli, "distinct": distinct,
                "max_min_ratio": 2.0, "stdev": 1.0, "skewed": True,
                "counts": [0] * 4, "overloaded": [True] + [False] * 3,
                "_values": {f"v{i}": 11 for i in range(5)},
            }

        results = [entry("g", 0, 5), entry("a", 1, 9), entry("b", 2, 9)]
        core._build_suggestions(results, 4, 60)
        self.assertEqual(results[0]["suggestion"],
                         {"type": "compose", "target": "a"})


class BadRowTest(unittest.TestCase):
    def _run(self, rows):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "data.jsonl")
            write_jsonl(path, rows)
            with self.assertRaises(core.BadRow) as ctx:
                core.evaluate(path, core.parse_keys("k"), 4)
            return ctx.exception.lineno

    def test_empty_line(self):
        self.assertEqual(self._run(['{"k":"a"}', ""]), 1)

    def test_invalid_json(self):
        self.assertEqual(self._run(["not json"]), 0)

    def test_non_string_value(self):
        self.assertEqual(self._run(['{"k":1}']), 0)
        self.assertEqual(self._run(['{"k":{"x":"y"}}']), 0)

    def test_non_object_json(self):
        self.assertEqual(self._run(["[1,2]"]), 0)

    def test_bad_row_in_unsampled_line_still_rejected(self):
        rows = ['{"k":"v%d"}' % i for i in range(40000)] + ['{"k":9}']
        self.assertEqual(self._run(rows), 40000)

    def test_cli_exit_code_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "bad.jsonl")
            write_jsonl(path, ['{"k":1}'])
            self.assertEqual(
                main(["--data", path, "--keys", "k", "--shards", "4",
                      "--web-dir", os.path.join(tmp, "web")]),
                2,
            )


class GoldenSampleTest(unittest.TestCase):
    def test_all_samples_match_expected_byte_for_byte(self):
        for name, keys in sorted(SAMPLE_CASES.items()):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                proc = subprocess.run(
                    [sys.executable, "-m", "shardeye",
                     "--data", os.path.join(ROOT, "samples", "data", f"{name}.jsonl"),
                     "--keys", keys, "--shards", "16",
                     "--web-dir", os.path.join(tmp, "web")],
                    capture_output=True, cwd=ROOT,
                )
                self.assertEqual(proc.returncode, 0, proc.stderr.decode())
                expected = open(
                    os.path.join(ROOT, "samples", "expected", f"{name}.txt"), "rb"
                ).read()
                self.assertEqual(proc.stdout, expected)

    def test_deterministic_across_runs(self):
        data = os.path.join(ROOT, "samples", "data", "06-tail-hot.jsonl")
        with tempfile.TemporaryDirectory() as tmp:
            report1 = core.evaluate(data, core.parse_keys("sku,warehouse"), 16)
            report2 = core.evaluate(data, core.parse_keys("sku,warehouse"), 16)
            self.assertEqual(core.render_stdout(report1), core.render_stdout(report2))
            path = core.write_web_data(report1, tmp)
            payload = json.load(open(path, encoding="utf-8"))
            self.assertEqual(payload["rows_total"], 60000)
            self.assertEqual(payload["rows_sampled"], 20001)
            self.assertEqual(payload["shards"], 16)
            sku = next(c for c in payload["candidates"] if c["key"] == "sku")
            self.assertEqual(
                sku["suggestion"],
                {"type": "salt", "value": "sku-HOT", "salt_count": 5},
            )
            self.assertEqual(len(sku["counts"]), 16)
            self.assertEqual(len(sku["overloaded"]), 16)
            self.assertTrue(any(sku["overloaded"]))


if __name__ == "__main__":
    unittest.main()
