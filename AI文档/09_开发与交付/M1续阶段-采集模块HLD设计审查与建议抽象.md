---
author: 'ZCode（AI助手）'
created: '2026-11-05'
updated: '2026-11-05'
version: 'v1.0'
status: 'M1续阶段设计决策记录（随本文件§6–§7落地）'
scope: 'src/yantu/acquisition(fetcher/parsers/store/structurer)+scripts demo;延续《M1阶段-内核HLD设计审查与建议抽象》的三判据'
---

# M1续阶段-采集模块HLD设计审查与建议抽象

## 1. 审查问题

采集模块（M1续/续二产出）中：哪些冗余逻辑值得抽象？哪些地方值得用设计模式增强可读性/可扩展性？反向：哪些不该动。

## 2. 过度设计判定标准（沿用三判据）

≥2 个真实消费方（现在或紧邻里程碑）；设计文档有明确要求；净简化（消除的重复/脆弱 ≥ 新增样板）。

## 3. 采纳的抽象（决策）

| # | 现状（问题） | 决策 | 模式 | 判据对应 |
|---|---|---|---|---|
| R1 | F7 协议承诺 ParserRegistry 但未实现：只有一个 HtmlNoticeParser，且 `supports()` 自检写在 `parse()` 内部（选择与执行耦合）；未来 PDF/纯文本解析器无接入点 | `ParserRegistry`（register/select by carrier）+ 模块级 `parse_snapshot()` 分发；UNSUPPORTED 由**分发器**产出（选不到解析器），解析器自身只假设载体正确 | Registry + Strategy | F7 协议原文；ACQ-04 载体四态的统一入口；PDF 解析器（G8/附件）是第2消费方 |
| R2 | **日期/时间解析双实现**：`parsers.extract_dates`（只到日，时刻为0）与 `structurer`（年月日+中午/下午+时刻，但用组号偏移取组——脆弱，本轮开发中实际出过 off-by-one bug）；两处正则能力不一致 | 抽公共模块 `text_dates.py`：`find_datetimes(text) -> list[DatetimeHit(dt,start,end)]`（统一处理 `-`/`年月日` 两种写法+中午/上午/下午+`时/:点`时刻）；parsers 与 structurer 都委托它；structurer 改为"关键词就近回看"（日期命中点向前 40 字找 截止/开放 句式），删除组号偏移代码 | DRY + 值对象(DatetimeHit) | 两处现存重复（判据3）；脆弱性已被实际 bug 证实；消除后 structurer 不再自己拼 datetime |
| R3 | demo 里 fetch→选解析器→快照落盘→入口库登记 的编排是**内联过程**，接入其余 4 校时必然复制粘贴；失败标记（fetch_failed）逻辑也会重复 | `HarvestService`（采集门面）：`collect(url,name,school,…) -> HarvestOutcome`，一次调用完成抓取/分发解析/落盘/入口库登记（含失败登记）；`fetch_fn` 可注入（测试离线、未来换请求栈） | Facade + 依赖注入 | 首批 4 校接入与未来 LLM 采集 Agent 是第2/3消费方；失败登记单一出口（ACQ-12） |
| R4 | 真实页面正文前 ~500 字全是导航菜单（实测 ECNU 3 页），污染 `ParseResult.text` 与日期提取（导航/页脚日期风险） | `HtmlNoticeParser` 正文净化启发式：若 `<title>` 文本出现在正文 stripped text 中，则正文从该处开始；找不到则保持全文（保守降级） | — | 解析质量实证（3页实测）；date 提取与未来结构化都受益 |

## 4. 拒绝的抽象（决策：不做）

| 诱惑 | 理由 |
|---|---|
| 每校一个 Collector 子类（模板方法） | 校际差异在数据（URL/入口），不在流程；HarvestService+配置即可，现在抽象是猜需求 |
| 通用序列化框架（替代 to_record/from_record 手写映射） | 只有一个 Opportunity 持化形态，手写映射显式可读；引入框架是负简化 |
| demo 脚本类化/参数化 | 脚本就是脚本；等第2个场景出现再抽 |
| 集中各测试文件的 sys.path 引导 | pytest 已由 pyproject `pythonpath` 覆盖；unittest 用 PYTHONPATH；4 行×4 文件的重复不值得引入新机制 |

## 5. 保持不动

fetcher 的不可变 Snapshot（内容寻址证据基底）；store 三件套的 JSON 实现（接口稳定后 M5 可换）；structurer 的"保守提取+人工优先"语义；解析四态语义。

## 6. 落地记录

- 2026-11-05：R1–R4 全部实现——`parsers.py` 重写（`ParserRegistry`+`parse_snapshot` 分发器独占 UNSUPPORTED，解析器自检删净；`_strip_tags` 剔除 `<head>`；R4 导航净化：去空白视图定位**全文标题优先、16字前缀降级**，精确映射回原文索引）；新增 `text_dates.py`（统一日期/时刻解析，parsers 与 structurer 均委托）；`structurer.py` 重写（关键词就近回看，删除组号偏移自拼 datetime）；新增 `harvest.py`（`HarvestService` 门面 + `fetch_fn` 注入）；demo 改用门面。

## 7. 验证

`PYTHONPATH=src python -m unittest discover -s tests` → **98 项测试全部通过**（改造前 87 + review 回归 11）。demo 真抓 3 页经门面跑通，自动截止提取与种子交叉验证仍一致（2026-09-07 12:00），`var/entries.json` 经 collect 落盘。

## 8. Subagent 全量 review 处置（2026-11-05，独立评审者实测探针验证）

| 级别 | 发现 | 处置 |
|---|---|---|
| P1×6 | ①"下午"只查 tail 前3字符,多空白/换行时漏检→时刻差12小时;②24:00/24点导致整条日期丢弃(12:60同);③12字符时刻窗口跨"X日至Y日"区间句式串扰到前一日期;④structurer 去重先到先得,同日期后到的 exact 被丢;⑤HarvestService 只 upsert 不 save,入口库从不落盘;⑥fetch_failed 构造全新记录,抹掉历史 last_sha256/fetched(证据链断裂) | **全修**：①先定位时刻再在其前片段搜"下午";②24:00→次日00:00(ISO语义),时刻非法但日期合法→回退日级;③时刻搜索截至下一日期命中;④按(kind,date)取置信度最高;⑤collect 两条路径均 book.save();⑥失败登记继承旧记录字段 |
| P2 已修 | ParseResult.dates/extract_dates 补 `list[datetime]` 类型;merge_deadline 多 exact 取**最早**(保守方向,docstring 论证);_cut_navigation 全文标题优先、16字前缀降级(截断策略记录于本节);SnapshotStore meta 首见才写(镜像不覆盖来源);HarvestOutcome.ok 语义 docstring(只表网络层成功)+fetch_fn 契约声明+非预期异常登记后上抛;`Parser`/`extract_dates` 补入包导出;无标题有日期→PARTIAL(原FAILED过严);删无消费方的 `WindowCandidate.to_window`;demo 返回类型修正 | **已修**并附回归测试 |
| P2 记录推迟 | EntryBook 全量覆盖无并发保护(M1 单进程,docstring 记为已知限制);同页多 exact 的批次归属人工复核提示(交互层后续) | 记录在案 |

## 9. 修订记录

- 2026-11-05 v1.0：审查发现 R1–R4 + 4 项拒绝；按用户授权当日改造。
- 2026-11-05 v1.1：subagent 全量 review（P1×6/P2×10）处置：P1 全修、高价值 P2 同批修复、2 项记录推迟（§8）；测试 87→98 全绿；R4 补记"全文优先、16字前缀降级"策略。
