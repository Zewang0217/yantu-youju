"""统一日期文本解析，保留原文偏移及精度，不赋予报名语义。

兼容旧调用的日级dt占位（00:00）；可信消费方必须检查precision/time_status，
不得把缺时刻或非法时刻的日期占位当成实际午夜。时间只绑定紧随日期的片段。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

_DATE = re.compile(r"(?<!\d)(\d{4})\s*[-年]\s*(\d{1,2})\s*[-月]\s*(\d{1,2})(?!\d)(?:\s*日)?")
_TIME = re.compile(
    r"\s*(?P<period>中午|上午|早上|凌晨|下午|晚上|傍晚)?\s*"
    r"(?P<hour>\d{1,3})\s*(?:(?P<colon>[:：])\s*(?P<minute>\d{1,3})?"
    r"|(?P<unit>[时点])(?:\s*(?P<unit_minute>\d{1,3})\s*分)?)"
    r"(?!\d)(?P<seconds>\s*[:：]\s*\d+)?"
)
_UNSUPPORTED_CLOCK_TAIL = re.compile(r"\s*(?:[:：][^\s，。；;]*|\d+\s*秒|分|秒|(?:a\.?m|p\.?m)\b)", re.I)


@dataclass(frozen=True)
class DatetimeHit:
    """end保持旧的日期区间语义；value_end涵盖完整时刻，精度另行声明。"""

    dt: datetime
    start: int
    end: int
    value_end: int = 0
    precision: str = "day"  # day / hour / minute
    time_status: str = "missing"  # missing / valid / invalid


def _clock_value(match: re.Match) -> tuple[int, int, str] | None:
    """验证时刻/时段一致性；未支持的秒级或模糊中午不补造精确值。"""
    minute_text = match["minute"] if match["colon"] else match["unit_minute"]
    if (match["seconds"] or len(match["hour"]) > 2 or
            (match["colon"] and (minute_text is None or len(minute_text) != 2)) or
            (minute_text is not None and len(minute_text) > 2)):
        return None
    hour, minute = int(match["hour"]), int(minute_text or 0)
    period = match["period"]
    if not (0 <= hour <= 24 and 0 <= minute < 60) or (hour == 24 and (minute or period)):
        return None
    if period:
        if not 1 <= hour <= 12:
            return None
        if period in ("下午", "晚上", "傍晚") and hour < 12:
            hour += 12
        elif period == "凌晨" and hour == 12:
            hour = 0
        elif period in ("上午", "早上", "晚上", "傍晚") and hour == 12:
            return None
        elif period == "中午" and hour not in (11, 12):
            return None
    return hour, minute, "minute" if minute_text is not None else "hour"


def find_datetimes(text: str) -> list[DatetimeHit]:
    """按原文位置返回日期；排版空白不改变偏移，时刻不跨过下一日期。"""
    hits: list[DatetimeHit] = []
    dates = list(_DATE.finditer(text))
    for index, match in enumerate(dates):
        try:
            day = datetime(*(int(value) for value in match.groups()))
        except ValueError:
            continue
        next_start = dates[index + 1].start() if index + 1 < len(dates) else len(text)
        clock = _TIME.match(text, match.end(), min(next_start, match.end() + 128))
        value, precision, time_status = day, "day", "missing"
        value_end = match.end()
        if clock:
            value_end = clock.end()
            resolved = _clock_value(clock)
            unsupported_tail = _UNSUPPORTED_CLOCK_TAIL.match(text, value_end, next_start)
            if unsupported_tail:
                value_end = unsupported_tail.end()
                resolved = None
            if resolved is None:
                time_status = "invalid"
            else:
                hour, minute, precision = resolved
                try:
                    value = day + timedelta(hours=hour, minutes=minute)
                except OverflowError:
                    time_status, precision = "invalid", "day"
                else:
                    time_status = "valid"
        hits.append(DatetimeHit(value, match.start(), match.end(), value_end, precision, time_status))
    return hits


def date_expression_spans(text: str) -> list[tuple[int, int]]:
    """原始年月日表达区间，包含非法日历值；用于覆盖检查，不能当日期值。"""
    return [(match.start(), match.end()) for match in _DATE.finditer(text)]


def extract_dates(text: str) -> list[datetime]:
    """兼容解析器的日期候选列表；需要精度/证据的调用方用find_datetimes。"""
    return sorted({hit.dt for hit in find_datetimes(text)})
