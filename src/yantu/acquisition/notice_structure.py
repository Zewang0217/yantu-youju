"""通知标题和原生招生表的独立证据视图；不从扁平正文猜测行列关系。"""

from __future__ import annotations

import hashlib
import re
from html.parser import HTMLParser

from .fetcher import Snapshot
from .parse_models import ParseResult, ParseStatus
from .html_text import decode_html, html_title
from .source_evidence import SourceClaim, claim_record

_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
_PROGRAM_LABEL = re.compile(r'软件工程(?:（(?:信息技术创新|可信智能软件卓越项目|[\u4e00-\u9fff]{2,50}大学-法国学校联合培养计划)）)?')
_PROGRAM_PLAN = re.compile(r'若干|约?[1-9][0-9]*')


class _Tables(HTMLParser):
    """仅采集明确正文内的平面表；嵌套、跨行跨列、隐藏或畸形表一律拒绝。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.tables = []
        self.current = None
        self.row = None
        self.cell = None
        self.best_scope = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        parent_scope = self.stack[-1][1] if self.stack else 0
        scope = max(parent_scope, 3 if 'wp_articlecontent' in (attrs.get('class') or '').split() else 2 if tag == 'article' else 1 if tag == 'main' else 0)
        hidden = (any(frame[2] for frame in self.stack) or 'hidden' in attrs or tag in ('script', 'style', 'noscript')
                  or bool(re.search(r'display\s*:\s*none|visibility\s*:\s*hidden', attrs.get('style') or '', re.I)))
        if not hidden:
            self.best_scope = max(self.best_scope, scope)
        if tag not in _VOID:
            self.stack.append((tag, scope, hidden))
        if tag == 'table' and self.current is None and scope and not hidden:
            self.current = {'rows': [], 'scope': scope, 'issues': [], 'line': self.getpos()[0]}
        elif tag == 'table' and self.current is not None:
            self.current['issues'].append('NESTED_TABLE')
        if self.current is None:
            return
        if hidden:
            self.current['issues'].append('HIDDEN_TABLE_CONTENT')
        if tag == 'tr':
            if self.row is not None:
                self.current['issues'].append('MALFORMED_TABLE')
            self.row = []
        if tag in ('td', 'th'):
            if self.cell is not None or self.row is None:
                self.current['issues'].append('MALFORMED_TABLE')
            if any(attrs.get(key, '1') != '1' for key in ('rowspan', 'colspan')):
                self.current['issues'].append('MERGED_CELLS')
            self.cell = []

    def handle_data(self, data):
        if self.cell is not None and not any(frame[2] for frame in self.stack):
            self.cell.append(data)

    def handle_endtag(self, tag):
        if self.current is not None:
            if tag in ('td', 'th'):
                if self.cell is None or self.row is None:
                    self.current['issues'].append('MALFORMED_TABLE')
                else:
                    self.row.append(' '.join(''.join(self.cell).split()))
                self.cell = None
            if tag == 'tr':
                if self.row is None or self.cell is not None:
                    self.current['issues'].append('MALFORMED_TABLE')
                else:
                    self.current['rows'].append(self.row)
                self.row = None
            if tag == 'table':
                if self.row is not None or self.cell is not None:
                    self.current['issues'].append('MALFORMED_TABLE')
                self.tables.append(self.current)
                self.current = None
                self.row = self.cell = None
        if tag not in _VOID:
            if not self.stack or self.stack[-1][0] != tag:
                if self.current is not None:
                    self.current['issues'].append('MALFORMED_TABLE')
            for index in range(len(self.stack) - 1, -1, -1):
                if self.stack[index][0] == tag:
                    del self.stack[index:]
                    break


def _view(name: str, text: str, snapshot: Snapshot) -> dict:
    return {'view': name, 'text': text, 'textSha256': hashlib.sha256(text.encode()).hexdigest(),
            'snapshotSha256': snapshot.sha256, 'claims': []}


def notice_structure(snapshot: Snapshot, parsed: ParseResult) -> dict:
    """返回独立标题/DOM表视图及关系；未知表结构保持缺损，不借另一来源补全。"""
    result = {'views': {}, 'programRows': [], 'issues': []}
    if parsed.status in (ParseStatus.FAILED, ParseStatus.UNSUPPORTED):
        result['issues'].append('SOURCE_UNREADABLE')
        return result
    if any(link.kind in ('body_image', 'embedded_document') for link in parsed.links):
        result['issues'].append('UNRESOLVED_BODY_CARRIER')
    if snapshot.carrier == 'application/pdf':
        return result
    if len(snapshot.body) > 5_000_000:
        result['issues'].append('HTML_STRUCTURE_BUDGET_EXCEEDED')
        return result
    try:
        html = decode_html(snapshot.body)
    except UnicodeDecodeError:
        result['issues'].append('HTML_DECODE_FAILED')
        return result
    title = html_title(html)
    if title:
        view = _view('html_title', title, snapshot)
        view['claims'].append(claim_record(SourceClaim('claims[0]', title, 0, len(title)), title))
        result['views']['html_title'] = view
    parser = _Tables()
    parser.feed(html)
    parser.close()
    if parser.current is not None:
        result['issues'].append('UNCLOSED_TABLE')
    selected = [table for table in parser.tables if table['scope'] == parser.best_scope]
    if len(selected) > 16:
        result['issues'].append('TABLE_BUDGET_EXCEEDED')
        return result
    for table_index, table in enumerate(selected):
        rows = table['rows']
        if table['issues'] or not rows or len(rows) > 256:
            result['issues'].extend(table['issues'] or ['TABLE_ROW_BUDGET_OR_EMPTY'])
            continue
        if [re.sub(r'\s+', '', value) for value in rows[0]] != ['层次', '学科专业', '学位类型', '推免招生计划']:
            result['issues'].append('UNSUPPORTED_TABLE_HEADER')
            continue
        if any(len(row) != 4 for row in rows):
            result['issues'].append('IRREGULAR_TABLE_ROWS')
            continue
        name = f'html_program_table_{table_index}'
        text = '\n'.join('\t'.join(row) for row in rows)
        view = _view(name, text, snapshot)
        result['views'][name] = view
        offset = 0
        for row_index, row in enumerate(rows):
            line = '\t'.join(row)
            path = f'claims[{row_index}]'
            view['claims'].append(claim_record(SourceClaim(path, line, offset, offset + len(line)), text))
            offset += len(line) + 1
            if row_index == 0:
                continue
            match = re.fullmatch(r'(?P<base>[0-9]{6})(?:-(?P<direction>[0-9]{2}))?\s+(?P<label>[\u4e00-\u9fff（）()\s\-]+)', row[1])
            if not match or row[0] not in ('硕士', '博士') or row[2] not in ('专业学位', '学术学位'):
                result['issues'].append('UNSUPPORTED_PROGRAM_ROW')
                continue
            # 单元格也可能承载适用限定；结构对齐不能把任意备注当成中性名称/人数。
            if not _PROGRAM_LABEL.fullmatch(re.sub(r'\s+', '', match['label'])):
                result['issues'].append('UNSUPPORTED_PROGRAM_LABEL')
                continue
            if not _PROGRAM_PLAN.fullmatch(re.sub(r'\s+', '', row[3])):
                result['issues'].append('UNSUPPORTED_PROGRAM_PLAN')
                continue
            result['programRows'].append({'baseCode': match['base'], 'directionCode': match['direction'],
                'degree': 'masters' if row[0] == '硕士' else 'direct_doctorate', 'label': match['label'],
                'evidence': {'view': name, 'path': path}, 'tableIndex': table_index, 'rowIndex': row_index})
    return result
