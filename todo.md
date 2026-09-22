# ACTIVE TODO

更新时间：2026-09-22

## A. Platform API / Network Read Roadmap

### A-02 PLATFORM-API-P2 — Network Read API

- [x] 已完成并生产验收：新增受认证保护的只读网络接口：
  - `GET /network/overview`
  - `GET /network/devices`
  - `GET /network/topology`
  - `GET /network/isp`
- [x] 生产 SHA：`6607acf8b93a91807c9fe989ba805f5cd6ea023e`
- [x] 生产验收：devices 47、topology 15 nodes / 16 edges、ISP 5；normal / degraded / stale / partial unavailable / all unavailable / auth fail-closed 均已通过。
- [x] 部署边界确认：无设备 mutation、无 DELETE、Pending Delete / Auto Delete 配置未改变。

### A-02.1 HOTFIX — UniFi AP “新设备部署”重复通知

优先级：**高于 A-03/P3**。先解决本项，再继续前端接入。

现象证据：

- 同名 UniFi AP 在 DHCP/IP 变化后再次收到“新设备部署”卡。
- 已观察到 `lanserver` 从 `192.168.42.43` 变化为 `192.168.72.1` 后再次通知。
- 已观察到 `avl-2 / 192.168.71.138` 与后续 `avl / 192.168.71.8` 的再次部署通知。

当前审计结论：

- UniFi AP watcher 在拿到 MAC 时使用 `unifi-ap:<MAC>` 作为持久身份，正常情况下 DHCP 换 IP 不应重复通知。
- 通用 LibreNMS/SNMP online watcher 若某一轮 UniFi enrichment 未拿到 `unifi_mac`，当前 fallback identity 主要退回 hostname/IP；卡片显示名可以仍是稳定的 display/sysName，但该稳定名称没有进入 fallback identity。
- 因此 AP 换 IP + controller enrichment 短暂缺失时，新的 IP 可能被当成全新 identity，再发送“新设备部署”。
- 现有测试覆盖“MAC 可用时 DHCP 换 IP不重复”，但缺少“enrichment 暂时失败 / MAC 缺失时的跨 watcher 去重”回归场景。

待办：

- [ ] 先用生产只读诊断确认重复卡对应 AP 的 MAC、当前/历史 IP、`notified-devices.json` 和 AP inventory 是否一致；不得改生产状态。
- [ ] 修复身份策略：AP 的稳定 MAC identity 仍为首选；MAC 暂时不可得时，fallback 必须能与既有稳定 AP identity/名称安全关联，不能只靠变化中的 IP。
- [ ] 避免把普通非 AP 设备仅凭相同 display 名错误合并；名称 fallback 必须有明确设备类型/唯一匹配约束。
- [ ] 补回归测试：
  - 同一 MAC、不同 DHCP IP，只通知一次；
  - controller enrichment 暂时失败后恢复，不重复；
  - AP watcher 与 LibreNMS watcher 交叉观察同一 AP，不重复；
  - Bridge 重启后仍不重复；
  - 同名但不同 MAC 的两台 AP 不得被错误合并；
  - 真正新 MAC 的 AP 必须仍发送一次新设备部署。
- [ ] 验证 `bridge-state/notified-devices.json` 持久化不因正常 deploy/restart 丢失。
- [ ] focused tests + full relevant regression + CI + committed code audit。
- [ ] 公司服务器只更新必要 Bridge 代码并最小化重启；部署命令由 Agent 一次性给出，包含备份/停止条件/回滚。
- [ ] 生产验收不得通过真实删除/设备 mutation 制造场景；优先用隔离 fixture 验证 DHCP/IP 变化和双 watcher 去重。
- [ ] Hotfix 生产稳定后再继续 A-03/P3。

### A-03 PLATFORM-API-P3 — 前端 / 控制台接入统一 Network Read API

- [ ] 先设计并审计现有前端网络数据读取路径，列出哪些页面/组件仍直接读取 Prometheus、topology 文件、ISP discovery 或其他旧接口。
- [ ] 控制台/网络页优先接入 `/network/overview`，统一展示：
  - normal
  - degraded
  - stale
  - unavailable
- [ ] 详情区域分别接入：
  - `/network/devices`
  - `/network/topology`
  - `/network/isp`
- [ ] 第一阶段保留现有旧读取路径作为 fallback；不得一开始就删除旧路径。
- [ ] 前端必须明确区分：
  - 数据正常
  - 数据可用但 degraded
  - snapshot stale
  - 单域 unavailable
  - 全域 unavailable
- [ ] 不把 `unknown` 自动显示为 down/0；不得因单个域失败让整个网络页失效。
- [ ] P3 必须补齐前端/接口契约测试、CI、committed code audit、公司服务器部署和生产网页验收后才能收口。
- [ ] P3 范围禁止顺带修改设备写操作、DELETE、Pending Delete、Auto Delete、Bridge destructive logic。

### A-04 PLATFORM-API-P4 — 新 API 稳定后切换并清理旧读链路

前提：A-03 已生产运行稳定，并确认 fallback 没有实际依赖。

- [ ] 统计并确认前端仍在使用的 legacy 网络读取路径。
- [ ] 对比新旧数据结果，确认设备、拓扑、ISP 关键字段和异常语义无回归。
- [ ] 将新 Network Read API 设为唯一主读取路径。
- [ ] 分批删除仅用于前端重复读取的 legacy glue / duplicate fetch 逻辑；不得删除仍被 collector、generator、告警或运维脚本使用的数据源。
- [ ] 清理后继续保留 fail-closed auth、bounded reads、degraded/stale/unavailable 语义。
- [ ] 完成 full regression、CI、committed code audit、生产部署及回归验收后收口。

### A-05 PLATFORM-API-P5 — Inspector / Port 数据能力评估与按需扩展

前提：A-03/A-04 收口后再开始；先执行下方 T-01 数据源审计，不直接新增 polling。

- [ ] 复用 T-01 审计结果，确认现有 LibreNMS / Prometheus / SNMP Exporter / topology 数据是否足以支持 Node Inspector 和 Port Panel。
- [ ] 只有现有统一 Network Read API 无法表达必要只读字段时，才设计新的 bounded read-only API。
- [ ] 优先实现 T-02 Lightweight Node Inspector，再评估 T-03 Cisco Full Port Panel、T-04 Hillstone Inspector、T-05 UniFi AP integration。
- [ ] 禁止为 Inspector 新增第二套高频全量 SNMP polling。
- [ ] Link Inspector 继续保持 T-06 OPTIONAL / LOW PRIORITY。

### A 系列固定边界

- 后续方案由 Agent 先写设计、范围、测试、部署和验收标准；实现者再写代码。
- 每个阶段完成顺序固定为：方案 → 实现 → 测试 → push → CI → committed code audit → 公司服务器部署 → 生产验收 → 收口。
- 生产部署和检查命令由 Agent 一次性给完整版本，包含停止条件与回滚；不拆成多批临时命令。
- 不执行真实设备 mutation 或 DELETE 来做验收。
- 不为了制造 degraded/stale/unavailable 去破坏真实生产数据源；故障契约优先使用隔离 fixture / read-only 验证。
- 新 API 优先复用现有数据采集链路，不制造重复采集系统。

## B. 现场复验

- [ ] **B-03 飞书应用现场复验**：部署后发送一次测试告警，目标为 `channel=app`、`appChatResolved=true`、`appError` 为空；失败时按 `docs/feishu-app-chat-troubleshooting.md` 的业务错误处理。

## C. 真实现场故障注入

- [ ] **C-01 防火墙单机掉线/恢复**：分别拔一台物理防火墙，确认只报物理机，VIP 不误报。
- [ ] **C-02 HA 切换**：分别对实际使用的 Hillstone/WatchGuard HA 做主备切换，确认不产生虚假超大带宽。
- [ ] **C-03 ISP 采集中断**：中断 SNMP 或制造接口名不匹配两分钟，确认收到中断及恢复卡。
- [ ] **C-04 完整告警回归**：新设备、设备离线/恢复、端口/聚合链路、风暴、LibreNMS 转发和 48 小时退役确认各测一次。
- [ ] **C-05 干净 Linux 部署恢复**：从仓库全新构建，验证 Docker 重启、配置/私密设置/状态保留，并完成一次离线包安装。
- [ ] **C-06 运行数据恢复演练**：执行备份—重装—恢复并记录恢复时间与校验结果。

## D. 后续维护

- [ ] **D-04 ShellCheck 收紧**：逐步清理现存 warning，再把 CI 从 error 提高到 warning。
- [ ] **D-05 应用/树莓派机器人状态**：业务接口明确后再接入。
- [ ] **D-06 组播业务检查**：只有项目需要时才启用，并先评估老交换机 CPU。

## T. Topology Backlog

### T-01 Port Panel / Inspector 数据源审计

- [ ] 审计现有 LibreNMS、Prometheus、SNMP Exporter、topology generator 和已有 API，确认是否已有：
  - `ifName`
  - `ifDescr`
  - `ifAlias` / description
  - `ifAdminStatus`
  - `ifOperStatus`
  - speed / `ifHighSpeed`
  - RX/TX
  - utilization
  - errors
  - discards
  - access VLAN
  - trunk/native/allowed VLAN
  - stack member
  - LAG members
  - LLDP/CDP neighbor
  - Hillstone HA state
  - UniFi Controller/AP data

原则：优先复用已有监控数据，不得因为 Port Panel 新增第二套高频全量 SNMP polling。

### T-02 Lightweight Node Inspector

- [ ] 保持现有 topology 不变，点击节点后在右侧显示轻量详情。

Cisco：

- hostname
- management IP
- model
- online/latency
- port up/down summary
- uplink summary
- warnings
- 查看端口入口

Hillstone：

- management IP
- online
- HA state
- interface summary

UniFi AP：

- IP
- online
- uplink
- clients/radio summary

### T-03 Cisco Full Port Panel

- [ ] 显示所有端口，包括 DOWN。每个端口目标字段：
  - `ifName`
  - `ifAlias` / description
  - stack member
  - port number
  - admin state
  - oper state
  - speed
  - RX
  - TX
  - RX utilization
  - TX utilization
  - input/output errors
  - input/output discards
  - access VLAN
  - trunk/native/allowed VLAN（数据可获得时）

Cisco Stack 可以按 `Gi1/0/x`、`Gi2/0/x` 的 member 做 UI 分组，但只是 presentation，不新增 topology relation。

点击端口以后才按需加载历史 traffic。

### T-04 Hillstone Inspector

- [ ] 只提供：
  - interface
  - up/down
  - traffic
  - IP
  - HA state

不扩展：

- policy
- NAT
- session
- IPS
- security policy

### T-05 UniFi AP topology integration

- [ ] 允许 AP 进入现有单 topology，关系为：

  ```text
  Access Switch
  ->
  AP
  ```

无线客户端不进入 topology。

提供 `Show APs`；开关状态可以持久化并记住上次选择。

优先研究 UniFi Controller API，避免无意义增加 AP SNMP polling。

### T-06 Link Inspector — OPTIONAL / LOW PRIORITY

- [ ] 当前 topology 已经直接显示双端 physical port labels，因此 Link Inspector 暂时不是优先功能。只有以后能提供额外价值时再实现，例如：
  - link traffic
  - errors/discards
  - VLAN
  - LAG metadata
  - evidence/stale

不要为了重复展示两端端口而实现。

## 固定架构约束

- 保留现有单 topology。
- 不恢复 Operations | Physical。
- 不新增第二张 Physical View。
- Phase 1 / Phase 2 保持冻结。
- 主画布 LAG label 优先 physical member ports。
- Po/Port-channel 是 aggregate metadata，只在没有可信 members 时作为 fallback。
- 不猜测 LAG member pairing。
- 不新增高频全量交换机 SNMP polling。
- Port Panel 优先复用 LibreNMS / Prometheus 已采集数据。
- 无线客户端不进入 topology。
- Snapshot 当前不做。
- generic Service Discovery 当前不做。
- ELK 当前不引入。
