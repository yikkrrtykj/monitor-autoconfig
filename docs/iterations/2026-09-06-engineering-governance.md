# 2026-09-06 — 工程规范与持久化交接

维护日期：2026-09-06（Asia/Shanghai）。状态：工程规范已提交并推送；本地验证完成，CI 未确认通过。此追加记录保存发布核验结果。

## 目标与范围

用户要求按软件工程标准配置 Agent、文档、Git、代码规范，并在每轮结束后更新文档、提交仓库、压缩上下文，把关键信息持久化。
本轮仅修改 `AGENTS.md`、`.editorconfig`、根 README 和 `docs/`；不改业务实现、不重审 Batch 5.1/ISP、不连接服务器、不动 Golden。

## 基线与决策

- 本地主检出 main 基线 `8135c8542bc7c98076961959aae00ff586fb2ca3`；开始时工作区干净、暂存区为空。
- 2026-09-06 用 `git ls-remote origin refs/heads/main` 实际核对远端也是 8135c85。初次受限环境查询失败，经宿主正常权限流程重试成功，没有更改 Git 认证/TLS 配置。
- 用户交接中的实现 SHA 6754094 之后已有 8135c85 测试补丁；保留两者，不把旧快照当成最新版本，不回退 main。
- 标准入口采用 `AGENTS.md`；保留用户 main 串行工作流，工程标准不强迫切换 feature branch/PR。
- 根 Agent 协议保持紧凑，长期约束、短状态、迭代证据、操作手册分开维护，通过索引连接。
- 持久化原始交接中的业务语义和非凭据配置；实际 SNMP community 不复制入库。
- 增加编辑器 UTF-8/LF/缩进约定，沿用 `.gitattributes`；不全仓格式化、不新增依赖或 CI 门禁。
- 提交时发现本地主检出启用了非 cone 模式的 sparse-checkout，原范围排除了新增规范。使用 `git sparse-checkout add /AGENTS.md /.editorconfig /docs/` 成功保留既有路径并纳入新文件，保证后续同步不会丢失文档入口；没有关闭稀疏检出或扩大到其它目录。
- 部署手册固定到已知 8135c85，通过 main 快进拉取，防止未来 main 漂移后顺带部署未审查代码。
- 上下文压缩依赖客户端；本轮无可调用压缩工具，采用“先保存文档并提交，再通过支持的客户端入口压缩”的真实能力约定。不能声称写文档等于已压缩。

## 交付内容

- [Agent 协议](../../AGENTS.md)：每次启动/恢复必读、权限边界、审查、验证、提交推送与压缩顺序。
- [工程标准](../ENGINEERING.md)、[Git 规范](../GIT_WORKFLOW.md)、[文档规范](../DOCUMENTATION.md)。
- [项目上下文](../PROJECT_CONTEXT.md)、[当前状态](../STATUS.md)、[Batch 5.1 记录](batch-5.1.md) 和 [迭代模板](TEMPLATE.md)。
- [完整公司部署及验收手册](../runbooks/company-deployment.md)，保留当前业务唯一下一步。

## 验证与发布证据

- 验证范围：Markdown 本地链接、代码围栏配对、新文件 UTF-8/LF/末尾换行、部署手册外层与实际 Bash 正文语法、源码/容器路径核对、规范冲突与敏感信息自查、Git diff 检查。
- 验证环境：Windows、本地 Node v24.13.0 和 Git Bash。12 个 Markdown 文件的 47 个本地链接及围栏检查通过；新文件 UTF-8/LF/末尾换行通过；部署外层和实际 Bash 正文均通过 `bash -n`。AGENTS.md 为 5744 字节。
- 源码/容器路径、四开关及缓存头要求已对照 Compose 与既有交接核对；初次链接检查提示本轮迭代文档尚未创建，补齐后复查通过。未发现规则冲突或新增真实凭据；`git diff --check` 通过。
- 只做文档与编辑器约定，因此不重跑业务测试；未执行 Linux Python 3.13/Node 20、Docker/服务器命令、浏览器自动化或生产验收。
- 发布提交：`7376319170cb3cdee591814375ea702b1dfc4034`，`docs: establish engineering standards and persistent agent handoff`。2026-09-06 普通 push 成功，实时 `git ls-remote` 与本地 HEAD 一致，工作区和暂存区干净。
- 2026-09-06 查询基线 8135c85 和发布提交 7376319 的 GitHub workflow runs 均返回空列表，未确认 CI 通过；后续如需部署仍应核对目标 SHA 的实际 CI。此记录追加提交的 CI 状态同样不能从前一提交推断。
- 本记录自身提交 SHA 通过 `git log -1 --format="%H %s" -- docs/iterations/2026-09-06-engineering-governance.md` 定位，避免自引用循环；远端发布通过该提交与实时 origin/main 的关系核验。

## 下一步与交接

本轮文档不需要重启服务。当前业务仍是用户执行 [Batch 5.1 部署及网页验收](../runbooks/company-deployment.md)，随后 Agent 更新记录、追加提交收口。
交接已写入仓库文档；本轮未执行客户端上下文压缩。恢复时读取根 AGENTS、STATUS、PROJECT_CONTEXT 和活跃迭代，再核对 Git。
