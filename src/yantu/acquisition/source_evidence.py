"""同一来源文本视图上的引用与质量摘要；不赋予语义支持或发布权限。"""

from dataclasses import asdict, dataclass
import hashlib
import re

from .parse_models import PageText, ParseResult, ParseStatus


@dataclass(frozen=True)
class SourceClaim:
    """一个候选字段指向原文区间；所属文档另行绑定snapshot/view/hash。"""

    path: str
    quote: str
    start: int
    end: int


def claim_record(claim: SourceClaim, text: str, pages: tuple[PageText, ...] = ()) -> dict:
    """校验精确引文与同一视图的页定位，禁止清洗/拼接后继续复用旧偏移。"""
    if (any(isinstance(value, bool) or not isinstance(value, int) for value in (claim.start, claim.end))
            or not 0 <= claim.start < claim.end <= len(text) or text[claim.start:claim.end] != claim.quote):
        raise ValueError("候选字段引文/区间与来源视图不一致")
    page_numbers = []
    for page in pages:
        if not (0 <= page.start_offset <= page.end_offset <= len(text)) or text[page.start_offset:page.end_offset] != page.text:
            raise ValueError("页定位与来源文本视图不一致")
        if page.start_offset < claim.end and page.end_offset > claim.start:
            page_numbers.append(page.number)
    return {**asdict(claim), "pages": page_numbers}


def source_quality(parsed: ParseResult | dict | None) -> dict:
    """共享当前解析和历史记录的质量投影；只保留缺损，不复制整页正文。"""
    record = {"status": parsed.status, "issues": parsed.issues, "notes": parsed.notes,
              "declared_page_count": parsed.declared_page_count, "parser_info": parsed.parser_info,
              "pages": [{"number": page.number, "status": page.status} for page in parsed.pages]} if isinstance(parsed, ParseResult) else parsed or {}
    status = record.get("status")
    pages = record.get("pages") or []
    declared = record.get("declared_page_count")
    return {"parseStatus": status.value if isinstance(status, ParseStatus) else status,
            "issues": list(record.get("issues") or []), "notes": list(record.get("notes") or []),
            "declaredPageCount": declared, "parsedPageCount": len(pages),
            "unprocessedPageCount": max(0, (declared or 0) - len(pages)),
            "pageGaps": [{"number": page["number"], "status": page["status"]} for page in pages if page["status"] != "text_extracted"],
            "parser": record.get("parser_info") or {"identity": None, "version": None, "configurationStatus": "not_recorded"}}


class EvidenceResolver:
    """按来源序号、视图和路径解析证据；跨视图偏移、悬空路径或摘要不一致均抛错。

    原文正文和HTML标题/表格各有独立坐标；提供pages时同时校验PDF页定位。
    不读取存储，不证明引文的语义支持或来源官方身份。
    """

    def __init__(self, sources: list[dict], pages: list[tuple[PageText, ...] | None] | None = None):
        self.sources = sources
        self.pages = pages

    def resolve(self, reference: dict) -> dict:
        """返回已核对的claim；sourceIndex必须为整数，路径只接受字段与列表下标。"""
        index, view_name, path = reference.get('sourceIndex'), reference.get('view'), reference.get('path')
        if (type(index) is not int or not 0 <= index < len(self.sources) or not isinstance(view_name, str)
                or not isinstance(path, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*(?:\[[0-9]+\])?(?:\.[A-Za-z_][A-Za-z_0-9]*(?:\[[0-9]+\])?)*', path)):
            raise ValueError('无效证据来源、视图或路径')
        source = self.sources[index]
        is_root = view_name == source.get('view')
        if is_root:
            view = source
            value = source['fields'] if path.split('.')[0] in source.get('fields', {}) else source
        else:
            view = source.get('noticeStructure', {}).get('views', {}).get(view_name)
            if not view or view.get('snapshotSha256') != source.get('snapshotSha256'):
                raise ValueError('证据视图未绑定同一源快照')
            value = view
        text = view.get('text', '')
        if hashlib.sha256(text.encode('utf-8')).hexdigest() != view.get('textSha256'):
            raise ValueError('证据文本视图摘要不一致')
        try:
            for key in re.findall(r'[^.\[\]]+', path):
                value = value[int(key)] if isinstance(value, list) else value[key]
            value = value.get('claim', value)
            page_view = self.pages[index] if is_root and self.pages is not None else None
            checked = claim_record(SourceClaim(value['path'], value['quote'], value['start'], value['end']), text, page_view or ())
            if value['path'] != path or (page_view is not None and checked['pages'] != value['pages']) or (not is_root and value['pages']):
                raise ValueError('引用路径或页码与所属视图不一致')
        except (KeyError, IndexError, TypeError, AttributeError) as error:
            raise ValueError('证据路径未指向有效原文引用') from error
        return value

    def body_claims(self, source_index: int) -> dict[str, dict]:
        """索引正文引用，明确排除标题/表格中的局部claims路径，防止同名冲突。"""
        source, claims = self.sources[source_index], {}

        def visit(value):
            if isinstance(value, dict):
                if {'path', 'quote', 'start', 'end'} <= value.keys():
                    reference = {'sourceIndex': source_index, 'view': source['view'], 'path': value['path']}
                    resolved = self.resolve(reference)
                    if resolved != value:
                        raise ValueError('正文路径重复或不一致')
                    claims[value['path']] = resolved
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        for key in ('fields', 'documentPreamble', 'contextClaims', 'unclassifiedSections', 'naturalEvidence'):
            visit(source.get(key))
        return claims

    def compact_span(self, source_index: int, parent: dict, left: str, right: str | None = None) -> tuple[int, int]:
        """在去空白匹配视图定位后返回原文区间，不把派生坐标当原文偏移。"""
        text = self.sources[source_index]['text']
        positions = [index for index in range(parent['start'], parent['end']) if not text[index].isspace()]
        compact = ''.join(text[index] for index in positions).replace('．', '.')
        start = compact.index(left)
        end = compact.index(right, start + len(left)) if right else len(compact)
        return positions[start], positions[end - 1] + 1
