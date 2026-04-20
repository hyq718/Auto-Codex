## 2026-04-20 项目介绍更新

### 一、项目现在能做什么

`Auto-Codex` 现在已经不是“一个 prompt + 一次性长对话”，而是一套围绕长期 autoresearch 设计的运行系统。

它现在已经具备以下能力：

- 用户只提供一个 mission markdown，例如 `autoresearch.md`
- 在当前项目目录下自动创建 `./auto-codex` runtime，而不是写到工具仓库目录里
- 先生成 plan preview，在用户确认前不正式执行
- 用户确认后进入正式执行阶段，并把 plan 作为持久化状态保存在磁盘
- 持续记录 state / jobs / inputs / events / summaries
- 支持通过 skill 或 mode 命令在对话里查看状态、更新方向、暂停、恢复
- 支持飞书文档写入、飞书文档轮询输入、后台 daemon、SLURM job 跟踪

换句话说，这个项目现在的目标不是“让 Codex 一口气做完所有事”，而是“让 Codex 在长期任务中可以稳定恢复、持续推进、并且可被用户中途介入纠偏”。

### 二、项目当前的核心架构

当前架构可以理解为四层：

#### 1. Mission 层

用户入口仍然是一个 mission markdown。

它负责表达：

- 总目标
- 约束
- 背景信息
- 输出要求
- 飞书链接
- 如果有的话，已有的高层 plan

#### 2. Mode 层

这是当前最接近 `/autoresearch` 体验的一层。

它负责：

- 把 mission bootstrap 成 runtime
- 输出 plan preview
- 等用户确认或修改 plan
- 把 runtime 当前状态渲染成适合对话阅读的形式

当前关键 mode 命令包括：

- `mode-start`
- `mode-approve-plan`
- `mode-revise-plan`
- `mode-status`
- `mode-sync`
- `mode-update`
- `mode-plan`
- `mode-jobs`
- `mode-pause`
- `mode-resume`
- `mode-stop`

#### 3. Runtime / Supervisor 层

这是持久执行内核。

它负责：

- 把 mission 编译成 runtime 目录
- 维护 `state.json`
- 维护 plan、inputs、jobs、events、notes
- 按 tick 唤起短 burst worker
- 在等待 job 时休眠并恢复
- 对接飞书和 SLURM

#### 4. Worker 层

这是每轮真正干活的短 burst。

它不负责长期记忆；长期记忆已经落盘到 runtime。

它负责：

- 读取 mission/state/plan/inputs/notes/jobs 摘要
- 做一小段最有价值的工作
- 返回结构化 JSON
- 更新 plan、phase、next action、job metadata、summary

### 三、这个项目最新补上的三件关键能力

#### 1. Plan Handshake

现在 `mode-start` 默认不会立刻开跑。

而是会先：

- 读取 mission
- 生成或提取 starter plan
- 进入 `awaiting_plan_confirmation`
- 输出 plan preview 给用户确认

这一步是为了避免系统在一开始就沿着错误 plan 长时间推进。

对应的运行规则是：

- `plan.preview.json`：未确认的预览计划
- `plan.json`：已确认的正式计划
- `notes/plan_preview.md`：给用户看的预览计划
- `notes/plan.md`：正式 current plan 的可读版本

这意味着现在的系统已经支持：

- 先看 plan
- 再修改 plan
- 再批准 plan
- 才正式进入执行

#### 2. Token-Aware Job Reading

现在系统不再把 `jobs/` 整个目录当成默认上下文。

而是采用“先摘要，后深读”的策略：

- 每轮先生成 `notes/jobs_focus.md`
- 只挑当前最需要关注的一小组 job
- worker 先读这个 focused jobs 摘要
- 只有当前步骤确实需要时，才进一步打开某个 job 的 json 或日志

这层设计是为了节省 token，并且减少无关日志对决策的干扰。

当前策略的核心是：

- 先找最相关 job
- 先 grep / 搜索
- 找不到再 tail 小窗口
- 再扩大读取
- 最后才考虑全量读取或扫描其他 job

#### 3. Recovery-Oriented Status

这是当前最重要的新设计。

系统现在不仅记录“大 plan”，还记录“恢复对话时的精确下一步”。

也就是说，系统现在区分三层东西：

- `plan`：整个任务的大方向
- `phase_plan`：当前阶段的小计划
- `next_action`：恢复后第一步该做什么

这部分会落在：

- `state.json` 的 `execution` 字段
- `notes/resume_status.md`

其中 `execution` 现在包括：

- `current_phase`
- `phase_plan`
- `next_action`

`next_action` 会尽量表达清楚：

- 下一步是什么
- 为什么是这一步
- 去哪里看
- 搜什么关键词
- 如果没找到，按什么 read ladder 扩展
- 什么算成功
- 找不到时 fallback 到哪里

这层设计的价值在于：

- 重新进入对话时，Codex 不需要重新大范围阅读和重新规划
- 它应该能直接知道“先做这一件事”

### 四、现在 runtime 的核心文件分别在干什么

当前运行时最关键的文件包括：

- `mission.md`
- `state.json`
- `plan.preview.json`
- `plan.json`
- `inputs.jsonl`
- `events.jsonl`
- `jobs/<job_id>.json`
- `notes/plan_preview.md`
- `notes/plan.md`
- `notes/jobs_focus.md`
- `notes/latest_summary.md`
- `notes/resume_status.md`

可以把它们理解为：

- `mission.md`：原始任务意图
- `state.json`：机器主状态
- `plan.preview.json`：待确认 plan
- `plan.json`：当前正式 plan
- `jobs/*.json`：每个 job 的持久化元数据
- `jobs_focus.md`：当前 tick 只该优先看的 job 摘要
- `latest_summary.md`：上一轮工作的浓缩摘要
- `resume_status.md`：恢复时最重要的行动包

### 五、现在的系统是怎么帮助“恢复对话”的

这一版系统的核心不再只是“记住发生过什么”，而是“记住下次该做什么”。

也就是说，下一次恢复对话时，Codex 理论上应该先看：

- `notes/resume_status.md`
- `notes/latest_summary.md`
- `plan.json`
- `notes/jobs_focus.md`

而不是默认就去重读大量历史日志。

如果当前是一个 job 等待场景，那么 `next_action` 应该告诉它：

- 当前最关注哪个 job
- 去哪个日志文件看
- 搜哪些模式，比如 `10k` / `eval` / `eval loss`
- 找不到时先 `rg`
- 再 `tail -n 20`
- 再扩大窗口
- 再全文
- 最后才扩展到其他 job

这代表当前项目已经从“长期记录系统”进一步走向了“可恢复执行系统”。

### 六、当前我对这个项目的整体判断

如果用一句话概括当前版本：

`Auto-Codex` 正在从“带状态的 autoresearch runtime”进化成“可计划、可恢复、可节省 token 的长期研究执行系统”。 

当前最值得保留的设计已经比较明确：

- mission 单入口
- plan preview 先确认再执行
- runtime 跟随当前被研究项目目录
- jobs 和 logs 采用 summary-first 策略
- 恢复时优先看 `next_action packet`

如果这套思路后续继续完善，那么它的价值就不只是“帮忙自动 research”，而是让 Codex 在长周期工程任务里真正具备：

- 持久上下文
- 低 token 恢复
- 清晰的阶段控制
- 人机协作中的即时纠偏能力
