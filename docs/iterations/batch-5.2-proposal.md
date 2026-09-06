# Batch 5.2 候选 — 事故分析页请求顺序

维护日期：2026-09-06。状态：**限定分析完成，已本地复现，尚未实施**。
来源：Batch 5.1 收口后按原交接要求分析下一批前端问题。本地 main 基线 b02bd5427aeb702e736b1d6888ed1965e8272748；不连接服务器，不代表已发生生产故障。

## 确认的问题（P2）

`librenms+grafana/bigscreen/incident/incident-panel.js` 的 `runIncidentAnalysis()`（当前第 166 行）只捕获页面 `lifecycleGeneration`；该代次只在 `stop()` 时递增，同一页每次提交没有独立请求序号。
在 `/incident` 连续改变条件并提交 A、B 时，两次结果都满足 `isCurrent()`。如果 B 先成功、A 后成功，A 会覆盖 B，结果与表单和地址栏中的 B 条件不一致，可能误导事故判断。
这与 Batch 5.1 的控制台认证/定时刷新是不同控制器，不是重新审查已经验收的实现。

## 本地复现与验证

环境：Windows、Node v24.13.0。使用现有 `test_bigscreen_incident_panel.js` 中的 FakeDocument、deferred、createHarness，仅模拟请求和 DOM，不访问网络或设备。

1. 首次 start 使用阈值 0.05，挂起其 inventory Promise，形成旧请求 A。
2. 保持页面不退出，将表单阈值改为 0.08，触发表单 submit，形成 B。
3. 让 B 先完成，验证页面显示 0.08。
4. 放行 A；实际页面被改成 0.05，而 URL 和表单仍为 0.08。

本地执行成功复现上述不一致。既有 `node librenms+grafana/tests/test_bigscreen_incident_panel.js` 同时通过：现有用例覆盖 stop/restart 作废旧请求，未覆盖同页连续提交的乱序。
复现代码可在仓库根目录使用 Node 执行（借用已有测试夹具；不修改文件）：

```javascript
const fs = require('fs');
const path = require('path');
const { createRequire } = require('module');
const testPath = path.resolve('librenms+grafana/tests/test_bigscreen_incident_panel.js');
const harness = fs.readFileSync(testPath, 'utf8').split('(async () => {')[0];
const reproduction = `
(async () => {
  const old = deferred();
  const h = createHarness({
    ispQueue: [old.promise, Promise.resolve([])],
    result: (data, threshold) => emptyResult({
      verdict: { level: 'good', text: 'threshold ' + threshold, detail: 'result' }
    })
  });
  const olderQuery = h.panel.start();
  h.document.getElementById('incidentThreshold').value = '0.08';
  h.document.getElementById('incidentForm').dispatch('submit');
  await settle();
  assert.ok(h.document.getElementById('incidentVerdict').innerHTML.includes('threshold 0.08'));
  old.resolve([]);
  await olderQuery;
  assert.ok(h.document.getElementById('incidentVerdict').innerHTML.includes('threshold 0.05'));
  assert.strictEqual(h.document.getElementById('incidentThreshold').value, '0.08');
  assert.ok(h.window.location.search.includes('threshold=0.08'));
  console.log('REPRODUCED: older result overwrites the newer conditions');
})().catch(error => { console.error(error); process.exitCode = 1; });
`;
new Function('require', '__filename', '__dirname', harness + reproduction)(
  createRequire(testPath), testPath, path.dirname(testPath)
);
```

以上断言用于证明现有缺陷；修复时必须转换成“旧结果不能覆盖新结果”的回归测试，不能把缺陷现象固化为正确行为。

## 建议的最小修复范围

- 仅修改 `bigscreen/incident/incident-panel.js`、对应测试和文档；优先使用该面板内的请求序号，不引入跨控制器重构。
- 用户主动提交新条件即代表新的分析意图：新请求开始后，只允许最新请求的成功或失败更新视图；页面 stop/restart 仍作废旧请求。
- 不为了顺序正确性强制 abort 所有旧网络调用；保留请求超时机制，先保证过期响应不再提交 UI。
- 不改变查询公式、ISP inventory/identity、阈值含义、事故持久化、控制台认证刷新、Apply 或配置草稿逻辑。

## 拟定验收条件与下一步

- 同页 B 成功后 A 迟到成功/失败，均不覆盖 B；B 失败后 A 迟到成功也不能展示为 B 的结果。
- 新条件等待期间旧结果不能冒充新条件的结果；单个慢请求无后续新提交时仍能正常完成。
- stop/restart、原有 URL 参数、表单绑定、查询和渲染测试保持通过。
- 修改后的 JS 语法、事故面板针对性测试通过；未运行全量或 Linux/浏览器验收时如实记录。
- 下一轮明确继续该范围时，先新增失败的行为测试，再做最小修复；完成后一次交付完整部署与网页验收命令。当前仅完成分析，不需要部署或生产复现。

交接已持久化；未执行客户端上下文压缩。
