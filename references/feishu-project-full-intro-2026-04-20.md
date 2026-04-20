## 2026-04-20 项目介绍（完整版）

### 一、这个项目现在到底是什么

`Auto-Codex` 现在的目标不是“写一个大 prompt，让 Codex 一次性做完所有事”，而是做一个可以长期自动工作的 `autoresearch runtime`。

更准确地说，它现在是一套围绕 Codex 构建的“可计划、可恢复、可续跑”的研究执行系统。

它解决的问题是：

- 用户只给一个 mission markdown，例如 `autoresearch.md`
- 系统自动把它编译成一个可持续推进的 runtime
- 即使中间要等待几个小时的 job，也不是靠聊天记忆硬撑
- 下一轮自动 worker 醒来时，能低 token 地继续工作
- 用户也可以在当前对话里随时介入、修改方向、查看状态

所以这个项目的重点不是“聊天记忆”，而是：

- 持久化状态
- 自动续跑
- 低 token 恢复
- 人机协作时的计划与纠偏

### 二、整体结构是什么

当前系统可以理解成四层。

#### 1. Mission 层

用户入口仍然是一个 mission markdown。

它负责表达：

- 总目标
- 约束
- 任务背景
- 输出要求
- 飞书链接
- 如果有的话，已有的高层计划

#### 2. Mode 层

这是用户在 Codex 对话里看到的那一层，负责提供接近 `/autoresearch` 的体验。

它做的事包括：

- 从 mission 创建 runtime
- 输出 plan preview
- 等用户确认 plan
- 展示 runtime 状态
- 接收用户中途追加的新输入

当前核心 mode 命令包括：

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

- 管理 `state.json`
- 管理 `plan.preview.json` 和 `plan.json`
- 管理 `inputs.jsonl`
- 管理 `jobs/*.json`
- 管理 `events.jsonl`
- 管理 `notes/`
- 定时唤醒 worker
- 在等待 job 时 sleep，并在之后自动继续
- 与飞书同步

#### 4. Worker 层

这是每轮真正干活的短 burst。

它不是长期记忆容器，长期记忆已经落盘在 runtime 中。

它负责：

- 读取最小必要上下文
- 做一个最值得做的工作单元
- 返回结构化 JSON
- 更新当前计划、阶段状态、下一步动作和 sleep 估计

### 三、现在用户是怎么使用它的

当前用户使用路径已经比较清楚：

1. 用户提供一个 mission markdown
2. `mode-start` 生成 runtime 并展示 plan preview
3. 用户确认 plan 后，系统才正式执行
4. 执行过程中，用户可以通过 `mode-status` / `mode-sync` 查看状态
5. 用户可以通过 `mode-update` 注入新方向
6. 后台 supervisor 可以持续推进长任务

这里特别重要的一点是：

- `mode-start` 默认不会直接开跑
- 它会先进入 `awaiting_plan_confirmation`
- 只有 `mode-approve-plan` 之后，才进入正式执行

这是为了避免系统一开始就沿着错误计划长时间推进。

### 四、runtime 默认创建在哪里

当前这条规则已经明确：

- 如果用户没有显式指定 `runtime_dir`
- 默认 runtime 会创建在“当前工作目录”下的 `./auto-codex`

例如：

- 用户在 `/home/yqhao/LLaMA-Factory` 中启动 autoresearch
- 默认 runtime 会落在 `/home/yqhao/LLaMA-Factory/auto-codex`

而不是落在 `Auto-Codex` 工具仓库目录下。

这样设计的原因是：

- runtime 应该跟随“被研究的工程项目”
- 而不是跟随“工具本身”

### 五、runtime 里现在有哪些关键文件

当前最关键的运行时文件包括：

- `mission.md`
- `state.json`
- `plan.preview.json`
- `plan.json`
- `events.jsonl`
- `inputs.jsonl`
- `jobs/<job_id>.json`
- `notes/plan_preview.md`
- `notes/plan.md`
- `notes/jobs_focus.md`
- `notes/latest_summary.md`
- `notes/resume_status.md`
- `logs/supervisor.log`
- `logs/codex/`

它们分别承担不同角色：

- `mission.md`：原始任务目标
- `state.json`：主状态
- `plan.preview.json`：待确认计划
- `plan.json`：正式 current plan
- `jobs/*.json`：单个 job 的持久化元数据
- `jobs_focus.md`：当前 tick 应优先看的 job 摘要
- `latest_summary.md`：上一轮工作的浓缩摘要
- `resume_status.md`：下一轮自动 worker 最重要的恢复包

### 六、Plan 是怎么工作的

当前系统已经不再是“mission 一进来就直接执行”，而是先有一个 plan handshake。

#### 1. Plan Preview

系统会先从 mission 中抽取已有计划，或者自动生成 starter plan。

这个 preview 会写到：

- `plan.preview.json`
- `notes/plan_preview.md`

此时状态是：

- `awaiting_plan_confirmation`

#### 2. Current Plan

只有用户批准后，preview 才会变成正式 current plan。

这部分会写到：

- `plan.json`
- `notes/plan.md`

#### 3. Plan Updates

执行过程中，worker 每一轮都可以返回：

- `plan_updates`

runtime 会把它 merge 回 current plan。

所以现在的计划系统已经分成了两层：

- preview plan：执行前确认
- current plan：执行中持续更新

### 七、现在系统如何控制 token 使用

这件事现在已经是显式设计，而不是“让模型自己注意一点”。

当前原则是：

- 先读最小必要上下文
- 先看摘要
- 先做检索
- 只有必要时再扩大读取范围

#### 1. Jobs

worker 不再默认扫描整个 `jobs/` 目录。

每轮会先生成：

- `notes/jobs_focus.md`

它只包含一小组当前最相关的 jobs。

worker 现在默认应当：

- 先读 `jobs_focus.md`
- 再只打开其中最相关 job 的 json 或日志
- 只有确实必要时才扩大到其他 job

#### 2. Resume Status

worker 不应该每次醒来都重新大范围阅读所有文件。

所以当前系统会把恢复所需的最小行动包写到：

- `notes/resume_status.md`

这样下一轮 worker 不需要重新“思考我该干什么”，而是直接执行上一次已经决定好的下一步。

### 八、当前最重要的新设计：Recovery-Oriented Status

这一层是现在整个项目最关键的设计之一。

它不是为了帮助用户“回忆”，而是为了让“未来的自动 worker”低 token 地继续执行。

当前系统现在明确区分三层：

- `plan`：整个任务的大方向
- `phase_plan`：当前阶段的小计划
- `next_action`：恢复后第一步到底做什么

这些内容现在会落到：

- `state.json` 的 `execution`
- `notes/resume_status.md`

其中 `execution` 现在包括：

- `current_phase`
- `phase_plan`
- `next_action`

`next_action` 会尽量表达清楚：

- 下一步做什么
- 为什么是这一步
- 去哪里看
- 搜什么关键词
- 没找到时按什么 read ladder 扩展
- 什么算成功
- 找不到时 fallback 去哪里

这意味着系统现在的核心目标已经很明确：

- 不是“用户回来后能恢复记忆”
- 而是“几个小时后下一轮自动 worker 自己醒来能继续干”

### 九、等待 job 时系统现在如何 sleep

当前 sleep 策略采用“两手准备”。

#### 1. 保底策略

默认 sleep：

- `1h`

系统不会无限久睡下去。

这样即使 worker 没有给出可靠估计，也会在 1 小时内重新检查一次。

#### 2. 更优策略

如果 worker 或 runtime 能从日志里推断出更早的有用检查点，那么可以给出更短 sleep。

并且现在系统不只是靠 worker 自己自由发挥，而是已经补了一层本地 helper。

也就是说：

- worker 可以返回 `next_sleep_seconds`
- worker 可以解释 `sleep_reason`
- 如果 worker 没估好，而当前是 `waiting_job`
- runtime 还会尝试自己从 focused job 的日志里估一次

#### 3. 当前 helper 已支持哪些格式

当前这层估时 helper 已经支持多类常见训练日志模式：

- `5s/it`
- `it/s`
- `iteration time: 4.0 s`
- `global_step=9500`
- `iteration 8200`
- `10k`
- `10000`
- `eval every 1000 steps`

它会尽量推断：

- 当前 step
- 目标 step 或下一个 eval interval
- 每步大致耗时
- 由此估出下一次最值得回看的时间

但最终 sleep 仍然有一个统一 cap：

- 最长 `1h`

所以现在的 sleep 逻辑可以概括为：

- 优先用估算值
- 估算失败则回退到 1 小时
- 永远不超过 1 小时

### 十、这套 sleep 设计为什么重要

因为它直接影响两件事：

1. Autowork 能不能真的自动续跑
2. Token 会不会被浪费在无意义的重复检查上

如果 sleep 太短：

- 会频繁拉起 worker
- 浪费 token

如果 sleep 太长：

- 重要结果已经出来了，系统却迟迟不看

所以现在这层设计的目标是：

- 用最保守的 1 小时作为安全上限
- 用日志估算把检查点尽量推近到真正有信息增量的时刻

### 十一、飞书在系统里扮演什么角色

飞书现在不是“附属输出”，而是系统的一部分。

当前飞书承担三种角色：

#### 1. 进度面板

系统可以把：

- heartbeat
- worker 进展
- stop note
- final summary

追加到飞书文档。

#### 2. 异步输入面板

runtime 会定期回读飞书文档，把新增的用户可见内容转成输入。

也就是说，用户离开 Codex 后，也可以在飞书里补要求。

#### 3. 远程验收面板

几小时后，用户不一定先回本地终端，也可以先在飞书里看：

- 当前做到哪
- 卡在哪
- 下一步是什么

### 十二、skill 和 plugin 现在分别解决什么问题

当前项目已经同时提供：

- installable skill：`skills/auto-codex`
- repo-local plugin：`plugins/auto-codex`

它们分别解决：

#### 1. skill

主要用于：

- Codex CLI
- `$auto-codex ...`

它解决的是“在对话或 CLI 中怎么进入这套模式”。

#### 2. plugin

主要用于：

- 支持 plugin / marketplace 发现的前端

它解决的是“UI 里怎么发现这套能力”。

#### 3. install.sh

这层是为了开源后的可用性。

它会自动把：

- skill
- plugin
- marketplace entry

挂到合适的位置，让别人 clone 后更接近一条命令安装。

### 十三、当前项目还没有做完什么

当前系统已经有了比较清晰的主骨架，但还有边界。

主要包括：

1. 现在更像“通过 skill 进入 autoresearch mode”，还不是 Codex 内建原生的 `/autoresearch` slash command。
2. plugin 是否在所有 Codex 前端里自动发现，仍然取决于具体前端实现。
3. SLURM 层还是 baseline helper，还不是完整调度系统。
4. 当前依然是单 worker 主架构，没有正式做多 agent 协调层。
5. sleep helper 现在已经能处理常见日志格式，但还不是全覆盖所有训练框架日志风格。

### 十四、现在对这个项目最准确的定义

如果要给当前版本一个最准确的定义，我会这样说：

`Auto-Codex` 当前是一个围绕 Codex 构建的 autoresearch runtime framework。  
它的重点不是“长聊天”，而是“低 token、可恢复、可续跑的自动工作系统”。

它现在已经包含：

- mission 单入口
- plan preview 与 plan handshake
- current plan 持续更新
- token-aware jobs 读取
- recovery-oriented status
- next action packet
- 1 小时上限的 sleep 策略
- 日志估时 helper
- 飞书同步与输入回读
- 可安装 skill
- repo-local plugin
- 一键安装脚本

如果后续继续往前推进，它会越来越像一个真正意义上的“长期研究执行层”，而不是单次 prompt 技巧。 
