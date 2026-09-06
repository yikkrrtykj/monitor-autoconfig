# 工程文档索引

维护日期：2026-09-06。项目面向赛事现场及公司长期监控部署；使用说明仍从 [根 README](../README.md) 进入。

| 文档 | 职责 |
|---|---|
| [AGENTS.md](../AGENTS.md) | Agent 必须遵守的启动、实施、交付、压缩顺序 |
| [STATUS](STATUS.md) | 当前进度、活跃批次、阻断与唯一下一步；恢复上下文首先读 |
| [PROJECT_CONTEXT](PROJECT_CONTEXT.md) | 长期业务约束、冻结边界、验收历史 |
| [ENGINEERING](ENGINEERING.md) | 架构边界、代码、安全、测试与完成标准 |
| [GIT_WORKFLOW](GIT_WORKFLOW.md) | main 串行提交、发布核验、失败与回退 |
| [DOCUMENTATION](DOCUMENTATION.md) | 文档分类、更新触发、证据和上下文持久化 |
| [迭代模板](iterations/TEMPLATE.md) | 新批次的记录结构 |
| [Batch 5.1](iterations/batch-5.1.md) | 控制台刷新生命周期修复，服务器与网页验收通过、已收口 |
| [Batch 5.2 候选](iterations/batch-5.2-proposal.md) | 事故分析页请求乱序问题；已复现，仅分析、未实施 |
| [工程规范建设](iterations/2026-09-06-engineering-governance.md) | 本次规范和持久化建设记录 |
| [公司部署与验收](runbooks/company-deployment.md) | Batch 5.1 完整服务器命令和网页验收 |

专题文档保留在既有位置，避免为了整理目录打断已有链接：

- [飞书应用与群排障](../librenms+grafana/docs/feishu-app-chat-troubleshooting.md)
- [拓扑采集负载评估](../librenms+grafana/docs/topology-load-assessment.md)

新会话最小入口：`AGENTS.md` → `docs/STATUS.md` → `docs/PROJECT_CONTEXT.md` → 活跃迭代。历史记录按需查阅。
