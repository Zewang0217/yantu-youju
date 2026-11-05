---
author: 'ZCode（AI助手）'
created: '2026-11-05'
updated: '2026-11-05'
version: 'v1.0'
status: 'M1阶段设计决策记录（已按用户授权执行）；取代同日早先的评审记录稿'
scope: 'src/yantu 领域内核；决策依据=SRS v0.4、领域模型§8、07数据契约、OPS-07/REC-13/ACQ-05/INV-13'
---

# M1阶段-内核HLD设计审查与建议抽象

## 1. 审查问题与范围

对 M1 领域内核（`src/yantu`）做高层设计审查：哪些函数/方法值得抽象复用、哪些地方值得引入设计模式、哪些**不该**抽象。范围含 M1 续（抓取/解析/结构化）即将出现的代码形态。

## 2. 过度设计判定标准（本次采纳的判据）

一项抽象只有同时满足以下条件才落地，否则记录为"暂不做"：

1. **≥2 个真实消费方**：现在或紧邻里程碑（M1续/M2）有具体使用处，不是"将来可能"；
2. **设计文档有明确要求**：SRS/领域模型/07契约中能指出对应条目（开闭、可替换、证据链）；
3. **净简化**：引入后删除/避免的重复与分支 ≥ 新增的样板代码。

## 3. 采纳的抽象（决策）

| # | 决策 | 模式 | 判据对应 | 落点 |
|---|---|---|---|---|
| F1 | **条件类型注册表 + 编解码**：`ConditionRegistry`（类型名↔类，`register()` 开放注册）+ `ConditionCodec.build(spec)/spec(cond)`（dict↔条件树，独立模块，领域类不加序列化方法）；未知类型抛结构化错误，由解析侧降级 `UnstructuredCondition`（INV-13） | Registry + Factory Method + Interpreter（序列化） | 领域模型§8开闭扩展；07"规则版本"要求可存储；M1续解析器是第2消费方 | `domain/condition_codec.py` |
| F2 | **未结构化条款 Null Object**：`UnstructuredCondition` 恒 UNKNOWN、携带原文片段/定位；`evaluate` 的 `conditions=None` 特判收敛为一处默认包装 | Null Object | INV-13/ACQ-13 降级一等公民化；I8诊断/I6详情是第2消费方 | `domain/conditions.py` + `eligibility.py` |
| F3 | **排序策略协议**：`RankingStrategy`（score 签名）+ `BaselineA` 实现类；硬门槛过滤（fail/closed/开放状态）固定于 `rank()`，对所有策略共用 | Strategy | OPS-07 基线可替换；REC-13/T12 硬门槛独立性由结构保证；08 的 B/C 基线对照是第2消费方 | `domain/ranking.py` |
| F4 | **阈值条件基类**：`_ThresholdCondition` 吸收"字段缺失→UNKNOWN + 阈值比较"骨架，**保留领域命名子类** | Template Method | 三处现存重复（判据3）；新阈值类型（TOEFL/口径变体）一行接入 | `domain/conditions.py` |
| F5 | **条件↔证据关联**：`Condition.evidence_refs`（类属性默认空，子类按需声明为字段）+ `ConditionResult.evidence_refs`，判定结果可直达支持原文 | 数据设计（非模式） | ACQ-05 硬要求；REC-14 理由/REC-16 诊断/I6 详情依赖 | `models.py` + `eligibility.py` |
| F6 | **unknown_points 派生**：从 `details` 派生，消除双份真相 | DRY | 判据3 | `models.py` |
| F7 | **M1续开工协议**（写代码前定形，本文件即依据）：解析器 `can_parse(carrier)->bool` + `parse(content)->ParseResult(complete/partial/unsupported/failed)`，`ParserRegistry` 按载体选择；存储走 `EntryRepository`/`OpportunityRepository` 接口 | Registry + Strategy；Repository | ACQ-04 载体区分成为类型；07 存储可替换；M5 平台切换不动内核 | M1续代码（尚未编写） |

## 4. 拒绝的抽象（决策：不做，及理由）

| 诱惑 | 理由 |
|---|---|
| `AndGroup`/`OrGroup` 合并为带 mode 的通用聚合器 | 两个 6 行方法比带 flag 的通用方法可读；§4 聚合语义应在类名可见 |
| 反射式 `FieldThreshold(field_name: str)` | 失去类型安全与统一语言；F4 基类已消重复 |
| 现在引入 Result/Either 单子 | M2 按 OPS-05 错误分类、07 契约定形后再引入；现在没有第2消费方 |
| StudentProfile 拆嵌套子对象 | M3 画像版本化按 07 设计再动；现在拆只加构造噪音 |

## 5. 保持不动的既有设计

条件树 Composite/Interpreter（`AndGroup`/`OrGroup`/`ReliefClause`/`PriorityItem`，引擎零侵入）；纯函数内核（无 I/O/日志）；领域命名类即统一语言；`Verdict` 三值语义集中。

## 6. 落地记录

- 2026-11-05：F1–F6 全部实现——`condition_codec.py`（注册表+build/spec/priority+结构化错误）、`conditions.py` 重写（`_ThresholdCondition` 基类+`UnstructuredCondition`）、`models.py`（`evidence_refs` 关联+`unknown_points` 派生）、`eligibility.py`（NullObject 包装+证据收集）、`ranking.py` 重写（`RankingStrategy` 协议+`BaselineA` 类+固定过滤）。F7 为 M1 续开工协议（尚未编写代码）。

## 7. 验证

`PYTHONPATH=src python -m unittest discover -s tests` → **53 项测试全部通过**（33 项抽象落地 + 20 项 review 回归）。新增用例钉住：编解码回环与开闭登记（含保留字/冲突/别名/`type` 字段拒绝）、恶意策略不能让 fail 复活（T12/REC-13 结构验证）、未结构化条款入树保留原文、证据引用沿路径传播到 `ConditionResult`、空组拒绝、单边界窗口推导、窗口边界含端点、放宽 base=unknown 组合、开放未知排除原因（REC-11）。

## 8. Subagent 全量 review 处置（2026-11-05，独立评审者实测探针验证）

| 级别 | 发现 | 处置 |
|---|---|---|
| P0×1 | `undergrad_c9: bool=False` 使"未提供学校"被当作明确非C9，经确定性放宽可致整体误判 FAIL | **已修**：改 `Optional[bool]=None`，`IsC9` 缺失→UNKNOWN；回归测试 |
| P1×5 | ①空 AndGroup 空真放行/空 OrGroup 误杀且 codec 不拒；②单边界已知时可推导的 closed/not_yet 被降级 UNKNOWN；③`Window.required=False` 语义未定义；④注册表允许同类多名（别名）回环改写 type；⑤证据链断链（Evidence 无主键）+PriorityItem 只能 build 不能 spec | **全修**：①空组构造即拒（ValueError）+codec 同步；②`window_state` 单边界保守推导；③M1 口径文档化：非必需入口不参与聚合，"替代入口 OR"推迟 M2 显式建模；④`register` 拒绝别名；⑤`Evidence.eid` 主键 + `spec_priority` 双向 |
| P2 已修 | `type` 保留字段（注册时拒绝含该字段名的类）；build 缺键裸 KeyError→结构化 ValueError；明细只展一层致嵌套证据断链（递归展开+路径前缀，ReliefClause 条款级单行）；时区/纯度契约 docstring；`HasCet6` 按"通过≥425"口径；pyproject `pythonpath=["src"]`；测试补齐（边界/组合/排除原因等） | **已修**并附回归测试 |
| P2 记录推迟 | 组合器默认值双份维护（注释提醒）；`evaluate` 整树二次求值（以纯度契约声明承担）；BaselineA 缺"官方历史"因子（M2 历史数据接入后加，模型需补历史统计字段）；方向/偏好子串匹配（基线假设，金标校准时重审）；非 dataclass 注册类的 `spec()` 延迟失败 | 记录在案，随对应里程碑处理 |

## 9. 修订记录

- 2026-11-05 v1.0：审查发现 7 项（F1–F7）+ 4 项拒绝结论；用户确认"非过度设计即开发"，F1–F6 当日落地。
- 2026-11-05 v1.1：subagent 全量 review（P0×1/P1×5/P2×10）全部处置：P0/P1 全修、高价值 P2 同批修复、其余记录推迟（§8）；测试 33→53 全绿。
