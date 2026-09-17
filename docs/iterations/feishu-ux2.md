# FEISHU-UX2：网络巡检分页交互卡

日期：2026-09-18。状态：设计已确认，实施及本地验证进行中；未部署生产。

## 基线与范围

- 本地 main、工作区和暂存区初始干净；HEAD 为 `ed01865a19616dd0573ba7548b13bf6e11db70f7`。本轮实时 `git ls-remote origin refs/heads/main` 核对远端相同。
- 用户确认稳定发布 Platform Version `2026.08.1` / Golden Build `ed01865` 已发布并投入使用；此为用户交接证据，不是本轮生产复验。下一版本开发不修改 Golden、VERSION 或 BUILD-INFO。
- 本轮仅审计、提出设计和持久化交接。用户原文要求“确认设计后再开始开发”。
- 仅网络巡检展示及必要的只读分页交互；不修改 shared-group / EVENT_NAME 路由、HELP、scoped/unscoped 行为、Pending/Auto Delete、LibreNMS destructive logic、ISP、拓扑、platform-api、Ping gauge、发布部署流程；不做 Bridge R5 重构。
- 旧 STATUS 和 PROJECT_CONTEXT 中“飞书隔离尚未实施”已过时：当前 Git 包含 `f10a26e` 隔离修复、`bf573d2` 全局帮助及 `4aafd85` 帮助测试。这里只更正恢复入口，不重新审计这些功能，也不据此推定各功能独立生产验收通过。

## CURRENT FLOW

以下行号以本轮代码基线为准，路径均在 `librenms+grafana/` 下。

```text
command
  feishu-ws-client.py: process_polled_messages (506) / on_message (588)
  → 既有 EVENT_NAME / HELP 路由 → _process_message (461)
  → query_via_bridge (154) → POST /bot/query
  ↓
inspection data
  alertmanager-feishu-bridge.py: Handler._handle_bot_query (5844)
  → handle_bot_query (1649)
  → fetch_librenms_devices + fetch_network_reachability (1359)
  → build_cisco_stackwise_audit_cards (1598)
     → Prometheus + evaluate_cisco_stackwise_samples + 既有堆叠基线
  ↓
render
  build_network_device_status_cards (1399)：每 35 台生成一张卡
  build_cisco_stackwise_audit_cards：额外一张堆叠卡，最多展开 30 组
  → _make_card (2563) → feishu_bridge/card_presentation.py: make_card
  ↓
Feishu card
  _process_message → _decorate_card → reply_to_message
  对 cards 逐张回复，间隔 0.15 秒；没有 prev/next
  ↓
callback
  网络巡检目前无按钮、无分页回调
  现有 on_card_action (139) 只接收 retire_delete / retire_keep
  → resolve_via_bridge → POST /retire/resolve
  → build_response (98) 返回 toast + raw card，替换被点击的卡
```

数据语义不变：排除 disabled 设备，异常优先；Ping 观测优先，缺失时沿用 LibreNMS 状态。网络巡检的旧别名继续同义。堆叠评估会维护已有学习基线，因此“分页只读”必须指点击按钮只读快照，不重复运行评估或写基线。

### action / update / state

- 当前 schema 2.0 按钮使用 `behaviors: [{type: "callback", value: {...}}]`；value 为 `{action: "retire_keep" | "retire_delete", key, token, device}`。位置：Bridge `build_retire_confirm_card`，2341 行起。
- SDK `event.action.value` 由 `on_card_action` 提取；`DEVICE_PENDING_DELETE_ENABLED=false` 直接退出。`build_event_handler`（633 行）也只在该开关开启时注册 `card.action.trigger`。
- 已有同步回调原卡替换机制，但 `build_response` 的文案和行为专属删除确认，不能直接拿它渲染巡检。未发现通用 PATCH message / update-card helper；`FeishuDelivery` 负责发送、token/chat 缓存和投递健康。
- Bridge 有 JSON 状态文件、各领域锁及缓存；WS 有 token 和消息去重状态。没有可直接复用的通用分页 session/TTL 存储。不能把巡检快照放进 `DEVICE_DOWN_STATES` 或删除事务状态。
- 当前 `/bot/query` 只接收 text，WS 回复方法不返回消息 ID；可信来源 chat/message 上下文和已发送卡片绑定需补充。上下文来自 SDK/查询入口，不能来自按钮 value。

## 设计前置问题：共享 App 回调归属

飞书长连接回调文档说明：同一 App 多个客户端以集群方式接收，随机一个收到，非广播；回调需在 3 秒内完成。
参考：[飞书回调长连接文档镜像](https://feishu.apifox.cn/doc-7518469)、[官方 Python SDK 回调入口](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/server-side-sdk/python--sdk/handle-callbacks)。本轮官方网页正文抓取为空，镜像可读取；上线前仍需用实际 SDK/飞书验证投递和更新行为。

**由平台约束和本地代码推导：** A 实例创建的 session 可能由 B 实例收到点击；B 的本地 Bridge 找不到它。加 EVENT_NAME 字段只能识别归属，不能把回调送回 A；群历史轮询也不会重放卡片点击。把错误实例的 miss 当成“过期”会误导用户。

因此“多实例共享 App 下可靠分页 + 只保存本地 session + 不修改回调投递架构”不能作为同时可保证的验收承诺。此处是本功能前置约束，不是要求重构现有路由。

推荐选择：保持本轮范围，先以**同一 App 只有一个接收客户端**作为交互分页验收前提；仍支持这个实例内的多群、多次巡检和多人点击。是否符合实际环境由用户确认，Agent 不自动修改 App/部署配置。若用户要求当前多实例共享 App 也必须可靠工作，应先单独确认跨实例回调方案及范围，再开始实现；不能用刷屏、客户端携带完整结果或偶发成功冒充满足要求。

## 最小设计草案（须先确认前置条件）

1. 每次巡检生成一次结构化展示快照。设备结果、完整堆叠检查项组成有序 items；每页默认 6 项，不按字符串行数切割。保留设备/堆叠原有文案、颜色和异常排序；顶部固定检查时间、全局设备统计和堆叠摘要。将现有堆叠采集/评估与 item 渲染做最小拆分，不改变评估及学习语义。
2. 一张卡显示当前页。单页无页码/按钮；多页显示“第 X / N 页”，首页隐藏上一页，末页隐藏下一页。不加入 /control URL、内部 ID 或新配色。初次只发送第一页，回调只返回原卡更新。
3. 小型 `feishu_bridge/inspection_pagination.py` 保存分页纯函数、专用有界 session store，Bridge 持有实例。优先复用现有 card presentation helper，不引入数据库和新的服务。
4. session 保存随机不可猜 ID、created_at、过期期限、完整展示快照、page_size、来源 App/本实例/赛事/群/源消息以及发送后的卡片消息绑定。TTL 默认 20 分钟，创建/读取时惰性清理；重启失效。建议上限 128 sessions、单快照 1 MiB、总快照 16 MiB；创建前检查容量，满额明确失败，不悄悄截断检查项或提前驱逐未过期会话。
5. value 仅 `{action: "inspection_page", session_id: "...", page: 2}`。页数和内容只从服务端快照计算；严格验证类型（拒绝 bool、浮点、非整数字符串）、长度、有效范围、TTL 和来源绑定。拒绝未知 action，不把它映射成删除分支的默认 keep。
6. 初次查询在现有路由通过后传递可信来源上下文；回调从 SDK context 读取 App/群/卡片消息并比对 session。同一群内其他成员可查看同一只读卡片，不绑定为只有请求人可操作；跨群或缺上下文安全拒绝。不凭客户端 value 声称权限或来源。
7. 单独只读 `/bot/inspection/page` 路径，不经过 `/retire/resolve`；对该路径请求体和响应有界，短超时（内部调用目标 1 秒），为飞书 3 秒回调预留时间。不采集、不重跑巡检、不写堆叠/删除状态、不调用设备 mutation 或发新消息。
8. 成功返回 SDK raw card，使用独立巡检 response builder；非法页仅短 toast 并保持原卡；已知过期提示“本次巡检结果已过期，请重新执行巡检。”未知 session 使用结果不可用提示，避免错误断言它必然过期。网络异常同样不换错误卡、不自动重试巡检。
9. 将 callback 注册与 Pending Delete 开关解耦：巡检 handler 可用，但 retire action 仍严格受原开关控制。不改变路由、帮助回应或删除 handler。更新“非公司模式完全不注册 callback”这一过期测试契约，同时保留非公司模式不能调用 retire 的安全断言。
10. session 快照不可变，page 是绝对目标页，同页重复点击幂等，不使用全局 current_page。并发用户共享一张卡的显示页；会话 A/B 独立。**仅加锁不足以证明飞书客户端不会乱序应用响应**：实现时验证 SDK/平台更新顺序，测试服务端并发并进行真实快速点击验收。若无法保证最新交互不被旧响应覆盖，需再确认采用串行更新/版本机制的最小补充，不能先宣称该项 PASS。

## 预计修改文件

实现获确认后预计：

- `librenms+grafana/alertmanager-feishu-bridge.py`：巡检快照编排、只读分页端点；最小拆分堆叠展示。
- `librenms+grafana/feishu_bridge/inspection_pagination.py`（新增）：分页、session、专用渲染；复用现有 `card_presentation.py`，预计无需改公共卡片样式。
- `librenms+grafana/feishu-ws-client.py`：查询来源传递、发送消息绑定、独立分页回调和注册；不改 routing 算法。
- `librenms+grafana/tests/test_inspection_pagination.py`（新增）、`test_bridge_delivery.py`、`test_feishu_ws_client.py`；必要时同步 `test_deployment_contracts.py` 的 callback 注册契约。
- 根 `README.md`、`librenms+grafana/docs/feishu-app-chat-troubleshooting.md`、本记录、`docs/STATUS.md` 及本批验收操作手册。若容器复制规则不自动包含 helper，再明确最小打包改动，不改部署流程。

本审计轮仅修改本记录、STATUS、PROJECT_CONTEXT。

## 现有测试与后续验证

现有源码覆盖（本轮未执行测试）：

| 文件（均在 librenms+grafana/tests） | 覆盖内容 |
|---|---|
| test_bridge_delivery.py | 网络汇总/离线优先/禁用过滤/旧别名；堆叠健康、角色、版本、环路、学习基线、附加卡；帮助文案；Pending 卡片及发送回退 |
| test_feishu_ws_client.py | EVENT_NAME 匹配与多实例排他、群静默、p2p 边界、HELP、消息去重、群轮询；多卡逐张发送；公司 callback 注册、token 转发、异常反馈 |
| test_bridge_pending_delete_safety.py / test_bridge_pending_delete_transaction.py | 既有删除安全与事务回归入口 |
| test_feishu_delivery.py / test_deployment_contracts.py | 投递与部署契约，含 callback 注册检查 |

尚无 inspection session、分页按钮、分页回调、TTL 或分页更新次序测试。

实现后必须新增：单页内容完整且无分页；两页首/next/prev；三页以上中间页；0/-1/越界/非整数/bool/缺失及过期 session；跨群/错误 message 绑定；A/B 会话、多线程、多用户、重复点击；容量/超时/重启失效；分页路径上设备 mutation、Pending 状态保存和重新采集一律断言零调用。

验证入口：相关 pytest 后 `python -m pytest -q` 全量，以及现有 CI 全部检查。不能用修改路由或删除旧安全断言使测试变绿。源码/模拟测试不能代替实际飞书 1/2/3+ 页、快速点击、并存会话、旧卡片和手机视觉验收。

## 本轮验证、交付与下一步

- 已执行源码和 Git 只读核对；无运行代码、测试、配置、版本文件修改；未连接公司服务器、未发飞书、未接触 Golden。
- 本轮纯文档检查：提交前检查 diff、相对链接和所引用源码路径；没有运行分页或 full suite，不能标为 TEST PASS。当前 shell PATH 未找到 python/pytest，不代表宿主没有其他可用运行时；实现阶段再定位隔离运行时。
- 初次远端查询因沙箱凭据访问失败，获准后重试成功；远端基线如上。提交/推送结果用本文件 Git 历史定位；CI 未核验不声称通过。
- 纯文档交付不需要部署或重启。历史未验证事项仍保持未验证；未取消或完成任何既有待验收项。
- CODE：未实施；TEST：未运行；SAFETY：仅审计、无运行变更；DEPLOY：未部署/本轮无需；VISUAL：未验收；STATUS：DESIGN PENDING，不可 CLOSED。
- 唯一下一步：用户确认设计及“单 App 单接收客户端”前提是否可接受；若必须覆盖多实例共享 App，先确认回调归属问题的独立方案范围。
- 交接持久化后无可调用客户端压缩工具，未执行客户端压缩。

## 2026-09-18 实施记录

用户接受“同一飞书 App 只有一个有效长连接接收客户端”作为本版本分页可靠性的验收前提，
并明确排除 Redis、数据库、callback forwarding 和 ownership routing。本轮以文档提交
`78666bf57c8ed1792dd5e511e8bc3ee219005b95` 为实施基线，未修改 Golden、VERSION、
BUILD-INFO、EVENT_NAME 路由、HELP、删除状态机、拓扑、platform-api 或页面布局。

实现结果：

- 新增 `feishu_bridge/inspection_pagination.py`。session 默认 TTL 20 分钟，最多 128 个，
  单快照最多 1 MiB，总快照最多 16 MiB；容量不足明确失败，不截断检查项、不驱逐未过期
  session。快照以确定性 JSON bytes 保存，创建后不再受调用方对象修改影响，进程重启失效。
- 开启 `INSPECTION_PAGINATION_ENABLED` 后，网络巡检单次获取 LibreNMS/Ping 数据并只运行
  一次 StackWise 评估/基线提交；设备和堆叠结果转换成有序结构化 items，每页 6 项。旧开关
  关闭时继续使用原多卡行为，Golden 默认不变。
- 初次回复只发送第一页；飞书回复成功返回 card message ID 后，WS 将 App、群、源消息及卡片
  消息 ID 绑定到 Bridge session。按钮 value 只含 `action/session_id/page`。
- 新增独立的绑定与只读分页端点。分页只读取内存快照；严格拒绝 bool、float、字符串、缺失、
  非正数和越界页码，跨群、错误卡片、错误 App 或缺失上下文均 fail closed。
- callback 注册由分页开关与 Pending Delete 开关分别控制。未知 action 不进入 retire 路径；
  retire keep/delete 在原开关关闭时仍不执行。
- 成功 callback 返回 raw card 更新原卡；失败只返回 toast。已知 TTL 到期使用指定过期文案，
  未知 session 使用“结果不可用”，Bridge/网络异常不替换原卡、不重新运行巡检。

新增测试覆盖 1/2/3+ 页、严格页码、TTL/未知 session、来源绑定、A/B 隔离、多人等价访问、
重复页幂等、容量、重启、并发绝对页读取、首次发卡绑定、独立 callback 注册及分页路径零采集/
零 StackWise 重跑/零状态写入/零设备 mutation。真实飞书快速点击响应顺序仍只在生产视觉验收中
确认；若平台会把迟到响应覆盖新响应，再单独审计最小 revision/serialization，不提前扩大架构。

部署与验收入口为 [FEISHU-UX2 部署与验收](../runbooks/feishu-ux2-acceptance.md)。提交前验证结果：

- 聚焦 pytest：186 passed，覆盖分页 store、WS 接线、Bridge delivery 和 deployment contracts。
- Bigscreen JavaScript：34 个脚本通过；全仓 JavaScript 语法检查通过。
- Python compileall、文档路径检查和 `git diff --check`：通过。
- 全量 pytest 在 Windows 本机得到 1665 passed、1 skipped、12 subtests passed；另有 31 个
  Docker 不可用导致的 Nginx 代理测试 setup error，以及 2 个可独立复现的既有 Windows 环境失败。
  本机没有 Docker，因此 Compose 和真实 Nginx 矩阵未运行，交由 Ubuntu GitHub CI 验证。
- 真实飞书桌面/手机、1/2/3+ 页、快速连续点击、并存会话及旧卡片：未运行，必须在生产部署后
  由用户按手册验收。没有用户反馈前不得标记收口。

实现已提交并普通推送：

- 实现提交：`d79b4492a4906cb95f6508b3e28d7f2bd7769346`
- GitHub CI run：`35250338126`，结论 `success`。
- CI 全部作业通过：Ubuntu Python 3.13 full pytest、Python syntax、JavaScript、ShellCheck、
  shell syntax、Compose configuration、Linux static smoke 与 Dashboard JSON。

当前状态为代码、push 与 CI 完成，尚待用户按验收手册执行部署和真实飞书桌面/手机验收；
本批在收到生产反馈前保持未收口。
