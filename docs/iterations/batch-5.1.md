# Batch 5.1 — control refresh lifecycle correctness

更新日期：2026-09-06。状态：**待生产验收**。
来源：用户交接（既有实现及测试结论）；本轮 Git 检查（补充测试提交）。本轮不重做代码审计。

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

当前无本批次公司服务器或网页验收结果。待用户执行并回传；验收通过后更新本记录和 STATUS，追加提交，再开展下一批。
