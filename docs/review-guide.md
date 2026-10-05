# 给代码审阅者的阅读路线

目标：在相同验收质量与模型配置下，尝试降低长任务的失败、时间和token；当前数据尚未支持相对Codex优势。实现迭代借助编码Agent完成；请对代码实际行为提出判断，不把方案文字当作已实现证据。

## 第一组：执行与累计成本

先读`models.py`、`engine.py`的`run`、`call`、`budget`、`finishing`与`closeout`。模型负责生成分析或工具请求，框架负责执行、权限、状态、预算与验证。所有角色共享预算。完成的响应才可能返回完整用量，在途截止会留下未知用量。

请检查：是否缺少在调用前对累计输入成本、剩余token与复核成本的联合安排；少一次摘要是否让更多历史反复进入后续主请求；预算停止前是否能完成规定交付。

## 第二组：工作上下文与读取

读`context.py`的`work_messages`、`build`、`split`、`read_source`，再读`engine.py`的`read_with_context`与`restore_sources`。持久历史与临时工作视图是两个对象，不能通过修改视图破坏工具调用与结果的配对或丢弃必要证据。

请检查：较旧成功结果改成引用后，是否导致读回循环；有效文件正文的6,000估算token总保留额度是否适合正在复核的资料；`force=true`是否频繁绕过去重；引用输出是否携带过多协议包装。日志里已经观察到重读，但各项因果贡献尚未完整分解。

## 第三组：Reviewer、证据和恢复

读`engine.py`的`ensure_review`、`delegate`，`assurance.py`、`verifier.py`、`store.py`。复核保存已完成轮次；窗口到期可续同一子任务；目标或文件快照变化会使进度失效。

请检查：复核任务是否只有提示词限定、没有有限风险清单；180秒分段窗口是否缺少子任务总成本控制；必要复核和修复预算是否与实际消耗相符；模型确认、实际检查通过与正式评分是否被混用。不要通过直接取消必要复核来宣称同条件提速。

## 第四组：可靠性与权限

读`auth.py`、`secrets.py`、`sandbox.py`、`files.py`、`processes.py`。工程测试位于`tests/`；尤其查看`test_global_efficiency.py`、`test_focused_context.py`、`test_context_failures.py`、`test_compaction_progress.py`、`test_budget_closeout.py`、`test_requirement_evidence.py`与`test_resource_resume.py`。

请区分“规则按测试运行”与“真实整题成本降低”。213项工程检查通过并不能证明效率优势。后续优化需同时保留目标、权限、来源、状态恢复和验收证据。

## 评测接口

`benchmark.py`定义题包数据，`grader.py`对停止后的快照独立评分，`evaluation.py`与`scripts/four_harness/run_round.py`调度尝试并记录用量。公开pilot是开发样例；私有正式实例没有上传。统计接口见`statistics.py`。

建议反馈包含：对应代码位置、可复现的反例、预计影响的整题指标和必要验证。当前未实现新的框架重设计，不应把设计建议当成现有能力。
