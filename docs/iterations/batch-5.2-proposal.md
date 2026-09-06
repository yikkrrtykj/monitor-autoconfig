# Batch 5.2 — 事故分析页请求顺序

维护日期：2026-09-06。状态：**已正式收口（用户确认）**。
实现从 `main` 基线 `71bceff27f9c2cc2529e7cd9a214255334c0bc11` 开始；实现提交 `d9da3865fad6b7b8bc8528c5ff44c32615c08561`，后续测试契约补丁 `ce36e96662987da690cd97d1472455b554261df5`。

## 目标与范围

修复 `/incident` 同一页面连续提交分析条件时，较早请求的迟到成功或失败覆盖较新请求视图的问题。
范围仅包括事故面板请求提交顺序、对应行为测试和交付文档。

本批不改变查询公式、ISP inventory/identity、阈值含义、事故持久化、控制台认证刷新、Apply 或配置草稿逻辑；不取消旧网络请求，继续使用现有超时机制。

## 原因与实现

旧实现只捕获页面 `lifecycleGeneration`。该代次在 `stop()` 时递增，却不能区分同一页面内连续发起的请求 A、B，因此两者都能提交 UI。

面板现在维护单调递增的 `requestSequence`。每次分析开始时取得自己的请求号；异步成功或失败只有同时满足以下条件才可更新视图：

- 页面仍处于 active 状态；
- 页面生命周期代次仍相同；
- 请求号仍是最新发起的请求。

新请求开始后会立即拥有视图提交权。旧请求可自然完成，但其成功、失败、分析调用和错误展示都会被丢弃。`stop()` / restart 的原生命周期失效机制继续生效；没有后续提交的单个慢请求仍可正常完成。

## 修改文件

- `librenms+grafana/bigscreen/incident/incident-panel.js`
- `librenms+grafana/tests/test_bigscreen_incident_panel.js`
- `docs/STATUS.md`
- `docs/PROJECT_CONTEXT.md`
- `docs/iterations/batch-5.2-proposal.md`
- `docs/runbooks/company-deployment.md`

## 本地证据

环境：Windows、Node v24.13.0。测试仅使用现有 FakeDocument 和 deferred 依赖，不访问网络、设备或事故数据。

新增行为测试先在旧实现上失败：旧请求在新条件等待期间进入分析，断言得到 `1 !== 0`。最小修复后覆盖：

- 新条件等待期间，旧成功不能退出 loading 或进入分析；
- B 成功后，A 迟到成功不能覆盖 B；
- B 成功后，A 迟到失败不能显示错误；
- B 失败后，A 迟到成功不能替换失败结果；
- 无后续提交的单个慢请求正常完成；
- 原 stop/restart、URL、表单、查询和渲染契约继续通过。

执行结果：

```text
node librenms+grafana/tests/test_bigscreen_incident_panel.js
bigscreen Incident panel tests passed

node --check librenms+grafana/bigscreen/incident/incident-panel.js
node --check librenms+grafana/tests/test_bigscreen_incident_panel.js
git diff --check
均通过
```

另执行 `test_bigscreen_incident.js` 和 `test_bigscreen_incident_registry.js`，均通过。未运行全量回归、Linux Node 20、浏览器自动化或 Bash 手册语法检查；本机没有 Bash。上述未测项按项目协议不单独阻止 push。未连接公司服务器，未部署，未修改生产配置，未执行 DELETE，未发送飞书。

## 生产验收

### 最新收口结论（2026-09-06）

用户明确确认：请求乱序修复 PASS、GitHub CI PASS、生产部署 PASS、服务器 39/39 PASS、实际页面从首页进入 PASS、Blocking finding NONE。上述结论来源为用户最新反馈，Agent 未独立连接服务器或重新核验该次 CI。
此前仅显示 8135c85 的输出不能证明 Batch 5.2 已部署；保留该历史证据边界，以用户随后提供的最新完整验收结论收口，不再要求重跑。
已知 P3 `/incident` 直接刷新问题明确 DEFERRED，不属于本批阻断，不继续扩展修复。
下一步已转回 [Feishu EVENT_NAME 共享群隔离](feishu-event-name-isolation.md)，随后是 pre-refactor 只读审计。本批下述部署说明仅保留为历史交付参考。

完整命令和网页步骤见 [公司服务器部署手册](../runbooks/company-deployment.md)。服务器需快进至交付回复中的完整目标 SHA，运行部署、configured、源码/容器/HTTP 一致性和三层四开关检查。

网页重点检查 `/incident` 正常加载；同页快速提交两组不同条件后，最终表单与地址栏保持最新条件，较早请求迟到时不应让页面回退或出现旧错误。通过后由用户反馈，随后更新本记录和 STATUS 为已收口。

交接已持久化；当前工具未提供客户端上下文压缩能力。
