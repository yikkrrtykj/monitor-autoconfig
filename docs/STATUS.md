# 当前状态与恢复入口

更新日期：2026-09-18（Asia/Shanghai）。维护者：当前迭代 Agent；生产结果由用户反馈。

## 当前结论

- [Batch 5.1 — control refresh lifecycle correctness](iterations/batch-5.1.md) **已收口**：用户回传服务器检查通过，并明确确认网页检查全部通过。
- 已知实现 SHA：`675409458a02ac21d478aa01634d71b84f981dd5`。
- 用户回传日志确认：服务器先部署 `6754094` 并通过检查，随后快进至 `8135c8542bc7c98076961959aae00ff586fb2ca3`；后者仅调整测试契约，无需再次部署。已跟踪文件无修改，既有未跟踪生产文件保留。
- [Batch 5.2 — 事故分析页请求顺序](iterations/batch-5.2-proposal.md) 已正式收口：用户明确确认修复、GitHub CI、部署、服务器 39/39、从首页进入实际页面均 PASS，Blocking finding NONE。已知 P3 `/incident` 直接刷新问题 DEFERRED，不继续处理。
- Batch 5.2 实现为 d9da386，测试契约补丁为 ce36e96。2026-09-06 隔离方案基线为 `ce36e96662987da690cd97d1472455b554261df5`，不是当前 HEAD。
- 当前工作：[FEISHU-UX2 网络巡检分页卡](iterations/feishu-ux2.md)。用户已确认设计及单 App 单接收客户端前提；实现提交 `d79b4492a4906cb95f6508b3e28d7f2bd7769346` 已推送，GitHub CI run `35250338126` 全部通过，本轮未部署生产。
- FEISHU-UX2 实施基线为 `78666bf57c8ed1792dd5e511e8bc3ee219005b95`，实现提交与 CI 证据见上。用户确认 2026.08.1 / Golden `ed01865a19616dd0573ba7548b13bf6e11db70f7` 已发布投入使用；下一版本开发不回写 Golden。
- 更正旧恢复记录：Git 已包含 `f10a26e` 隔离修复、`bf573d2` HELP 实现及 `4aafd85` 测试，因此旧隔离方案“尚未实施”不是当前事实；本轮不重审或修改这些模块。
- 工程治理历史记录：[2026-09-06 工程规范建设](iterations/2026-09-06-engineering-governance.md)。规范已随 `7376319` 提交推送并核对远端；其发布结果由 b02bd54 追加记录，CI 当时查询无运行记录。
- 当前 Git HEAD、远端发布和 CI 以实际查询为准；不把旧 SHA 写成永远有效的“最新版本”。治理提交可通过其迭代文档的 Git 历史定位。

## 唯一下一步

由用户按[验收手册](runbooks/feishu-ux2-acceptance.md)部署并执行真实飞书桌面/手机、1/2/3+ 页及快速点击验收。多实例共享 App 仍不在本轮范围。

## 恢复时必须保留

- 不重做 Batch 5.1 审计；不重审冻结 ISP；不访问/修改 Golden。
- 不自动连接公司服务器，不主动测试发飞书，不执行真实设备 DELETE。
- 四个删除开关保持 `true / true / true / false`，详见 [项目约束](PROJECT_CONTEXT.md)。
- main 串行普通提交和 push，无分支/PR/历史改写；保留服务器既有未跟踪生产文件。
- 已验收 Batch 5.1/5.2 不重复部署、审计或验收。FEISHU-UX2 尚未部署；不修改生产配置或删除服务器既有未跟踪文件。

## 证据与未验证项

- 已验收 Batch 2～4.2 见 [长期上下文](PROJECT_CONTEXT.md)，来源为用户交接，未在本轮重测。
- Batch 5.1 回传结果：Bootstrap 33/33、Configured 39/39、5 个新鲜 ISP、三个 JS 与缓存检查、三层四开关均 PASS；用户随后确认网页检查全部通过。来源为用户执行输出与确认，Agent 未连接服务器。
- Batch 5.2 生产/CI 通过状态来源为用户最新确认，覆盖了此前待验收状态；Agent 未重新连接服务器。
- 2026-09-06 Feishu 历史审计：当时以 Python 3.12.14 提取纯函数核对空名称行为，pytest 因缺包未运行；不能据该记录推断当前实现的测试状态。
- 工程治理的检查、发布结果及上下文压缩状态见其迭代记录；没有实际工具结果不得宣称客户端压缩已完成。
- FEISHU-UX2 聚焦 pytest 186 passed，Bigscreen JavaScript 34 个脚本和全仓 JavaScript 语法通过，Python compileall 与 diff 检查通过。Windows 全量 pytest 为 1665 passed、1 skipped、12 subtests passed，另有 31 个 Docker 缺失 setup error 和 2 个既有 Windows 环境失败；Ubuntu GitHub CI run `35250338126` 已全部通过。真实飞书和手机视觉未验收前不能 CLOSED。未连接公司服务器、未发测试消息、未执行 DELETE。
