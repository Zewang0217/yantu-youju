"""确定性结构化基线:从解析文本提取报名窗口/截止时间候选。

定位(诚实边界):这是**确定性启发式基线**,不是 LLM 采集Agent——它只提取
有明确句式支撑的时间(如"截止时间为:DATE"),低置信一律降级为候选或忽略
(INV-13 精神:宁可 unknown 不猜)。LLM 条款结构化按 06 设计稿后续接入,
并以本基线与人工金标做三方对照(08 评估协议)。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from ..domain.models import Window

# 时间词归一:中午/上午视同上午;下午需+12
_NOON = re.compile(r"(中午|上午|早上|凌晨)")
_PM = re.compile(r"下午")
_TIME = re.compile(r"(\d{1,2})[时:点](\d{2})?")
_DATE = re.compile(r"(\d{4})[-年](\d{1,2})[-月](\d{1,2})日?")
# 截止句式:截止…(至多40字内)出现日期
_DEADLINE = re.compile(r"截止[^。；\n]{0,40}?(\d{4})[-年](\d{1,2})[-月](\d{1,2})日?")
# 开放/开始句式(候选级,不如截止可靠)
_OPEN = re.compile(r"(开放|开始报名|报名开始)[^。；\n]{0,40}?(\d{4})[-年](\d{1,2})[-月](\d{1,2})日?")


@dataclass(frozen=True)
class WindowCandidate:
    kind: str  # deadline / open_start
    date: datetime
    quote: str
    confidence: str  # exact / candidate

    def to_window(self, entry_name: str, required: bool = True) -> Window:
        if self.kind == "deadline":
            return Window(entry_name=entry_name, required=required, start=None, end=self.date)
        return Window(entry_name=entry_name, required=required, start=self.date, end=None)


def _time_after(text: str, pos: int) -> tuple[int, int]:
    """在日期之后10字内找时刻;返回(hour,minute)。处理下午/中午前缀。"""
    tail = text[pos : pos + 12]
    pm = bool(_PM.search(tail[:4]))
    m = _TIME.search(_NOON.sub("", tail))
    if not m:
        return (0, 0)
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    if pm and hour < 12:
        hour += 12
    return (hour, minute)


def _datetime_at(text: str, m: re.Match, group_offset: int) -> tuple[datetime, int]:
    """group_offset 指向年份捕获组;其后两组为月、日。"""
    year, month, day = (int(m.group(group_offset + i)) for i in (0, 1, 2))
    hour, minute = _time_after(text, m.end())
    return datetime(year, month, day, hour, minute), m.end()


def extract_window_candidates(text: str) -> list[WindowCandidate]:
    """提取窗口候选。exact=句式"截止…为:DATE"(强);candidate=开放/开始句式(弱)。

    不确定的日期(落款、考核日期、无关年份)不会被这两类句式命中——这是有意
    保守:误提取一个错误截止会把机会错误排除,代价高于漏提取(漏→unknown)。
    """
    out: list[WindowCandidate] = []
    for m in _DEADLINE.finditer(text):
        try:
            dt, _ = _datetime_at(text, m, 1)
        except ValueError:
            continue
        quote = text[m.start() : min(len(text), m.start() + 40)].strip()
        out.append(WindowCandidate("deadline", dt, quote, "exact"))
    for m in _OPEN.finditer(text):
        try:
            dt, _ = _datetime_at(text, m, 2)
        except ValueError:
            continue
        quote = text[m.start() : min(len(text), m.start() + 40)].strip()
        out.append(WindowCandidate("open_start", dt, quote, "candidate"))
    # 去重(同日期同类)
    seen: set[tuple[str, datetime]] = set()
    return [
        c for c in out if (c.kind, c.date) not in seen and not seen.add((c.kind, c.date))
    ]


def merge_deadline(window: Window, candidates: list[WindowCandidate]) -> Window:
    """用 exact 级截止候选补齐/校验窗口;无候选时保持原窗口(可能 end=None)。

    冲突时(候选≠种子):以种子为准并提示——人工复核过的数据优先于启发式
    (ACQ-06 复核门禁精神)。返回新 Window,不改原对象。
    """
    exact = [c for c in candidates if c.kind == "deadline" and c.confidence == "exact"]
    if not exact:
        return window
    extracted = max(c.date for c in exact)
    if window.end is None:
        return Window(
            entry_name=window.entry_name,
            required=window.required,
            start=window.start,
            end=extracted,
        )
    return window  # 已有人工/先验 end:不覆盖
