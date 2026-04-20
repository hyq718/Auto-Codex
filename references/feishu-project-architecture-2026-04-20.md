## 2026-04-20 项目整体架构说明（详细版）

### 一、项目现在到底是什么

`Auto-Codex` 现在不是单个 prompt，也不是单个 skill，而是一套围绕“长期 autoresearch”设计的混合系统。

它的核心目标是：

1. 用户只提供一个 mission markdown，例如 `autoresearch.md`。
2. Codex 可以把这个 mission 变成可持续推进的 runtime。
3. 运行过程中既支持后台长时间推进，也支持前台对话式介入。
4. 状态不会只存在聊天上下文里，而会持久化到磁盘。
5. 飞书既可以作为进度面板，也可以作为补充输入面板。
6. 这个项目最终可以以开源仓库的形式被别人 clone、安装、使用。

当前 GitHub 仓库：

- `https://github.com/hyq718/Auto-Codex`

### 二、整体设计思想

这个项目当前采用的是“前台 mode + 后台 runtime + 短 burst worker”的三层结构。

#### 1. 前台层：Auto-Codex Mode

这一层的目标是提供接近 `/autoresearch` 的交互体验。

职责：

- 把当前 Codex 会话变成一个 autoresearch 控制台
- 把用户的自然语言要求转成结构化 runtime 操作
- 把 runtime 当前状态重新格式化成可读的对话输出
- 提供 `mode-start / mode-status / mode-sync / mode-update / mode-plan / mode-jobs / mode-pause / mode-resume / mode-stop`

这一层负责“用户体验”。

#### 2. 中间层：Auto-Codex Runtime

这一层是整个系统的执行内核。

职责：

- 从单个 `autoresearch.md` 初始化 runtime
- 管理 supervisor 循环
- 管理状态文件、计划、输入、作业、日志、摘要
- 与飞书同步
- 在长时间等待后恢复执行
- 协调 worker 每一轮的工作

这一层负责“持久运行”和“恢复能力”。

#### 3. 底层：Auto-Codex Worker

这一层是每次真正干活的短 burst。

职责：

- 读取当前 mission、state、plan、inputs、jobs、notes
- 判断下一步该做什么
- 执行一个有限工作单元
- 返回结构化 JSON 给 runtime

这一层负责“每轮推进”。

### 三、为什么必须用三层，而不是单个 prompt

如果只有一个 prompt：

- 可以交互，但不可靠
- 长时间 job 等待后无法自然恢复
- 会话退出后状态容易丢
- 难以稳定支持飞书、作业轮询、计划更新

如果只有后台脚本：

- 可以长期运行
- 但用户在 Codex 对话里没有“进入 mode”的感觉
- 中途插话、纠偏、追问的体验不好

所以当前项目采用的是混合模型：

- 用户感知上：更像在用一个长期研究模式
- 系统实现上：靠 runtime 和 supervisor 保障可靠性

### 四、用户入口现在有哪些

当前项目已经有三类入口。

#### A. 低层 CLI 入口

主入口文件：

- `scripts/autoresearch.py`

它提供：

- `init`
- `start`
- `status`
- `stop`
- `add-input`
- `list-inputs`
- `ack-input`
- `submit-job`
- `sync-jobs`
- `list-jobs`
- `daemon-start`
- `daemon-status`
- `daemon-stop`

这条入口偏底层，适合直接操作 runtime。

#### B. 对话模式入口

这是当前最接近 `/autoresearch` 的一层。

它提供：

- `mode-start`
- `mode-status`
- `mode-sync`
- `mode-update`
- `mode-plan`
- `mode-jobs`
- `mode-pause`
- `mode-resume`
- `mode-stop`

这条入口偏用户可见、偏会话交互。

#### C. Skill / Plugin 入口

当前项目已经同时提供：

- installable skill：`skills/auto-codex`
- repo-local plugin：`plugins/auto-codex`

其中：

- 在 Codex CLI 里，当前更实际的入口是 `$auto-codex`
- 在支持 plugin 的界面里，可以通过 plugin 被发现

### 五、runtime 是怎么组织的

运行时的关键不是对话历史，而是 runtime 目录。

当前 runtime layout 主要包含：

- `mission.md`
- `runbook.md`
- `state.json`
- `plan.json`
- `events.jsonl`
- `inputs.jsonl`
- `jobs/`
- `notes/latest_summary.md`
- `notes/plan.md`
- `notes/pending_inputs.md`
- `logs/supervisor.log`
- `logs/codex/`
- `outbox/`
- `snapshots/`
- `supervisor.pid`

这些文件共同构成真正的“持久记忆”。

### 六、状态是如何流动的

#### 1. mission -> runtime

用户只提供一个 `autoresearch.md`。

`init` 或 `mode-start` 会把它编译成 runtime：

- 复制 mission 内容
- 提取标题
- 提取飞书链接
- 自动生成 starter plan 或读取 mission 中已有计划
- 初始化 state、notes、runbook、prompt

#### 2. runtime -> worker

每轮 worker 都会读取：

- `mission.md`
- `state.json`
- `plan.json`
- `jobs/`
- `notes/latest_summary.md`
- `notes/pending_inputs.md`
- `outbox/`

然后生成一个短 burst 的行动结果。

#### 3. worker -> runtime

worker 返回结构化 JSON，runtime 会把它写回：

- lifecycle status
- summary
- next_sleep_seconds
- plan_updates
- jobs_submitted / job metadata
- acknowledged_input_ids
- lark_update_markdown
- final_summary_markdown

#### 4. runtime -> mode

mode 层从 runtime 里再渲染出用户可读结构。

当前 `mode-status` / `mode-sync` 的主要输出结构是：

1. `Goal`
2. `Current Plan`
3. `Latest Progress`
4. `Waiting / Blockers`
5. `Active Jobs`
6. `Pending Inputs`
7. `Recent Runtime Events`
8. `Next Action`

### 七、当前输入系统是怎么设计的

项目现在已经不再把“用户补充要求”只留在聊天里。

输入层使用：

- `inputs.jsonl`

每条输入都至少包括：

- `id`
- `source`
- `author`
- `title`
- `content`
- `status`
- `created_at`
- `acknowledged_at`
- `resolution`

当前输入源主要有三种：

1. `manual`
2. `chat`
3. `feishu`

其中最关键的是：

- `mode-update` 会把对话要求写成 `source=chat`
- 飞书回读会把文档新增内容写成 `source=feishu`
- worker 可以通过 `acknowledged_input_ids` 消费并回执

所以项目现在已经具备“前台聊天输入”和“飞书异步输入”汇入同一输入层的能力。

### 八、计划系统怎么工作

项目现在有持久化 plan，不再只是 prompt 中临时想一遍。

计划层使用：

- `plan.json`
- `notes/plan.md`

其工作方式是：

1. runtime 初始化时从 mission 中抽取计划 section
2. 如果 mission 没写明确计划，就自动生成 starter plan
3. worker 每轮可以返回 `plan_updates`
4. supervisor 合并 plan 并重写到磁盘
5. `status --json`、`mode-status`、`mode-plan` 都可以查看当前计划

所以 plan 现在已经从“提示词里的思考片段”变成了机器可读状态。

### 九、为什么它可以长时间运行

真正持续活着的不是单个对话，而是 supervisor。

当前实现里有两种运行方式：

- 前台 `start`
- 后台 `daemon-start`

后台模式下：

- runtime 会写 `supervisor.pid`
- stdout/stderr 进入 `logs/supervisor.log`
- supervisor 周期性唤醒 worker
- worker 完成一轮后退出
- supervisor 决定下次 sleep 多久

也就是说，持久运行依赖的是：

- supervisor 进程
- 持久化状态文件
- 每轮短 burst 的 worker

而不是“一个永不退出的聊天”。

### 十、SLURM / 长 job 是怎么接进来的

当前项目已经有一层最小可用的 SLURM helper：

- `submit-job`
- `sync-jobs`
- `list-jobs`

这层做的事包括：

- 调用 `sbatch`
- 解析 job id
- 写入 `jobs/<job_id>.json`
- 在 state 中登记 job
- 后续通过 `squeue` 刷新 job 状态

这条链路解决的是：

- “提交 job 之后怎么记住它”
- “过几个小时后怎么回来继续”

它当前还只是 baseline，不是完整调度系统，但已经把最核心闭环接通。

### 十一、飞书在整个项目里扮演什么角色

飞书不是附属输出，而是系统的重要组成部分。

当前飞书承担三种角色：

#### 1. 进度面板

runtime 会把重要进展追加写到文档中，包括：

- worker 的进展摘要
- supervisor heartbeat
- stop note
- final summary

#### 2. 异步输入面板

runtime 会定期回读飞书文档，把新增的用户可见内容转成输入。

所以用户离开 Codex 后，仍然可以在飞书里给系统补要求。

#### 3. 远程验收面板

用户几小时或几天后回来时，不一定要先进本地 runtime；也可以先在飞书里查看：

- 最近做了什么
- 卡在哪
- 下一步是什么

### 十二、skill 和 plugin 现在分别解决什么问题

#### 1. `skills/auto-codex`

这是当前对 Codex CLI 最重要的一层。

它的作用是：

- 把 Auto-Codex 这套流程封装成一个可显式调用的 skill
- 用户可以在 CLI 里用 `$auto-codex ...`
- skill 内部会提示 agent 采用 mode-oriented 的工作方式

它解决的是“在 CLI 里怎么进入这套模式”。

#### 2. `plugins/auto-codex`

这是当前对支持 plugin 的前端更友好的层。

它提供：

- `.codex-plugin/plugin.json`
- plugin-local skill
- marketplace entry

它解决的是“图形界面或 plugin host 如何发现 Auto-Codex”。

#### 3. 为什么两层都要保留

因为当前 Codex 的不同前端并不统一：

- 有的地方更像 skill discovery
- 有的地方更像 plugin discovery

所以项目当前的策略是双轨兼容：

- skill 解决 CLI 和 skill-triggered 场景
- plugin 解决面板和 marketplace 场景

### 十三、为什么又做了 install.sh

这一层是为了开源可用性。

在没有安装器之前：

- 仓库里的 skill/plugin 是存在的
- 但别人 clone 后还需要自己猜应该挂到哪个目录

当前新增的安装器包括：

- `install.sh`
- `scripts/install.py`

默认行为：

- 安装 `auto-codex` 到 `~/.agents/skills/auto-codex`
- 安装 `auto-codex` 到 `~/.codex/skills/auto-codex`
- 安装 plugin 到 `~/plugins/auto-codex`
- 更新 `~/.agents/plugins/marketplace.json`

这样别人现在可以更接近：

```bash
git clone https://github.com/hyq718/Auto-Codex.git
cd Auto-Codex
./install.sh
```

然后重启 Codex，再在 CLI 中使用：

- `$auto-codex ...`

### 十四、当前项目已经做到什么程度

从架构上看，当前项目已经完成了从“单个 prompt”到“可恢复 autoresearch 系统”的跃迁。

已经具备：

- 单 mission markdown -> runtime 的自动编译
- runtime 持久化
- supervisor + short burst worker
- 计划持久化
- 输入层持久化
- 飞书写入
- 飞书回读
- baseline SLURM helper
- 对话模式渲染
- installable skill
- repo-local plugin
- 一键安装脚本

### 十五、当前还没有完全做完的点

下面这些是当前仍然存在的边界：

1. 现在更像“通过 skill 进入 mode”，还不是 Codex 内建的原生 `/autoresearch` slash command。
2. plugin 是否在所有 Codex 前端自动发现，仍然依赖具体前端实现。
3. SLURM 层目前还是 baseline，没有完整的失败分类、日志分析、自动重试策略。
4. 飞书输入检测仍然是基于文档 diff 的 best-effort，而不是更强的块级或评论级语义同步。
5. 现在已经能把 chat / feishu / runtime 接起来，但“每条普通对话都自动投影到 runtime”还没有彻底原生化。

### 十六、现在这套系统最准确的定义

如果要给当前项目下一个比较准确的定义，我会这样描述：

`Auto-Codex` 当前是一个围绕 Codex 构建的 autoresearch runtime framework。

它包含：

- 一个持久化 runtime
- 一个对话模式适配层
- 一个可安装 skill
- 一个 repo-local plugin
- 一个面向开源使用的一键安装层

它已经不再是“写一个大 prompt”，而是一个可以逐步走向产品化的系统骨架。
