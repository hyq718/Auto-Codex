## 2026-04-19 项目总览

### 项目定位

`Auto-Codex` 的目标是把“给 Codex 一份 mission markdown，然后让它长期自主推进任务”这件事，从单个 prompt 提升为一个可恢复、可追踪、可汇报的 runtime。

当前 GitHub 仓库地址：

- `https://github.com/hyq718/Auto-Codex`

这个项目当前聚焦的问题是：

1. 用户只提供一个 `autoresearch.md`。
2. 系统可以持续运行数小时到数天。
3. 中间即使有长时间 job、会话中断、模型限额，也不会直接丢状态。
4. 用户既可以完全不管，也可以中途继续加要求。
5. 飞书文档既是进度面板，也是输入面板的一部分。

### 当前它能做什么

当前版本已经具备下面这些核心能力：

#### 1. 从单个 mission markdown 生成 runtime

用户给一个 mission markdown 后，系统可以自动生成一套 runtime 目录，里面包含：

- `mission.md`
- `runbook.md`
- `state.json`
- `plan.json`
- `events.jsonl`
- `inputs.jsonl`
- `jobs/`
- `notes/latest_summary.md`
- `notes/plan.md`
- `logs/`

也就是说，用户表面上只维护一个 markdown，但系统内部已经自动拆成了可运行的状态结构。

#### 2. 以前台或后台方式运行 supervisor

当前 CLI 支持：

- `start`
- `daemon-start`
- `status`
- `daemon-status`
- `stop`
- `daemon-stop`

其中 `daemon-start` 可以让 supervisor 在后台运行，避免占着前台终端。

#### 3. 用短 burst 的 `codex exec` 驱动工作

系统不是要求一个 agent session 永远活着，而是：

- supervisor 周期性拉起 worker
- worker 做一小段工作
- 返回结构化 JSON
- supervisor 写回状态并决定下次何时再拉起

这是项目最核心的架构设计。

#### 4. 持久化 plan

系统会从 mission 里自动提取计划 section，或者自动生成 starter plan。

plan 会持续保存在：

- `plan.json`
- `notes/plan.md`

worker 也可以返回 `plan_updates`，由 supervisor 合并。

#### 5. 用户输入持久化

用户的补充要求不再只能存在对话里，而可以进入 runtime 的输入层。

当前支持：

- `add-input`
- `list-inputs`
- `ack-input`

每条输入都会记录：

- `id`
- `source`
- `author`
- `title`
- `content`
- `status`
- `created_at`
- `acknowledged_at`
- `resolution`

#### 6. supervisor 自动把待处理输入注入 worker

当前 pending inputs 会在每轮 worker prompt 中以两种形式出现：

- 一个 `pending_inputs.md` 文件
- 一个简短 summary

worker 消费后，可以返回 `acknowledged_input_ids`，由 supervisor 自动回执。

#### 7. 飞书文档写入

如果 mission 中包含 `docx/doc/wiki` 链接，或者初始化时显式传了 `--doc-url`，runtime 会把它当作默认汇报目标。

当前支持的写入类型有：

- worker 返回的增量进展
- supervisor 周期性 heartbeat
- 停止时的 stop 信息
- 完成时的 final summary

系统写入的区块统一使用：

- `Autoresearch System: ...`

这个前缀，便于后续识别和过滤。

#### 8. 飞书文档回读，并转化为输入

当前 supervisor 支持定期回读飞书文档，把用户新增的“用户可见内容”转成 `source=feishu` 的输入记录。

这意味着：

- 输入源不再只限于 Codex 对话
- 用户也可以直接在飞书文档里补要求
- 系统后续 tick 时可以把这些新增要求注入 worker

#### 9. 基础 SLURM helper

当前已经有一个最小可用的 SLURM 标准层：

- `submit-job`
- `sync-jobs`
- `list-jobs`

它能闭合第一条核心链路：

- 提交 `sbatch`
- 解析 job id
- 写入 runtime state
- 后续通过 `squeue` 刷新状态

### 项目结构

当前项目主要结构如下：

- `scripts/autoresearch.py`
  这是主入口，负责 runtime 初始化、supervisor、输入层、Lark、SLURM helper、daemon 管理。

- `templates/worker_prompt.md.tmpl`
  负责生成每轮 `codex exec` 的 worker prompt。

- `schemas/agent_response.schema.json`
  规定 worker 必须返回的结构化 JSON 形状。

- `skills/autoresearch/SKILL.md`
  这是 Codex skill 的说明和使用入口。

- `README.md`
  对外说明整个项目如何使用、能做什么、当前限制是什么。

- `examples/minimal-autoresearch.md`
  最小 mission 示例。

- `references/`
  当前主要放设计文档和本次项目说明。

### 核心设计手段

下面按功能说一下每个能力是通过什么方式实现的。

#### A. 为什么能长时间工作

不是靠一个长对话，而是靠：

- `daemon-start` 启动长期存在的 supervisor 进程
- `perform_tick()` 周期性执行一轮 worker
- 每轮 worker 都是独立的 `codex exec`

所以真正“持续运行”的是 supervisor，不是单个 chat session。

#### B. 为什么中断后还能接着做

因为核心状态都落在磁盘，而不是只留在上下文：

- `state.json`
- `plan.json`
- `inputs.jsonl`
- `jobs/*.json`
- `events.jsonl`
- `notes/latest_summary.md`

只要这些状态文件还在，后续就可以恢复推进。

#### C. 为什么用户后续输入不会丢

因为新增要求不会只留在对话里，而是进入 runtime 的 input queue。

当前输入来源已经分成两类：

- `manual`
- `feishu`

后续还可以继续增加更多 source。

#### D. 为什么飞书不会自己写的内容又被自己当成用户输入

因为系统在写飞书时，专门使用了：

- `Autoresearch System: ...`

作为系统区块前缀。

回读飞书时，会先剥离这些 system sections，再对用户可见内容做 diff。

#### E. 为什么 plan 不会只停留在会话里

因为 plan 现在是 runtime 的一等公民。

实现方式是：

1. 初始化时从 mission 中抽取 plan 或自动生成。
2. plan 写入 `plan.json`。
3. render 成 `notes/plan.md`。
4. worker 返回 `plan_updates` 时，supervisor 合并回状态。

#### F. 为什么基础 job tracking 已经闭环

因为项目现在已经显式把 job 作为 runtime 实体对待：

- `submit-job` 负责产生 job id
- `jobs/*.json` 存储 job metadata
- `state.json` 记录 job 列表
- `sync-jobs` 根据 `squeue` 刷新状态

虽然这还不是完整 scheduler integration，但主链路已经具备。

### 当前验证情况

本次实现后，我已经做过一轮本地验证。

验证方式不是直接用真实飞书和真实集群硬跑全部流程，而是用了 mock 环境做功能验证，避免污染线上状态。

验证过的能力包括：

1. `add-input -> list-inputs -> ack-input`
2. pending input 注入 worker prompt
3. 飞书首次 poll 只建快照，不误生输入
4. 飞书后续变化会变成 `source=feishu` 输入
5. heartbeat 写入
6. final summary 写入
7. `submit-job -> sync-jobs -> list-jobs`
8. `python3 -m py_compile scripts/autoresearch.py`

### 当前限制

虽然主链路已经通了，但当前版本仍然有明显限制：

1. GitHub 远端仓库还没有创建，因此项目当前只完成了本地 git 初始化和本地 commit。
2. 飞书输入检测目前是“文档 diff + 忽略系统区块”的 best-effort 方案，还不是精细的块级追踪。
3. SLURM 现在只有基础 helper，还没有更强的 `sacct`、日志解析、失败分类、自动重试策略。
4. 还没有插件化打包，当前还是 `skill + scripts` 形式。
5. 还没有真正跑一个长期真实任务来验证数天级稳定性。

### 当前 Git 状态

当前本地仓库已经初始化并提交，commit 为：

- `dafe633`

提交信息：

- `Initial autoresearch runtime`

这意味着项目已经处于“本地可直接推送”的状态。

当前还差的唯一 GitHub 步骤是：

- 创建远端仓库
- 添加 remote
- push 到 GitHub

### 结论

当前这个项目已经不再是“一个 prompt 文件”，而是一个真正具备 runtime 结构的 autoresearch 原型系统。

它现在已经能解决下面这些关键问题：

- 单个 mission markdown 入口
- 磁盘状态持久化
- plan 持久化
- 用户输入持久化
- 飞书写入
- 飞书回读为输入
- 后台 supervisor
- 基础 job tracking

也就是说，项目的主骨架已经成型。

接下来如果继续推进，最自然的方向会是：

1. 创建并推送 GitHub 远端仓库
2. 增强 SLURM 集成
3. 增强飞书报告格式
4. 做 plugin / package 化
5. 用真实长周期任务做端到端验证
