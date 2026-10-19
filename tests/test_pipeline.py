"""
图内纯函数的离线自检。

    python -m unittest discover -s tests -v

不调 LLM、不打网络：只验证「压缩/规划校验/守卫/引用检查」这些确定性逻辑。
主链路的端到端正确性由 eval/ 下的评测集负责，这里守的是最容易写错的边界。
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent import llm as llm_mod  # noqa: E402
from app.agent.nodes.compress import MAX_PER_ROUTE, compress  # noqa: E402
from app.agent.nodes.confirm import _parse, build_outline  # noqa: E402
from app.agent.nodes.guard import guard  # noqa: E402
from app.agent.nodes.planner import _ensure_progress, _normalize  # noqa: E402
from app.agent.nodes.review import _resolve  # noqa: E402
from app.agent.nodes.report import _check_citations, _normalize_citations  # noqa: E402
from app.agent.nodes.worker import call_key  # noqa: E402


def _ev(source: str, claim: str, ref: str = "x", conf: float = 0.8, todo: str = "r1t1") -> dict:
    return {
        "source": source,
        "claim": claim,
        "snippet": claim * 2,
        "ref": ref,
        "confidence": conf,
        "todo_id": todo,
    }


class TestCompress(unittest.TestCase):
    def _run(self, evidences: list[dict]) -> list[dict]:
        return asyncio.run(compress({"evidences": evidences}))["citations"]

    def test_dedup_identical_across_routes(self):
        """同一句话被两路召回，只保留置信度高的那条。"""
        text = "HAK180 烫金机的转印温度应设置为 120 摄氏度并保持稳定"
        out = self._run([_ev("kb", text, conf=0.9), _ev("web", text, conf=0.5)])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["source"], "kb")

    def test_different_facts_kept(self):
        """内容不同的证据不能被误合并。"""
        out = self._run([_ev("kb", "转印温度设置为 120 摄氏度"), _ev("kb", "工作电压为 220 伏交流电")])
        self.assertEqual(len(out), 2)

    def test_empty_claim_dropped(self):
        out = self._run([_ev("web", "", conf=0.9), _ev("web", "有效结论内容超过八个字符", conf=0.5)])
        self.assertEqual(len(out), 1)

    def test_route_quota(self):
        """单一路来源过多时按配额截断，不挤占其他路。"""
        evs = [_ev("web", f"公开网页第{i}条互不相同的结论内容{i*7}", conf=0.9) for i in range(20)]
        evs += [_ev("kb", "知识库独有的一条内部事实内容", conf=0.3)]
        out = self._run(evs)
        web = [c for c in out if c["source"] == "web"]
        kb = [c for c in out if c["source"] == "kb"]
        self.assertLessEqual(len(web), MAX_PER_ROUTE)
        self.assertEqual(len(kb), 1, "低置信度的另一路也必须保留代表")

    def test_index_sequential(self):
        out = self._run([_ev("web", f"第{i}条互不相同的内容结论{i*13}") for i in range(5)])
        self.assertEqual([c["index"] for c in out], list(range(1, len(out) + 1)))

    def test_snippet_clipped(self):
        ev = _ev("web", "一个结论")
        ev["snippet"] = "长" * 5000
        out = self._run([ev])
        self.assertLessEqual(len(out[0]["snippet"]), 600)

    def test_no_evidence(self):
        self.assertEqual(self._run([]), [])


class TestPlannerNormalize(unittest.TestCase):
    def test_chinese_routes_mapped(self):
        out = _normalize([{"question": "公司的差旅报销标准是什么", "routes": ["知识库", "数据库"]}])
        self.assertEqual(out[0]["routes"], ["kb", "sql"])

    def test_unknown_route_dropped(self):
        out = _normalize([{"question": "随便一个问题内容", "routes": ["ftp"]}])
        self.assertEqual(out, [], "未知证据路的子任务应被丢弃")

    def test_id_reassigned_by_round(self):
        out = _normalize([{"id": "t1", "question": "第一轮的问题内容", "routes": ["web"]}], start=2)
        self.assertEqual(out[0]["id"], "r2t1")

    def test_max_todos(self):
        raw = [{"question": f"问题内容第{i}个足够长", "routes": ["web"]} for i in range(10)]
        self.assertEqual(len(_normalize(raw)), 5)

    def test_short_question_dropped(self):
        self.assertEqual(_normalize([{"question": "短", "routes": ["web"]}]), [])

    def test_not_a_list(self):
        self.assertEqual(_normalize({"todos": []}), [])


class TestEnsureProgress(unittest.TestCase):
    """实测：用户要求补检索时，模型会原样再规划一遍，导致整轮空转。"""

    def test_appends_feedback_when_identical(self):
        todos = [{"id": "r2t1", "question": "按品类汇总销售额", "routes": ["sql"]}]
        out = _ensure_progress(todos, ["按品类汇总销售额"], "想看华东华南的差异")
        self.assertIn("用户补充要求", out[0]["question"])

    def test_keeps_new_plan(self):
        todos = [{"id": "r2t1", "question": "按区域和品类汇总销售额", "routes": ["sql"]}]
        out = _ensure_progress(todos, ["按品类汇总销售额"], "想看华东华南的差异")
        self.assertEqual(out[0]["question"], "按区域和品类汇总销售额")

    def test_first_round_untouched(self):
        todos = [{"id": "r1t1", "question": "按品类汇总销售额", "routes": ["sql"]}]
        out = _ensure_progress(todos, [], "")
        self.assertEqual(out[0]["question"], "按品类汇总销售额")


class TestResolveTodoId(unittest.TestCase):
    """模型把 r1t2 简写成 t2 是实测现象，不兜住就会漏掉缺口。"""

    BY_ID = {"r1t1": "问题一", "r1t2": "问题二", "r2t1": "问题三"}

    def test_exact(self):
        self.assertEqual(_resolve(["r1t2"], self.BY_ID), ["r1t2"])

    def test_abbreviated(self):
        self.assertEqual(_resolve(["t2"], self.BY_ID), ["r1t2"])

    def test_bare_number(self):
        self.assertEqual(_resolve(["2"], self.BY_ID), ["r1t2"])

    def test_unknown(self):
        self.assertEqual(_resolve(["t9"], self.BY_ID), [])

    def test_dedup(self):
        self.assertEqual(_resolve(["r1t2", "t2"], self.BY_ID), ["r1t2"])


class TestGuard(unittest.TestCase):
    def _run(self, state: dict) -> dict:
        return asyncio.run(guard(state))

    def test_no_trip(self):
        self.assertEqual(self._run({"steps": 1, "tokens": 10, "calls": ["web::a"]}), {})

    def test_step_budget(self):
        out = self._run({"steps": 999, "tokens": 0, "calls": []})
        self.assertEqual(out["stop_reason"], "step_budget")

    def test_token_budget(self):
        out = self._run({"steps": 1, "tokens": 10**9, "calls": []})
        self.assertEqual(out["stop_reason"], "token_budget")

    def test_loop_detected_after_review(self):
        calls = ["web::a", "web::a", "kb::b", "kb::b"]
        out = self._run({"steps": 4, "tokens": 1, "calls": calls, "review_round": 1})
        self.assertEqual(out["stop_reason"], "loop_detected")

    def test_duplicates_in_first_round_not_tripped(self):
        """首轮的重复由 worker 跳过即可，不该直接终止整条链。"""
        calls = ["web::a", "web::a", "kb::b", "kb::b"]
        self.assertEqual(self._run({"steps": 4, "tokens": 1, "calls": calls, "review_round": 0}), {})


class TestCitationCheck(unittest.TestCase):
    def test_in_range(self):
        self.assertEqual(_check_citations("结论如此 [1][3]。", 3), set())

    def test_out_of_range(self):
        self.assertEqual(_check_citations("结论如此 [9]。", 3), {9})

    def test_zero(self):
        self.assertEqual(_check_citations("结论如此 [0]。", 3), {0})

    def test_no_citation(self):
        self.assertEqual(_check_citations("没有任何引用。", 3), set())


class TestCitationNormalize(unittest.TestCase):
    """模型把 [1] 写成 <1> / ［1］ / `[1]` 是实测常见现象。"""

    def test_angle(self):
        self.assertEqual(_normalize_citations("结论 <1>。"), "结论 [1]。")

    def test_angle_with_backtick(self):
        self.assertEqual(_normalize_citations("结论 `<1>`。"), "结论 [1]。")

    def test_fullwidth(self):
        self.assertEqual(_normalize_citations("结论 ［2］。"), "结论 [2]。")

    def test_backtick_bracket(self):
        self.assertEqual(_normalize_citations("结论 `[3]`。"), "结论 [3]。")

    def test_already_correct(self):
        self.assertEqual(_normalize_citations("结论 [4]。"), "结论 [4]。")

    def test_html_tag_untouched(self):
        """<br> 这类标签不含纯数字，不能被误伤。"""
        self.assertEqual(_normalize_citations("换行<br>继续 [1]"), "换行<br>继续 [1]")


class TestJsonExtract(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(llm_mod.extract_json('{"a": 1}'), {"a": 1})

    def test_fenced(self):
        self.assertEqual(llm_mod.extract_json('说明\n```json\n{"a": 2}\n```\n后面还有'), {"a": 2})

    def test_noise_around(self):
        self.assertEqual(llm_mod.extract_json('好的，结果如下：{"a": 3} 以上'), {"a": 3})

    def test_invalid(self):
        with self.assertRaises(ValueError):
            llm_mod.extract_json("完全不是 JSON")

    def test_trailing_prose_with_braces(self):
        """实测踩到的坑：JSON 后面补了一句带花括号的解释，首尾截取会把两段吞在一起。"""
        text = '{"todos": [{"id": "t1", "routes": ["sql"]}]}\n说明：routes 只能取 {web, kb, sql}'
        self.assertEqual(llm_mod.extract_json(text)["todos"][0]["id"], "t1")

    def test_prose_between_objects(self):
        text = '先看示例 {"demo": true}，正式结果：{"a": 4}'
        self.assertEqual(llm_mod.extract_json(text), {"demo": True})

    def test_brace_inside_string(self):
        """字符串里的花括号不算层级，否则配平会算错。"""
        text = '{"q": "怎么用 {} 占位", "n": 1}'
        self.assertEqual(llm_mod.extract_json(text)["n"], 1)


class TestConfirm(unittest.TestCase):
    """HITL 的纯函数部分：恢复值解析与大纲派生。节点本身靠 interrupt，不在单测里跑。"""

    def test_parse_approve(self):
        self.assertEqual(_parse({"decision": "approve"}), ("approve", ""))

    def test_parse_revise(self):
        self.assertEqual(_parse({"decision": "REVISE", "feedback": " 补华东 "}), ("revise", "补华东"))

    def test_parse_garbage_defaults_to_approve(self):
        """前端超时、脚本传错格式时都不能卡在等人确认上。"""
        self.assertEqual(_parse(None), ("approve", ""))
        self.assertEqual(_parse("看不懂的东西"), ("approve", ""))

    def test_outline_counts(self):
        state = {
            "todos": [{"id": "r1t1", "question": "按品类汇总", "routes": ["sql", "kb"]}],
            "citations": [{"todo_ids": ["r1t1"]}, {"todo_ids": ["r1t1"]}, {"todo_ids": []}],
        }
        out = build_outline(state)
        self.assertEqual(out[0]["证据数"], 2)
        self.assertEqual(out[0]["来源"], ["数仓", "知识库"])


class TestCallKey(unittest.TestCase):
    def test_normalized(self):
        """标点与空白的差异不该被当成新调用。"""
        self.assertEqual(call_key("web", "你好，世界！"), call_key("web", "你好世界"))

    def test_route_matters(self):
        self.assertNotEqual(call_key("web", "同一个问题"), call_key("kb", "同一个问题"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
