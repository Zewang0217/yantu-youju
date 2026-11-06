"""自然语言通知的分源范围核验；闭合正文语法阻止未知全局限定逃逸。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Callable

_UNIT = r'(?P<unit>[\u4e00-\u9fff]{2,50}大学[\u4e00-\u9fff]{1,30}学院)(?:（(?P<alias>[\u4e00-\u9fff]{2,40}学院)）)?'
_TITLE = re.compile(_UNIT + r'(?P<year>20[0-9]{2})年(?P<role>推免生综合考核实施细则|接收优秀应届本科毕业生免试攻读研究生（含本科直博）预报名通知)')
_URL = r'https://[A-Za-z0-9:/?=&._%+\-]+'
_DATE = r'20[0-9]{2}年[0-9]{1,2}月[0-9]{1,2}日'
_TIME = r'[0-9]{1,2}[：:][0-9]{2}'
_QUALIFICATION = r'(?:已获得或预计获得|已获得)就读学校推荐免试资格的优秀应届本科毕业生'
_CONDITIONS = (
    r'(?:一|二)、报名条件1[.．]中华人民共和国公民，拥护中国共产党领导，遵纪守法、品德良好；'
    r'2[.．]' + _QUALIFICATION + r'；3[.．]学习成绩优异，具有较强的创新意识、研究潜质和专业基础；'
    r'(?:4[.．]具有较强的英语听说和写作能力，通过英语六级或其他社会认可度较高的英语水平测试成绩（如雅思、托福等）者优先；)?'
    r'[45][.．]诚实守信，学风端正，无任何(?:有违|违反)学术道德、专业伦理等行为；'
    r'[56][.．]身心健康，符合国家规定的体检要求。'
)
_PRE_CHANNEL = (
    r'二、报名流程（一）时间及方式我院预报名的起止时间为' + _DATE + _TIME + r'——' + _DATE + r'中午' + _TIME +
    r'，有意申请的考生登录[\u4e00-\u9fff]{2,50}大学研究生报考服务系统（' + _URL + r'，以下简称报名系统）注册账号，在截止时间前填写并提交申请志愿。'
    r'每位考生在我院仅可填报一条独立志愿。在院系规定报名截止时间前，考生可自行取消提交状态、修改完善后再次提交。'
    r'注意事项：1[.．]“申请信息——备用信息”：（1）直博生申请人须填写意向导师姓名；（2）硕士申请人填写意向研究领域（4选1）：'
    r'软件科学与技术、嵌入式软件与智能系统、安全可信理论与技术、工业软件。'
    r'2[.．][0-9]{6}（[0-9]{2}信息技术创新）为校际联培项目，推免阶段仅招收[\u4e00-\u9fff]{2,40}大学联培项目研究生，其他联培项目研究生在统招阶段招收。'
)
_MATERIALS = (
    r'（二）报名材料1[.．]有效居民身份证；2[.．]教育部学籍在线验证报告；'
    r'3[.．]预报名申请表（报名系统填写完毕后下载打印、签字后扫描上传）；'
    r'4[.．]诚信承诺书（报名系统下载打印、签字后扫描上传）；'
    r'5[.．]本科阶段成绩排名证明（由本科院校教务出具的前六学期本科阶段成绩排名证明，须加盖公章）；'
    r'6[.．]英语能力等级证书；7[.．]个人简介，合成一个PDF，不超过[0-9]+MB，要求制作封面及目录，包含：（1）'
    r'个人陈述（介绍本人的学习业绩、外语能力、获奖情况、论文发表情况、曾参与过的科研项目与研究成果、学术研究兴趣及未来研究方向等）；'
    r'（2）本科成绩单；（3）佐证材料（如：论文发表、专利发明、各类获奖或资格证书等扫描件）。'
)
_PRE_OTHER = (
    r'三、其他说明1[.．]考生须保证提交材料真实、准确，不得弄虚作假，不得有学术不端行为。'
    r'2[.．]报名截止后，我院将组织专家对申请材料进行审核，遴选复试入围名单，并根据上级部门和学校政策规范组织复试考核工作。'
    r'请考生密切关注报名系统中填写的邮箱，以及系统中的审核状态，及时确认是否参加复试并查看复试相关通知。'
    r'3[.．]报名系统中展示的学科专业及研究方向仅供参考，我校20[0-9]{2}年推免专业目录最终以全国推荐免试攻读研究生（免初试、转段）信息公开管理服务系统'
    r'（网址：' + _URL + r'，以下简称推免服务系统）公布为准。'
    r'4[.．]除预报名外，所有通过推荐免试方式攻读我校20[0-9]{2}年研究生的考生均须在正式取得推免资格后，在推免服务系统履行填写报考志愿、接收并确认复试及待录取通知等正式程序。'
    r'推免服务系统正式开放时间请及时关注中国研究生招生信息网相关公告。'
    r'5[.．]错过预报名的考生如仍有意愿报考我院，可在推免服务系统开通后直接报名。我院将结合实际，确定是否再次组织推免复试考核与录取。'
    r'6[.．]若上级部门对推免招生工作有新的要求，我院将遵照执行。'
)
_ASSESSMENT_CHANNEL = (
    r'三、报名（一）报名方式学院通过[\u4e00-\u9fff]{2,50}大学研究生报考服务系统（网址：' + _URL + r'，以下简称“学校报考系统”）接受预报名，详见《'
    + _UNIT.replace('(?P<unit>', '(?:').replace('(?P<alias>', '(?:') +
    r'20[0-9]{2}年接收优秀应届本科毕业生免试攻读研究生（含本科直博）预报名通知》（网址：' + _URL + r'）。'
    r'错过预报名但仍有意报考本院的推免生，可在“全国推荐免试攻读研究生（免初试、转段）信息公开管理服务系统”'
    r'（网址：' + _URL + r'，以下简称“教育部推免系统”）开放后在教育部推免系统中直接报名。学院将根据实际情况，确定是否再次组织复试；届时请密切关注我院公告、通知。'
    r'（二）报名截止时间学院推免预报名截止时间为：' + _DATE + r'中午' + _TIME
)
_ASSESSMENT = (
    r'四、综合考核（一）材料审核我院将组织专家对推免生的材料进行审核，根据材料审核成绩由高到低择优确定入围综合考核的推免生名单，并通知推免生参加综合考核。'
    r'（二）资格审核我院将在综合考核前组织实施资格审核，推免生应配合完成。未参加资格审核或资格审核不通过的推免生，我院不予安排综合考核。'
    r'（三）综合考核1．时间：预计20[0-9]{2}年[0-9]{1,2}月[0-9]{1,2}-[0-9]{1,2}日，以邮件通知为准'
    r'2．地点及方式：[\u4e00-\u9fff]{2,50}大学[\u4e00-\u9fff]{2,20}校区[\u4e00-\u9fff]{2,20}大楼'
    r'具体安排以邮件通知为准，请推免生注意查收邮件，及时回复是否参加，并按规定时间参加综合考核，否则视为放弃。请推免生务必保持通信畅通，随时关注邮件，以免错失机会。'
    r'3．内容、形式与总成绩计算（1）考核内容及形式包括：1）专业素质（面试，满分[0-9]+分）2）综合能力（面试，满分[0-9]+分）3）外语能力（面试，满分[0-9]+分）'
    r'综合考核总成绩=专业素质成绩×[0-9]+%\+综合能力成绩×[0-9]+%\+外语能力成绩×[0-9]+%。'
    r'注：面试前进行编程能力考察，时长[0-9]+分钟，考察结果不计入综合考核成绩。'
    r'（2）综合考核总成绩在[0-9]+分（含）以上为综合考核合格，否则为综合考核不合格。'
    r'（3）综合考核过程中还将进行思想政治素质和品德考核，成绩不计入综合考核总成绩，但该项考核不合格者为综合考核不合格。'
    r'（四）考核结果、拟录取与教育部推免系统流程综合考核结束后[0-9]+日内，我院将以一定方式公布考核结果，并对所有综合考核合格的推免生，按照各学科及专业方向根据综合考核总成绩由高到低分别排序，并结合当年招生计划确定推荐拟录取名单，报学校审核批准。'
    r'我院将通知相关推免生至教育部推免系统正式办理复试录取各项程序。主要包括：推免生注册→推免生填报志愿→我院发出复试通知→推免生回复确认→我院发出拟录取通知→推免生回复确认等。'
    r'推免生须密切关注教育部推免系统开通注册时间、填报志愿等时间。未在我院规定时间内在教育部推免系统填报志愿，或未在我院规定时间内确认待录取的推免生，均将视为放弃待录取资格，我院将依次递补综合考核合格的推免生。教育部推免系统确认待录取后不得更改。'
)
_ASSESSMENT_OTHER = (
    r'五、其他说明1[.．]推免生须保证提交材料真实、准确，无弄虚作假、学术不端行为。'
    r'2[.．]推免招生录取的学科专业、校区、学制、学费、奖助及住宿等政策以当年招生简章和学科专业目录规定为准，如有疑问，可及时咨询我院。'
    r'3[.．]根据教育部相关规定，已被学校拟录取的推免生不得再报名参加当年全国硕士研究生招生考试。'
    r'4[.．]拟录取推免生应密切关注学校研究生招生信息网（网址：' + _URL + r'）、微信公众号（“[\u4e00-\u9fff]+”），以及我院电话、邮件等通知，以便及时了解招生信息。'
    r'5[.．]相关工作调整、更新及未尽事宜，将另行通知。'
)
_CONTACT = r'[\u4e00-\u9fff]{1,5}老师，电话：[0-9\-]+，邮箱：[A-Za-z0-9_.+\-]+@[A-Za-z0-9.\-]+'


def compact(text: str) -> str:
    """仅供语法识别；页码装饰剔除不改变原文引用坐标。"""
    return re.sub(r'\s+', '', re.sub(r'(?m)^\s*-\s*[0-9]+\s*-\s*$', '', text))


def _section(inventory: dict, name: str) -> str:
    sections = inventory['fields'][name]['sections']
    return compact(sections[0]['claim']['quote']) if len(sections) == 1 else ''


def _unit_label(identity: re.Match) -> str:
    return identity['unit'] + ('（' + identity['alias'] + '）' if identity['alias'] else '')


def _pre_context(inventory: dict, structure: dict, identity: re.Match) -> list[bool]:
    text = inventory['text']
    channel, material = _section(inventory, 'channels'), _section(inventory, 'materials')
    checks = [bool(material and channel.endswith(material)),
              bool(re.fullmatch(_PRE_CHANNEL, channel[:-len(material)] if material else channel))]
    footer = text[text.rfind('四、咨询方式'):]
    footer_pattern = (r'四、咨询方式联系人：[\u4e00-\u9fff]{1,5}老师邮箱：[A-Za-z0-9_.+\-]+@[A-Za-z0-9.\-]+电话：[0-9\-]+'
                      r'（学院暑假轮休期间，建议优先通过电子邮件方式咨询）' + re.escape(_unit_label(identity)) + _DATE)
    checks.append(bool(re.fullmatch(footer_pattern, compact(footer))))
    expected = compact(inventory['documentPreamble']['quote']) + _section(inventory, 'conditions') + channel + _section(inventory, 'other_policies') + compact(footer)
    checks.append(expected == compact(text))
    return checks


def _assessment_context(inventory: dict, structure: dict, identity: re.Match) -> list[bool]:
    text = inventory['text']
    tables = [value for name, value in structure.get('views', {}).items() if name.startswith('html_program_table_')]
    checks = [len(tables) == 1, not structure.get('issues')]
    table_text = compact(tables[0]['text']) if len(tables) == 1 else ''
    checks.append(_section(inventory, 'scope') == '一、招生学科专业' + table_text)
    footer = text[text.rfind('六、咨询及申诉'):]
    footer_pattern = (r'六、咨询及申诉（一）咨询' + _CONTACT.replace('老师，', '老师（博士），') + _CONTACT.replace('老师，', '老师（硕士），') +
        r'（二）申诉若对我院推免招生有异议，可以书面形式具名向我院进行申诉，联系方式如下。' + _CONTACT +
        r'[\u4e00-\u9fff0-9]+年推免考核名单\.pdf' + re.escape(_unit_label(identity)) + _DATE)
    checks.append(bool(re.fullmatch(footer_pattern, compact(footer))))
    expected = ''.join(_section(inventory, name) for name in ('scope', 'conditions', 'channels', 'assessment', 'other_policies')) + compact(footer)
    checks.append(expected == compact(text))
    return checks


@dataclass(frozen=True)
class _NoticeProfile:
    """一种已支持通知格式的闭合段落规则和上下文核验策略；不接受外部动态注册。"""

    section_patterns: tuple[tuple[str, str], ...]
    context_checks: Callable[[dict, dict, re.Match], list[bool]]


_PROFILES = MappingProxyType({
    ('pre_registration', 'pdf_text'): _NoticeProfile(
        (('conditions', _CONDITIONS), ('materials', _MATERIALS), ('other_policies', _PRE_OTHER)), _pre_context),
    ('assessment', 'html_content_text'): _NoticeProfile(
        (('conditions', _CONDITIONS), ('channels', _ASSESSMENT_CHANNEL), ('assessment', _ASSESSMENT), ('other_policies', _ASSESSMENT_OTHER)), _assessment_context),
})


def resolve_notice_scope(inventory: dict, structure: dict) -> dict:
    """识别完整支持的真实通知格式；任一新增/未知正文使上下文待审而非默认中性。"""
    result = {'status': 'unresolved', 'unit': None, 'enrollmentYear': None, 'primaryStage': None,
              'identityEvidence': [], 'blockers': [], 'finalDirectoryStatus': 'unresolved', 'grammarVersion': 'notice-scope-v1'}
    text = inventory.get('text', '')
    if not text:
        result['blockers'].append('BODY_TEXT_MISSING')
        return result
    is_pdf = inventory.get('view') == 'pdf_text'
    preamble = inventory.get('documentPreamble')
    if is_pdf:
        candidate = compact(preamble['quote']) if preamble else ''
        title_match = _TITLE.match(candidate)
        if title_match:
            year = title_match['year']
            intro = f'我院{year}年接收优秀应届本科毕业生免试攻读研究生（含本科直博），现将预报名工作安排通知如下：'
            if candidate[title_match.end():] != intro:
                title_match = None
        refs = [{'view': inventory['view'], 'path': 'documentPreamble'}]
    else:
        title_view = structure.get('views', {}).get('html_title')
        title_match = _TITLE.fullmatch(compact(title_view['text'])) if title_view else None
        refs = [{'view': 'html_title', 'path': 'claims[0]'}]
    if title_match is None:
        result['blockers'].append('UNSUPPORTED_OR_AMBIGUOUS_NOTICE_IDENTITY')
        return result
    unit, year = title_match['unit'], title_match['year']
    stage = 'pre_registration' if '预报名通知' in title_match['role'] else 'assessment'
    result.update(unit=unit, enrollmentYear=int(year), primaryStage=stage, identityEvidence=refs)
    fields = inventory['fields']

    profile = _PROFILES.get((stage, inventory['view']))
    checks = ([bool(re.fullmatch(pattern, _section(inventory, name))) for name, pattern in profile.section_patterns]
              + profile.context_checks(inventory, structure, title_match)) if profile else [False]
    if inventory.get('unclassifiedSections') or any(not value for value in checks):
        result['blockers'].append('UNINTERPRETED_NOTICE_CONTEXT')
    declared_years = re.findall(r'我校(20[0-9]{2})年', compact(text))
    if any(value != year for value in declared_years):
        result['blockers'].append('CONFLICTING_ENROLLMENT_YEAR')
    quality = inventory.get('sourceQuality', {})
    if quality.get('issues') or structure.get('issues'):
        result['blockers'].append('UNINTERPRETED_SOURCE_STRUCTURE')
    if (quality.get('pageGaps') or quality.get('unprocessedPageCount') or quality.get('parseStatus') in ('failed', 'unsupported')
            or any(code not in ('TABLE_RELATIONSHIPS_NOT_INTERPRETED', 'PAGE_DECORATION_RETAINED_IN_RAW_QUOTES') for code in inventory.get('issues', ()) )):
        result['blockers'].append('SOURCE_TEXT_INCOMPLETE')
    for field in fields.values():
        if field['issues'] and field['issues'] != ['TABLE_RELATIONSHIPS_NOT_INTERPRETED']:
            result['blockers'].append('FIELD_DISCOVERY_INCOMPLETE')
        if any(row.get('issues') for row in field['sections'] if not (row.get('issues') == ['NUMBERED_LIST_NOT_IDENTIFIED'] and row['heading'] not in ('报名条件', '报名材料'))):
            result['blockers'].append('SECTION_DISCOVERY_INCOMPLETE')
    if not result['blockers']:
        result['status'] = 'supported_notice_scope'
    return result
