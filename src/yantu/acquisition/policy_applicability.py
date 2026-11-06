"""核对分源阶段与适用范围表述；表述支持不授予机会绑定或发布权限。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from .source_evidence import EvidenceResolver

APPLICABILITY_VERSION = "policy-applicability-v2-notice-scope"
_STAGES = {
    "pre_registration": r"预报名",
    "assessment": r"复试|综合考核|材料审核|资格审核",
    "formal_admission": r"正式程序|正式办理|待录取|推免生注册|推免生填报志愿",
    "late_application": r"错过预报名",
}
_PROGRAM = re.compile(r"(?<!\d)(?P<base>\d{6})(?!\d)(?:\s*[-－—]\s*(?P<dash>\d{2})(?![\d.．])|\s*[（(]\s*(?P<paren>\d{2})(?:\s+[\u4e00-\u9fff]+)?\s*[）)])?")
_QUALIFICATION = re.compile(r"(?P<wording>已获得或预计获得|已获得)就读学校推荐免试资格的优秀应届本科毕业生[。；;]?")
_JOINT_RESTRICTION = re.compile(
    r"(?P<base>\d{6})[（(](?P<direction>\d{2})信息技术创新[）)]为校际联培项目，"
    r"推免阶段仅招收(?P<project>[\u4e00-\u9fff]+)联培项目研究生，其他联培项目研究生在统招阶段招收[。]?"
)
_SCOPED_QUALIFICATION = re.compile(
    r"本条仅适用于：入学年份：(?P<enrollment_year>20\d{2})；培养单位：(?P<unit>[\u4e00-\u9fff]{2,80})；"
    r"专业代码：(?P<base_code>\d{6})；方向码：(?P<direction_code>\d{2}|无)；"
    r"学位：(?P<degree>硕士|直博)；招生阶段：(?P<stage>预报名|综合考核|正式推免|补录)；"
    r"适用条款：(?P<wording>已获得或预计获得|已获得)就读学校推荐免试资格的优秀应届本科毕业生[。；;]?"
)
_DEGREES = {"硕士": "masters", "直博": "direct_doctorate"}
_BINDING_STAGES = {"预报名": "pre_registration", "综合考核": "assessment", "正式推免": "formal_admission", "补录": "late_application"}
_UNKNOWN_UNITS = {"未知", "未定", "暂无", "待定", "未提供", "不详", "待确认", "任意", "全部", "所有", "未填写"}


@dataclass(frozen=True)
class ApplicabilityTarget:
    """待匹配的目标范围，仅作查询值；不可用它填补原文缺失的任何轴。"""

    enrollment_year: int
    unit: str
    base_code: str
    direction_code: str | None
    degree: str
    stage: str

    def __post_init__(self) -> None:
        if (type(self.enrollment_year) is not int or not 2000 <= self.enrollment_year <= 2099
                or not isinstance(self.unit, str) or not re.fullmatch(r"[\u4e00-\u9fff]{2,80}", self.unit) or self.unit in _UNKNOWN_UNITS
                or not isinstance(self.base_code, str) or not re.fullmatch(r"\d{6}", self.base_code)
                or (self.direction_code is not None and (not isinstance(self.direction_code, str) or not re.fullmatch(r"\d{2}", self.direction_code)))
                or self.degree not in _DEGREES.values() or self.stage not in _BINDING_STAGES.values()):
            raise ValueError("目标范围必须显式提供有效年度、完整培养单位、六位专业码、两位方向码或null、学位及阶段")


@dataclass(frozen=True)
class _Evidence:
    """字段清单中的一个完整条目或章节；定位始终复用同源原文。"""

    field: str
    claim: dict
    context_refs: tuple[str, ...]


def _evidence_units(inventory: dict) -> list[_Evidence]:
    units = []
    if inventory.get("documentPreamble"):
        units.append(_Evidence("context", inventory["documentPreamble"], ()))
    units.extend(_Evidence("context", claim, ()) for claim in inventory.get("contextClaims", ()))
    units.extend(_Evidence("context", row["claim"], ()) for row in inventory.get("unclassifiedSections", ()))
    for name, field in inventory["fields"].items():
        for section in field["sections"]:
            rows = section.get("items") or [section]
            for row in rows:
                units.append(_Evidence(name, row["claim"], tuple(row.get("contextRefs", ()))))
        for row in field["relatedExcerpts"]:
            units.append(_Evidence(name, row["claim"], tuple(row.get("contextRefs", ()))))
    return units


def _plain_statement(quote: str) -> str:
    """只为句式匹配去空白及一级编号；引用自身不清洗、不重算坐标。"""
    compact = re.sub(r"\s+", "", quote)
    return re.sub(r"^\d+[.．、]", "", compact)


def _finding(unit: _Evidence, **values) -> dict:
    return {"evidenceRef": unit.claim["path"], "contextRefs": list(unit.context_refs), **values}


def _neutral_binding_context(unit: _Evidence, claims: dict[str, dict], source_text: str) -> bool:
    """完整源只能有当前受支持条款及中性日期/条件标题；其他正文可能限定全篇。"""
    for ref in unit.context_refs:
        claim = claims[ref]
        text = claim["quote"]
        if claim["start"] <= unit.claim["start"] and unit.claim["end"] <= claim["end"]:
            left, right = unit.claim["start"] - claim["start"], unit.claim["end"] - claim["start"]
            text = text[:left] + text[right:]
        text = re.sub(r"\s+", "", text)
        if not (not text or re.fullmatch(r"20\d{2}-\d{2}-\d{2}", text)
                or re.fullmatch(r"(?:[一二三四五六七八九十]+、|[（(][一二三四五六七八九十]+[）)])(?:报名条件|申请条件|申报条件|基本条件)[：:]?", text)):
            return False
    remainder = source_text[:unit.claim["start"]] + source_text[unit.claim["end"]:]
    remainder = re.sub(r"\s+", "", remainder)
    return bool(re.fullmatch(r"(?:20\d{2}-\d{2}-\d{2})?(?:[一二三四五六七八九十]+、|[（(][一二三四五六七八九十]+[）)])(?:报名条件|申请条件|申报条件|基本条件)[：:]?", remainder))


def _target_binding(unit: _Evidence, match, target: ApplicabilityTarget, claims: dict[str, dict], blockers: list[str], source_text: str) -> dict:
    binding = _finding(unit, status="unresolved", sourceScope=None, mismatchedAxes=[], reason="SOURCE_SCOPE_INCOMPLETE")
    if match is None:
        return binding
    scope = {"enrollment_year": int(match["enrollment_year"]), "unit": match["unit"],
             "base_code": match["base_code"], "direction_code": None if match["direction_code"] == "无" else match["direction_code"],
             "degree": _DEGREES[match["degree"]], "stage": _BINDING_STAGES[match["stage"]]}
    binding["sourceScope"] = scope
    if scope["unit"] in _UNKNOWN_UNITS:
        binding["reason"] = "SOURCE_IDENTITY_PLACEHOLDER"
        return binding
    if (any(code in blockers for code in ("SOURCE_GAPS_REQUIRE_REVIEW", "INVENTORY_GAPS_REQUIRE_REVIEW"))
            or not _neutral_binding_context(unit, claims, source_text)):
        binding["reason"] = "SOURCE_OR_CONTEXT_UNRESOLVED"
        return binding
    mismatches = [axis for axis, value in scope.items() if value != getattr(target, axis)]
    binding.update(status="not_applicable" if mismatches else "applicable", mismatchedAxes=mismatches,
                   reason="EXPLICIT_CLAUSE_SCOPE_MISMATCH" if mismatches else "EXPLICIT_CLAUSE_SCOPE_MATCH")
    return binding


def review_document_applicability(inventory: dict, target: ApplicabilityTarget | None = None) -> dict:
    """返回受限表述核对与绑定阻断；输入必须来自本轮校验快照后的字段清单。

    支持状态仅证明完整条目符合声明的正向句式，不裁定该条目当前生效、
    适用哪些机会或学生。未知来源、父节限定、表格关系与目录依据均保留。
    """
    result = {"stages": [], "qualification": [], "programs": [], "restrictions": [], "degreeScopes": [],
              "status": "unresolved", "bindingDecision": "hold", "publicationStatus": "not_published",
              "currentAvailability": "not_evaluated", "coverage": "not_established",
              "identityBinding": {"enrollmentYear": "unresolved", "unit": "unresolved", "target": "unresolved"},
              "targetBindings": [], "targetStatus": "provided" if target else "not_provided",
              "blockers": ["OPPORTUNITY_IDENTITY_NOT_VERIFIED", "CROSS_SOURCE_SCOPE_NOT_VERIFIED"],
              "analyzerVersion": APPLICABILITY_VERSION}
    fields = inventory["fields"]
    statuses = {field["status"] for field in fields.values()}
    if statuses <= {"source_unavailable", "source_unreadable", "not_attempted"}:
        result["status"] = "not_attempted"
        result["blockers"].append("SOURCE_NOT_USABLE")
        return result
    if not inventory.get("text", "").strip():
        result["blockers"].append("BODY_TEXT_MISSING")
        return result
    if (inventory.get("issues") or any(field["issues"] for field in fields.values())
            or any(section.get("issues") for field in fields.values() for section in field["sections"])):
        result["blockers"].append("INVENTORY_GAPS_REQUIRE_REVIEW")
    if inventory.get("sourceQuality", {}).get("parseStatus") != "complete" or inventory.get("inputGaps"):
        result["blockers"].append("SOURCE_GAPS_REQUIRE_REVIEW")
    seen = {name: set() for name in ("stages", "programs", "degreeScopes", "restrictions")}
    claims = EvidenceResolver([inventory]).body_claims(0)
    for unit in _evidence_units(inventory):
        text = _plain_statement(unit.claim["quote"])
        span = (unit.claim["start"], unit.claim["end"])
        stages = [stage for stage, pattern in _STAGES.items() if re.search(pattern, text)]
        if stages and span not in seen["stages"]:
            seen["stages"].add(span)
            result["stages"].append(_finding(unit, observedStages=stages, status="observed_only",
                bindingStatus="unresolved", conditional=bool(re.search(r"错过|是否|开通后|开放后|另行|以.*为准", text))))
        if unit.field == "conditions" and re.search(r"推荐免试资格|推免资格", text):
            match = _QUALIFICATION.fullmatch(text)
            scoped = _SCOPED_QUALIFICATION.fullmatch(text)
            supported = match or scoped
            result["qualification"].append(_finding(unit,
                wording=("acquired_or_expected" if supported["wording"] == "已获得或预计获得" else "acquired") if supported else None,
                status="supported_wording" if supported else "unresolved",
                stageBinding="unresolved", programBinding="unresolved", policyEffect="not_evaluated"))
            if target is not None:
                result["targetBindings"].append(_target_binding(unit, scoped, target, claims, result["blockers"], inventory["text"]))
        # 代码与后续人数之间的空白是边界；不可先压紧表格文本再识别数字。
        for match in _PROGRAM.finditer(unit.claim["quote"]) if unit.field == "scope" else ():
            base, direction = match["base"], match["dash"] or match["paren"]
            key = (*span, base, direction)
            if key not in seen["programs"]:
                seen["programs"].add(key)
                result["programs"].append(_finding(unit, baseCode=base, directionCode=direction,
                    status="observed_only", relationshipStatus="unresolved"))
        degrees = [degree for degree, pattern in (("direct_doctorate", r"直博"), ("masters", r"硕士")) if re.search(pattern, text)]
        if degrees and span not in seen["degreeScopes"]:
            seen["degreeScopes"].add(span)
            result["degreeScopes"].append(_finding(unit, observedDegrees=degrees, status="observed_only", bindingStatus="unresolved"))
        if re.search(r"仅|限|统招|以.*为准", text) and span not in seen["restrictions"]:
            seen["restrictions"].add(span)
            match = _JOINT_RESTRICTION.fullmatch(text)
            result["restrictions"].append(_finding(unit, status="supported_wording" if match else "unresolved",
                program={"baseCode": match["base"], "directionCode": match["direction"]} if match else None,
                recommendationProject=match["project"] if match else None,
                otherProjectsStage="unified_exam" if match else None, bindingStatus="unresolved"))
    if len({stage for row in result["stages"] for stage in row["observedStages"]}) > 1:
        result["blockers"].append("MULTIPLE_STAGE_USES_DO_NOT_PROPAGATE_DOCUMENT_STAGE")
    if len({row["wording"] for row in result["qualification"] if row["wording"]}) > 1:
        result["blockers"].append("LOCAL_QUALIFICATION_WORDING_DIFFERS")
    result["blockers"].append("FINAL_PROGRAM_DIRECTORY_NOT_VERIFIED")
    return result


def compare_qualification_wording(sources: list[dict]) -> list[dict]:
    """汇总同一资料包中的表述差异；不声称同机会、政策冲突、收紧或替代。"""
    observations = [
        {"sourceIndex": index, "evidenceRef": row["evidenceRef"], "wording": row["wording"]}
        for index, source in enumerate(sources) for row in source["applicabilityReview"]["qualification"]
        if row["status"] == "supported_wording"
    ]
    if len({row["sourceIndex"] for row in observations}) < 2 or len({row["wording"] for row in observations}) < 2:
        return []
    return [{"kind": "QUALIFICATION_WORDING_DIFFERS", "observations": observations,
             "mergeDecision": "blocked", "replacementStatus": "not_established",
             "reason": "不同来源表述不同；年度、阶段及专业等价关系未核验，禁止拼为统一门槛"}]
