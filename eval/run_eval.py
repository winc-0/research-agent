"""
评测入口。

    python -m eval.run_eval record              # 取证录制（真实调用，会花钱）
    python -m eval.run_eval replay              # 六组消融回放 + 判分
    python -m eval.run_eval all --limit 8       # 冒烟：前 8 题走完整流程

常用参数：
    --ids k01,s01,m01     只跑指定题
    --variants G1,G5      只跑指定变体
    --no-judge            跳过 LLM 判分（只看规则指标，零模型成本）
    --model qwen-plus     被测主链模型（默认用便宜的 eval 型号）
    --judge-model qwen-plus  判分模型（默认 LLM_JUDGE_MODEL_NAME，刻意与被测模型分开）
    --run-id 20261020_1   复用/指定产物目录

产物在 eval/results/<run-id>/：每组一个 JSON、summary.csv、report.md。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

from app.conf.settings import settings
from app.core.log import logger
from eval import metrics
from eval.judge import judge_rows
from eval.record import record_all
from eval.schema import LAYER_ORDER, load_items
from eval.scoring import score_row
from eval.variants import BY_ID, VARIANTS, run_all

RESULTS = Path(__file__).resolve().parent / "results"


def _ids(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    return [x.strip() for x in raw.split(",") if x.strip()]


def _variants(raw: str | None):
    if not raw:
        return VARIANTS
    picked = []
    for token in raw.split(","):
        token = token.strip().upper()
        if token not in BY_ID:
            raise SystemExit(f"未知变体 {token}，可选：{', '.join(BY_ID)}")
        picked.append(BY_ID[token])
    return picked


async def cmd_record(args) -> int:
    items = load_items(ids=_ids(args.ids))
    if args.limit:
        items = items[: args.limit]

    print(f"录制 {len(items)} 题，取证会真实调用三个下游服务（履约知识助手 :8001 / 仓配自助问数 :8010 / 百炼 WebSearch）")
    rows = await record_all(items, force=args.force)

    done = [r for r in rows if not r.get("error")]
    skipped = sum(1 for r in rows if r.get("skipped"))
    calls = sum(int(r.get("calls") or 0) for r in done)
    tokens = sum(int(r.get("tokens") or 0) for r in done)
    print(f"\n完成 {len(done)}/{len(rows)}（复用 {skipped}），取证 {calls} 次，编排 token {tokens}")
    for r in rows:
        if r.get("error"):
            print(f"  ! {r['qid']}: {r['error']}")
    return 0 if len(done) == len(rows) else 1


async def cmd_replay(args) -> int:
    items = load_items(ids=_ids(args.ids))
    if args.limit:
        items = items[: args.limit]
    variants = _variants(args.variants)

    run_id = args.run_id or datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = RESULTS / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    judge_model = settings.llm.judge_model_name

    # 把当次用的模型记下来。报表要照实写，否则「用 flash 跑出来的数」会被当成 plus 的
    metrics.write_json(out_dir / "meta.json", {
        "run_id": run_id,
        "items": len(items),
        "variants": [v.id for v in variants],
        "main_model": settings.llm.model_name,
        "judge_model": judge_model if args.judge else "",
        "judged": bool(args.judge),
        # 同族模型自评会系统性地抬高分数，这一位必须在产物里留痕，
        # 否则日后回看这份报告，没人知道当时的判分器是不是被测模型自己
        "judge_same_as_main": bool(args.judge) and judge_model == settings.llm.model_name,
    })

    print(f"回放 {len(variants)} 组 × {len(items)} 题，产物目录 {out_dir}")
    if args.judge:
        same = " ⚠ 判分模型与主链相同，存在自我偏好偏差" if judge_model == settings.llm.model_name else ""
        print(f"主链模型 {settings.llm.model_name}，判分模型 {judge_model}{same}")
    else:
        print(f"主链模型 {settings.llm.model_name}，跳过判分")

    items_by_id = {i.id: i for i in items}
    summaries = []

    for variant in variants:
        print(f"\n=== {variant.label}：{variant.desc}")
        rows = await run_all(variant, items)
        scored = [_attach(item, row) for item, row in zip(items, rows)]

        if args.judge:
            judged = await judge_rows(rows, items_by_id, concurrency=args.judge_concurrency)
            for row in scored:
                row["judge"] = judged.get(row["qid"]) or {}

        metrics.write_json(out_dir / f"{variant.id}.json", scored)
        summaries.append((variant.label, metrics.aggregate(scored)))
        print(f"  完成 {len(scored)} 题，产物 {variant.id}.json")

    _write_report(out_dir, summaries, items, run_id)
    return 0


def _attach(item, row: dict) -> dict:
    """把一题的结果算成一行指标，并保留判分位置。"""
    return {**score_row(item, row), "judge": {}}


def fmt_pct(value) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def _write_report(out_dir: Path, summaries, items, run_id: str) -> None:
    metrics.write_csv(out_dir / "summary.csv", summaries)

    meta_path = out_dir / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}

    full_key = "G5"
    full_rows = []
    full_path = out_dir / f"{full_key}.json"
    if full_path.exists():
        full_rows = json.loads(full_path.read_text(encoding="utf-8"))

    lines = [
        f"# 物流研究助手 Research-Agent 编排层消融实验（{run_id}）",
        "",
        f"- 题库：`eval/qa_set.jsonl`，共 {len(items)} 题",
        "- 取证：录制一次后所有变体回放同一证据池（证据相同，差异只来自编排）",
        f"- 主链模型：`{meta.get('main_model') or settings.llm.model_name}`",
        f"- 判分模型：`{meta.get('judge_model') or settings.llm.judge_model_name}`（0~2 分，三项均分）",
        "- 回放耗时不含取证：真实端到端是 20~100 秒，此列只能横向比较各组多花的模型时间",
        "",
        "## 总表",
        "",
        metrics.summary_table(summaries),
        "",
    ]

    if full_rows:
        lines += ["## 完整链路（G5）按层拆解", "", metrics.layer_table(full_rows), ""]

    # 逐级贡献：阶梯上每一级相对上一级的变化。比只看首尾更能说明「哪一步在起作用」
    if len(summaries) >= 2:
        lines += ["## 逐级贡献", "",
                  "| 这一级加了什么 | 事实命中 | 相对上一级 | 平均 token | 相对上一级 |",
                  "|---|---|---|---|---|"]
        prev_label, prev = summaries[0]
        for label, cur in summaries[1:]:
            a, b = prev.get("fact_hit"), cur.get("fact_hit")
            ta, tb = prev.get("tokens"), cur.get("tokens")
            dfact = f"{(b - a) * 100:+.1f} 个点" if (a is not None and b is not None) else "—"
            dtok = f"{(tb - ta) / ta * 100:+.0f}%" if (ta and tb) else "—"
            lines.append(
                f"| {label} | {fmt_pct(b)} | {dfact} | {cur.get('tokens') or 0:.0f} | {dtok} |"
            )
            prev = cur
        lines.append("")
        lines.append("> G6 是留一法对照（在完整链路上关掉去重），不参与这条阶梯。")
        lines.append("")

    # 关键结论写成文字，省得读表的人自己算
    by_label = dict(summaries)
    g1 = by_label.get("G1 单智能体一把梭") or {}
    g5 = by_label.get("G5 +审查回环（完整）") or {}
    if g1 and g5:
        lines += ["## 关键差值（G1 → G5）", ""]
        # 三种量纲分开格式化：百分比、0~2 分、绝对值。混用会把 1.92 分印成 192%
        for key, name, kind in (
            ("fact_hit", "关键事实命中", "pct"),
            ("cite_coverage", "段落引用覆盖", "pct"),
            ("judge_cover", "覆盖度", "score"),
            ("judge_honest", "诚实性", "score"),
            ("tokens", "平均 token", "abs"),
        ):
            a, b = g1.get(key), g5.get(key)
            if a is None or b is None:
                continue
            if kind == "pct":
                lines.append(f"- {name}：{a * 100:.1f}% → {b * 100:.1f}%（{(b - a) * 100:+.1f} 个点）")
            elif kind == "score":
                lines.append(f"- {name}：{a:.2f} → {b:.2f}（{b - a:+.2f} 分，满分 2）")
            else:
                pct = f"{(b - a) / a * 100:+.0f}%" if a else "—"
                lines.append(f"- {name}：{a:.0f} → {b:.0f}（{pct}）")
        lines.append("")

    (out_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"\n总表已写入 {out_dir / 'report.md'}")
    print("\n" + metrics.summary_table(summaries))


async def cmd_report(args) -> int:
    run_id = args.run_id
    out_dir = RESULTS / run_id if run_id else _latest_run()
    if not out_dir or not out_dir.exists():
        raise SystemExit("找不到产物目录，先跑 replay")
    items = load_items(ids=_ids(args.ids))
    summaries = []
    for variant in VARIANTS:
        path = out_dir / f"{variant.id}.json"
        if path.exists():
            summaries.append((variant.label, metrics.aggregate(json.loads(path.read_text(encoding="utf-8")))))
    _write_report(out_dir, summaries, items, out_dir.name)
    return 0


def _latest_run() -> Path | None:
    if not RESULTS.exists():
        return None
    dirs = sorted((d for d in RESULTS.iterdir() if d.is_dir()), key=lambda d: d.name)
    return dirs[-1] if dirs else None


def main() -> int:
    parser = argparse.ArgumentParser(description="物流研究助手编排层消融评测")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--ids", help="只跑指定题号，逗号分隔")
        p.add_argument("--limit", type=int, default=0, help="只跑前 N 题（冒烟用）")

    p_rec = sub.add_parser("record", help="录制证据池")
    common(p_rec)
    p_rec.add_argument("--force", action="store_true", help="覆盖已有证据池")

    p_rep = sub.add_parser("replay", help="回放消融 + 判分")
    common(p_rep)
    p_rep.add_argument("--variants", help="只跑指定组，如 G1,G5")
    p_rep.add_argument("--no-judge", dest="judge", action="store_false", help="跳过 LLM 判分")
    p_rep.add_argument("--judge-concurrency", type=int, default=4)
    p_rep.add_argument("--run-id", help="产物目录名")
    p_rep.add_argument("--model", help="主链模型（被测），默认用 LLM_EVAL_MODEL_NAME")
    p_rep.add_argument("--judge-model", dest="judge_model",
                       help="判分模型，默认用 LLM_JUDGE_MODEL_NAME（qwen-plus）。"
                            "刻意与被测模型分开：自己给自己打分会有自我偏好偏差")
    p_rep.set_defaults(judge=True)

    p_rep2 = sub.add_parser("report", help="用已有产物重新出表")
    p_rep2.add_argument("--run-id", help="产物目录名，默认取最新")
    p_rep2.add_argument("--ids")

    args = parser.parse_args()

    if args.cmd == "record":
        return asyncio.run(cmd_record(args))

    if getattr(args, "model", None):
        settings.llm.model_name = args.model
    elif args.cmd == "replay":
        # 默认换成便宜型号：消融比较的是相对差异，主链全用 plus 会让成本翻几倍
        settings.llm.model_name = settings.llm.eval_model_name

    # 判分模型单独指定，**不跟随主链**：让被测模型给自己打分等于既当运动员又当裁判
    if getattr(args, "judge_model", None):
        settings.llm.judge_model_name = args.judge_model

    if args.cmd == "replay":
        return asyncio.run(cmd_replay(args))
    return asyncio.run(cmd_report(args))


if __name__ == "__main__":
    sys.exit(main())
