---
author: 'Codex（AI助手）'
created: '2026-10-29'
updated: '2026-11-05'
version: 'v0.2'
status: '经独立设计审阅修订的候选契约与状态设计；未实现'
---

# Agent接口与共享状态设计

作者：Codex（AI助手）。日期：2026-10-29。定义两套Agent团队怎样交接任务、传递证据和恢复执行；字段细则由[数据与工具契约](D:/Zewang/newProject/ICT/AI文档/07_详细设计/数据与工具契约设计.md)维护，业务规则来自[SRS](D:/Zewang/newProject/ICT/AI文档/05_需求分析/软件需求规格说明书_SRS.md)。

## 1. 统一消息壳

每个跨角色或跨团队请求携带：`schemaVersion, requestId, runId, taskId, parentTaskId, stepId, attempt, operation, scope, inputRefs, expectedVersions, deadline, budget, actorRef`。`actorRef`为经过鉴权的调用身份引用，不能信任模型生成的“我是管理员”。预算包含工具调用、模型token、金额与探索/返工次数的可用子集，子任务领取后父级同时扣账，不能每个角色自行拥有全局预算副本。

账本实行原子预留→按callId幂等结算→释放未用额度；在途/超时但可能计费的usage_pending保留预留，不能算0。详细语义在[数据契约§3.1](D:/Zewang/newProject/ICT/AI文档/07_详细设计/数据与工具契约设计.md)集中维护。涉及写入的命令另带执行租约fencingToken，身份、版本和执行权分别校验。

结果结构：`status, resultRefs, observedVersions, evidenceRefs, unknownPoints, changedFields, addedOpportunityRefs, failures, usage, startedAt, finishedAt`。`status`区分success/partial/failed/needs_input；具体业务状态在独立字段，不把success解释为“用户符合且现在开放”。字段schema合法只表示能处理，事实发布还须质量核验。

跨角色采用**请求—返回**为主：主协调流程持有用户交互权；专长角色不能擅自直接向学生提问或接管会话。专长角色返回澄清提案，由对话入口发给用户。未来若需要handoff，单独定义控制权与恢复契约，不混用。

## 2. 跨团队候选复核

调用`review_scope`的目标是候选相关院校/培养单位/目标方向，而非只重读旧通知。请求包含候选机会、已知官方入口、所用资料版本和待查点；资料侧既检查旧文档变化，也检查相关范围内新增通知/批次。探索仍受任务范围/预算限制，不扩大成全网刷新。

| 返回类别 | 数据侧含义 | 咨询侧动作 |
|---|---|---|
| verified_unchanged | 对指定关键来源和适用范围核实无实质变化 | 引用原版本及本次成功核验记录，再判当前时间 |
| verified_updated | 新版本/更正/新机会已验证发布 | 用新规则重判，新增机会走同一判断流程 |
| partial | 范围内部分成功，附每文档/机会结果及缺损字段 | 逐项看关键依赖，不能把整批partial视作所有机会通过 |
| access_failed | 对应来源在本次有界访问中失败 | 依赖它的机会skip，记录失败；达到条件产生warn |
| unresolved | 冲突/证据不足/条款不支持；原文可得 | 不生成确定性满足；开放已核实且仅资格不明时保留unknown候选 |

开放unknown仅在官方内容可读、尽力查询仍不明且个人条件pass时末尾提示。不可读的关键来源属于access_failed/解析缺损，不能借unknown开放例外进入提示。源未披露、未找到、获取失败、解析失败、冲突与用户未回答都应有不同reasonCode。

## 3. 并发、缓存与取消

- 复核合并键按范围＋资料目标版本＋策略版本构造，避免同一批咨询重复重扫；仅合并访问与核验，不合并不同用户的画像/排序。等待者保留各自deadline和取消状态。
- 缓存key包含来源/机会/年度/范围、成功核验版本及策略版本；有新内容或适用期变化则失效。失败不能覆盖成功时间，也不能作为“无变化”缓存。某次访问明确失败后，本次依赖仍skip。
- 取消只停止当前调用者的等待；共享复核仍有其他消费者时不能直接终止。最后消费者退出后按任务策略停止，已完成证据留存但不可越过发布门禁。
- 数据发布使用expectedVersion的比较更新。过时worker不能覆盖新版本；返回`VERSION_CONFLICT`后读取当前版本，有界重判而非盲写。并发不以“最新时间戳覆盖”解决内容冲突。
- 发布还在同一事务校验taskId与fencingToken；失去租约的worker即使expectedVersion仍相同也返回LEASE_LOST，不能提交。只检查资料CAS不足以保证执行权。
- 每次推荐生成一个不可变的引用集合。实时输出前再次检查时钟及关键版本，变化则重判；预算耗尽只给已确认部分或显式重跑建议，不能无限追逐更新。

## 4. 状态归属及迁移

任务状态由应用编排器写，Agent只返回建议事件。资料发布状态由发布应用写，资格状态由领域求值结果产生，开放状态由时点和可验证事实产生。模型不能直接发“任务完成”并获得写入终态权限。

| 对象 | 主要状态与合法流转 | 终止/恢复 |
|---|---|---|
| 批量/复核任务 | queued→running→retry_wait→running；running→completed/partial/failed/cancelled | retry受剩余预算；终态不可原地改回running，重跑新run关联原任务 |
| 单文档处理 | discovered→fetched→parsed→checking→published；checking→repair→checking | 超预算或未解决→quarantined；访问失败→skipped；修复从受影响步骤重跑 |
| 对话推荐任务 | queued→profile_confirmation→clarifying/research_assessment→retrieving→reviewing→rejudging→ranking→explaining→validating→completed/partial | 等待用户是waiting_input；回答带会话/画像版本校验；关键错误failed |
| 资料版本 | staged→verified_published→superseded；staged→quarantined | superseded快照仍可历史回放；撤销当前版本时先撤当前引用再重判受影响结果 |

状态机描述控制流，和open/closed、pass/fail/unknown及文档质量状态分别保存。[状态机源图](D:/Zewang/newProject/ICT/AI文档/06_架构设计/PlantUML/03_采集任务状态机.puml)([SVG](D:/Zewang/newProject/ICT/AI文档/06_架构设计/PlantUML/03_采集任务状态机.svg))、[咨询时序源图](D:/Zewang/newProject/ICT/AI文档/06_架构设计/PlantUML/04_候选复核与推荐时序.puml)([SVG](D:/Zewang/newProject/ICT/AI文档/06_架构设计/PlantUML/04_候选复核与推荐时序.svg))。

上表profile_confirmation/clarifying等是`currentStage`，不是另一个taskStatus字典；等待用户时`taskStatus=waiting_input`并保留currentStage和版本，回答后回running，从同一阶段继续。running下的步骤失败可创建新attempt；终态重跑建新run关联旧任务。字段claimVerdict、usability、publicationStatus和taskStatus的唯一字典见[数据契约§1.1](D:/Zewang/newProject/ICT/AI文档/07_详细设计/数据与工具契约设计.md)。

## 5. 上下文分配与内存

| 接收方 | 允许收到 | 默认不收到 |
|---|---|---|
| 入口探索角色 | 院校/单位/目标方向/年度、已知入口、覆盖缺口 | 学生成绩、私人偏好、全部历史对话 |
| 抽取角色 | 一组官方快照、解析文本/页图、抽取schema、适用线索 | 其他学校无限资料，系统密钥 |
| 复核角色 | 抽取提案、原始快照/其他证据、待核验字段、质量策略 | 仅有抽取者总结而无原文的“自证材料” |
| 科研/资格专长角色 | 当前用户必要画像字段、适用政策及版本 | 其他学生画像；不相关长对话 |
| 排序/解释角色 | 经过硬约束的候选、可用特征、偏好、判断理由与证据 | fail项成为可恢复候选的写权限；自由搜索后直接改事实 |

跨角色传引用＋必要片段，长文件分块时保留文档级上下文与例外条件。摘要是缓存，不是证据替代品；原文版本变化后相关摘要失效。工具密钥仅留基础设施运行环境，模型只接收工具schema。

## 6. 共享状态契约检查

用例至少覆盖：同校多咨询去重、一个消费者取消而另一个继续、旧worker晚到、同URL新版本、画像修改后旧回答晚到、等待恢复后权限变化、发布前跨截止、错误日志能关联回原请求。对应测试种类和质量评估见[评估设计](D:/Zewang/newProject/ICT/AI文档/08_测试与评估/评估与持续改进设计.md)。这些是尚待实现验证的行为，不凭状态图宣称可靠性通过。
