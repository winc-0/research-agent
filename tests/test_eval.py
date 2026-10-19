"""评测套件的离线自检。

评测代码本身也是代码，判分逻辑错了会让整张表变成一堆假数字，
而且错得很安静——分数看着合理，结论全错。所以这里把每个口径都钉死：
数值容差、去重开关、引用统计、回放缺失，全部有断言。

    python -m unittest tests.test_eval -v
"""

from __future__ import annotations

import asyncio
import unittest

from app.agent.nodes.compress import make_compress
from app.agent.nodes.planner import plan_single
from app.tools.base import Evidence, ToolResult
from eval.replay import Pool
from eval.schema import body_of, load_items
from eval.scoring import (
    cite_stats,
    declared_limitation,
    fact_hits,
    numbers,
    paragraph_citation_ratio,
    route_hit,
)
from eval.variants import VARIANTS, _overrides, compress_none


class TestQASet(unittest.TestCase):
    def setUp(self):
        self.items = load_items()

    def test_layers(self):
        self.assertEqual(len(self.items), 40)
        counts = {}
        for it in self.items:
            counts[it.layer] = counts.get(it.layer, 0) + 1
        self.assertEqual(counts, {"kb": 12, "sql": 12, "multi": 8, "web": 4, "unanswerable": 4})

    def test_sql_items_mostly_numeric(self):
        """数仓题的答案基本是数字，否则「事实命中」会退化成关键词匹配。

        s05（前三品牌）这类题的答案是名称，属于合理例外，所以只要求多数题有数值。
        """
        sql_items = [it for it in self.items if it.layer == "sql"]
        numeric = [it for it in sql_items if any(f["type"] == "num" for f in it.facts)]
        self.assertGreaterEqual(len(numeric), 8, "数值型事实太少，检查题库是否写漏")
        for it in sql_items:
            self.assertTrue(it.facts, f"{it.id} 没有任何事实清单")

    def test_no_duplicate_question(self):
        qs = [i.question for i in self.items]
        self.assertEqual(len(qs), len(set(qs)))


class TestBody(unittest.TestCase):
    def test_strip_appendix(self):
        report = "# 标题\n\n正文有结论 [1]。\n\n---\n\n## 参考来源\n\n- [1] [kb] a#1\n"
        self.assertEqual(body_of(report), "# 标题\n\n正文有结论 [1]。")

    def test_empty(self):
        self.assertEqual(body_of(""), "")


class TestNumbers(unittest.TestCase):
    def test_thousands_separator(self):
        self.assertIn(2636218.99, numbers("销售额 2,636,218.99 元"))

    def test_wan_unit(self):
        vals = numbers("约 263.6 万元")
        self.assertTrue(any(abs(v - 2636000) < 1 for v in vals))

    def test_citation_number_ignored(self):
        """引用编号 [3] 不能被当成数值，否则 3 会误命中某些事实。"""
        self.assertEqual(numbers("结论见 [3]。"), [])


class TestFacts(unittest.TestCase):
    def test_text_normalized(self):
        facts = [{"type": "text", "any": ["C盘"]}]
        self.assertEqual(fact_hits("请备份 C 盘数据。", facts), [True])

    def test_num_tolerance(self):
        facts = [{"type": "num", "value": 4207712.68, "tol": 0.02}]
        self.assertEqual(fact_hits("2025 年 GMV 为 420.8 万元", facts), [True])

    def test_num_out_of_tolerance(self):
        facts = [{"type": "num", "value": 4207712.68, "tol": 0.02}]
        self.assertEqual(fact_hits("约 300 万元", facts), [False])

    def test_missing(self):
        facts = [{"type": "text", "any": ["dialer-rule"]}]
        self.assertEqual(fact_hits("只说了拨号", facts), [False])


class TestCitations(unittest.TestCase):
    def test_invalid_detected(self):
        s = cite_stats("结论 [1]，另一句 [9]。", total=3)
        self.assertEqual(s["invalid"], 1)
        self.assertEqual(s["distinct"], 2)
        self.assertAlmostEqual(s["valid_rate"], 0.5)

    def test_no_citation(self):
        self.assertEqual(cite_stats("没有任何引用", total=5)["valid_rate"], 0.0)

    def test_paragraph_ratio_skips_headings(self):
        body = (
            "## 标题\n\n"
            "这是一段足够长的正文内容，用来测试段落统计口径是否合理 [1]。\n\n"
            "另一段落同样写了很长的一段话但是完全没有带任何引用编号，它应该被算进分母。\n"
        )
        self.assertAlmostEqual(paragraph_citation_ratio(body), 0.5, places=2)


class TestDeclared(unittest.TestCase):
    def test_refusal(self):
        self.assertTrue(declared_limitation("现有证据无法确认该型号的销量。"))
        self.assertFalse(declared_limitation("2025 年销售额为 420 万元 [1]。"))


class TestRouteHit(unittest.TestCase):
    def test_partial(self):
        stats = {"kb": {"evidences": 3}, "sql": {"evidences": 0}}
        self.assertAlmostEqual(route_hit(stats, ["kb", "sql"]), 0.5)

    def test_no_expectation(self):
        self.assertIsNone(route_hit({}, []))


class TestPool(unittest.TestCase):
    def test_miss_is_not_retryable(self):
        pool = Pool(qid="x", question="q", rounds=[], evidence={}, meta={}, misses=[])
        r = asyncio.run(pool.caller("kb", "没录过的问题"))
        self.assertFalse(r.ok)
        self.assertFalse(r.retryable)  # 回放缺失不是工具故障，重试没有意义
        self.assertEqual(len(pool.misses), 1)

    def test_pack_and_hit(self):
        pool = Pool(qid="x", question="q", rounds=[], evidence={}, meta={}, misses=[])
        ev = Evidence(source="kb", claim="c", snippet="s", ref="r", confidence=0.9)
        pool.pack("kb", "问题 A", ToolResult(source="kb", ok=True, evidences=[ev]))
        r = asyncio.run(pool.caller("kb", "问题A"))  # 归一化后应命中
        self.assertTrue(r.ok)
        self.assertEqual(len(r.evidences), 1)
        self.assertEqual(pool.misses, [])


class TestVariants(unittest.TestCase):
    def test_six(self):
        self.assertEqual([v.id for v in VARIANTS], ["G1", "G2", "G3", "G4", "G5", "G6"])

    def test_overrides_cover_three_nodes(self):
        for v in VARIANTS:
            self.assertEqual(set(_overrides(v, [[]]).keys()), {"plan", "compress", "review"})

    def test_compress_none_keeps_everything(self):
        """基线不能偷偷去重，否则「不压缩」这一组就不是基线了。"""
        evs = [
            {"source": "kb", "claim": "同一句话", "snippet": "同一句话", "ref": "a", "confidence": 0.9},
            {"source": "kb", "claim": "同一句话", "snippet": "同一句话", "ref": "b", "confidence": 0.8},
        ]
        out = asyncio.run(compress_none({"evidences": evs}))
        self.assertEqual(len(out["citations"]), 2)

    def test_quota_variant_dedups(self):
        """配额组开了去重，重复项必须被吃掉——这是 G3/G4 与 G6 的分界线。"""
        evs = [
            {"source": "kb", "claim": "同一句话反复出现", "snippet": "同一句话反复出现", "ref": "a", "confidence": 0.9},
            {"source": "web", "claim": "同一句话反复出现", "snippet": "同一句话反复出现", "ref": "b", "confidence": 0.8},
        ]
        node = make_compress(dedup=True, quota=True)
        out = asyncio.run(node({"evidences": evs}))
        self.assertEqual(len(out["citations"]), 1)

    def test_no_dedup_variant_keeps_duplicates(self):
        evs = [
            {"source": "kb", "claim": "同一句话反复出现", "snippet": "同一句话反复出现", "ref": "a", "confidence": 0.9},
            {"source": "web", "claim": "同一句话反复出现", "snippet": "同一句话反复出现", "ref": "b", "confidence": 0.8},
        ]
        node = make_compress(dedup=False, quota=True)
        out = asyncio.run(node({"evidences": evs}))
        self.assertEqual(len(out["citations"]), 2)


class TestPlanSingle(unittest.TestCase):
    def test_one_todo_three_routes(self):
        out = asyncio.run(plan_single({"question": "问题", "review_round": 0}))
        self.assertEqual(len(out["todos"]), 1)
        self.assertEqual(out["todos"][0]["routes"], ["web", "kb", "sql"])
        self.assertEqual(out["todos"][0]["id"], "r1t1")

    def test_no_token_cost(self):
        """基线不调模型，所以不应产生 token —— 否则成本对比会失真。"""
        out = asyncio.run(plan_single({"question": "问题", "review_round": 0}))
        self.assertNotIn("tokens", out)


if __name__ == "__main__":
    unittest.main()
