---
author: 'Codex（AI助手）'
created: '2026-11-05'
updated: '2026-11-05'
status: '冻结回归参考；AI预标注待人工终审，不是人工金标或真实模型准确率数据集'
---

# 单页可信政策回归资料

作者：Codex。日期：2026-11-05。`manifest.json`是唯一用例定义，记录来源、scope、asOf、预期终态与标注状态。

- `ecnu_rules.html`及`ecnu_rules_source.json`：此前获取的官方通知快照及摘要；未下载所链接的学生考核名单附件。本轮不是实时复核。
- `synthetic_notice.html`：明确合成的院校、窗口和门槛，用于正向与对抗边界；不代表实际招生政策。
- `manifest.json`中的草案是测试替身，不冒充新提示词真实模型输出；对官方快照不允许按合成变换改写后再声称原文。

运行`python scripts/evaluate_policy.py`，隔离产物写入项目`var/policy_eval/`；演示为`python scripts/trusted_policy_demo.py`。验证解释见[08报告](../../../AI文档/08_测试与评估/单页可信发布回归报告_2026-11-05.md)。
