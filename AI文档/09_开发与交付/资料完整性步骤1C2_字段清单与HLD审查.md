---
author: Codex（AI助手）；独立参考/测试及HLD预审由并行子代理完成
created: 2026-11-05
updated: 2026-11-06
status: 五项修复及补测完成；252项测试与21例离线回归通过，双路独立复核通过
---

# 步骤1C2：逐文档候选字段清单

用户要求沿用逐步开发→HLD分析/改造→subagent全量review→修复复核的流程，并授权最高可用模型和推理档。本步并行安排3个`gpt-6-astra / ultra`子代理，分别负责规格/HLD、真实来源参考与独立测试、正确性/反例审阅。生产实现由主代理串行负责，未并行开发其他工作单。

## 验收边界

本步输出每份文档的conditions/materials/assessment/channels/scope/other_policies六组候选原文、相关提及和缺损。**不是可计算规则、已核验专业归属、通道AND/OR或正式推荐事实。** `publicationStatus=not_published`、`semanticVerification/applicability=not_evaluated`、`coverage=not_established`；not_found仅表示受限标题策略未识别。

HTML使用正文容器`content_text`，PDF使用分页合成`text`；每个文档保存view、text、textSha256、snapshotSha256，所有SourceClaim按该视图定位。主章节与子章节保留层级，材料7中的（1）（2）（3）保留在同一条；未知/混合标题形成显式未分类范围，不吞入前一章节。相关政策提及保留所在完整编号条目，不将“错过预报名后可能再组织”的表述转成当前开放状态。

正文PDF的页码装饰原样保留并标记，清单不假装已做语义清洗。原解析与本次重解析的partial/页缺损独立保存；未访问、预算未执行、失败来源也保持六字段分母。标题/条目/文本有界，预算不足与未识别区分。

## 实现及数据流

`inspect_policy_fields.py` → `field_inventory_service.inspect_fields` → 校验资料包/快照字节 → 按当前ParserRegistry重解析 → `inventory_document` → 证据与缺损清单 → JSON/Markdown。

源blob为本次证据基底，不使用可被修改后重新计算摘要的缓存正文作为唯一事实输入。旧资料包的缺损仍保留；新解析器identity/version/configuration及PDF引擎版本单独记录。没有网络或模型调用，不写PublicationRepository。

## HLD分析与已完成改造

| 实际重复/职责问题 | 决定及实现 |
|---|---|
| 字段清单与窗口检查重复拼quote/span/page | `source_evidence.py`共享SourceClaim/claim_record，严格校验切片与页视图；policy_verification兼容导出旧名 |
| 原解析、本次解析、窗口报告重复页缺损算法，甚至复制整页正文 | 共用source_quality，统一状态/页缺损/解析器摘要；时间脚本保留旧输出键兼容 |
| CLI混有校验/重解析/日志/分母逻辑 | 移至field_inventory_service；CLI仅参数、格式化及原子产物写入，保留inspect_fields导入兼容 |
| 载体判断/表格提示混入字段发现逻辑 | 集中在_source_view输入构造；表格只保留原文与未解释关系提示，不输出猜测的行关联 |
| 解析参数改变会改变候选文本 | ParseResult记录parser_info；未知/自定义Adapter配置明确not_reported，不能伪造版本 |

沿用声明式字段表与ParserRegistry；没有每校/每字段子类、插件市场或通用万能框架。SourceClaim不含verified时间或语义支持标记。

## 验证及独立review

首版237测试通过（含子代理依据真实参考独立编写15项）；HLD改造后240全量测试通过。新增原文/视图、隐藏HTML、相同引文不同位置、嵌套编号、漏号/重号、混合标题、失败来源分母、缓存文本变造后重算摘要、源blob损坏、编程异常传播及PDF页截断等反例。

真实回放：考核细则5条条件、预报名PDF6条条件及7项材料，包裹HTML无正文保留缺损；PDF无本地详细考核章，仅相关提及；细则无本地报名材料清单，不借PDF补齐。参考见[AI预标注清单](../08_测试与评估/ECNU字段覆盖参考_2026-11-05.md)，不是人工金标或真实模型eval。

2026-11-06收尾：修复前两路审阅指出的五项P2（条目预算、上级限定、空白混合标题、异常消息泄露、子类版本冒用）均已补测；新增12项测试。补测暴露混合标题fallback仍可进入字段，已要求recognized为真；移除40字符前瞻限制，并修正未分类引用路径索引。提取器升级为policy-inventory-v2-contexts。

主代理最终全量252项测试、21例离线政策回归通过；同一版工作区的规格/HLD及反例/证据独立复核各自跑通252项全量测试，均无阻断P1/P2。三源真实产物54条引用与78处上下文引用可逐条同源回溯。完整证据与两份独立报告入口见[修复与复核报告](../08_测试与评估/字段清单修复与复核报告_2026-11-06.md)。

本步通过，下一子步骤为1C3招生阶段与专业适用性；预通知“已获得或预计获得”与后续细则“已获得”必须分源处理，不先合并资格门槛。当前资料包仍partial，跨站引用/入口身份、完整机会范围、OCR、真实模型语义质量及资料任务恢复未由本步完成。
