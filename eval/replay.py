"""
证据池：录制与回放。

池子按「题号 → 一份 JSON」落盘，不塞进一个大文件：跑 40 题要半小时以上，
中途某个服务抽风就整批重来，代价太大。一题一文件才能断点续跑。

键是 `route::归一化问题`，与 worker 的重复调用检测用同一个函数——两处必须一致，
否则回放会大面积 miss，表现成「所有变体都取不到证据」，非常难查。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from app.agent.nodes.worker import call_key
from app.tools.base import Evidence, ToolResult

ROOT = Path(__file__).resolve().parents[1]
POOL_DIR = ROOT / "eval" / "pool"


@dataclass
class Pool:
    """一道题的全部证据记录。"""

    qid: str
    question: str
    rounds: list[list[dict]]
    evidence: dict[str, dict]
    meta: dict

    # 回放期统计：哪些键没录到。有 miss 说明这个变体问出了录制时没问过的问题，
    # 属于实验设计问题，必须显式暴露，不能静默当成「没查到」
    misses: list[str]

    @classmethod
    def load(cls, qid: str, pool_dir: Path = POOL_DIR) -> "Pool":
        path = pool_dir / f"{qid}.json"
        if not path.exists():
            raise FileNotFoundError(f"没有证据池：{path}（先跑 eval record）")
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            qid=raw["qid"],
            question=raw["question"],
            rounds=raw.get("rounds") or [],
            evidence=raw.get("evidence") or {},
            meta=raw.get("meta") or {},
            misses=[],
        )

    def pack(self, route: str, question: str, result: ToolResult) -> str:
        """
        把一次真实调用记进池子，返回键。

        同一个键可能被打两次（worker 失败后会重试一次）。worker 的取值规则是
        「重试成功就用重试的结果，否则留第一次的错误」，所以这里必须跟着走：
        **成功的结果优先覆盖失败的结果**，否则池子里留下的是被丢弃的那次，
        回放时会凭空多出一次「失败」，进而触发降级、问到录制时没问过的键。
        """
        key = call_key(route, question)
        prev = self.evidence.get(key)
        if prev is not None and (prev.get("ok") or not result.ok):
            return key

        self.evidence[key] = {
            "source": route,
            "question": question,
            "ok": result.ok,
            "error": result.error,
            "retryable": result.retryable,
            "elapsed": round(result.elapsed, 3),
            "evidences": [e.to_dict() for e in result.evidences],
            # SQL 那一路把实际执行的 SQL 留下来，出问题时能直接看是哪条
            "sql": (result.evidences[0].payload.get("sql") if result.evidences else "")
            if route == "sql"
            else "",
        }
        return key

    def dump(self, pool_dir: Path = POOL_DIR) -> Path:
        pool_dir.mkdir(parents=True, exist_ok=True)
        path = pool_dir / f"{self.qid}.json"
        payload = {
            "qid": self.qid,
            "question": self.question,
            "rounds": self.rounds,
            "evidence": self.evidence,
            "meta": {
                **self.meta,
                "calls": len(self.evidence),
                "misses": self.misses,
            },
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        return path

    async def caller(self, route: str, question: str) -> ToolResult:
        """回放版取证：只查池子，绝不发真实请求。"""
        key = call_key(route, question)
        hit = self.evidence.get(key)
        if hit is None:
            self.misses.append(key)
            return ToolResult(
                source=route,
                ok=False,
                error=f"pool_miss: 录制时没有这条调用（{key}）",
                retryable=False,
            )

        return ToolResult(
            source=route,
            ok=bool(hit.get("ok")),
            error=hit.get("error") or "",
            retryable=bool(hit.get("retryable", False)),
            elapsed=float(hit.get("elapsed") or 0.0),
            evidences=[_to_evidence(e) for e in (hit.get("evidences") or [])],
        )


def _to_evidence(d: dict) -> Evidence:
    """池子里的 dict 还原成证据对象。"""
    return Evidence(
        source=d.get("source") or "",
        claim=d.get("claim") or "",
        snippet=d.get("snippet") or "",
        ref=d.get("ref") or "",
        confidence=float(d.get("confidence") or 0.0),
    )


def pool_exists(qid: str, pool_dir: Path = POOL_DIR) -> bool:
    return (pool_dir / f"{qid}.json").exists()
