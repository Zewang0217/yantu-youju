"""确定性结构化基线:从解析文本提取报名窗口/截止时间候选(R2 改造版)。

本版不再自拼 datetime(组号偏移已删),全部委托 text_dates.find_datetimes,
再对每个日期命中做"关键词就近回看":命中点前 40 字内出现"截止"且同一句
(无 。；分隔)→ exact 级 deadline;出现 开放/开始报名 → candidate 级。
落款等无关日期天然不命中——保守:误提取错误截止的代价高于漏提取(漏→unknown)。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from ..domain.models import Window
from .text_dates import find_datetimes

_LOOKBACK = 40
_DEADLINE_KW = "截止"
_OPEN_KW = ("开放", "开始报名", "报名开始")
_CLAUSE_BREAK = re.compile(r"[。；;]")


@dataclass(frozen=True)
class WindowCandidate:
    kind: str  # deadline / open_start
    date: datetime
    quote: str
    confidence: str  # exact / candidate


def extract_window_candidates(text: str) -> list[WindowCandidate]:
    out: list[WindowCandidate] = []
    for hit in find_datetimes(text):
        left = text[max(0, hit.start - _LOOKBACK) : hit.start]
        kind: str | None = None
        confidence = ""
        pos_deadline = left.rfind(_DEADLINE_KW)
        if pos_deadline >= 0:
            kind = "deadline"
            confidence = (
                "exact"
                if not _CLAUSE_BREAK.search(left[pos_deadline:])
                else "candidate"
            )
        else:
            open_pos = max((left.rfind(k) for k in _OPEN_KW), default=-1)
            if open_pos >= 0:
                kind = "open_start"
                confidence = "candidate"
        if kind is None:
            continue
        quote = text[max(0, hit.start - _LOOKBACK) : hit.start + 18].strip()
        out.append(WindowCandidate(kind, hit.dt, quote, confidence))
    # 去重:同(kind,date)取置信度最高者(exact > candidate),不做先到先得
    best: dict[tuple[str, datetime], WindowCandidate] = {}
    for c in out:
        key = (c.kind, c.date)
        if key not in best or (c.confidence == "exact" and best[key].confidence != "exact"):
            best[key] = c
    return list(best.values())


def merge_deadline(window: Window, candidates: list[WindowCandidate]) -> Window:
    """用 exact 级截止候选补齐窗口;人工/先验 end 优先于启发式(ACQ-06)。

    多个互不相同的 exact 截止(常见于同页多批次)取**最早**(保守方向:
    宁可提示学生更紧的时间,不错误放宽);此场景建议人工复核该页批次归属。
    冲突时以种子为准——人工复核过的数据优先;返回新 Window,不改原对象。
    """
    exact = [c for c in candidates if c.kind == "deadline" and c.confidence == "exact"]
    if not exact:
        return window
    extracted = min(c.date for c in exact)
    if window.end is None:
        return Window(
            entry_name=window.entry_name,
            required=window.required,
            start=window.start,
            end=extracted,
        )
    return window
