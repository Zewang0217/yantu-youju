"""将已核验通知范围组合为逐条款目标判断；本源条件与跨源专业声明分别留证。"""

from __future__ import annotations

from .parse_models import ParseResult
from .policy_applicability import ApplicabilityTarget
from .source_evidence import SourceClaim, claim_record, EvidenceResolver


def declared_programs(sources: list[dict]) -> list[dict]:
    """只复用同年同单位的本地招生表身份，不把后续通知条件复制给前序通知。"""
    rows = []
    for index, source in enumerate(sources):
        scope = source.get('noticeScope', {})
        if scope.get('status') != 'supported_notice_scope':
            continue
        for row in source['noticeStructure']['programRows']:
            rows.append({**row, 'unit': scope['unit'], 'enrollmentYear': scope['enrollmentYear'],
                         'sourceIndex': index, 'identityEvidence': scope['identityEvidence']})
    return rows


def _membership(scope: dict, target: ApplicabilityTarget, programs: list[dict]) -> dict | None:
    return next((row for row in programs if row['unit'] == scope['unit'] and row['enrollmentYear'] == scope['enrollmentYear']
                 and row['baseCode'] == target.base_code and row['directionCode'] == target.direction_code and row['degree'] == target.degree), None)


def _claim(source: dict, parsed: ParseResult, start: int, end: int) -> str:
    claims = source.setdefault('naturalEvidence', [])
    path = f'naturalEvidence[{len(claims)}]'
    pages = parsed.pages if source['view'] == 'pdf_text' else ()
    claims.append(claim_record(SourceClaim(path, source['text'][start:end], start, end), source['text'], pages))
    return path


def _slice(source: dict, parsed: ParseResult, parent: dict, left: str, right: str | None = None) -> str:
    start, end = EvidenceResolver([source]).compact_span(0, parent, left, right)
    return _claim(source, parsed, start, end)


def _root_ref(source: dict, path: str, source_index: int) -> dict:
    return {'sourceIndex': source_index, 'view': source['view'], 'path': path}


def _bind(source: dict, target: ApplicabilityTarget, programs: list[dict], *, source_index: int, path: str,
          kind: str, stage: str, degree: str | None = None, program: tuple[str, str | None] | None = None,
          conditional: bool = False, **values) -> dict:
    scope = source['noticeScope']
    evidence = [_root_ref(source, path, source_index)] + [dict(ref, sourceIndex=source_index) for ref in scope['identityEvidence']]
    binding = {'kind': kind, 'evidenceRef': path, 'contextRefs': [], 'supportingEvidence': evidence,
               'status': 'unresolved', 'sourceScope': {'unit': scope['unit'], 'enrollment_year': scope['enrollmentYear'], 'stage': stage},
               'mismatchedAxes': [], 'reason': 'PROGRAM_MEMBERSHIP_UNPROVEN', 'conditional': conditional,
               'currentAvailability': 'not_evaluated', 'finalDirectoryStatus': 'unresolved', **values}
    if scope['status'] != 'supported_notice_scope':
        binding['reason'] = 'NOTICE_CONTEXT_UNRESOLVED'
        return binding
    mismatches = []
    if scope['unit'] != target.unit:
        mismatches.append('unit')
    if scope['enrollmentYear'] != target.enrollment_year:
        mismatches.append('enrollment_year')
    if kind == 'qualification' and scope['primaryStage'] == 'assessment' and target.stage != 'assessment':
        if not mismatches:
            binding['reason'] = 'ASSESSMENT_CONDITION_OTHER_STAGE_UNPROVEN'
            return binding
    elif stage != target.stage and not (kind == 'joint_program_restriction' and stage == 'recommendation'):
        mismatches.append('stage')
    if degree is not None and degree != target.degree:
        mismatches.append('degree')
    if program is not None:
        binding['sourceScope'].update(base_code=program[0], direction_code=program[1])
        if program[0] != target.base_code:
            mismatches.append('base_code')
        if program[1] != target.direction_code:
            mismatches.append('direction_code')
    if mismatches:
        binding.update(status='not_applicable', mismatchedAxes=mismatches, reason='NOTICE_CLAUSE_SCOPE_MISMATCH')
        return binding
    member = _membership(scope, target, programs)
    if member is None:
        return binding
    binding['supportingEvidence'].append(dict(member['evidence'], sourceIndex=member['sourceIndex']))
    binding['supportingEvidence'].extend(dict(ref, sourceIndex=member['sourceIndex']) for ref in member['identityEvidence'])
    binding['sourceScope'].update(base_code=member['baseCode'], direction_code=member['directionCode'], degree=member['degree'])
    binding.update(status='applicable', reason='NOTICE_AND_ANNOUNCED_PROGRAM_SCOPE_MATCH', programMembership='announced_in_notice')
    return binding


def apply_notice_bindings(source: dict, parsed: ParseResult, target: ApplicabilityTarget | None,
                          programs: list[dict], source_index: int) -> None:
    """补充分源范围证据及真实条款判断；不改原条件、不解除发布或最终目录门禁。"""
    scope, review = source['noticeScope'], source['applicabilityReview']
    review['noticeScopeStatus'] = scope['status']
    review['finalDirectoryStatus'] = 'unresolved'
    if scope['status'] != 'supported_notice_scope':
        return
    review['identityBinding'] = {'enrollmentYear': 'supported_in_notice', 'unit': 'supported_in_notice', 'target': 'not_evaluated'}
    if target is None:
        return
    result = []
    for row in review['qualification']:
        if row['status'] != 'supported_wording':
            continue
        result.append(_bind(source, target, programs, source_index=source_index, path=row['evidenceRef'],
                            kind='qualification', stage=scope['primaryStage'], wording=row['wording']))
    fields = source['fields']
    channel = fields['channels']['sections'][0]['claim']
    if scope['primaryStage'] == 'pre_registration':
        path = _slice(source, parsed, channel, '我院预报名的起止时间为', '注意事项：')
        result.append(_bind(source, target, programs, source_index=source_index, path=path, kind='registration_channel', stage='pre_registration'))
        for item in fields['materials']['sections'][0]['items']:
            result.append(_bind(source, target, programs, source_index=source_index, path=item['claim']['path'], kind='material', stage='pre_registration'))
        for degree, left, right in (
                ('direct_doctorate', '直博生申请人须填写意向导师姓名', '（2）'),
                ('masters', '硕士申请人填写意向研究领域', '2.')):
            path = _slice(source, parsed, channel, left, right)
            result.append(_bind(source, target, programs, source_index=source_index, path=path, kind='degree_form_requirement', stage='pre_registration', degree=degree))
        restriction = next(row for row in review['restrictions'] if row['status'] == 'supported_wording')
        program = restriction['program']
        result.append(_bind(source, target, programs, source_index=source_index, path=restriction['evidenceRef'],
                            kind='joint_program_restriction', stage='recommendation', program=(program['baseCode'], program['directionCode']),
                            requiredProject=restriction['recommendationProject'], otherProjectsStage='unified_exam'))
        other = fields['other_policies']['sections'][0]['claim']
        formal = _slice(source, parsed, other, '4.', '5.')
        late = _slice(source, parsed, other, '5.', '6.')
        result.append(_bind(source, target, programs, source_index=source_index, path=formal, kind='formal_qualification', stage='formal_admission', wording='acquired'))
        result.append(_bind(source, target, programs, source_index=source_index, path=late, kind='conditional_direct_application', stage='late_application', conditional=True))
    else:
        parent = fields['assessment']['sections'][0]['claim']
        for left, right, kind, stage in (
                ('（一）材料审核', '（二）资格审核', 'material_review', 'assessment'),
                ('（二）资格审核', '（三）综合考核', 'qualification_review', 'assessment'),
                ('（三）综合考核', '（四）考核结果', 'assessment_details', 'assessment'),
                ('（四）考核结果', None, 'formal_procedure', 'formal_admission')):
            path = _slice(source, parsed, parent, left, right)
            result.append(_bind(source, target, programs, source_index=source_index, path=path, kind=kind, stage=stage))
        pre = _slice(source, parsed, channel, '学院通过', '错过预报名')
        late = _slice(source, parsed, channel, '错过预报名', '（二）报名截止时间')
        result.append(_bind(source, target, programs, source_index=source_index, path=pre, kind='pre_registration_reference', stage='pre_registration', detailsStatus='requires_referenced_notice'))
        result.append(_bind(source, target, programs, source_index=source_index, path=late, kind='conditional_direct_application', stage='late_application', conditional=True))
    review['targetBindings'] = result
    review['identityBinding']['target'] = 'evaluated_per_clause'
