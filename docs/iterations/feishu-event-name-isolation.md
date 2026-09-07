# Feishu EVENT_NAME 共享群隔离方案

维护日期：2026-09-07。状态：**最小修复已实施，待独立审计与生产验收**。
审计基线（问题最初确认）：`ce36e96662987da690cd97d1472455b554261df5`。实施基线：`e44ca0e84449b2ea822e67147fb15a99d1c2751a`（独立审计 PLAN AUDIT 指定，未回退到 ce36e96）。实施开始时本地 main 工作区与暂存区干净。
授权范围：按独立审计通过的方案实施最小修复；本轮不修改生产配置、不部署、不发送飞书消息。

## 目标与结论

同一个飞书 App 服务多台监控服务器、共用一个群时，群命令只交给匹配本机 EVENT_NAME 的服务器。
**EVENT_NAME 为空或全空白的服务器不得执行或回复任何群命令**，包括无前缀命令、带其它赛事前缀的命令、帮助及仅 @ 机器人。

ce36e96 中该问题仍存在：

- `librenms+grafana/feishu-ws-client.py` 的 `route_event_command()`（基线第 301 行）在规范化后 event 为空时执行 `return text`。
- `process_polled_messages()`（基线第 434 行）和 `on_message()`（基线第 490 行）都调用它。通过路由后会启动 `_process_message`，进而查询 Bridge 并回复群消息。
- `main()` 未要求 EVENT_NAME 非空；Compose 的 EVENT_NAME 默认值也为空，不能依赖配置层阻止该路径。
- 既有测试明确要求空名称接收无前缀命令；修复时必须更新这个已过期的兼容契约，同时保留原测试的去重、baseline 和回退覆盖。
- 非空名称的普通跨赛事、无前缀、SG/SG2 边界和多词名称已有匹配机制，不重写其算法。

只读证据：使用 Python 3.12.14 从 AST 单独提取原路由函数，验证空/空白名称接受无前缀、Singapore 接受本站前缀且拒绝 Shanghai 和无前缀。没有导入整个服务或触发 I/O。
上一轮尝试运行 pytest 时环境缺少 pytest，未把测试记为通过；该限制不影响上述源码与纯函数证据。

## 行为约定

| 入口/场景 | 本机 EVENT_NAME | 预期 |
|---|---|---|
| 群历史轮询，任意命令 | 空或全空白 | 静默丢弃；不查询 Bridge、不启动命令任务、不回复 |
| 长连接群消息，任意命令 | 空或全空白 | 同上；即使 CHAT_TARGET 为空也拒绝 |
| 长连接，chat_type 缺失/未知但原消息过滤允许 | 空或全空白 | 按群消息安全规则拒绝，不推断成私聊 |
| 群消息：`Singapore 网络巡检` | Singapore | 剥离本站前缀，执行网络巡检 |
| 群消息：其它赛事前缀或无前缀 | Singapore | 静默丢弃 |
| 群消息：仅 `Singapore` 前缀 | Singapore | 沿用现有行为，显示本站帮助 |
| 明确为 p2p 的长连接消息：无前缀 | 空或全空白 | 保留既有私聊兼容行为，只在原回退入口可处理时生效 |
| 明确为 p2p 的长连接消息 | 非空 | 保留现有前缀路由，不扩大为所有私聊免前缀 |
| 普通群聊、非文本、重复消息、历史 baseline | 任意 | 保留原过滤、去重与 baseline 行为 |

轮询来源本身就是配置的群历史，不能因为条目里缺失 chat_type 或声称 p2p 就启用私聊例外。
`_POLL_READY` 的长连接抑制行为不改变；本轮不解决私聊在轮询 ready 时是否需要单独放行的问题。

## 最小实现设计

1. 为纯函数 `route_event_command()` 增加仅关键字参数 `allow_unscoped=False`，默认拒绝空名称。空名称时仅在显式允许无前缀兼容的路径返回原文本，否则返回 `None`；非空名称沿用现有匹配逻辑。
2. 群历史轮询调用保持默认拒绝，不提供 `allow_unscoped=True`。不以 CHAT_TARGET 是否为空猜测单机/共享群模式。
3. 长连接入口只有消息 chat_type 明确为 `p2p` 时才传 `allow_unscoped=True`；group、缺失和未知类型一律保持 False。类型字符串沿用原小写规范化语义。
4. 两个入口继续在 `command is None` 时立即退出。路由拒绝必须发生在启动命令线程之前；禁止把 None 经 `or "帮助"` 转成帮助回复。原消息 ID 预留与重复消息处理保持不变。
5. 不新增“允许空群名称”的环境开关，不默认为某个赛事名，不更改轮询/重连/告警发送/卡片回调。缺少 EVENT_NAME 的单机群命令也停止响应，恢复方式是配置明确名称并加前缀，不能自动退回不隔离模式。

拟修改范围：

- `librenms+grafana/feishu-ws-client.py`：路由函数和长连接调用处，更新相邻说明。
- `librenms+grafana/tests/test_feishu_ws_client.py`：路由与两个入口的行为测试。
- 根 README、`.env.example` 的相关注释及飞书排障文档：说明群命令非空名称要求、静默拒绝与私聊兼容边界；不改 `.env.example` 的现有默认值。
- 本记录、STATUS、项目上下文和本批专用部署手册。

不修改：Bridge 查询/告警/设备删除逻辑、凭据及 token 校验、ISP/HA、Golden、已收口 Batch 5.1/5.2，以及 P3 `/incident` 刷新问题。

## 测试与实施顺序

先增加能在 ce36e96 上失败的测试，再修改最小路由分支；不得通过删除旧测试掩盖行为变化。

| 层次 | 必测场景与断言 |
|---|---|
| 纯函数 | 空串、空格、制表符名称默认返回 None；明确私聊例外保留原行为；具名命令大小写、分隔符、多词和名称边界不变 |
| 群轮询 | 空/空白名称收到无前缀、其它名称、帮助和仅 @ 时返回 handled=0；线程、Bridge 查询与回复调用次数均为 0；条目缺少 chat_type 或误标 p2p 也不能绕过 |
| 长连接回退 | `_POLL_READY=False` 时，group/未知/缺失类型与空名称不启动任务；覆盖 CHAT_TARGET 有值和空值；具名正确路由仍可处理 |
| 私聊兼容 | 明确 p2p、空名称在原回退条件下可处理；非空名称仍要求原前缀；`_POLL_READY=True` 的既有抑制保持 |
| 多实例同一消息 | 同一组消息分别交给 Singapore、Shanghai 和未命名实例；各实例分别模拟独立进程的去重状态，不能共用 `_SEEN_MESSAGES` 把重复响应隐藏掉；仅目标具名实例执行 |
| 原有功能 | 保留 baseline 不重放、消息去重、轮询 ready 抑制回退，以及公司模式卡片注册/token 转发用例 |

使用假消息、线程替身、mock Bridge 和 mock 回复函数，网络访问设置为意外调用即失败。无需真实 App 凭据，不发群消息，不执行 DELETE。
原先用空名称验证 baseline/回退的正向测试改用具名消息，保留其原断言目的；另建空名称拒绝的负向测试。

实现阶段执行：

```text
python -m pytest -q librenms+grafana/tests/test_feishu_ws_client.py
python -m pytest -q librenms+grafana/tests/test_deployment_contracts.py -k feishu
python -m py_compile librenms+grafana/feishu-ws-client.py
git diff --check
```

先确认测试工具可用，可在隔离开发环境安装仓库开发依赖；不改全局解释器或生产容器来补测试工具。工具不可用时记录准确的未测项，不假称通过。
远端 CI 以实际修复提交 SHA 为准；其它已验收模块不为本批重复审计。若针对性测试暴露相关真实回归，修复后再交付。

## 部署、验收与回退要求

本轮方案不需要部署。实现交付时新增本批完整手册，不能复用旧 Batch 5.2 的目标 SHA 或前端文件哈希清单：

1. 给出已核对的完整修复 SHA，服务器 main 已跟踪内容干净，fetch 后只快进到该目标。保留所有既有 untracked 生产文件；版本/路径冲突即停止。
2. 部署前核对 `.env` 与 Compose 的四个删除开关为 true/true/true/false，核对 feishu-ws 的 EVENT_NAME 来源和值与预期一致，不输出完整配置或凭据。
3. 执行 `docker compose config --quiet`、`./deploy.sh </dev/null`、`./deploy-check.sh configured </dev/null`；部署后核对 Bridge ready 和运行时四开关。
4. 校验本批 `feishu-ws-client.py` 的已提交源码、宿主文件和 feishu-ws 容器实际运行文件 SHA 一致，并检查容器本轮已启动。脚本未通过 HTTP 提供，HTTP 验收只核对大屏/API 健康，不编造 Python 源码 HTTP 路径。
5. 用脱离业务进程的纯函数/模拟消息检查覆盖未命名实例静默拒绝和具名实例匹配；不修改生产 EVENT_NAME 以制造测试场景，不调用真实 Bridge 查询/回复。
6. 网页只检查现有控制台及主要页面仍可正常使用。实际飞书网络投递若未验证，明确写“未测”；不得为了验收主动发消息。用户自行观察到真实业务命令的结果时再补充证据。
7. 回退先说明影响：直接撤销会重新开放空名称群命令。优先追加最小修复；确需 revert 时必须经明确授权，使用追加提交和完整部署验证，不 reset/rebase，不自动改生产配置或清理文件。

## 边界与收口后主线

本补丁保证缺少名称时不接受群命令，不把本机路由器描述成全局实例注册系统。共享群仍要求各实例配置规范化后唯一、无前缀歧义的名称；现有分隔符匹配不能自行确认其它 VM 的配置或名称唯一性。本轮不改命令语法、不新增集中配置服务。

主线固定为：**Feishu EVENT_NAME 缺口最小修复与验收 → pre-refactor 只读审计 → 无剩余 P0/P1 blocker 时停止扩大 correctness 修复 → behavior-preserving refactor**。
Pre-refactor 审计只检查拟重构模块及其调用边界，输出基线、行为不变量、已有测试、P0/P1 证据及可延后项；不能凭局部审计声称全仓无 blocker。P2/P3 记录后延后，不重新打开 `/incident` 刷新或冻结 ISP。
重构前先固定第一批模块范围和验证清单；纯重构不得混入行为修复。本轮不启动实现、广泛审计或重构。

## 交付记录

本节记录实施轮（2026-09-07）。实现改动严格限定在 `librenms+grafana/feishu-ws-client.py` 与
`librenms+grafana/tests/test_feishu_ws_client.py`；`test_deployment_contracts.py` 无 stale 断言，未修改。
文档仅做必要语义补充：根 README、`.env.example`（默认值不变）、飞书排障文档、STATUS、PROJECT_CONTEXT、
docs/README、本记录，以及新增本批专用部署手册。

关键实现点：

- `route_event_command()` 新增仅关键字参数 `allow_unscoped=False`；空/空白 EVENT_NAME 默认返回 `None`，仅 `allow_unscoped=True` 时返回原文本；非空名称匹配逻辑不变。
- 群轮询 `process_polled_messages()` 保持默认拒绝，不传 `allow_unscoped=True`，条目 chat_type 缺失或误标 p2p 均不绕过。
- 长连接 `on_message()` 仅在消息 chat_type 小写规范化后明确为 `p2p` 时传 `allow_unscoped=True`；group/缺失/未知类型一律拒绝。
- 两个入口在 `command is None` 时于启动命令线程前退出，消息 ID 预留与去重不变。
- 审计修正项 5 已锁定：`route_event_command("网络巡检", "Singapore", allow_unscoped=True) is None`，即 allow_unscoped 不能绕过非空名称的前缀要求。
- 原先用空名称验证 baseline/回退的正向测试改用具名消息（断言目的不变），新增空名称拒绝、p2p 兼容及多实例隔离负向测试。

本地验证结果（Windows 本地 Python 3.14 环境）：

```text
python -m pytest -q librenms+grafana/tests/test_feishu_ws_client.py
→ 20 passed, 1 warning（仅 pytest cache 目录写权限警告，与测试无关）
python -m pytest -q librenms+grafana/tests/test_deployment_contracts.py -k feishu
→ 3 passed, 68 deselected
python -m py_compile librenms+grafana/feishu-ws-client.py
→ 通过
git diff --check
→ 通过
```

未测项（NOT RUN）：真实飞书投递、生产部署、服务器运行时核对；待独立代码审计通过后按
[部署手册](../runbooks/feishu-event-name-isolation-deployment.md) 由用户执行。
实现提交不 amend/rebase/reset；本轮提交后暂不 push，待独立审计。
交接已持久化；无可调用的客户端压缩工具，未执行上下文压缩。
