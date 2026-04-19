## 2026-04-19 设计更新

### 背景

这次对 `autoresearch` 的核心判断是：单个 `autoresearch.md` 可以作为唯一用户入口，但不能把“24 小时持续推进、等待 job、恢复执行、进度同步”这些能力全部寄托在一段 prompt 上。

之前纯 prompt 方案的问题主要有两类：

1. prompt 可以描述工作流程，但不能保证 Codex 在对话结束后数小时自动回来继续工作。
2. prompt 可以要求“sleep 10 分钟后继续”，但只要 session 停止，这种等待逻辑就失效。

因此现在的设计方向是：

- 用户入口仍然保持为一个 `autoresearch.md`
- 由系统自动把这个 mission 编译成一个可持续运行的 runtime
- Codex 只负责一轮一轮地做具体工作
- 持久性、恢复、轮询、模型切换交给 supervisor

### 设计目标

当前设计的目标是下面这几条：

1. 用户只需要提供一个 mission markdown。
2. Codex 可以把 mission 自动拆解成可执行计划，而不是要求用户预先写很多辅助文件。
3. 当任务包含长时间 job 时，系统可以在等待期间退出当前 worker，并由 supervisor 后续重新唤醒。
4. 当前进度、计划、job 状态和摘要必须落盘，不能只存在对话上下文里。
5. 飞书文档应该作为一个持续追加的外部可见进度面板。
6. 模型限额、失败恢复、人工接管都需要有明确状态和痕迹。

### 核心架构

当前版本的设计分成三层：

#### 1. Mission 层

`autoresearch.md` 是单一入口，负责表达：

- 目标
- 约束
- 参考资料
- 报告要求
- 执行优先级

用户视角上，最好始终是“给一个 markdown，然后启动 autoresearch”。

#### 2. Worker 层

Worker 本质上是一次短 burst 的 `codex exec` 调用。

每一轮 worker 的职责是：

- 读取 mission 和当前 runtime 状态
- 决定下一步最高优先级动作
- 执行一个有限工作块
- 把新的 job、摘要、计划变化写回磁盘
- 返回结构化 JSON

也就是说，worker 不是长期存活的 agent，而是可重复拉起的执行单元。

#### 3. Supervisor 层

Supervisor 负责长期运行，不负责“思考”任务本身。

它的职责包括：

- 定时唤醒 worker
- 检查 `squeue` 和日志
- 管理 sleep / retry / stop
- 处理模型 fallback
- 管理 pid 和后台进程
- 把对外进度同步到飞书

关键点是：真正持续 24 小时运行的是 supervisor，不是单次 Codex session。

### 为什么是 `skill + runtime`，而不是只有 skill

现在的结论是：

- `skill` 很适合封装规范工作流
- 但 `skill` 本身不提供持久执行

所以更合适的形态是：

- `skill` 提供行为规范、状态约定、执行提示
- `runtime` 提供 supervisor、state、job tracking、Lark reporting

这意味着最终开源形态也更自然：不是一个 prompt 文件，而是一个 `skill + scripts` 项目。

### 当前已经实现的 runtime 设计

目前已经在 `/home/yqhao/autoresearch_for_codex` 下实现了一个最小可用版本。

主要入口：

- `scripts/autoresearch.py`
- `skills/autoresearch/SKILL.md`

CLI 当前支持：

- `init`
- `start`
- `status`
- `stop`
- `daemon-start`
- `daemon-status`
- `daemon-stop`

这里面的设计重点是：

1. `init` 从单个 mission markdown 自动生成 runtime。
2. `start` 运行前台 supervisor loop。
3. `daemon-start` 让 supervisor 后台运行，而不是占着前台终端。
4. `status` 和 `daemon-status` 分别提供任务状态和后台进程状态。
5. `stop` / `daemon-stop` 提供可记录原因的优雅停止。

### 持久化状态设计

当前 runtime 的核心不是对话记忆，而是磁盘状态。

当前重要文件包括：

- `mission.md`：任务原文副本
- `runbook.md`：运行说明
- `state.json`：机器可读状态
- `plan.json`：当前计划
- `events.jsonl`：事件流
- `jobs/`：job 级别元数据
- `notes/latest_summary.md`：最新摘要
- `notes/plan.md`：可读计划
- `logs/codex/*.log`：每轮 worker 的原始日志
- `logs/supervisor.log`：后台 supervisor 日志
- `supervisor.pid`：后台 pid 文件

这个设计的目的，是让任何一次中断之后，后续 worker 或人工都能继续接手。

### Plan 设计

这次实现里，一个很重要的改动是把 plan 也变成 runtime 的一等公民，而不是只停留在会话里。

当前 plan 机制是：

1. `init` 时扫描 mission 中类似 `plan`、`priority`、`workflow`、`步骤`、`工作优先级` 这样的 section。
2. 如果能提取到列表项，就把它们种成初始 plan。
3. 如果提取不到，就生成一份默认 starter plan。
4. worker 每轮可以返回 `plan_updates`。
5. supervisor 把 plan 更新写入 `plan.json` 和 `notes/plan.md`。
6. `status --json` 会直接暴露当前 plan。

这样做的意义是：

- 计划不会随着对话结束而丢失
- 每轮推进都有明确的下一步
- 后续更容易支持 UI、可视化、人工接管

### 飞书同步设计

飞书在这个系统里承担的是“外部可见状态面板”的角色，而不只是最终报告位置。

当前同步方式是：

- mission 中若包含 `docx/doc/wiki` 链接，则自动识别为默认报告目标
- 对 wiki 链接会先解析到真实 `docx` token
- 每轮 worker 可以返回 `lark_update_markdown`
- supervisor 使用 `lark-cli docs +update --mode append` 追加更新
- 当任务停止时，会追加停止时间和原因

这部分后续可以继续增强，比如：

- 增加固定格式的 progress card
- 增加实验表格化记录
- 把 job 状态变化自动写到文档

### 模型与鲁棒性设计

当前 supervisor 里已经加入了基础的模型 fallback 设计：

- 首选 `gpt-5.4`
- fallback `gpt-5.3-codex-spark`

这样做的目的，是让 autoresearch 在模型限额或短时失败时不要直接停掉。

后续仍然可以继续增强：

- 更明确地区分 rate limit / parse failure / tool failure
- 发生模型切换时自动写飞书
- 对连续失败增加退避和恢复策略

### 当前设计的一个核心判断

当前整个方案里最关键的判断是：

> `autoresearch` 不应该建模成“一次很长的 agent 对话”，而应该建模成“可重复拉起的 worker + 持久 supervisor + 磁盘状态”。

这是它能真正支持长时间 job、恢复执行和外部汇报的根本原因。

### 当前限制

当前版本还是最小可用实现，已经能跑通主链路，但还不是终态。

主要限制包括：

- 还没有更强的 SLURM 辅助封装
- `sbatch` 输出到 `jobs/*.json` 的自动化还可以继续增强
- 还没有专门的 plugin 打包
- 还没有更完整的飞书实验表格和消息通知层
- 后台模式已经有，但还可以进一步补 `tmux` / `systemd` 包装

### 下一步建议

接下来比较自然的增强方向有：

1. 增加面向 SLURM 的 helper，把 job 提交、状态轮询、日志解析进一步标准化。
2. 把飞书更新从“自由 markdown 追加”增强成“结构化实验日志 + 状态摘要”。
3. 增加插件化包装，便于后续直接开源到 GitHub。
4. 提供几种标准 mission 模板，例如：
   - 实验对齐型
   - 文献调研型
   - 长周期 coding/benchmark 型

### 当前结论

当前 `autoresearch` 的最佳形态不是“让用户写更复杂的 prompt”，而是：

- 用户仍然只提供一个 mission markdown
- 系统自动生成 runtime
- worker 负责短执行块
- supervisor 负责长期恢复和调度
- plan、state、jobs、summary、Lark update 全部落盘并可持续迭代

这个方向更适合真正长期运行，也更适合后续整理成开源项目。
