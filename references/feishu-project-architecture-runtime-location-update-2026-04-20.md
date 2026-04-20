## 2026-04-20 路径规则修正

对上一版“项目整体架构说明（详细版）”补充一条重要路径规则：

- `mission.md`
- `state.json`
- `plan.json`
- `events.jsonl`
- `inputs.jsonl`
- `jobs/`
- `notes/`

这些 runtime 文件默认不应该创建在 `Auto-Codex` 工具仓库目录下。

正确规则是：

- 如果用户没有显式指定 `runtime_dir`
- Auto-Codex 默认使用“当前工作目录”下的 `./auto-codex`

例如：

- 用户在 `/home/yqhao/LLaMA-Factory` 里启动 autoresearch
- 默认 runtime 应创建在 `/home/yqhao/LLaMA-Factory/auto-codex`

这样设计的原因是：

1. runtime 应该跟随“任务所属项目”而不是跟随工具仓库。
2. 用户在项目目录里工作时，更容易理解状态文件和实验产物落在哪里。
3. 多个项目并行使用 Auto-Codex 时，每个项目各自拥有自己的 `auto-codex/` 目录，更自然也更安全。

当前这条规则已经落实到代码：

- `scripts/autoresearch.py` 现在会在未显式传参时默认解析到 `./auto-codex`
- `skills/auto-codex/scripts/auto_codex.py` 不再强行把工作目录切回 `Auto-Codex` 仓库

所以现在通过 CLI 或 `$auto-codex` 调用时，默认 runtime 都会创建在当前项目目录下。
