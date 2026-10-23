---
author: 'Codex（AI助手）'
created: '2026-10-16'
updated: '2026-10-16'
status: '官方文档核验，未实测'
scope: 'AgentArts高代码托管、多Agent与本项目平台适配'
---

# AgentArts高代码与多Agent能力核验

作者：Codex（AI助手）。创建／核验日期：2026-10-16（北京时间）。

本报告服务赛题选择，不替代PRD或架构决策。资料截至2026-10-16；未登录开通服务，未部署或压测。

## 结论

AgentArts支持LangChain／LangGraph代码开发与云端托管，不能理解为只接收外部框架的观测日志。我们的采集复核、画像推荐两部分可以考虑以LangGraph组织协作，再由AgentArts运行时托管。多Agent的具体协作方式仍由我们设计，平台不会自动替代业务编排、数据治理和错误恢复。

## 官方能力与适用边界

**1．代码运行与部署：已有直接证据。** 官方SDK介绍列出纯托管和能力增强两种模式，支持LangChain／LangGraph等框架开发的Agent部署。快速开始给出本地调试、镜像构建、上传SWR、创建AgentArts托管运行时和云端调用的完整步骤。因此，“使用LangGraph”与“部署在AgentArts”可以同时成立。[SDK介绍](https://support.huaweicloud.com/highcode-agentarts/agentarts_10_037.html)、[快速开始](https://support.huaweicloud.com/highcode-agentarts/agentarts_10_040.html)

**2．多Agent：区分平台原生低码与自行代码编排。** 低代码提供多Agent控制器，按意图路由到子工作流或子智能体。高代码路径接收容器化程序，适合掌控行为逻辑，两者不能互相代替作能力承诺。官方LangGraph模板实际只有一个process节点；它证明图可以运行，不能证明现成模板已经实现完整多Agent。采用多角色节点、子图与调度器构建我们自己的多Agent系统，是基于框架运行能力的工程推断，需通过最小部署验证。[开发路径](https://support.huaweicloud.com/qs-agentarts/agentarts_04_0002.html)、[多智能体配置](https://support.huaweicloud.com/lowcode-agentarts/agentarts_05_0106.html)、[模板源码](https://github.com/huaweicloud/agentarts-sdk-python/blob/main/src/agentarts/toolkit/utils/templates/langgraph/agent.py.j2)

**3．工具与MCP：可扩展，不代表已有完整招生采集器。** SDK具有工具扩展及MCP网关管理；网关文档给出接入远程MCP服务目标的示例。自定义官网访问、正文和附件解析、检索与复核逻辑，可以作为我们提供的代码工具或独立服务接入。这是集成方案；公开文档不保证任意高校页面、反爬环境或浏览器依赖均能直接运行。[网关指南](https://github.com/huaweicloud/agentarts-sdk-python/blob/main/docs/cn/sdk_user_guide/gateway_user_guide.md)

**4．多轮与状态：有持久化集成，完整图恢复仍需验证。** 官方示例用thread_id继续对话，支持AgentArts Memory云端状态存储和跨进程共享。当前saver源码存在中间写入实现，但恢复的channel_values主要围绕messages；不能据此承诺任意业务字段、分支、中断后恢复均完整兼容。画像、院校政策与执行任务状态应分别明确存储职责，长期记忆也不能直接替代业务数据库。[LangGraph集成示例](https://github.com/huaweicloud/agentarts-sdk-python/tree/main/examples/integration/langgraph)、[检查点源码](https://github.com/huaweicloud/agentarts-sdk-python/blob/0d620ccdf5a30e8ef2d556137a9c93cde4d18e78/src/agentarts/sdk/integration/langgraph/saver.py)

**5．长任务与并发：有基础机制，容量未知。** Runtime支持异步入口、后台任务健康跟踪、SSE／WebSocket以及可配置并发请求数；SDK默认并发15属于应用层设置，不能当成云平台承诺的并发容量。全校扫描所需任务时长、网关超时、资源回收、并发扩缩容与失败重试，尚未通过本次公开资料和实测确定。[Runtime指南](https://github.com/huaweicloud/agentarts-sdk-python/blob/main/docs/cn/sdk_user_guide/runtime_user_guide.md)

## 对本项目和赛题二的含义

建议候选方案为：信息侧和推荐侧保留独立协作设计，LangGraph承担代码编排，AgentArts提供实际托管；工具、资料库和画像库通过明确接口接入。此方案贴近现有需求，既保留可插拔探索能力，也能展示平台运行证据。

参赛材料应保留平台中的部署资源、可调用入口、运行记录、编排源码或图与交互演示。仅在外部服务器运行，再向AgentArts上报日志，不能自行视为满足赛题的构建、编排、部署要求。高代码托管是否被当届赛题接受为相应编排方式，应结合赛题说明或向组织方核实；产品支持不等于赛方已逐项认可。

## 设计前需验证的少量事项

- 账号是否可开通高代码运行时、Memory和所需模型服务，实际可用区域是否一致。SDK介绍当前列西南-贵阳一。
- 快速开始明确要求ARM64镜像，并建议Ubuntu 22.04 ARM64；现有Windows／x86环境需准备适用构建路径，采集依赖也需验证ARM兼容性。
- 用“两个协作节点＋自定义读取工具＋一次复核＋多轮状态”做最小验证，检查部署、来源返回、状态隔离和失败处理。
- 固定SDK及框架版本，验证复杂状态保存与恢复、请求时长和受控并发，不以滚动main的介绍代替发布版能力。

源码核验固定到官方仓库提交`0d620ccdf5a30e8ef2d556137a9c93cde4d18e78`（2026-10-15）。网页缓存存在旧版检查点实现，本报告采用直接读取的当前源码；后续实施仍需记录实际安装版本。
