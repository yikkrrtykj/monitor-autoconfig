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

### A-02.1 HOTFIX — UniFi AP 按 MAC 绑定“新设备部署”生命周期

优先级：**高于 A-03/P3**。先解决本项，再继续前端接入。

已确认现象与原因：

- 飞书持续出现 UniFi AP 的“新设备部署”卡；截图中的设备型号均为 UAP/U6 系列。
- 生产只读诊断确认 `UNIFI_AP_SNMP_AUTO_ADD=true`、`DEVICE_ONLINE_FROM_PING=false`。
- `/bridge-state/notified-devices.json` 正常存在，已有 84 个 identity；截图中的 6 个 IP 都已写入 ledger。
- `unifi-ap-inventory.json` 已按 `unifi-ap:<MAC>` 保存 AP 稳定身份。
- 当前 AP watcher 与通用 LibreNMS device-online watcher 都存在“新设备部署”发送路径；如果某一轮只拿到 IP/hostname，而没有稳定 MAC identity，就可能把同一物理 AP 的新 IP 当成新设备。

正式目标行为：

- [ ] UniFi AP 的**唯一物理设备身份**固定为规范化 MAC：`unifi-ap:<12位小写十六进制MAC>`。
- [ ] IP、hostname、display name、sysName、model 都是该 MAC 下的可变 metadata，不参与判断“是不是新设备”。
- [ ] 同一 MAC 第一次被确认在线并纳入监控时，最多发送 **1 次**“新设备部署”。
- [ ] 同一 MAC 后续发生 DHCP/IP 变化、名称变化、型号信息晚到、LibreNMS hostname 改写、Bridge 重启、Controller cache 刷新，都不得再次发送“新设备部署”。
- [ ] 新的物理 AP（新 MAC）仍应发送 **1 次**“新设备部署”。
- [ ] 同名但不同 MAC 的两台 AP 必须视为两台设备，各自最多发送 1 次。
- [ ] AP 掉线/恢复状态也以 MAC 为主键；IP/name 只用于卡片展示，IP 改变不能导致旧 outage 丢失或产生第二套状态。
- [ ] AP 继续自动加入 LibreNMS，保持 `UNIFI_AP_SNMP_AUTO_ADD=true`。
- [ ] AP 的 Ping / Controller / SNMP / 名称同步 / IP 迁移 / 掉线恢复能力全部保留。
- [ ] 普通交换机、防火墙等非 AP SNMP 设备维持现有 identity 和“新设备部署”行为，不受本 Hotfix 影响。

跨 watcher 统一要求：

- [ ] AP watcher 和通用 LibreNMS device-online watcher 必须共享同一个 MAC lifecycle identity，不能各自按 IP 再发一遍。
- [ ] 当 LibreNMS watcher 能从 Controller 或 AP inventory 唯一解析出 MAC 时，必须先转换成 `unifi-ap:<MAC>` 再做 notified 判断。
- [ ] Controller 临时不可用时，应优先用持久化的 `unifi-ap-inventory.json` 做“当前/历史 IP → MAC”映射；不能直接把新 IP 当作新 AP。
- [ ] 只有在没有任何可信 MAC 证据时，才允许把设备暂时视为“identity unresolved”；此时**宁可暂缓新设备通知，也不能凭 IP 立即发部署卡**。
- [ ] 禁止仅依靠型号字符串（如 `U6` / `UAP`）推断 AP 身份，以免误判普通设备。
- [ ] `notified-devices.json` 对 AP 的长期 canonical key 应为 `unifi-ap:<MAC>`；历史 IP/name key只作为兼容迁移证据，不作为未来主 identity。
- [ ] 不清空生产 `notified-devices.json`；旧 IP/name identity 应在确认到 MAC 时迁移/补记 canonical MAC，而不是重置 ledger。

生命周期边界：

- [ ] 默认情况下，同一物理 MAC 永远视为同一 AP，不因 DHCP、重启、重新加入 LibreNMS而创建“新设备生命周期”。
- [ ] 只有明确的人工生命周期重置/确认退役逻辑将来如需支持，必须单独设计；本 Hotfix 不因为普通掉线、自动重加或 IP 变化自动 reset MAC identity。

回归测试至少覆盖：

- [ ] 新 MAC AP 首次纳管 → “新设备部署”恰好 1 次。
- [ ] 同一 MAC、不同 DHCP IP → 总计仍只有 1 次。
- [ ] 同一 MAC、名称变化 → 不重复。
- [ ] Controller enrichment 暂时失败，但 AP inventory 已知旧/新 IP 与 MAC → 不重复。
- [ ] Controller 与 inventory 均暂时无法确认 MAC → 暂缓部署卡；恢复 MAC 后按 canonical ledger 决定是否需要首发，不得先按 IP误发。
- [ ] AP watcher 与 LibreNMS watcher 并发/交叉观察同一 MAC → 总计最多 1 次。
- [ ] Bridge 重启后同一 MAC → 不重复。
- [ ] 同名、不同 MAC 两台 AP → 各发 1 次。
- [ ] 新 IP + 新 MAC → 视为新 AP，发 1 次。
- [ ] AP 掉线/恢复跨 IP 变化仍沿用同一 MAC state。
- [ ] 真正新的非 AP SNMP 设备 → 原有“新设备部署”仍恰好 1 次。
- [ ] 不修改 Pending Delete / Auto Delete / DELETE 行为。

交付与生产边界：

- [ ] focused tests + relevant full regression + CI + committed code audit。
- [ ] 公司服务器只更新必要 Bridge 代码并最小化重启；部署命令由 Agent 一次性给出，包含备份、停止条件和回滚。
- [ ] 生产验收不得通过真实删除或设备 mutation 制造场景；MAC/IP 变化、双 watcher 去重使用隔离 fixture 验证。
- [ ] 生产只读验收确认 `notified-devices.json`、AP inventory 和当前 MAC/IP 映射正常，且不清空现有 ledger。
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
