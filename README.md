# GoalKeeper Harness

一个独立运行的长程任务 Harness 实验项目。模型通过项目自身的订阅授权直接调用；GoalKeeper自行负责目标、上下文、工具、状态、预算和验证。Codex是开发工具和对照系统，不是GoalKeeper运行时的底层执行器。

**当前公开版本用于朋友审阅代码与分析问题，不是已经优于Codex的成熟产品。** 来源提交：`ec7df3c33fa05ca126b2b27a3c93f20b3b133f14`，整理日期2026-10-05。此仓库使用一个干净快照，不包含原项目Git历史、账号凭据、原始会话或私有评测题包。

## 建议先读

1. [已知问题与失败结果](docs/known-issues.md)：为什么最新调优尚未降低整题成本。
2. [架构与执行过程](docs/architecture.md)：模块关系，以及框架、Agent、模型分别负责什么。
3. [当前实现与验证状态](docs/status.md)：哪些已有实现，哪些只是后续建议。
4. [审阅代码的路线](docs/review-guide.md)：问题对应的代码入口与待核对假设。

## 当前结果：没有证明优于Codex

最新第1题开发验证：交付文件通过15项独立测试，但必要Reviewer未完成，任务耗尽token预算，**不计为合格交付**。执行时间22分03秒是预算停止时间，不能写成成功交付提速。已记录至少1,022,929 token，用量不完整。

历史Codex同题记录为成功交付、13分29秒、302,163 token；本轮没有同时重跑Codex，不能把历史记录当作严格同步对照。首轮12题三组实验为Codex12/12、GoalKeeper10/12、受控基线6/12；其上下文工程缺陷及题目规模限制也必须计入解释。

[脱敏数据摘要](docs/evidence/m1-summary.json)保留两次最新开发尝试、前序记录与观测限制。原始记录留在本地，不随仓库发布。独立测试通过、内部交付完成、任务成功是需要分别记录的事情。

## 实现与配置

后端Python 3.11/FastAPI/Pydantic 2/httpx，前端React/TypeScript/Vite，SQLite WAL与FTS5，macOS Seatbelt、独立工作区、Worker worktree。

`src/goalkeeper/models.py`中的默认模型为`gpt-6.1-sol`，推理强度`xhigh`。旧`gpt-6-astra`标识只用于读取旧记录，新的运行检查要求当前模型。上下文配置默认64,000估算token，约80%触发压缩；每题默认30分钟、100万可观测token、最多两个子Agent。

| 配置开关 | 普通RunConfig默认 | 最新第1题开发尝试 |
|---|---:|---:|
| 四项Features | 开启 | 开启 |
| `efficiency_enabled` | false | true |
| `focused_context_enabled` | false | true |
| `global_efficiency_enabled` | false | true |
| `final_review_required` | false | true |

运行时必须保存完整RunConfig；仅运行默认配置不能复现开发尝试的所有行为。新的“按当前工作组织输入、明确风险清单与预算”的重设计尚未实现，不能归入本快照已实现功能。

## 安装与不调用模型的检查

真实工具执行与部分测试要求原生macOS。Linux/Windows不能直接复现Seatbelt沙箱。Python锁文件要求Python>=3.11且<3.13；前端开发环境使用Node 24。审阅核心代码和运行工程测试不需要ChatGPT授权。

```sh
uv sync --frozen --extra dev
npm --prefix frontend ci
```

在项目根目录运行：

```sh
.venv/bin/ruff check src tests scripts
.venv/bin/python -m pytest -q
npm --prefix frontend run lint
npm --prefix frontend run build
```

工程测试使用模拟模型与HTTP模拟。真实Seatbelt测试需要原生终端权限；嵌套外部沙箱可能阻止进程核对或Seatbelt启动，应报告环境限制，不能关闭安全规则来让测试通过。测试中的JWT/refresh-token文本是合成测试材料，不是真实凭据。

### 本地工作台

```sh
.venv/bin/python run_goalkeeper.py serve --port 8765
```

打开`http://127.0.0.1:8765/`。真实模型运行需要项目自身的“Continue with ChatGPT”授权，Codex已登录不能代替。可用性、订阅资格和额度需要实际确认，本仓库没有携带可复用授权。工作台真实闭环检查会调用模型；仅审阅代码时无需点击。

## 发布范围与可复现边界

- 包含核心源码、前端、锁文件、全部工程测试、通用四组调度器和已用于开发的合成pilot样例。
- 排除后续长任务与M1/M2题目生成器、私有实例、隐藏评分包、参考解包、账号授权数据和原始运行日志。`pilot.py`中的样例及反例是公开开发材料，未来不得再当作未见正式题。
- 保留评分器、题包协议和权限隔离代码，便于审阅；没有私有题包，就不能从仓库复现原正式成绩。
- Office演示依赖桌面工具运行库，不随仓库分发；私人用户名路径仅改为当前用户的`Path.home()`/`os.homedir()`，未新增办公能力。
- 旧协议文档保留为历史背景。当前模型、状态和发布范围以本README与status.md为准。

尚未指定开源许可证。公开仓库的当前用途是代码审阅；后续许可证另行确定。

## 来源和发布检查

[来源与排除清单](docs/publication-provenance.json)记录源提交、原文件校验值和发布整理范围。[发布工程检查](docs/publication-checks.json)记录本次检查结果。凭据、数据库、原始会话和本机完整路径不在公开清单中。
