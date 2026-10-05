> 历史开发或评测说明：部分命令依赖未公开题包、工具或本机数据。当前发布范围和验证结论以 [README](../README.md) 与 [已知问题](known-issues.md) 为准。

# 运行与评测说明

## 开始真实调用前

先运行 `check`、`doctor`、`pilot-prepare`，核对 `outputs/` 中的三份结果。通过工作台完成 GoalKeeper 自身授权，点击真实接入检查，然后再次运行 `doctor`。此时 `real_smoke_passed` 和 `codex.same_chatgpt_account` 必须为 true。程序不会因为 Codex 已登录就借用其凭据。

工程检查和评分器检查使用模拟模型或已写好的文件，真实模型调用数为零。它们不能替代真实闭环、pilot、并发测量和评测结果。

### 授权失败时

认证进程和模型客户端沿用本机已配置的系统及进程网络代理，不修改系统网络设置。任务工具仍使用清理后的环境，并受独立沙箱约束。首次接入实测中，禁用系统代理的请求返回`unsupported_country_region_territory`；沿用现有系统网络后，请求到达OAuth参数校验阶段。该连通性检查使用故意无效的测试参数，不代表真实授权已经成功。

工作台保留授权错误的HTTP状态、错误代码、响应形状和请求ID，不保存授权码或原始凭据。地区或权限拒绝需要先核对实际限制，不能靠反复授权解决。`invalid_grant`需要从工作台首页生成新的授权流程，不刷新旧回调页面。已经签发的公开client ID会在换取凭据前保存，后续重试复用该注册；账号仍须通过ID token和权限校验后才显示为已授权。

### 已授权但计划指定模型不在列表中

先使用自身授权读取`/api/models`，核对模型标识及推理档位。以独立接口当前账号的模型列表为准；Codex的`model/list`可能使用内置或缓存列表，不能直接替代这项检查。保存不含凭据的模型列表与账号一致性检查结果。此时重复授权不能确认模型可用，不要借用Codex凭据或把独立调用改接其他端点。

如果需要改变计划固定的模型，先确认三组共同的选择，再同步主Agent、子Agent、摘要和复核的配置，重新运行接入与工程检查。真实闭环通过前不能启动pilot，也不能冻结正式版本。

## 分步 pilot

以下命令在项目目录运行。真实模型、子 Agent、摘要和复核均会消耗现有订阅额度。

```sh
.venv/bin/python run_goalkeeper.py pilot --group goalkeeper --cases A1 --concurrency 1
.venv/bin/python run_goalkeeper.py pilot --group goalkeeper --cases A2 B2 --concurrency 2
.venv/bin/python run_goalkeeper.py pilot --group goalkeeper --cases A3 B3 C1 C2 --concurrency 4
.venv/bin/python run_goalkeeper.py pilot --group goalkeeper --cases B1 D1 D2 E1 E2 --concurrency 4
.venv/bin/python run_goalkeeper.py pilot --group controlled --concurrency 4
.venv/bin/python run_goalkeeper.py pilot --group codex --concurrency 4
```

并发增加前检查上一档记录。如果二并发或四并发有资源、存储或工程问题，保留记录并使用通过检查的较低并发；不要把最大并发配置当成实测吞吐。Codex 和受控基线固定跑 A1、B2、D1、E1 四道代表题。

额度中断后，同一命令会继续原尝试，保留已发生用量、任务时间和等待时间。已取得的有效成功或失败不会自动重跑，也不能重置预算。

最多四项诊断预留中的一项供 F1 使用。其他诊断运行必须明确选择一道题、说明原因；例如：

```sh
.venv/bin/python run_goalkeeper.py pilot --group goalkeeper --cases A1 --concurrency 1 --diagnostic-reason '修正接入解析后验证原工程故障'
```

基础失败和诊断结果分别保存，不能挑最好一次替换原记录。`runtime/private/pilot-fault-explanations.json` 按失败 run_id 保存 `cause`、`fix_commit`、`diagnostic_run_id`；进入冻结前，应核对诊断确实验证了修正。未解释工程故障会阻止冻结。

F1 真实演示：

```sh
.venv/bin/python run_goalkeeper.py office-pilot
```

程序使用模拟销售输入，但分析、表格、材料和改版由真实 GoalKeeper 执行。独立程序检查原始行、总额、公式、修改输入后的计算、原生图表及六页/三页要求。工作台在真实浏览器中也必须完成创建、目标更新、记忆管理与断线续接检查，证据保存在 `outputs/workbench-browser-checks.json`，通过后才允许冻结。F1成功后还需要逐页视觉检查，将证据及结论保存在 `outputs/f1-validation.json`。原生 PowerPoint 内实际编辑再打开的检查另记，未运行就不得声称通过。

`office-demo` 生成开发文件样例，不使用模型，不计入 Goal 完成条件。`office-pilot` 中断后再次执行会继续同一次演示。

## 冻结与正式新题

确认 [两个 Goal 的条件](goals.md)，保存程序的本地 Git 提交，再运行 `freeze --concurrency N`。N 必须是已实测通过的 1、2 或 4。程序核对当前源码与工程检查校验值，检查真实 pilot、权限、反例与 F1；不满足时返回具体阻挡原因。冻结文件不可覆盖。

**冻结完成后**才制作 `runtime/private/formal-package/`。用 `Case` 模型的结构定义题目，目录包含初始源码、隐藏独立测试、参考解、关键约束反例、manifest 和 `unseen-provenance.json`。后者记录 `created_after_freeze`（冻结提交）及 `novel_code_or_requirements_verified:true`，同时写明新代码、需求组合或数据来源的核对依据。这个布尔字段不能代替真实的新题审查。

每题公共检查不能读取隐藏评分资料。反馈使用独立业务进展探针，恢复题共同交接包只给目标、代码与业务进展；三组外部信息相同。为新题运行全部参考解与反例检查，保存证据后再封存。第一版不自动把开发题变成正式题。

正式运行：

```sh
.venv/bin/python run_goalkeeper.py evaluate
.venv/bin/python run_goalkeeper.py report --phase formal
```

`evaluate` 核对冻结、账号、12题分布、新题声明和离线权限；首次封存输入校验值与36次运行顺序。后续恢复检查相同封存值。额度恢复可再次执行同一命令；已经有效的结果保留，只续跑资源中断。

## 评分异常与保存资料

评分进程异常时，保留停止后的交付物，只恢复评分，不重新调用 Agent。若发现评分**标准或程序有错误**，按正式协议将受影响的三组结果统一作废，保存原轮记录，修正并生成新冻结版本后重新运行。不可在原轮修改隐藏测试后直接续跑；输入校验会拒绝这种操作。

备份源码提交、两份依赖锁、`runtime/private/`（含数据库和原文）、`runtime/artifacts/` 与 `outputs/`。授权凭据留在系统钥匙串，不导出到分享材料。JSONL 是可重建的记录导出，不应与数据库分别修改。

## F1 交付副本与原始证据

真实 Agent 停止后保留不可覆盖的原快照。`office-pilot` 对成功文件生成单独交付副本，自动把图表轴编号转换为 OOXML 的无符号格式，不改变图表数据。原始文件、交付副本、转换前后校验值和每个修改部件均有记录。显示检查中的单次排版修正另外记录，不能描述成通用自动排版功能，也不计入正式编码评测。

再次执行已完成的 `office-pilot` 只复核已有文件，不重新调用模型。每次复核会将视觉结论重置为待检查；只有检查当前副本的全部页面后，才能记录通过。新增 F1 诊断与其他 pilot 尝试合计受 24 次上限约束，不重置失败尝试的消耗。
