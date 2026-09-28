# Git 管理标准

维护日期：2026-09-28。当前使用 GitHub Issue + PR 工作流；2026-09-28 以前直接提交 main 的记录是历史事实，不补造 PR。

## 开始与同步

在本地主检出根目录检查：

```text
git branch --show-current
git status --short --branch
git diff --stat
git diff --cached --stat
git log -5 --oneline
git remote -v
```

确认当前分支及其对应 Issue/PR。新任务从已同步的 `main` 创建 `codex/` 分支；已有 PR 则继续它的工作分支。发现分支异常时先说明，不自动移动他人改动。
远端应为 `https://github.com/yikkrrtykj/monitor-autoconfig.git`；核对身份后再操作。
工作区/暂存区干净且没有本地分歧时可 `git fetch origin`，在 `main` 上执行 `git pull --ff-only origin main`；不得把新任务直接写到 main。
现有未提交内容保留并标明归属；不自动 stash、不清理 untracked、不覆盖文件。

本地主检出当前启用了非 cone 模式的 sparse-checkout。必须保留根 `AGENTS.md`、`.editorconfig` 和 `docs/`，否则后续恢复会缺少交接入口。必要时先用 `git config --get core.sparseCheckoutCone` 确认仍为 `false`，再执行 `git sparse-checkout add /AGENTS.md /.editorconfig /docs/` 追加路径；不要重置整份检出范围。

## 提交与推送

1. Issue 写明目标、TODO、范围与验收条件。完成针对性验证，仅在稳定行为、安全约束或操作方式变化时更新相应文档。查看 `git diff`，确认没有真实配置、日志、缓存、临时文件或无关重排。
2. `git diff --check`；使用 `git add -- <逐项列出的本轮文件>` 精确暂存，不执行宽泛 add。
3. 检查 `git diff --cached --check`、`git diff --cached --stat` 和完整暂存 diff，确认暂存区只含本轮授权改动。
4. `git commit -m "<type>: <变更目的>"`。使用 `fix`、`feat`、`test`、`docs`、`refactor`、`chore` 等清晰类型；一个提交一个可审查目的，代码和对应文档尽量同行。
5. `git push origin <工作分支>`，禁止 force。使用 `git rev-parse HEAD` 与 `git ls-remote origin refs/heads/<工作分支>` 核验远端提交。创建或更新面向 `main` 的 PR，正文关联 `Refs #编号`，说明最终行为、测试、限制和部署/验收边界；创建后将 PR URL 关联当前任务。
6. 检查 PR 的 CI 和审查状态；不能以 push 成功代替 CI 成功，也不能把 PR 已创建写成已合并。默认由授权人员合并；本地 Agent 不自行合并或部署。
7. 合并后核实 `main` 包含本轮代码，生产结果和剩余 TODO 回写 Issue。只有实际生产验收完成才能关闭相应业务 Issue；最终再次检查工作区，不清理他人文件。

常用类型是项目约定，不额外引入 commit hook 或强制依赖。
不 amend、不 rebase、不 force push、不改写既有提交；发现问题在工作分支追加最小修复 commit。PR 合并策略由授权人员决定，Agent 不自行合并。

## 异常与回退

- 网络/凭据/权限失败：保留本地提交，记录错误类别及待推送 SHA，不输出凭据；依宿主权限流程尝试合法重试。不得修改全局认证、关闭 TLS 验证或改成无鉴权传输。
- non-fast-forward：停止 push，fetch 并查看双方差异。无本地独有提交可 fast-forward；确实分歧时保留双方历史，先报告最小整合方案，经用户明确同意后普通 merge；禁止自行 rebase/force。
- 本次真实阻断：保留已有提交，追加修复。无法安全修复时报告影响及下一步，不扩大生产操作。
- 需要撤销已发布逻辑时，优先说明影响后使用追加 revert commit；这是生产行为变化，需要明确范围和部署方案，不自动回滚用户数据或数据库。
- 服务器同步只允许 `main` 快进；已跟踪文件必须无修改，已有 untracked 文件允许存在。如 untracked 与新版本路径冲突，停止并报告，不删除或移动它们。

## 版本与证据

`VERSION` 为仓库已有软件版本入口，文档/测试迭代不机械改版本。正式版本策略变化另行制定。
本地根目录跟踪的 `VERSION` 与服务器既有未跟踪 `VERSION` 必须区别对待。
代码 SHA、CI、部署 SHA 和用户验收是独立证据；完整生产流程见 [操作手册](runbooks/company-deployment.md)。

提交不能可靠地在自身内容里写自己的最终 SHA。PR 自动关联提交与 CI，Issue 保存生产验收和未完成项；不要为更新 SHA 反复提交 Markdown。推送/CI 失败要在对应 Issue/PR 如实记录实际状态与下一步，远端不可用期间不得标为已发布。
