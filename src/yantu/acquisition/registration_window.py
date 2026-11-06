"""单文档报名时间核验；只返回有原文支持的分钟边界，不裁定跨阶段适用性。"""

import re
from datetime import datetime

from ..domain.models import Window
from .policy_verification import PolicyValidationError, SourceClaim
from .text_dates import date_expression_spans, find_datetimes

_CLAUSE_BREAK = re.compile(r"[。；;]")
_UNCERTAIN_CONTEXT = re.compile(r"不再|取消|废止|作废|失效|原定|原报名|旧的|旧版|预计|暂定|拟定|拟于|可能|待定|另行确定|以[^。；;]*为准|延期|延后|调整|变更|更正")
_BOUNDARY_MODIFIER = re.compile(r"\s*(?:以后|之后|之前|以前|前后|左右|前|后|起|止)")
_NEUTRAL_PREFIX = re.compile(r"(?:(?:我院|本院|学院|本校|学校|院系|本学院|今年|本年度|本轮|推免|的))*")
_POSITIVE_SUFFIX = re.compile(
    r"(?:[。；;]?|[，,]请(?:提交完整资料|按时报名|按时提交)[。；;]?|"
    r"，有意申请的考生登录[\u4e00-\u9fff（）()]{2,60}报考服务系统[（(]"
    r"https://[A-Za-z0-9:/?=&._%+\-]+，以下简称报名系统[）)]注册账号，"
    r"在截止时间前填写并提交申请志愿。?)"
)


def _statement_span(text: str, start: int, value_end: int) -> tuple[int, int]:
    """保留整个分句上下文，并拒绝本核验器尚不能裁定的限定语。"""
    previous = list(_CLAUSE_BREAK.finditer(text, 0, start))
    left = previous[-1].end() if previous else 0
    following = _CLAUSE_BREAK.search(text, value_end)
    right = following.end() if following else len(text)
    if _UNCERTAIN_CONTEXT.search(text[left:right]) or _BOUNDARY_MODIFIER.match(text, value_end):
        raise PolicyValidationError("报名时间含取消/更改/不确定或边界限定，需独立语义核验")
    prefix = re.sub(r"\s+", "", text[left:start]).lstrip("：:")
    prefix = re.sub(r"^[（(][一二三四五六七八九十\d]+[）)](?:时间及方式|报名截止时间|报名时间)", "", prefix)
    suffix = re.sub(r"\s+", "", text[value_end:right])
    # 正向声明语法是有限子集。词表漏掉的新否定或未知说明不能自动获得授权。
    if not _NEUTRAL_PREFIX.fullmatch(prefix) or not _POSITIVE_SUFFIX.fullmatch(suffix):
        raise PolicyValidationError("报名时间声明的前后文超出已支持的正向句式，保留未知")
    return left, right


def extract_registration_window(text: str) -> tuple[Window, list[SourceClaim]]:
    """只接受同页唯一、明确到分钟的单通道时间；冲突与日级边界不猜测。"""
    matches: dict[str, list[tuple[datetime, SourceClaim]]] = {"start": [], "end": []}
    header = re.search(r"[一二三四五六七八九十]+、报名(?:流程|方式)?(?!条件)", text)
    if not header:
        return Window("配置机会的单一报名通道"), []
    following = re.search(r"[一二三四五六七八九十]+、", text[header.end():])
    end_offset = header.end() + following.start() if following else len(text)
    registration_text = text[header.end():end_offset]
    if re.search(r"须同时|必须同时|均需|都需|同时提交|两个系统|两套", registration_text):
        raise PolicyValidationError("发现额外必需报名通道，当前单通道切片不能确认完整窗口")
    hits = find_datetimes(registration_text)
    by_start = {hit.start: index for index, hit in enumerate(hits)}
    used_dates: list[tuple[int, int]] = []
    labels = re.compile(r"(?P<label>(?:预报名|报名)(?:(?:开始|开放|截止)时间|(?:的)?起止时间))(?!\s*[前后内])(?:为|是)?[：:]?\s*")
    for label in labels.finditer(registration_text):
        index = by_start.get(label.end())
        if index is None:
            prefix = registration_text[max(0, label.start() - 16):label.start()]
            if label[0].strip() == label["label"] and re.search(r"[（(][一二三四五六七八九十\d]+[）)]\s*$", prefix):
                continue  # 子章节标题本身不声明日期，下面的具体时间另行核验。
            raise PolicyValidationError("报名时间声明缺少支持的日期/时刻")
        hit = hits[index]
        if hit.precision != "minute" or hit.time_status != "valid":
            raise PolicyValidationError("报名边界缺少有效的分钟级时刻")
        value_end = hit.value_end
        values = [("end" if "截止" in label["label"] else "start", hit.dt)]
        covered = [(hit.start, hit.end)]
        if "起止" in label["label"]:
            if index + 1 >= len(hits):
                raise PolicyValidationError("起止时间缺少终点")
            last = hits[index + 1]
            separator = re.sub(r"\s+", "", registration_text[hit.value_end:last.start])
            if separator not in ("至", "到", "—", "——", "-", "--", "~", "～") or last.precision != "minute" or last.time_status != "valid":
                raise PolicyValidationError("起止范围的连接或终点不明确到分钟")
            value_end = last.value_end
            values = [("start", hit.dt), ("end", last.dt)]
            covered.append((last.start, last.end))
        left, right = _statement_span(registration_text, label.start(), value_end)
        start, claim_end = header.end() + left, header.end() + right
        for key, value in values:
            matches[key].append((value, SourceClaim(f"window.{key}", text[start:claim_end], start, claim_end)))
        used_dates.extend(covered)
    # 引文可以很长；覆盖必须按实际解析过的日期计算，不能凭长引文掩盖遗漏。
    raw_dates = date_expression_spans(registration_text)
    raw_dates += [match.span() for match in re.finditer(r"(?<!\d)\d{1,2}\s*月\s*\d{1,2}\s*日", registration_text)]
    if any(not any(left <= start and end <= right for left, right in used_dates) for start, end in raw_dates):
        raise PolicyValidationError("报名段存在未解释日期/额外通道，当前单通道切片不能确认完整窗口")
    if any(len(values) > 1 for values in matches.values()):
        raise PolicyValidationError("同页存在多个报名时间，不能擅自选择最早/最晚")
    start = matches["start"][0][0] if matches["start"] else None
    end = matches["end"][0][0] if matches["end"] else None
    if start and end and start > end:
        raise PolicyValidationError("报名窗口起止矛盾")
    return Window("配置机会的单一报名通道", start=start, end=end), [claim for values in matches.values() for _, claim in values]
