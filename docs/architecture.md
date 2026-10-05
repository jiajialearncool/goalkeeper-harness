# 程序如何完成长程任务

`worker.py` 读取任务并创建独立模型客户端。`engine.py` 取得跨进程锁，核对残留进程、文件和不明确的操作，再组装上下文。模型提出工具调用后，程序检查目标版本、预算与权限，执行工具，保存实际输出、文件变化和 checkpoint。阶段与最终内部检查由程序运行，文本“已经完成”不能更新为交付成功。

GoalKeeper 的模型接入仅使用 `provider.py` 的 HTTP Responses。`codex.py`、`codex_worker.py` 只服务于产品对照。完整 GoalKeeper 与受控基线共用主循环与基础设施，仅通过 `Features` 切换四项机制。

## 接口

| 模块 | 入口 | 产生的结果 |
|---|---|---|
| 模型接入 | `ResponsesProvider.stream(ModelRequest)` | 流事件、完整响应、结束状态、可观测用量 |
| 主循环 | `Engine.run/execute/call` | 操作、预算与版本检查、恢复、交付申请 |
| 状态保存 | `Store.create/commit/get` | 原子保存状态和事件；拒绝旧版本覆盖 |
| 上下文 | `ContextManager.build/split/recall`、`Engine.compact` | 目标、摘要、近期原文与检索来源 |
| 工具 | `LocalTools.execute`、`Seatbelt.run` | 实际输出、退出码、超时与文件变化 |
| 子 Agent | `Engine.delegate` | 文件、patch、自检、问题、来源和父级合并检查 |
| 内部检查 | `Verifier.check` | 当前目标版本、文件校验值、结论与输出 |
| 独立评分 | `Scheduler.attempt/score`、`IndependentGrader.grade` | 停止后的快照、隐藏验收结论与证据 |

REST 负责创建任务、更新目标、暂停恢复和记忆管理。SSE 使用递增事件序号，客户端通过 `Last-Event-ID` 续接。工作台“内部检查通过”与正式独立得分是两个结论。

## 四项机制

目标锚保存原始目标、当前目标、硬约束与验收标准。用户或预设反馈程序可以更新，模型和文件内容不能授权更新。新版本取消尚未执行的旧版本操作，清空旧摘要、标记旧归档，使阶段证据失效。目标改变后可以重新提出阶段清单。

上下文上限为 64,000 **估算** token，估算器使用 tiktoken；模型没有映射时用 `o200k_base`。这是 Harness 输入组装配置，不等同于服务端实际计算。约 80% 触发压缩，目标降至 55%；摘要约 15%，近期原文约 20%。超长历史分块完整处理，完整原文先归档，摘要调用也计入预算。目标锚不参与压缩。FTS5 默认检索五条、最多 4,096 token，保留来源、时间与失效标记；第一版不具备向量语义检索。

进度骨架保存阶段、检查命令、状态和证据。本轮按可信检查依赖与文件校验判断哪些证据失效；动态导入、依赖不明和不确定操作仍采用全文件范围，目标变化保守重新验证。每阶段需要真实命令，模型申请检查后，程序核对结果再更新进度。

阶段和最终内部检查各最多两轮修复。全部角色、摘要、复核和重试共用预算。完成申请必须满足当前目标版本和文件的检查条件。Agent 可修改项目测试，正式隐藏验收在工作区外维护。

## 状态、记忆与恢复

SQLite 使用 WAL、`synchronous=FULL` 和 `BEGIN IMMEDIATE`。状态关联目标版本、操作、用量、事件、归档、记忆和进程所有权；任务保存完整配置与校验值。大记录存为 SHA-256 命名文件，数据库保存引用；JSONL 只是导出。

操作区分准备执行、执行中、完成、核对完成、结果不明确。调用前后与检查后保存记录。重启先核对进程创建时间，避免误杀重复使用 PID 的其他进程，再核对文件/Git 状态。不明确的写操作不直接重放，交给模型核对现状。跨进程锁阻止重复执行同一次尝试。

原文保存在过程归档；项目事实与显式偏好保存在记忆；待办由清单保存。主模型使用 `remember` 提出带来源的事实候选，程序核对文件原文并合并重复项，减少额外模型请求。文件不存在、事实原文改变时不再注入。目标、验收标准与待办不能借基础记忆恢复关闭的机制。偏好由用户输入，不自动推断凭据或身份；第一版未实现从所有自然语言自动抽取偏好。

## 角色与权限

Explorer 探索、Planner 规划、Reviewer 复核，均只读。Worker 在自己的 worktree 与分配文件内修改；父 Agent 合并 patch、处理冲突并检查合并结果。每次最多两个一次性子 Agent，子 Agent 不继续派生、不直接写中央状态库。

后端检查路径、角色和目标版本，Seatbelt 检查真实命令的文件与网络权限。环境变量重新组装，HOME/TMP 位于本次工作区，超时清理进程组。任务工具不能读取评分包、其他组工作区、认证元数据和工作台控制凭据。符号链接越界与 `.git` 写入受限制。正式三组完全离线；GoalKeeper 开发题可额外开放单个指定本地服务端口。

凭据存在系统钥匙串，元数据文件权限 600，私有目录权限 700。OAuth 校验 PKCE、state、浏览器会话、JWT 签名/issuer/audience/nonce；跨进程续期锁防止并发更新 refresh token。日志与记忆清除疑似凭据。

Codex 保留原生 Goal、子 Agent、恢复与压缩。控制程序统一预算、反馈、停止、快照和独立验收，不提供 GoalKeeper 清单或归档检索。初始任务输入进入原生 turn 后才激活 Goal，后续使用原生自动续跑；控制程序不注入“继续”对话。恢复前暂停持久 Goal，保留累计消耗。

## 当前接口依据

自身订阅授权与接口字段按 [官方注册说明](https://developers.openai.com/siwc/token-sharing-open-source/sign-in) 和 [订阅接口限制](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations) 实现。Codex 对照使用 [app-server 接口](https://learn.chatgpt.com/docs/app-server) 与本机生成的实验接口 schema。原生自动续跑的设计依据见 [官方 Goal 运行实现](https://github.com/openai/codex/blob/main/codex-rs/ext/goal/src/runtime.rs)；本机固定版本的真实行为仍需通过pilot确认。
