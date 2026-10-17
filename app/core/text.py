"""
文本处理工具：去重与相似度。

中文没有空格分词，用字符 n-gram 的 Jaccard 比 SequenceMatcher 快一个数量级，
对"同一段手册被两路各召回一次"这种近似重复的判定足够用。
"""

from __future__ import annotations

import re

# 只保留中日韩汉字、字母、数字：标点、空白、markdown 符号差异不该影响判重
_KEEP = re.compile(r"[^\u4e00-\u9fffa-zA-Z0-9]+")


def norm(text: str) -> str:
    """归一化：去掉一切非内容字符并转小写。"""
    return _KEEP.sub("", (text or "").lower())


def shingles(text: str, n: int = 3) -> set[str]:
    """字符 n-gram 集合。文本短于 n 时退化为整体。"""
    s = norm(text)
    if not s:
        return set()
    if len(s) <= n:
        return {s}
    return {s[i : i + n] for i in range(len(s) - n + 1)}


def jaccard(a: str, b: str, n: int = 3) -> float:
    """两段文本的相似度，0~1。"""
    sa, sb = shingles(a, n), shingles(b, n)
    if not sa or not sb:
        return 0.0
    inter = len(sa & sb)
    return inter / (len(sa) + len(sb) - inter)
