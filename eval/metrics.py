"""
聚合与报表。

输出三样东西，各自服务不同场景：
- `summary.csv`：每组一行，用来画图或粘进表格
- `report.md`：人看的，含结论与每层拆解
- 逐题 JSON：出问题时能一眼看到是哪道题、哪个指标掉了
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from eval.judge import mean
from eval.schema import LAYER_LABEL, LAYER_ORDER

# 报表列：(键, 中文名, 格式)。顺序按「先说效果，再说成本」排。
# 耗时列叫「回放耗时」而不是「耗时」：回放不跑取证，真实端到端是 20~100 秒，
# 这里只能横向比较各组多花的模型时间，不能当端到端延迟读。
COLUMNS = [
    ("fact_hit", "关键事实命中", "pct"),
    ("cite_valid", "引用有效率", "pct"),
    ("cite_coverage", "段落引用覆盖", "pct"),
    ("route_hit", "路由命中", "pct"),
    ("judge_faith", "忠实性(0-2)", "num"),
    ("judge_cover", "覆盖度(0-2)", "num"),
    ("judge_honest", "诚实性(0-2)", "num"),
    ("tokens", "平均 token", "int"),
    ("steps", "平均工具调用", "num"),
    ("elapsed", "回放耗时(s)", "num"),
    ("citations", "平均引用条数", "num"),
    ("pool_miss", "回放缺失", "int"),
]


def aggregate(rows: list[dict]) -> dict:
    """一组变体在全部题上的汇总。None 不参与平均（该题没有可判项就不拉低分母）。"""
    return {
        "n": len(rows),
        "fact_hit": mean([r.get("fact_hit") for r in rows]),
        "cite_valid": mean([r.get("cite_valid") for r in rows]),
        "cite_coverage": mean([r.get("cite_coverage") for r in rows]),
        "route_hit": mean([r.get("route_hit") for r in rows]),
        "judge_faith": mean([(r.get("judge") or {}).get("faithfulness") for r in rows]),
        "judge_cover": mean([(r.get("judge") or {}).get("coverage") for r in rows]),
        "judge_honest": mean([(r.get("judge") or {}).get("honesty") for r in rows]),
        "tokens": mean([r.get("tokens") for r in rows]),
        "steps": mean([r.get("steps") for r in rows]),
        "elapsed": mean([r.get("elapsed") for r in rows]),
        "citations": mean([r.get("citations") for r in rows]),
        "pool_miss": sum(int(r.get("pool_miss") or 0) for r in rows),
        "errors": sum(1 for r in rows if r.get("error")),
        # 不可答层的专用指标：明确承认查不到的比例
        "declared": _declared_rate(rows),
    }


def _declared_rate(rows: list[dict]) -> float | None:
    sub = [r for r in rows if r.get("layer") == "unanswerable"]
    if not sub:
        return None
    return round(sum(1 for r in sub if r.get("declared")) / len(sub), 4)


def by_layer(rows: list[dict]) -> dict[str, dict]:
    """按层拆解。整体分数会把「单源答得好」和「不可答时乱编」平均掉，必须拆开看。"""
    out = {}
    for layer in LAYER_ORDER:
        sub = [r for r in rows if r.get("layer") == layer]
        if sub:
            out[layer] = aggregate(sub)
    return out


def fmt(value, kind: str) -> str:
    if value is None:
        return "—"
    if kind == "pct":
        return f"{value * 100:.1f}%"
    if kind == "int":
        return f"{value:.0f}"
    return f"{value:.2f}"


def summary_table(summaries: list[tuple[str, dict]]) -> str:
    """主表：每组一行。"""
    head = "| 组别 | " + " | ".join(name for _, name, _ in COLUMNS) + " |"
    sep = "|" + "---|" * (len(COLUMNS) + 1)
    lines = [head, sep]
    for label, s in summaries:
        cells = [fmt(s.get(key), kind) for key, _, kind in COLUMNS]
        lines.append(f"| {label} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def layer_table(rows: list[dict]) -> str:
    """按层拆解（只用完整链路那一组，避免六组 × 五层的大表没人看）。"""
    blocks = by_layer(rows)
    if not blocks:
        return ""
    keys = [("fact_hit", "事实命中"), ("cite_valid", "引用有效"),
            ("judge_cover", "覆盖度"), ("judge_honest", "诚实性")]
    head = "| 分层 | 题数 | " + " | ".join(n for _, n in keys) + " | 备注 |"
    sep = "|" + "---|" * (len(keys) + 3)
    lines = [head, sep]
    for layer in LAYER_ORDER:
        s = blocks.get(layer)
        if not s:
            continue
        cells = [fmt(s.get(k), "pct" if k in ("fact_hit", "cite_valid") else "num") for k, _ in keys]
        note = ""
        if layer == "unanswerable":
            note = f"明确承认查不到 {fmt(s.get('declared'), 'pct')}"
        elif layer == "web":
            note = "该层不设事实清单，只判忠实性"
        lines.append(f"| {LAYER_LABEL[layer]} | {s['n']} | " + " | ".join(cells) + f" | {note} |")
    return "\n".join(lines)


def write_csv(path: Path, summaries: list[tuple[str, dict]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["variant"] + [name for _, name, _ in COLUMNS] + ["declared", "errors"])
        for label, s in summaries:
            writer.writerow(
                [label]
                + [s.get(key) for key, _, _ in COLUMNS]
                + [s.get("declared"), s.get("errors")]
            )


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
