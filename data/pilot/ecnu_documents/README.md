---
author: 'Codex（AI助手）'
created: '2026-11-05'
updated: '2026-11-05'
status: '官方政策冻结原文；当前用于载体/关系回归，不是实时招生结果或人工终审金标'
---

# 华东师大政策正文载体样本

作者：Codex。日期：2026-11-05。预报名HTML原文通过`pdfsrc`嵌入PDF，属于正文载体；不是先前猜测的纯图片。

- `prenotice.html`及其来源JSON：已有官方通知入口快照，保存原始内容。
- `prenotice.pdf`及其来源JSON：本轮从同源官方嵌入URL取得的两页文字PDF；未收集细则链接的学生考核名单。
- 每份来源JSON分别记录原URL、获取时间、载体和摘要，不把两次获取时间混为“同时在线复核”。

PDF内容哈希：`b0a300279b3e7cf2b19f52874286134ef7a102403c82139a127525b7730c8d5c`。新能力回归通过`tests/test_document_parsing.py`运行。页面文本定位依据PDF文字层，不等同于版面视觉的完全保真或OCR。
