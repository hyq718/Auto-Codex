## 2026-04-20 对话模式版 Auto-Codex 设计

### 为什么要调整方向

当前仓库里的实现更偏向“后台自治 runtime”：

- 用 `supervisor + codex exec + state files` 保证长期运行
- 适合长时间 job、状态恢复、飞书同步

这个方向解决了“能不能长期跑”的问题，但没有完全满足另一个更重要的体验目标：

- 用户希望在 **Codex 当前对话界面** 里直接进入 autoresearch
- 用户希望 **一边看 Codex 的研究过程，一边随时插话**
- 用户希望既有 **对话交互感**，又有 **后台持续推进能力**

所以需要把当前项目重新拆成两层：

1. **前台对话模式**
   用户看到的就是一个 `/autoresearch` 式的模式入口。

2. **后台 runtime**
   负责状态持久化、job 跟踪、飞书同步、恢复执行。

也就是说：

- 当前仓库不需要推翻
- 但它不应该再被当作完整产品形态
- 它更适合做 `Auto-Codex` 的执行内核

### 目标体验

用户侧的理想体验应该是这样：

1. 用户在当前 Codex 会话里输入类似 `/autoresearch start`。
2. 当前会话进入 `Auto-Codex mode`。
3. Codex 在对话里持续汇报：
   - 当前目标
   - 当前计划
   - 最新进展
   - 正在等待什么
   - 下一步准备做什么
4. 用户可以随时插话：
   - 改目标
   - 加建议
   - 纠偏
   - 要求解释
5. 如果用户离开，后台 runtime 继续推进。
6. 用户回来时，当前会话可以通过 `/autoresearch sync` 或 `/autoresearch status` 直接恢复上下文。

换句话说，用户应该感觉自己在使用：

- 一个“会持续工作、可随时打断、会自己恢复”的研究模式

而不是一个“后台 Python 脚本”。

### 整体架构

建议最终架构分成三层：

#### 1. Auto-Codex Mode

这是用户直接接触的层，存在于当前 Codex 对话里。

职责：

- 解析 `/autoresearch` 类命令
- 把用户文本转成 runtime 输入
- 把 runtime 状态渲染回对话
- 让当前会话表现得像一个长期研究控制台

它负责“体验”。

#### 2. Auto-Codex Runtime

这是当前仓库已经基本实现的部分。

职责：

- mission 初始化
- supervisor loop
- state / plan / inputs / jobs 持久化
- 飞书 heartbeat / summary / polling
- SLURM 基础辅助
- 后台恢复执行

它负责“持久运行”。

#### 3. Auto-Codex Worker

这是每轮短 burst 的 `codex exec` 执行单元。

职责：

- 读取当前 mission 和状态
- 决定下一步最高优先级动作
- 执行一段有限工作
- 返回结构化 JSON

它负责“具体干活”。

### 三层之间的关系

关系应该是：

```text
User Chat
   ↓
Auto-Codex Mode
   ↓
Auto-Codex Runtime
   ↓
Auto-Codex Worker
```

具体来说：

- 用户不直接操作 runtime 文件
- mode 层把用户输入转交给 runtime
- runtime 再把需要执行的任务交给 worker
- worker 的结果回到 runtime
- runtime 再由 mode 层格式化展示给当前会话

### 为什么必须是“前台 mode + 后台 runtime”的混合形态

因为单做其中一边都不够。

#### 如果只有前台对话

优点：

- 交互自然
- 用户可以随时打断
- 体验更像真正的 `autoresearch mode`

问题：

- 会话中断后不可靠
- 无法自然承载数小时到数天的长期任务
- 难以对接 job、等待、恢复、飞书同步

#### 如果只有后台 runtime

优点：

- 持久化强
- 长时间任务更稳
- 适合无人值守

问题：

- 用户感知弱
- 看不到过程
- 中途交互体验差

所以合理方案不是二选一，而是：

- **mode 做主入口**
- **runtime 做底层执行内核**

### 对话模式的命令设计

建议先定义一套清晰的模式命令。即使 Codex UI 还不支持真正 slash command，也可以先用同名文本协议实现。

建议命令包括：

#### `/autoresearch start <mission>`

作用：

- 进入 autoresearch 模式
- 读取或创建 runtime
- 启动前台/后台协同

#### `/autoresearch status`

作用：

- 返回当前整体状态
- 包括 lifecycle、plan、jobs、最新摘要、待处理输入

#### `/autoresearch sync`

作用：

- 把用户离开期间的后台进展同步回当前对话
- 这是“回来后快速恢复上下文”的关键命令

#### `/autoresearch update <message>`

作用：

- 把新的用户要求写入 runtime input queue

#### `/autoresearch plan`

作用：

- 单独查看当前 plan

#### `/autoresearch jobs`

作用：

- 单独查看当前 job 状态

#### `/autoresearch pause`

作用：

- 暂停后台推进

#### `/autoresearch resume`

作用：

- 恢复后台推进

#### `/autoresearch stop`

作用：

- 停止整个 autoresearch
- 写 stop note 到飞书

#### `/autoresearch logs`

作用：

- 展示最近几轮关键执行摘要

### 对话模式中的固定输出结构

为了让用户体验稳定、清晰，建议 mode 层每次都尽量按同一个格式回复，而不是自由聊天。

建议固定 6 个区块：

1. `Goal`
2. `Current Plan`
3. `Latest Progress`
4. `Waiting / Blockers`
5. `Active Jobs`
6. `Next Action`

这样用户每次一看就知道：

- 当前目标是什么
- 现在做到哪了
- 卡在哪
- 接下来要做什么

### 建议的状态机

建议让 `Auto-Codex mode` 和 runtime 共享同一套核心状态：

- `idle`
- `starting`
- `running`
- `waiting_job`
- `paused`
- `blocked`
- `completed`
- `stopped`

切换原则：

- `start` -> `starting` -> `running`
- 有长时间 job 在关键路径 -> `waiting_job`
- 用户主动暂停 -> `paused`
- 用户恢复 -> `running`
- 缺权限或缺关键人工决策 -> `blocked`
- 任务完成 -> `completed`
- 用户停止 -> `stopped`

这套状态机要尽量简单，避免 mode 和 runtime 各自有一套不一致的状态定义。

### 对话输入如何进入 runtime

这是整个系统最重要的桥之一。

用户在当前会话里讲的话，不应该只留在对话历史里，而应该同步进 runtime。

建议机制：

- 当前会话里用户输入 `/autoresearch update ...`
- 或者 mode 层识别“这是一条新的研究要求/建议”
- 自动写入 runtime `inputs.jsonl`

建议统一记录为：

- `source=chat`
- `author=user`
- `content=<用户消息>`
- `status=pending`

这样后续 worker 就可以像处理飞书输入一样处理对话输入。

### runtime 状态如何投影回对话

这是另一座桥。

用户说 `/autoresearch status` 或 `/autoresearch sync` 时，mode 层应该读取 runtime 的关键状态文件：

- `state.json`
- `plan.json`
- `inputs.jsonl`
- `jobs/`
- `notes/latest_summary.md`

然后把这些内容重新渲染成模式内固定结构。

这样就能做到：

- 后台和前台是同一个系统
- 对话只是 runtime 的一个视图层

### 飞书在这个模式里的角色

飞书不应该替代当前对话，而应该是另一个外部入口和外部观察面板。

建议角色分工如下：

#### 当前对话

负责：

- 实时交互
- 用户即时纠偏
- 状态查询
- 解释和讨论

#### 飞书文档

负责：

- 异步查看进展
- 用户离开电脑时的远程留言
- 周期性 heartbeat
- 最终总结与验收摘要

所以输入源应该统一抽象为：

- `chat`
- `feishu`

worker 不需要关心输入来自哪里，只需要看 input queue 即可。

### 用户回来后的恢复体验

这是对话模式最关键的体验之一。

用户离开数小时或数天后回到当前会话，应该可以通过：

- `/autoresearch sync`

得到一个明确恢复摘要。

建议摘要包括：

1. 离开期间完成了什么
2. 哪些 job 状态发生了变化
3. 飞书里有没有新增输入
4. 当前是否有 blocker
5. 现在建议用户做什么决策（如果需要）

这一步的本质是：

- runtime 持续推进
- mode 层负责“重新讲给当前会话听”

### 和当前代码仓库的关系

当前 `/home/yqhao/autoresearch_for_codex` 已经适合作为 runtime 层原型。

所以接下来并不是“推翻重写”，而是重新分层：

#### 当前仓库保留的部分

- `scripts/autoresearch.py`
- `state / plan / inputs / jobs / events`
- `daemon-start`
- Feishu heartbeat / polling
- SLURM helper

这些继续做底层。

#### 需要新增的部分

- `mode adapter`
- 模式命令协议
- runtime -> chat 的状态渲染
- chat -> runtime 的输入桥

也就是说，下一阶段不是再堆更多后台能力，而是要补“对话模式入口层”。

### 推荐的下一步实施顺序

建议按这个顺序继续推进：

1. **定义 mode 命令协议**
   先把 `/autoresearch start/status/sync/update/pause/resume/stop` 定义清楚。

2. **实现 chat input -> runtime input**
   让对话输入能稳定写进 `inputs.jsonl`。

3. **实现 runtime -> chat 状态渲染**
   把 `status / sync / plan / jobs` 做成稳定输出格式。

4. **实现 pause / resume / stop**
   让模式和后台 runtime 真正联动。

5. **实现“回来后恢复摘要”**
   这是用户体验的关键点。

6. **最后再考虑 plugin / slash command 化**
   先把交互协议跑通，再包装成更原生的 UI 入口。

### 关键结论

当前项目的下一步正确方向，不是继续把它做成更复杂的“Python 调 Codex”系统，而是：

- 把当前仓库沉到底层 runtime
- 在当前 Codex 对话里做 `Auto-Codex mode`
- 用 mode 负责交互体验
- 用 runtime 负责长期执行能力

一句话概括：

> `Auto-Codex` 的正确产品形态应该是“当前会话中的 autoresearch mode + 背后的持久 runtime”，而不是单独的后台脚本，也不是单独的长对话。

这个方向更符合用户真实使用习惯，也更接近一个真正可持续使用的研究助手产品。
