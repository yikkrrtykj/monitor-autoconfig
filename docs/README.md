# 工程文档索引

维护日期：2026-09-28。项目面向赛事现场及公司长期监控部署；使用说明仍从 [根 README](../README.md) 进入。活跃任务、TODO 和验收状态在 [GitHub Issues](https://github.com/yikkrrtykj/monitor-autoconfig/issues)。

| 文档 | 职责 |
|---|---|
| [AGENTS.md](../AGENTS.md) | Agent 必须遵守的启动、实施、交付、压缩顺序 |
| [PROJECT_CONTEXT](PROJECT_CONTEXT.md) | 长期业务约束、冻结与安全边界、历史证据入口 |
| [ENGINEERING](ENGINEERING.md) | 架构边界、代码、安全、测试与完成标准 |
| [GIT_WORKFLOW](GIT_WORKFLOW.md) | Issue/PR 工作流、发布核验、失败与回退 |
| [DOCUMENTATION](DOCUMENTATION.md) | 文档分类、更新触发、证据和上下文持久化 |
| [旧 TODO 与 STATUS 归档](https://github.com/yikkrrtykj/monitor-autoconfig/issues/16) | 原文快照见 #16、#17；旧迭代记录见 #18–#24 |
| [A-02.1 生产验收](https://github.com/yikkrrtykj/monitor-autoconfig/issues/13) | 尚未收口的 Hotfix 审计、部署和现场验收 |
| [A-02.1 部署与验收](runbooks/a-02.1-unifi-mac-hotfix.md) | 本次 Hotfix 的完整操作与回滚手册 |
| [公司部署与验收](runbooks/company-deployment.md) | 已收口 Batch 5.2 的历史部署参考，不能直接用作 Feishu 修复交付 |

专题文档保留在既有位置，避免为了整理目录打断已有链接：

- [飞书应用与群排障](../librenms+grafana/docs/feishu-app-chat-troubleshooting.md)
- [拓扑采集负载评估](../librenms+grafana/docs/topology-load-assessment.md)

新会话最小入口：`AGENTS.md` → `docs/PROJECT_CONTEXT.md` → 对应 Issue/PR → 实际 Git/CI。历史记录按需查阅归档 Issue。
