"""
题库的结构与校验。

一道题除了问题本身，还要带三样东西，否则指标算不出来：

1. `facts`：可自动核对的关键事实。文本型比对归一化后的子串，数值型允许 2% 误差
   （模型常把 2,636,218.99 写成「约 263.6 万」）。用规则而不是全交给 LLM 判，
   是因为规则可复现、零成本，只有规则判不了的部分才值得花一次模型调用。
2. `reference`：人工确认过的参考答案，给 LLM-as-judge 当尺子。
3. `expect_routes`：这题「应该」由哪一路回答，用来算路由命中率。空列表表示
   这题本来就查不到（第 5 层），路由命中率对它没有意义。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
QA_SET = ROOT / "eval" / "qa_set.jsonl"

# 五层分层。前两层考单源召回，第三层考规划拆分，第四层考外部检索，
# 第五层考「不知道就说不知道」——这一层最容易被忽略，也最能暴露幻觉。
LAYER_LABEL = {
    "kb": "单源·知识库",
    "sql": "单源·数仓",
    "multi": "多子任务复合",
    "web": "需联网",
    "unanswerable": "不可答",
}

LAYER_ORDER = ["kb", "sql", "multi", "web", "unanswerable"]


@dataclass
class QAItem:
    id: str
    layer: str
    question: str
    facts: list[dict] = field(default_factory=list)
    reference: str = ""
    expect_routes: list[str] = field(default_factory=list)
    note: str = ""
    source: str = ""

    @property
    def layer_label(self) -> str:
        return LAYER_LABEL.get(self.layer, self.layer)


def load_items(path: Path | str = QA_SET, *, ids: list[str] | None = None) -> list[QAItem]:
    """读取题库，可按 id 过滤（保持题库里的原始顺序）。"""
    p = Path(path)
    items: list[QAItem] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        raw = json.loads(line)
        items.append(
            QAItem(
                id=raw["id"],
                layer=raw["layer"],
                question=raw["question"],
                facts=raw.get("facts") or [],
                reference=raw.get("reference") or "",
                expect_routes=raw.get("expect_routes") or [],
                note=raw.get("note") or "",
                source=raw.get("source") or "",
            )
        )

    if ids:
        want = set(ids)
        unknown = want - {i.id for i in items}
        if unknown:
            raise KeyError(f"题库里没有这些 id：{sorted(unknown)}")
        items = [i for i in items if i.id in want]

    _validate(items)
    return items


def _validate(items: list[QAItem]) -> None:
    """结构校验。宁可在这里崩，也别等跑了两小时才发现某题的 facts 写错。"""
    seen: set[str] = set()
    for it in items:
        if it.id in seen:
            raise ValueError(f"重复的题号：{it.id}")
        seen.add(it.id)

        if it.layer not in LAYER_LABEL:
            raise ValueError(f"{it.id}: 未知分层 {it.layer}")

        # 前四层必须有事实或参考答案，否则这题没法判分，等于白跑
        if it.layer != "web" and not it.facts and not it.reference:
            raise ValueError(f"{it.id}: 既没有 facts 也没有 reference，无法判分")

        for f in it.facts:
            kind = f.get("type")
            if kind == "text" and not f.get("any"):
                raise ValueError(f"{it.id}: text 型 fact 缺少 any")
            if kind == "num" and not isinstance(f.get("value"), (int, float)):
                raise ValueError(f"{it.id}: num 型 fact 缺少 value")
            if kind not in ("text", "num"):
                raise ValueError(f"{it.id}: 未知 fact 类型 {kind}")


def body_of(report: str) -> str:
    """
    只取报告正文，去掉尾部由代码拼的「参考来源」和「运行信息」。

    这两节是代码生成的，天然不含事实陈述，留在正文里只会让「段落引用覆盖率」
    这类指标虚高——因为参考来源整节都带 [n]。
    """
    if not report:
        return ""
    parts = report.split("\n---\n", 1)
    return parts[0].strip()
