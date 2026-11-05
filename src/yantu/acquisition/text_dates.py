"""统一日期/时间文本解析(R2:消除 parsers 与 structurer 的双实现)。

能力:同一处代码支持 "2026-09-07"/"2026年9月7日" 两种日期写法,以及紧随
其后的时刻(含 中午/上午/下午 前缀与 时/:点 分隔;24:00 按 ISO 语义记
次日 00:00)。时刻非法但日期合法时回退为日级命中(00:00)而非整条丢弃;
日期本身非法(13月40日)才跳过。时刻搜索不越过下一个日期命中,避免
"X日至Y日9:00"的区间句式把时刻串扰到前一日期。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

_DATE = re.compile(r"(\d{4})[-年](\d{1,2})[-月](\d{1,2})日?")
_TIME = re.compile(r"(\d{1,2})[时:点](\d{2})?")
_NOON_AM = re.compile(r"(中午|上午|早上|凌晨)")
_PM = re.compile(r"下午")
# 日期之后允许出现时刻的距离(字符)
_TIME_WINDOW = 12


@dataclass(frozen=True)
class DatetimeHit:
    """文本中一次日期(含可选时刻)命中。span 覆盖日期片段本身。"""

    dt: datetime
    start: int
    end: int


def _resolve(y: int, mo: int, d: int, hour: int, minute: int) -> datetime | None:
    """组合日期+时刻;日期非法→None;时刻非法→回退日级;24:00→次日00:00。"""
    try:
        day_dt = datetime(y, mo, d)
    except ValueError:
        return None
    if hour == 24 and minute == 0:
        return day_dt + timedelta(days=1)
    try:
        return datetime(y, mo, d, hour, minute)
    except ValueError:
        return day_dt  # 时刻非法(如12:60):回退为日级命中


def find_datetimes(text: str) -> list[DatetimeHit]:
    """按出现位置顺序返回全部合法日期命中(同位置仅一条)。"""
    hits: list[DatetimeHit] = []
    for m in _DATE.finditer(text):
        # 时刻搜索窗口:截至下一个日期命中,避免"X日至Y日9:00"串扰
        nxt = _DATE.search(text, m.end())
        window_end = min(nxt.start() if nxt else len(text), m.end() + _TIME_WINDOW)
        tail = text[m.end() : window_end]

        hour, minute = 0, 0
        t = _TIME.search(_NOON_AM.sub("", tail))
        if t:
            hour = int(t.group(1))
            minute = int(t.group(2) or 0)
            # "下午"只认时刻之前紧邻片段(不越过时刻本身)
            if _PM.search(tail[: t.start()]) and hour < 12:
                hour += 12
        dt = _resolve(int(m.group(1)), int(m.group(2)), int(m.group(3)), hour, minute)
        if dt is None:
            continue
        hits.append(DatetimeHit(dt=dt, start=m.start(), end=m.end()))
    return hits
