# ACTIVE TODO

更新时间：2026-09-01

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
