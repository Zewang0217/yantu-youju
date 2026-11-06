"""HTML字节解码与唯一可见标题提取；多个标题不能静默决定来源身份。"""

from __future__ import annotations

import re
from html.parser import HTMLParser


def decode_html(body: bytes) -> str:
    """按声明字符集、UTF-8及GB18030解码；全部失败时抛UnicodeDecodeError。"""
    match = re.search(r'charset=["\']?([\w-]+)', body[:2048].decode('ascii', errors='ignore'), re.I)
    for encoding in ([match[1]] if match else []) + ['utf-8', 'gb18030']:
        try:
            return body.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    raise UnicodeDecodeError('decode', body, 0, 1, '所有候选编码失败')


class _Titles(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.titles = []
        self.headings = []
        self.current = None
        self.parts = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        blocked = (any(frame[1] for frame in self.stack) or tag in ('script', 'style', 'noscript') or 'hidden' in attrs
                   or bool(re.search(r'display\s*:\s*none|visibility\s*:\s*hidden', attrs.get('style') or '', re.I)))
        if tag not in ('br', 'img', 'meta', 'link', 'input', 'hr', 'wbr', 'embed', 'source', 'area', 'base', 'col', 'param', 'track'):
            self.stack.append((tag, blocked))
        if tag == 'title' or (tag == 'h1' and not blocked):
            if self.current is not None:
                self.titles.append('')
            self.current, self.parts = tag, []

    def handle_data(self, data):
        if self.current is not None and not any(frame[1] for frame in self.stack):
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == self.current:
            (self.titles if tag == 'title' else self.headings).append(' '.join(''.join(self.parts).split()))
            self.current = None
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break


def html_title(html: str) -> str:
    """只取唯一完整标题；hidden属性不消除title身份歧义，注释/脚本伪标签不计入。"""
    parser = _Titles()
    parser.feed(html)
    parser.close()
    if parser.current is not None:
        return ''
    values = parser.titles if parser.titles else parser.headings
    return values[0] if len(values) == 1 else ''
