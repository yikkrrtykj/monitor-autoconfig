# 当前状态与恢复入口

更新日期：2026-09-07（Asia/Shanghai）。维护者：当前迭代 Agent；生产结果由用户反馈。

## 当前结论

- [Batch 5.1 — control refresh lifecycle correctness](iterations/batch-5.1.md) **已收口**：用户回传服务器检查通过，并明确确认网页检查全部通过。
- 已知实现 SHA：`675409458a02ac21d478aa01634d71b84f981dd5`。
- 用户回传日志确认：服务器先部署 `6754094` 并通过检查，随后快进至 `8135c8542bc7c98076961959aae00ff586fb2ca3`；后者仅调整测试契约，无需再次部署。已跟踪文件无修改，既有未跟踪生产文件保留。
- [Batch 5.2 — 事故分析页请求顺序](iterations/batch-5.2-proposal.md) 已正式收口：用户明确确认修复、GitHub CI、部署、服务器 39/39、从首页进入实际页面均 PASS，Blocking finding NONE。已知 P3 `/incident` 直接刷新问题 DEFERRED，不继续处理。
- Batch 5.2 实现为 d9da386，测试契约补丁为 ce36e96。本轮方案基线 `ce36e96662987da690cd97d1472455b554261df5`，开始时本地干净，实时远端 main 一致。
- [Feishu EVENT_NAME 共享群隔离](iterations/feishu-event-name-isolation.md) **已推送，CI 通过，待生产部署验收**：实现为 `7ad540cc75bd955a51656c618a2511163dc8271f`，审计 R1～R5 已全部关闭并放行（放行记录 e61329e）。已 push 至 origin/main，实时 `ls-remote` 核对远端为 e61329e；CI run 34106488249 全部 8 个 job 通过（仅 Node 20 弃用警告）。详见 [独立审计](iterations/feishu-event-name-isolation-review.md)。
- 工程治理历史记录：[2026-09-06 工程规范建设](iterations/2026-09-06-engineering-governance.md)。规范已随 `7376319` 提交推送并核对远端；其发布结果由 b02bd54 追加记录，CI 当时查询无运行记录。
- 当前 Git HEAD、远端发布和 CI 以实际查询为准；不把旧 SHA 写成永远有效的“最新版本”。治理提交可通过其迭代文档的 Git 历史定位。

## 唯一下一步

R1～R5 全部关闭并复核放行，6 个提交（7ad540c → e61329e）已由 Agent 普通 push，`ls-remote` 实时核对远端 main = e61329e，CI run 34106488249 于 e61329e 上 8/8 job 通过。唯一下一步：用户按 [修订部署手册](runbooks/feishu-event-name-isolation-deployment.md) 执行服务器部署验收，目标提交 `7ad540cc75bd955a51656c618a2511163dc8271f`；验收通过后本批收口，返回 pre-refactor 只读审计主线。
主线顺序固定：Feishu 隔离修复与验收 → pre-refactor 只读审计 → 无剩余 P0/P1 blocker 后停止扩大 correctness 修复 → behavior-preserving refactor。
Pre-refactor 审计不得被 P2/P3 临时问题带离主线；进入实际重构前固定模块范围与验证清单。

## 恢复时必须保留

- 不重做 Batch 5.1 审计；不重审冻结 ISP；不访问/修改 Golden。
- 不自动连接公司服务器，不主动测试发飞书，不执行真实设备 DELETE。
- 四个删除开关保持 `true / true / true / false`，详见 [项目约束](PROJECT_CONTEXT.md)。
- main 串行普通提交和 push，无分支/PR/历史改写；保留服务器既有未跟踪生产文件。
- 已验收 Batch 5.1/5.2 不重复部署、审计或验收。本轮仅审计和记录，无需服务重启；Feishu 实现与修订手册已放行，仍待发布部署。

## 证据与未验证项

- 已验收 Batch 2～4.2 见 [长期上下文](PROJECT_CONTEXT.md)，来源为用户交接，未在本轮重测。
- Batch 5.1 回传结果：Bootstrap 33/33、Configured 39/39、5 个新鲜 ISP、三个 JS 与缓存检查、三层四开关均 PASS；用户随后确认网页检查全部通过。来源为用户执行输出与确认，Agent 未连接服务器。
- Batch 5.2 生产/CI 通过状态来源为用户最新确认，覆盖了此前待验收状态；Agent 未重新连接服务器。
- Feishu 隔离修复本地验证（2026-09-07，Windows 本地 Python 3.14 环境）：`python -m pytest -q librenms+grafana/tests/test_feishu_ws_client.py` 20 passed；`python -m pytest -q librenms+grafana/tests/test_deployment_contracts.py -k feishu` 3 passed、68 deselected；`python -m py_compile librenms+grafana/feishu-ws-client.py` 通过；`git diff --check` 通过。生产部署与真实飞书投递未测，待用户按手册执行。
- 工程治理的检查、发布结果及上下文压缩状态见其迭代记录；没有实际工具结果不得宣称客户端压缩已完成。
- 2026-09-07 独立审计：188 个无网络模拟路由组合、Python 编译及提交 diff 检查通过；未复跑 pytest。实时远端 main 核对仍为 e44ca0e，7ad540c 未发布；本轮审计记录也不 push。
- 9c78f1b 复核：8 段 Bash 静态语法及 5 段 Python 编译通过，mock 验证确认 R5。服务器命令未执行；开始时本地领先远端三个提交，新增复核记录仍仅本地保存。
- R5 修正轮验证：手册内嵌 Python 编译、Bash 块静态检查、链接与 `git diff --check` 通过；mock 执行两段校验逻辑验证 5 个场景（预期空/一致/Compose 漂移/运行时漂移/旧非空断言回归消除）全部符合预期。生产未执行，未测。
- 1b57ed3 独立复核：8 段 Bash 静态语法、5 段 Python 编译、14 个名称模拟组合及 Compose 字段缺失检查均通过；运行代码和测试自 7ad540c 起无变化。
- 2026-09-07 发布（本轮）：复核放行（e61329e，含 15 项模拟检查）后普通 push 六个提交；`git ls-remote` 实时核对远端 main = e61329e 与本地一致，此前复核轮的 GitHub 超时已解除；CI run 34106488249 于 e61329e 上 8/8 job 通过（Python syntax、Linux smoke、Compose、Pytest 3.13、JavaScript、Dashboard JSON、Shell syntax、ShellCheck；仅 Node 20 弃用警告）。生产部署与真实飞书投递未测，待用户按手册执行。
