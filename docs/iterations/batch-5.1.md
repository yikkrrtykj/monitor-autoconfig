# Batch 5.1 — control refresh lifecycle correctness

更新日期：2026-09-06。状态：**已收口（服务器与网页验收通过）**。
来源：用户交接（既有实现及测试结论）、后续服务器执行输出、本地 Git 差异核对。Agent 未连接公司服务器，不重做代码审计。

## 目标与范围

防止控制台旧响应覆盖当前状态，同时保留正常慢请求的提交机会和 10 秒定时刷新。
修改范围：`bigscreen/app.js`、`bigscreen/control/auth-controller.js`、`bigscreen/config/config-editor.js` 及对应 JS 测试，均位于 `librenms+grafana/`。

## 实现与提交

- `675409458a02ac21d478aa01634d71b84f981dd5` — `fix: prevent stale control responses from overwriting current state`。
- `8135c8542bc7c98076961959aae00ff586fb2ca3` — `test: align control lifecycle contract assertions`；本轮核对在前一提交之后，仅改 `tests/test_deployment_contracts.py` 的过期断言，未改变运行代码。
- 用户旧交接确认 6754094 已推送；2026-09-06 本轮实际查询 GitHub main 为 8135c85，与启动时本地 HEAD 一致。

控制台刷新使用生命周期代次、请求序号、已提交序号；新结果提交后，旧成功和失败均不能覆盖它。
丢弃结果不更新 `lastControlReport`、`lastDataSuccessAt` 或错误界面。
新请求不机械取消正常慢请求；离开控制台、退出、新登录、Apply 开始、重新进入均失效旧请求。
logout 在网络请求前立即失效旧认证探测并显示退出界面；认证控制器在写状态和 DOM 前检查代次与顺序。
Apply 最小回调失效刷新；operationId、恢复轮询、期限、成败语义不变；未保存草稿由现有 dirty-state 继续保护。

## 既有验证（用户交接，非本轮复测）

- 认证生命周期、配置编辑器、Apply 恢复、平台模型 JS 测试通过。
- 修改文件 Node 语法和 `git diff --check` 通过。
- 既有只读复核无阻断，相关 JS 测试再次运行通过。
- 未运行全量回归、Linux Node 20、浏览器自动化；未连接公司服务器。
- 8135c85 的当前 CI 状态必须另查，不能从提交名称推断测试通过。

## 部署与验收

完整命令与网页步骤见 [公司部署手册](../runbooks/company-deployment.md)。
必须同时核对 `.env`/Compose/Bridge 的四开关、三个 JS 的宿主机/容器/HTTP SHA、缓存头和 configured。
不需要故意断网、不需要点击 Apply，不发送测试飞书或真实 DELETE。

### 2026-09-06 收到的服务器验收结果

用户回传的 `BATCH51_DEPLOY` 输出显示先从 1d97260 快进到实现提交 6754094 并执行部署，最后输出 `BATCH 5.1 DEPLOYMENT CHECKS PASSED`。随后另一组 fetch/pull 输出确认 main 快进到完整 SHA `8135c8542bc7c98076961959aae00ff586fb2ca3`。
这是反馈接收日期，日志未提供完整执行时间戳；不据此推定具体部署时刻。仅持久化必要的脱敏证据摘要，不入库整段终端日志。

| 检查 | 用户输出与结论 |
|---|---|
| 部署前安全配置 | `.env` 和 Compose 四开关均 PASS：true / true / true / false |
| 本轮初始化任务 | librenms-config、grafana-setup 均报告 this operation、exit 0 |
| Bootstrap | `Result: PASS (33 passed)`；部署脚本报告完成 |
| 前端文件 | app.js、control/auth-controller.js、config/config-editor.js 均报告 `current Bigscreen source served` PASS |
| 缓存策略 | `Bigscreen JavaScript cache disabled` PASS |
| Configured | `Result: PASS (39 passed)` |
| ISP inventory | 5 fresh ISP(s)、5 availability target(s)，自动发现 flags 一致 |
| Bridge | health payload/watchers、enabled capabilities、readiness、LibreNMS token availability 均 PASS；输出明确 delivery not tested |
| 运行时删除安全 | 四开关全部 PASS：true / true / true / false，保持 dry-run |
| Git 与生产文件 | 最终 HEAD 为 8135c85；状态仅列既有 untracked 备份、日志、VERSION、运行目录，没有已跟踪文件修改；不清理这些文件 |

证据边界：JS 一致性与缓存结论来自用户脚本的 PASS 摘要，粘贴内容未完整保留检查实现与原始 SHA/响应头，Agent 未独立复验；没有把未执行的飞书投递测试记为通过。
本地 `git diff --name-only 6754094 8135c85` 确认只改 `librenms+grafana/tests/test_deployment_contracts.py`；因此已验证的运行代码未变，无需再次 deploy。8135c85 之后截至本轮基线 b02bd54 仅有规范/文档变化，也不要求服务重启。

### 2026-09-06 网页验收确认

用户对涵盖以下项目的验收问题明确回复“上述网页检查全部通过”：登录后等待至少 15 秒、页面切换、退出后等待超过 10 秒再登录、配置草稿、事故区域和五个 ISP 显示。
依据服务器结果和用户网页确认，Batch 5.1 收口。没有新问题证据时不重审、不要求重跑已通过步骤。

### 本次记录维护与下一步

- 本地 main 基线 `b02bd5427aeb702e736b1d6888ed1965e8272748`，开始时工作区和暂存区干净；实时核对远端 main 与该基线一致。本次只更新状态、上下文、索引、操作手册、验收记录与下一批候选文档。
- 验证通过：提交差异核对、13 个 Markdown 文件的 52 个本地链接与围栏检查、`git diff --check`。不重跑 Batch 5.1 回归或服务器部署；下一批分析单独运行事故面板既有测试和文档中的模拟复现，均达到预期，详见候选记录。
- CI 与生产验收是独立证据；当前不新增 CI 通过结论。记录提交用 `git log -1 --format="%H %s" -- docs/iterations/batch-5.1.md` 定位。
- 按原交接要求完成下一批限定分析，发现并本地复现 [Batch 5.2 候选](batch-5.2-proposal.md) 的事故分析页请求顺序问题；仅记录候选，不在本轮修改业务代码。
- 交接已持久化；当前无可调用上下文压缩工具，未执行客户端压缩。
