# Feishu 隔离实现提交独立审计

日期：2026-09-07。被审提交：`7ad540cc75bd955a51656c618a2511163dc8271f`，父提交 `e44ca0e84449b2ea822e67147fb15a99d1c2751a`。
状态：**隔离逻辑未发现阻断；提交整体审计未通过，禁止本轮 push**。

## 范围与基线

按用户最新状态核实，修复已经实施，不能再按旧方案状态重做。开始时本地 main 为 7ad540c、工作区及暂存区干净；实时 GitHub main 为 e44ca0e，本地领先一个实现提交。
本次审阅该提交的运行代码、测试、使用说明和部署手册；只记录发现，不修改实现/测试/部署命令，不连接生产服务器，不发送消息或执行 DELETE。
Batch 5.2 保持正式收口，P3 `/incident` 刷新延后，ISP/Golden 不审查。

## 代码与验证结论

- 空/空白 EVENT_NAME 默认返回 None；群历史入口没有开启 allow_unscoped，即使历史条目误标 p2p 也不能绕过。
- 长连接仅明确 p2p 可保留空名称兼容；非空 EVENT_NAME 仍须匹配前缀，allow_unscoped 不会跳过这一约束。
- 两个入口在 None 时均于启动命令任务前退出；原轮询 ready 抑制、去重、baseline 与卡片路径未被修改。
- 既有多实例测试分别清空实例去重状态，不会因共用去重缓存产生虚假隔离通过。
- 业务 diff 未发现本次范围内的阻断。此结论不是全仓审计，也不解除方案已经记录的共享名称唯一性/前缀歧义限制。

本轮独立验证：Windows、Python 3.12.14（可用的 bundled runtime），标准库 importlib + unittest.mock 装载被审模块，模拟线程并禁止网络、Bridge、回复；共 **188 个组合通过**：

1. 空串、空格、制表/换行名称 × 仅 @、帮助、无前缀命令、其它赛事命令 × group、unknown、None、字段缺失、p2p。
2. 群历史全部拒绝；长连接在 CHAT_TARGET 为空/有值时只允许明确 p2p；两种入口分别验证 Singapore、Shanghai、空名称的同组消息隔离。
3. 轮询 ready 时私聊继续被原入口抑制；allow_unscoped 不绕过非空名称。

Python 编译检查与 `git diff 7ad540c^ 7ad540c --check` 通过。未重跑 pytest：当前执行环境未发现上一实施轮使用的 Python 3.14，已知 bundled Python 未安装 pytest。
实施轮的 20 passed、contracts 3 passed/68 deselected 作为既有证据保留，不冒充本轮复跑。生产、Docker 实际执行及真实飞书投递未测。

## 必须修正的交付问题

以下行号均对应 **7ad540c** 的 `docs/runbooks/feishu-event-name-isolation-deployment.md`。

### R1 / P1 — 配置上下文输出不保证凭据隔离（第 36 行）

`docker compose config ... | grep -A2 -B2 EVENT_NAME` 输出的是完整渲染配置中的相邻字段，不能限定为 EVENT_NAME。服务环境包含 FEISHU_APP_SECRET 等凭据，邻近输出可能进入复制的验收日志；隐藏 stderr 不会隐藏 stdout 凭据。
修正：解析 Compose JSON，仅选定服务及非凭据字段并输出。不得打印原始 JSON 或相邻 YAML，不运行现有命令获取生产样例来验证泄漏。

### R2 / P2 — 运行文件路径错误，SHA 与隔离验证必然失败（第 67～68、81 行）

Compose 第 572 行实际挂载 `/feishu-ws-client.py`，第 582 行用该路径启动。手册 SHA 主命令、fallback 和 import 都指向不存在的 `/app/feishu-ws-client.py`；fallback 重复同一个错误路径，无法完成验收。
修正：统一为真实挂载/入口路径，固定 Compose 工作目录与服务名，移除按容器名重试的无效 fallback；不让用户临时猜路径。

### R3 / P1 — 没有验证实际执行删除逻辑的四个运行时开关（第 53～56 行及部署前检查）

四开关读取的是 feishu-ws，而该服务只注入 DEVICE_PENDING_DELETE_ENABLED；其余三个开关属于 alertmanager-feishu-bridge。grep 只匹配到一个值也会成功，不能证明删除安全。部署前只展示 .env，未断言 Compose 实际值；Bridge ready 也只 grep 历史日志，不核对当前 health payload。
修正：部署前断言 .env 和 Compose 中 Bridge 的四开关，部署后在 Bridge 容器断言四值并请求本机 `/health` 检查 `ready is True`；feishu-ws 单独核对 EVENT_NAME。缺失或错误即退出，不输出完整 env。

### R4 / P2 — 同步与部署缺少固定版本及失败停止保护（第 12～18 行，后续分段部署块）

手册直接 merge origin/main，没有锁定已审计的完整 SHA；main 后续追加提交时可能部署未经本轮审计的内容。git status 只打印，不执行分支/已跟踪差异断言；分段命令也没有统一的失败即停机制，前序失败后可能继续部署旧代码。
修正：单个 Bash 块使用失败停止语义，断言 main、已跟踪工作区/暂存区干净；fetch 后验证固定目标在远端历史中并可从 HEAD 快进，只合并该目标，部署前后核对 HEAD。保留已有 untracked 文件，冲突立即停止。比较容器实际 StartedAt，不能把 ps 中 Up 时长当成未经计算的本轮启动时间证据。

## 下一步与发布门槛

仅修正以上部署手册问题并同步 STATUS/本记录，**不重做隔离实现**。保留 7ad540c，另加普通 commit，不 amend/rebase/reset。
手册修正后核对 Compose 路径/服务/字段，并检查 Bash 及内嵌 Python 语法；未在生产执行的部分继续标记未测。
全部发现解决且复核通过后才能普通 push；此后用户按修订手册部署验收。验收后返回 pre-refactor 只读审计主线，不扩大 correctness 修复范围。

本次审计记录本地提交，不 push；62 个本地文档链接、Markdown 围栏及 `git diff --check` 检查通过。自身提交用 `git log -1 --format="%H %s" -- docs/iterations/feishu-event-name-isolation-review.md` 定位。
交接已持久化；无可调用的客户端压缩工具，未执行上下文压缩。
