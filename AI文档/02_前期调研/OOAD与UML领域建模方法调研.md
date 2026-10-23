---
author: 'ZCode（AI助手）'
created: '2026-10-23'
updated: '2026-10-23'
version: 'v0.1'
status: '方法调研草稿，待用户审阅；调研为文献与官方文档检查，未实测工具链'
---

# OOAD 与 UML 领域建模方法调研

作者:ZCode(AI助手)。日期:2026-10-23(北京时间)。目的:回答"阶段3(需求分析)的《领域与信息模型》应该按什么方法做",为设计提供依据。本稿只调研方法,不产出模型本身;模型见配套的《领域与信息模型》。

## 1. 结论:采用的方法组合

| 方法/实践 | 取什么 | 在本项目的落点 |
|---|---|---|
| OOAD 领域建模(Larman) | 概念类图、名词—动词分析、"领域模型≠数据模型" | 概念类图不加方法、不设外键式属性;概念从 PRD 与真实通知中提取 |
| DDD 统一语言(Evans) | 术语表作为团队与代码的共同语言 | 《领域与信息模型》内置中英对照术语表,后续代码命名沿用 |
| DDD 限界上下文 | 大模型按上下文划分,避免一词多义 | 资料生产线 / 咨询服务线两个上下文 + 共享已复核资料库 |
| UML 三种用法(Fowler) | sketch(沟通草图)→ blueprint(设计规格)渐进 | 阶段3用"偏 sketch":图服务讨论,不追求实现级完备 |
| 敏捷建模原则(Ambler) | 有目的地建模、多模型并行、够用即止 | 不为凑齐图类型而画图;每张图对应明确问题 |
| 活动图 + 泳道 | 按职责划分泳道,表达分支/并行/异常 | 两条业务活动图:泳道=运营者/采集线/复核、学生/咨询线 |
| PlantUML | 图即代码,可 diff、可版本管理 | 与 git 协作契合;源文件入库,确认后导出 SVG |
| Event Storming(可选) | 团队工作坊式领域发现 | 供三人团队面对面锤炼术语用,非必选 |

## 2. 关键依据

### 2.1 领域模型 ≠ 数据模型(Larman 的 OOAD 分析观)

Larman《Applying UML and Patterns》的核心做法:领域模型是"对问题域中概念的可视化",概念类图**没有方法、没有实现类职责**,属性只保留领域上有意义的少量几个;概念通过用例文本的名词—动词分析获得,而不是先设计数据库。[UCL 需求工程教材的概念模型章节](http://www0.cs.ucl.ac.uk/staff/E.Letier/book/302-Conceptual-Models.html)给出同样定义:概念模型描述"现实世界中的实体类型、属性与关联"。社区讨论([Stack Overflow: Conceptual vs Detailed class diagram](https://stackoverflow.com/questions/39606683/what-is-the-difference-between-conceptual-class-diagram-and-detailed-class-diagr)、[Visual Paradigm 类图演进指南](https://www.visual-paradigm.com/guide/the-evolution-of-uml-class-diagrams-a-comprehensive-guide-to-domain-analysis-and-design-stages/))强调同一张图不应同时承担"领域表达"与"软件设计"两种职责。

**本项目裁剪:**概念类图属性用中文领域描述(如"排名口径"),不写 `id`、`hash` 等存储字段;外键式关联改为带角色名与多重性的关联;ER 图留到《数据与存储设计》再派生,与概念类图不强制一一对应。旧草案 `04_设计草案/PlantUML/02_领域模型.puml` 的属性偏数据库风格,复审后按此原则重构(见 §3)。

### 2.2 DDD:统一语言与限界上下文

[Evans/Fowler: Ubiquitous Language](https://martinfowler.com/bliki/UbiquitousLanguage.html):开发者和领域专家之间建立严格、共同的语言,模型与语言一起演进;[Bounded Context](https://martinfowler.com/bliki/BoundedContext.html):大型模型按上下文划分边界,同一词在不同上下文含义不同。术语表的范围是单个上下文而非全领域([Stack Overflow 讨论](https://stackoverflow.com/questions/36329458/ddd-what-is-the-scope-of-the-ubiquitous-language-is-it-domain-or-bounded-conte))。

**本项目裁剪:**"复核"一词天然有两个含义——资料生产线的入库前复核,与咨询时的候选实时核验。按上下文区分:资料上下文称**复核(Review)**,咨询上下文触发的是**时效核验(Freshness Check,跨上下文请求)**。"推荐对象""招生年度"等歧义术语在词汇表内给唯一权威定义。

### 2.3 UML 用到什么程度(Fowler 三模式)

[Fowler: UML As Sketch / As Blueprint / As Programming Language](https://martinfowler.com/tags/uml.html):sketch 轻量沟通,blueprint 完整规格,executable 直接执行。[UmlAsBlueprint](https://martinfowler.com/bliki/UmlAsBlueprint.html) 指出蓝图模式成本高、要求细节完备;敏捷实践多用 sketch 模式。

**本项目裁剪:**阶段3的图承担"团队共识载体"职责,用**偏 sketch**;到《详细设计》的时序图、状态机图与接口契约,关键部分再向 blueprint 收敛。与《软件开发流程与文档规划》"不为凑齐图类型增加重复材料"一致。

### 2.4 敏捷建模原则

[Ambler: Agile Modeling 实践清单](https://agilemodeling.com/practices.htm):有目的地建模(Model With A Purpose)、与他人一起建模、并行创建多个模型、模型"痛了才更新"。

**本项目裁剪:**每张图先写"这张图回答什么问题";术语冲突当場在词汇表解决;文档不追求一次完备,未决问题显式列出而不是伪装成定论——与《文档处理规则》的状态语义一致。

### 2.5 活动图表达业务流程

UML 活动图用**分区(partition,俗称泳道)**按职责组织活动,支持判定、并行(fork/join)与异常路径([ModernAnalyst 活动图指南](https://www.modernanalyst.com/Resources/Articles/tabid/115/ID/2011/End-to-End-UML-Activity-Diagram.aspx);权威定义见 [OMG UML 规范](https://www.omg.org/spec/UML/))。

**本项目裁剪:**两条业务活动图泳道按**职责**而非按最终 Agent 角色划分(Agent 数量与分工属阶段4),并按《多Agent系统分析与设计计划》要求,用注释区分"普通处理 / Agent 决策 / 用户澄清 / 外部工具访问 / 跨上下文请求"。

### 2.6 工具与协作

PlantUML 以纯文本维护图源码,天然适配 git 版本管理与评审([PlantUML 活动图](https://plantuml.com/activity-diagram-beta)、[类图](https://plantuml.com/class-diagram));导出 SVG 后正文引用,与既有《软件开发流程与文档规划》第5节的图管理约定一致。可选的团队工作坊方法 Event Storming([eventstorming.com](https://eventstorming.com/))适合三人小组快速锤炼领域事件与术语,不作为必须步骤。

## 3. 对 2026-10-11 旧设计草案的复审结论

| 旧图 | 复审结论 |
|---|---|
| `02_领域模型.puml` | **词汇大部分复用**(Institution/Department/Program/Opportunity/EligibilityRule/Evidence/DocumentVersion/UserProfileVersion/EligibilityState/Recommendation/ChangeEvent);属性按 §2.1 概念化;需补充 PRD v0.6/v0.7 新概念:来源入口(一等概念,可复用入口库)、招生批次、招生年度、科研突出判定、待确认点、报名状态未知、采集任务/复核记录/告警、咨询会话、结论变化;CONFLICT 并入"无法判断"的原因分类(歧义/冲突→复核仲裁) |
| `01_概念架构.puml` | 含已过时假设(定时采集、人工审核成员为常态、AgentArts 组件划分),**不作为阶段4架构输入**,留作历史参考;新架构图在《系统架构设计》重画 |
| `03_资格判断流程.puml` | 判定分支逻辑(符合/不符合/无法判断+证据核验+降级)与当前 PRD 一致,**逻辑并入**咨询线业务活动图;实现层细节(保存版本关联等)移到详细设计 |

## 4. 局限

- 本调研基于公开文献与官方文档,未实测 PlantUML 本地渲染管线;导出脚本在图确认后另行验证。
- Larman/Evans 原著以书籍为准,本稿引用的是可公开访问的摘要与社区资料;方法裁剪为 AI 建议,待团队确认。
- Event Storming 未在团队内试运行,是否采用由团队决定。

## 5. 参考

- [UCL: Domain Conceptual Models](http://www0.cs.ucl.ac.uk/staff/E.Letier/book/302-Conceptual-Models.html) · [Stack Overflow: Conceptual vs Detailed](https://stackoverflow.com/questions/39606683/what-is-the-difference-between-conceptual-class-diagram-and-detailed-class-diagr)
- [Fowler: Ubiquitous Language](https://martinfowler.com/bliki/UbiquitousLanguage.html) · [Fowler: Bounded Context](https://martinfowler.com/bliki/BoundedContext.html) · [Fowler: UML 模式标签页](https://martinfowler.com/tags/uml.html)
- [Ambler: Agile Modeling Practices](https://agilemodeling.com/practices.htm) · [ModernAnalyst: Activity Diagram](https://www.modernanalyst.com/Resources/Articles/tabid/115/ID/2011/End-to-End-UML-Activity-Diagram.aspx)
- [OMG UML 规范](https://www.omg.org/spec/UML/) · [PlantUML 类图](https://plantuml.com/class-diagram) · [PlantUML 活动图](https://plantuml.com/activity-diagram-beta) · [Event Storming](https://eventstorming.com/)
- 项目内:[软件开发流程与文档规划](../03_计划建议/软件开发流程与文档规划.md)、[多Agent系统分析与设计计划](../03_计划建议/多Agent系统分析与设计计划.md)、[文档处理规则](../../文档处理规则.md)
