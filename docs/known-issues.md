# 已知问题与保留的失败结果

状态日期：2026-10-05。公开来源ec7df3c的实际实现，不是讨论中的新架构。公开数据见[evidence/m1-summary.json](evidence/m1-summary.json)。工程测试不能代替真实模型评测。

| 第1题指标 | 首次全流程开发验证 | 修正后复验 | 历史Codex参考 |
|---|---:|---:|---:|
| 执行时间 | 23分05秒 | 22分03秒 | 13分29秒 |
| 可观测token | 至少1,003,223 | 至少1,022,929 | 302,163 |
| 独立文件测试 | 15/15通过 | 15/15通过 | 通过 |
| 完整交付 | 未完成必要复核 | 未完成必要复核 | 完成 |

两次GoalKeeper均因token预算停止，不能把更早停止当作成功交付提速。合计至少2,026,152 token、45分09秒执行时间。Codex没有本轮同步重跑；历史记录只供诊断。普通RunConfig的final_review_required默认false，最新尝试设为true；此前开发记录的复核门槛不同，不能直接作严格前后对照。

## 累计输入成本仍高

复验主Agent32次请求，输入634,738、输出25,939，共660,677 token。摘要调用0次不等于整题更省。末次工作请求约37,627估算token，低于64,000窗口的80%触发值。窗口安全与累计输入低是两件事。

检查context.py的work_messages、build、should_compact，以及engine.py的run、call。历史、工具参数、正文与协议包装的具体因果贡献尚未完整归因。

## 正文整理与复核需求没有协调好

工作视图保留有效文件正文总额度约6,000估算token，global模式单文件最多3,000；其他较旧成功工具结果转成引用。Reviewer读取文件32次，其中22次force=true，另有12次read_source。

日志确认重读；可能形成移出、读回、再次移出的循环。force保留必要读取的自由，也能绕过去重。主Agent重复正文从前序记录8次降至5次，但最新所有角色实际file_read_deduplicated事件为0，不能据此宣称整题去重收益。

入口：context.work_messages；engine.read_with_context、restore_sources、delegate。

## Reviewer没有及时形成结论

Reviewer22次请求，已记录输入357,861、输出4,391，共362,252 token，另有1次用量未知；模型请求耗时约4分38秒。没有得到结构化finish结论，不代表已经确认没有缺陷。

180秒分段后能继续同一子任务，保留进度与子Agent计数；复核范围仍主要依赖提示词，缺少有限风险清单及适合实际消耗的总体安排。入口：engine.ensure_review、delegate、review_progress。不能通过取消必要复核制造同质量降本的结论。

## 预算与收尾不匹配

默认收尾预留50,000 token；复验Reviewer记录362,252。用量常在响应结束后返回，记录到22,929 token阈值超出及未知在途用量。

复验quota_wait_seconds约30秒实际是内部review_window分段调度等待，不是确认的额度耗尽或外部拥堵。字段归类有解释限制。入口：engine.budget、finishing、finish_reserve、call、closeout与通用调度器run_round.py。

## 已修复而未产生效率优势的事项

首条全流程运行的一次Reviewer发生RemoteProtocolError，原异常处理把资源错误变成普通工具失败。当前改为向上返回、保存后续跑。复核窗口结束沿用原子任务；来源引用使用确切call，而非整段历史归档。原先工具大结果、压缩配对等已有工程检查，可靠性修复不等于效率优化。

原版213项工程检查曾通过14.04秒；发布副本另行重新检查，见publication-checks.json。

## 尚未实施的新设计

按当前工作与证据重组每轮输入；按有限风险清单安排Reviewer；联合分配开发、验证、修复和交付预算。这些仍是建议，没有量化收益承诺。

## 第一轮结果的边界

最早三组12题一次运行：Codex12/12、完整GoalKeeper10/12、受控基线6/12，模型gpt-6-astra/xhigh。题目规模、自然压缩覆盖及基础上下文缺陷影响解释，不能据此证明稳定收益或Codex所有长任务100%成功。
