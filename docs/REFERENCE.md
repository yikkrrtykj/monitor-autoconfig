# 功能与配置详细参考

日常安装和使用从 [README](../README.md) 开始。这里保留配置字段、告警行为、采集限制和维护细节，按需要查阅即可。已安装的公司服务器更新见[公司服务器发布与验收](runbooks/company-deployment.md)。

## 账号和访问

控制台、Grafana 和 LibreNMS 的默认用户名均为 `admin`，默认密码均为 `global123!@#`。可以在 `.env` 分别修改 `PLATFORM_ADMIN_PASSWORD`、`GRAFANA_PASSWORD` 和 `LIBRENMS_ADMIN_PASSWORD`；`SNMP_COMMUNITY` 是单独的设备采集凭据。这些入口供受信任内网使用。

## 告警和配置保护

UniFi AP 的部署通知与 AP 掉线/恢复以 MAC 作为物理身份；同 MAC 换 IP、改名或 Bridge 重启不会创建新的部署通知生命周期。Controller 不可用时使用持久 AP inventory；已知 AP 但 MAC 无法唯一确定时暂缓通知。历史通知记录保留并按唯一归属迁移，新 MAC 仍正常通知。A-02.1 的升级、兼容边界及验收见 [A-02.1 历史交付与验收记录](runbooks/a-02.1-unifi-mac-hotfix.md)。

链路聚合告警仅在聚合及成员状态明确恢复时发送恢复通知。采集数据消失、接口被关闭或聚合配置被移除时保留已有故障状态及起点，不把缺失数据当作恢复。

已有登录凭据文件损坏或超限时，登录与需要认证的接口明确失败并保留原文件，不自动恢复默认密码。配置回滚先检查快照的文件存在标记及全部所需备份；快照不完整时停止，不删除当前配置或先恢复其中一份。

## 控制台

打开：

```text
http://服务器IP:8088/control
```

主要区域：

| 区域 | 用途 |
|---|---|
| 需要关注 | 只列出当前异常或缺失项，不再显示分数 |
| 基础配置 | 直接修改赛制、VLAN、SNMP、设备、ISP、UniFi、飞书等配置 |
| 配置中心 | 当前赛制、网络、平台 API、选手探测目标等只读状态 |
| 拓扑诊断 | 检查核心、防火墙、接入交换机、LLDP 边 |
| 事故流转 / 事故库 | 记录事故、恢复时间和复盘线索 |
| 赛前工具 | 执行赛前体检和发送测试告警 |
| 交换机配置巡检 | 粘贴 Cisco `show run` 片段，检查现场风险 |
| 核心交换机 Telnet | 填写登录信息并执行只读连接测试；密码不随赛事配置导出 |

已登录的控制台与 `/topology` 只使用统一 Network Read API 读取设备、拓扑边和 ISP 清单；API 不可用时显示异常，不用旧清单补成正常结果。匿名 `/topology` 仍使用兼容读取。仅在数据降级、快照陈旧或不可用时显示简短异常提示；正常和匿名浏览不显示内部数据源状态。拓扑探测暂时失败时保留最近成功的节点结构，旧延迟不会继续显示，未获当前 API 状态覆盖的节点显示为未知；首次探测失败会提示拓扑不可用。已登录拓扑的 API 边读取失败时仅保留此前成功的 API 边并提示不可用，首次失败不会显示为正常的零链路。设备状态未知时不会显示为离线；趋势和 ISP 流量图仍读取 Prometheus。拓扑缓存边只在“对端消失或链路 down”时保留（默认 24 小时，用于故障留证）；同一端口被观测到已改接其它设备时旧边当轮失效，即使新对端端口暂时无法解析、只能画出部分端点，也不再显示为“已省略过期邻接”。失效判断只使用该设备本机的邻居观察：对端通告过来的端口名不当作那台设备自身端口的证据。

已登录的 Network Read API 提供只读 `GET /network/ap-placement`（AP 影子定位，不驱动拓扑连线）：服务端读取 `topology-data` 中的 `ap-placement.json`，校验 schema、`valid`、生成时间、AP 身份唯一性、原始 counts 与 placements 一致性；文件级异常返回 503（`ap_placement_unavailable` / `ap_placement_malformed` / `ap_placement_schema_unsupported` / `ap_placement_invalidated` / `ap_placement_stale` / `ap_placement_oversize`），不返回旧位置。当前定位（confirmed-direct / observed-attachment）必须同时具备 `evidence.fdb.status=matched`、`evidence.exact_snmp.status=matched`、`evidence.port.verified=true`，且 `evidence.online` 与顶层 online 不冲突；confirmed-direct 还必须具备 `manual_confirmation.status=confirmed` 与 `direct_neighbor.status=operator-attested/source=manual-allowlist`，且 `evidence.online.status` 必须存在并与顶层 online 严格一致，否则降级为 unresolved 并清空当前连接字段。逐 AP 的 exact 证据年龄按 `evidence_age_seconds + max(0, now - generated_at)` 并参考 `validated_at` 保守换算；`validated_at` 必须存在、非超前，且与 `generated_at - evidence_age_seconds` 在 5 秒时钟偏差内一致，缺失/超前/显著矛盾同样降级；超过 `max_age_seconds` 或字段异常时仅把该 AP 降级为 `unresolved`、清空当前位置并在 warnings 说明；原始 counts 校验后按最终 placements 重算。响应只包含白名单字段，不返回凭据、客户端 MAC 历史或文件路径；未登录 401、认证关闭 503、成功响应 `Cache-Control: no-store`。匿名 kiosk 不读取该 API。

`/infra` 是独立网络总览，包含基础设施与 ISP 摘要、WAN 卡片、设备故障和网关状态表。网关表分内外网：内网按核心、防火墙 VIP、已有防火墙物理成员探测排列，外网列 ISP 网关。物理成员各有独立状态和 Ping 历史入口。普通分线仍可在设备故障区及 `/ping` 查询。已确认 DOWN 以逐设备长条显示，质量异常和探测未知分开呈现。总览健康判断覆盖基础设施和 ISP，AP 状态通过无线总览查看。`FIREWALL_PING` 配置的每个地址都需要对应的 `infra-fw-ping` 目标。目标即使超出历史清单与到期提示窗口，仍会提示覆盖不足；清单中仍存在但无可信样本的目标显示 UNKNOWN。留空时不要求 FW VIP 覆盖。

目标身份、ICMP 四态与统计复用 `/ping`。总览状态每 5 秒刷新，30 分钟目标清单缓存最多 30 秒，最多 12 个网关的 5 分钟 STDDEV 每 30 秒查询。离开页面时取消请求和定时器。目标超出 30 分钟观察窗口后移出清单，随后约 5 分钟显示覆盖提示；缓存可能使清单变化延后最多 30 秒。`/ping` 保留 AP ICMP 分组及设备历史、P95、Max 和 CSV。选手延迟从比赛座位进入 `/latency`，不参加 `/ping` 和 `/infra` 的统计。旧卡顿分析页 `/incident` 已退役，控制台事故记录继续可用。

WAN 从现有 `isp_targets.json` 和 `isp-discovery-state.json` 读取已发布接口身份，发现状态超过 300 秒或异常时提示不可用；安全浏览器环境另核对发布文件 SHA-256。手工 ISP 名称通过现有 IF-MIB 唯一绑定到接口。流量按管理地址与 ifIndex 精确对应，每 15 秒读取 15 分钟历史，入站/出站沿用 WAN 接口收发方向；抓取失败、样本超过 35 秒、接口歧义或计数重置显示 N/A。只有名称唯一且明确写入 `BIGSCREEN_ISP_MAX_BANDWIDTH` 的逐线路容量显示利用率，缺省全局 1000 Mbps 继续用于既有赛事图表。当前 VLAN 区域显示来源待核实；SVI 计数和 trunk 汇总需要单独确认计量范围。

每张 WAN 卡片分别显示流量与探测关联。探测依据已发布的 gateway 地址，或目标标签中明确的 `metric_target + metric_ifindex` 接口绑定。手动 `ISP_PING` 可按相同地址规则匹配已发布的 gateway；若多个不同 WAN 接口共用该地址，未绑定接口的同一探针不能重复作为覆盖证据，各卡片显示关联歧义，需各自唯一的 canonical 接口绑定探针才能确认覆盖。只有同名信息时，关联状态为待核实。WAN 未提供 gateway、未匹配探测或关联存在歧义时，页面提示覆盖不足并保留有效流量。已确认的 DOWN/DEGRADED 与数据域不可用同时发生时，状态标题并列显示网络异常和监控可用性。

拓扑的“AP: 显示 / 隐藏”默认隐藏，手动选择仅在当前浏览器会话中保存；刷新和页面切换不会重新打开。AP 身份与运行状态沿用 unpoller，LibreNMS FDB 只提供历史候选，只有当前 exact MAC/VLAN SNMP GET 唯一确认后才显示接入交换机叶节点，并显示“AP 已定位数/总数”。缺失、歧义、过期证据不连线。`TOPOLOGY_AP_FDB_CANDIDATE_MAX_AGE_SECONDS` 默认 28800 秒（0–28800），每 AP `TOPOLOGY_AP_FDB_CANDIDATE_CAP` 默认上限 8（0–8；超限整台不定位）。内部 LibreNMS `vlan_id` 按设备 FK 映射为真实 VLAN 后才验证；同交换机移位须重新检查端口，跨交换机移位等待新候选。`TOPOLOGY_AP_FDB_MAX_AGE_SECONDS` 默认 7200 秒（非负整数），用于当前 exact 验证证据在输出和浏览器中的有效期，独立于服务器 FDB 的 900 秒窗口；`ap-attachments.json` 每轮覆盖，无旧所有权和 24 小时保留，AP 验证仅使用有界 GET，计入独立 `ap_snmp_gets` / `ap_snmp_walks`（后者必须为 0）；不新增 FDB walk 或改变既有 SNMP/发现频率。

collector 每轮另写独立的影子文件 `ap-placement.json`（P1，仅供离线比对，不驱动连线、告警或自动删除）。每台 AP 输出 `online` / `offline` / `unknown` 与 `confirmed-direct` / `observed-attachment` / `ambiguous` / `unresolved` 四级定位证据、`coverage_status` 和 `reason`；`observed-attachment` 仅表示当前 FDB + 精确 GET 验证的“朝向 AP 的转发出口（egress toward AP）”，不宣称物理直连，FDB 采集不完整（`coverage_status` 为 partial/unknown）时不会提升等级。在线状态只使用 unpoller v2.15.3 真实导出的 `unpoller_device_info`（含 MAC）与 `unpoller_device_uptime_seconds`（仅 type/site_name/name/source，无 MAC）：uptime 是历史运行时长而不是当前心跳，因此不能据此判 online，P1 保持 unknown 并记录 source/reason；查询失败、样本缺失或过期同样为 unknown。P1 的 `confirmed-direct` 只来自 `TOPOLOGY_CORE_AP_PORTS` 人工确认的核心口（operator-attested）、当前精确 GET，且要求 AP 当前 `online`；`online=unknown` 最多为 observed，`online=offline` 不产生当前定位，只在 `last_observed` 保留历史证据，未来 UI 不得把旧位置画成当前实线。顶层 `evidence_age_seconds` 与正式 AP 定位一致，表示本轮精确 GET 的 `now-validated_at` 年龄；FDB 候选年龄独立保留在 `evidence.fdb.candidate_age_seconds`，`last_observed` 同样区分精确验证年龄与候选年龄。AP 身份清单为空、身份查询失败或没有拓扑采集设备时，collector 必须写入显式失效快照（`valid=false`、`invalid_reason`、零计数、空 placements），不得保留上一轮文件冒充当前快照。影子核心候选的额外查询受两层预算限制：`TOPOLOGY_AP_SHADOW_MAX_LOOKUPS`（高层尝试，0–64，默认 16；异常也计数）与 `TOPOLOGY_AP_SHADOW_MAX_GETS`（底层真实 SNMP GET，0–256，默认 32；lookup 内逐次预留），任一为 0 时不会产生 shadow SNMP 请求。覆盖率按预期设备、实际轮询成功/FDB 成功统计，缺采集或 FDB API 失败时为 partial/unknown；该文件不改变 `ap-attachments.json`、大屏现有连线、`edges.json`、核心白名单门禁、服务器 FDB 或自动删除安全开关。

无线页新增独立的“AP 定位”区域（P2-B B1），只读展示上述影子快照，不绘制任何新拓扑连线。该区域由 `BIGSCREEN_AP_PLACEMENT_ENABLED` 控制，默认 `false`，经 bigscreen entrypoint 的 Base64 配置注入生成 `config.js` 的 `apPlacementEnabled`；关闭时不请求 API、不启动定时器、区域保持隐藏。开启后仅已登录用户可见：前端通过现有同源会话调用受认证的 `GET /platform-api/network/ap-placement`，匿名 kiosk 隐藏且不发请求；注销、401/403 或会话失效时立即清空旧位置，不保留本地缓存。首次进入立即读取，之后每 30 秒刷新，离开无线页停止；同一时刻只允许一个请求，旧响应不得覆盖新状态，页面隐藏后恢复会重新验证时效。列表展示 AP 名称、管理 IP、四级定位等级、当前交换机/端口/VLAN、online（仅 API 的 online/offline/unknown；不复用 unpoller uptime 或 ICMP 推断；ICMP 显示“未接入（#140）”）、Exact 验证年龄、FDB 候选年龄、coverage 和 reason；支持搜索、等级筛选与按需展开的证据详情。`observed-attachment` 不得显示为已确认直连，`unresolved` 不展示当前位置，`last_observed` 只在折叠的“历史（非当前）”中展示。API 503、超时、malformed 或 `valid=false`（服务端返回 503）时清空列表并显示安全错误码；单条降级只影响该 AP，不使用旧数据冒充当前。B1 不关联 `ap-attachments.json` 生成正式定位徽标，也不改正式拓扑、采集器或删除安全开关。

AP 使用 52 px 圆形叶节点，按已验证父交换机分组并使用避开其他节点的直角分支。上联端口显示在圆形上方；仅名称显示在下方，RTT、真实 VLAN、型号和客户端数保留在 Inspector；AP Inspector 仅展示 Hostname、管理 IP、型号、状态、延迟、VLAN、上联端口和客户端，不展示泛用上联/无线电/端口/连接摘要。单 AP 在父交换机下方居中，安全通道内使用直线，无总线；有真实障碍时使用最短安全直角路线，无安全路线则不绘制。AP 开关固定有线图的坐标、路线、屏幕缩放和位置，仅扩展可滚动空间。独立可视化 ICMP job `infra-ap-ping` 每 5 秒通过现有 blackbox ICMP 模块读取 RTT：collector 将同一轮当前唯一 UniFi 管理 IP 原子写入 `ap-ping-targets.json`（最多 512 个，查询失败清空，不保留旧目标）；Prometheus file_sd 每 60 秒读取。前端按精确 `target_ip` 关联成功的 RTT，缺失/重复/失败保持无延迟，不覆盖 UniFi 在线状态。该 job 不进入 `DEVICE_DOWN_JOBS`，不改变 AP 告警，不增加 SNMP。

拓扑中的核心/接入交换机及未确认类型设备卡片仅显示主名称与状态图标，管理 IP、延迟等资料保留在 Inspector。AP 上联标签复用有线接口标签的字体与样式；分支总线仅覆盖实际子节点锚点，单 AP 行不绘制横向总线。拓扑页的提示条与工具栏使用同一 header，窄屏保持有界 canvas，AP 内容不会参与有线图的容器尺寸计算。

已登录时，点击拓扑中的交换机或防火墙节点可打开右侧轻量详情，显示单节点身份、在线与延迟、端口状态汇总及已发现邻居；UniFi AP 从无线页 AP 卡片或拓扑中已定位的 AP 节点打开同一个详情。详情由经过认证的单节点只读 API 提供，局部数据缺失会标记未知并提示。Hillstone HA 只使用 `sysHAStatus.0`（`1.3.6.1.4.1.28557.2.2.1.8.0`）作为权威：独立 `infra-fw-ha-snmp` 每 60 秒对 `FIREWALL_UNIT_SNMP_TARGETS` 中每台物理机读取一个 scalar，重试 0 次、超时 2 秒，不走接口表。API 精确匹配 `target_ip`，采集时间年龄须在 0–180 秒；缺失、过期、重复或未支持枚举显示未知。0/1/2/3/4/6 分别为 none/init/hello/backup/master/AA-mode；5 保留厂商 `slase` 为未知，不猜测角色。逻辑 VIP 不继承物理机角色；未配置物理目标时 HA 不可用。选中物理机时，“HA 角色”仅展示本机状态，“HA 对端”单独展示其他物理单元；逻辑 VIP 使用“HA 集群”展示物理单元摘要，不赋予 VIP 物理角色。HA 长文本可换行，其他详情行保留紧凑省略样式。完整接口流量与历史继续通过“查看接口”按需打开；不提供策略、NAT、会话或 IPS 数据。匿名拓扑仍保留原有节点详情。

已登录的 Cisco IOS / IOS-XE 等受支持系统的交换机详情可通过“查看端口”打开端口面板。面板按堆叠成员、接口类型和槽位排列可识别的物理端口；逻辑或无法安全识别的接口放在默认折叠的“其他接口”中。悬浮、键盘聚焦和点击可查看当前端口数据。无采集覆盖的指标显示“—”及来源提示；VLAN 仅是观测资料，错误和丢弃数是累计计数，不等于当前故障。面板只在打开时读取一次端口 API；悬浮和聚焦不读取历史。点击固定端口或接口详情时，向现有 Prometheus 查询最近 15 分钟的 RX/TX（30 秒步长，每次选择两个有超时的查询），不自动刷新。历史覆盖独立于当前值来源；任一方向有可用 Prometheus 历史时保留该来源，不混用方向。双向均无可用历史或 Prometheus 查询失败时，再读取一次经鉴权的 LibreNMS RRD 历史（原存储步长，不插值）；标题标明来源。空覆盖、单方向缺失或查询失败保留当前详情，不使用 LibreNMS 当前值补造历史。platform-api 使用 rrdtool 经现有 rrdcached 刷新后只读获取同设备、同 ifIndex 对应的 port RRD，不新增 SNMP job、target 或轮询。

已登录的 Hillstone 防火墙详情可通过“查看接口”打开接口列表。列表保留 LibreNMS 中的全部接口，优先使用现有 `firewall-snmp` 中与该管理 IP 精确匹配的新鲜 IF-MIB 数据。只有 ISP inventory 的目标 IP 与 ifIndex 均精确、唯一匹配时，接口才进入 WAN / ISP 区域。接口流量不用于推断 HA 主备角色，接口列表也不模拟 Cisco 物理面板。

Cisco Small Business（`ciscosb`，兼容旧 `cisco-access`）详情使用“查看接口”和通用交换机接口列表，不模拟堆叠物理布局；其他未支持的 OS 不默认为 Cisco，也不显示详细接口按钮。型号缺失显示“—”。Cisco / Hillstone / Small Business 的接口没有精确 Prometheus 当前指标时，使用同一次 LibreNMS 端口读取中的 poller 统计作为备用：仅接受数字 epoch `poll_time`，年龄须为 0–600 秒（现有约 5 分钟轮询的两个周期）；字节速率乘 8 转为 bps，备用累计错误保留累计语义；丢弃计数只使用 Prometheus，LibreNMS 备用丢弃值保持“—”（device-ports API 不读取 statistics 表）。利用率须有可信速率。缺失、过期或无效数据保持“—”并提示，界面标明 Prometheus / LibreNMS poller 来源与可用的 poller 年龄。数据不跨设备或 HA 节点借用；本功能不增加 SNMP 目标、任务或采集频率。

### 自定义 Ping 地址策略

管理员在 `/ping` 新建目标、改 IP 或恢复探测时，平台校验 IPv4。未设置或留空 `PLATFORM_CUSTOM_PING_ALLOWED_CIDRS` 时，允许符合安全策略的公网单播和 RFC1918 地址。特殊地址（含回环、链路本地、组播、共享、文档及基准测试网段）、已知云平台服务地址、选手网段及重复监控目标仍会被拒绝。Prometheus 当前目标清单无法确认时，相关写入返回 HTTP 503。

需要限制可探测范围时，在部署目录的 `.env` 填写以逗号分隔的 IPv4 CIDR。无效项或空条目使相关写入返回 HTTP 503。地址按最长匹配前缀检查网络地址和定向广播；`/31` 和 `/32` 可用于点对点及单主机。未提供实际网段掩码时，平台无法从目标 IPv4 单独判断定向广播。修改环境变量后重建 `platform-api` 容器生效。

升级保留已有清单和历史回执。收紧策略后，如需停止已在探测的自定义目标，请在管理界面停用该目标；恢复时会重新检查当前策略。

### 自定义 Ping 域名后端

域名后端沿用 /platform-api/ping/targets 管理路径。旧的 {name, ip, group} 输入保持兼容，新输入可用 {name, kind: "ipv4", address, group} 或 {name, kind: "dns", address, group, privateCidrs: []}。域名按 IDNA、小写和去末尾点规范化，最多 253 个 ASCII 字符。名称沿用 80 字符限制。只接受合格主机名及 A 记录，查询始终使用绝对域名和系统配置的普通 DNS 服务器。支持公网域名及普通 DNS 可解析的内网 FQDN，拒绝单标签名称、.local 和 .localhost；同一限制也用于 CNAME 目标。保存成功后可以是 DNS_PENDING，配置持久化和 DNS 成功独立。

首次成功写入 DNS 配置时清单升级为 Schema 2。原 IPv4 行、UUID、revision、旧回执和幂等摘要保留，IPv4 API 与只读 Schema 1 保持兼容。DNS 行增加 kind、address、privateCidrs 和仅在地址、授权范围或启停变化时更新的 endpointRevision。损坏或未知 Schema 明确失败。DNS 运行状态在同一私有目录的 custom-ping-dns.json 中独立原子保存，自动刷新不修改管理员 revision。清单、运行状态和 file_sd 通过同一锁与投影发布，跨卷失败显示 pending 并由 reconciliation 重试。

每个启用域名最多发布一个实际 IPv4。保留仍在合规 A 集合内的当前地址，否则按 IPv4 数值从小到大选址。每次结果重新检查 D1 地址策略、玩家/无线网段、跨来源目标及自定义目标冲突。私网解析要求该域名的 privateCidrs 明确授权，最多 16 个 RFC1918 内部 CIDR。按最具体授权前缀排除网络地址与定向广播，保留 /31、/32。全局 CIDR 与 D1 的其他限制继续独立生效。改为不同规范化域名时，省略 privateCidrs 会清除原授权；等价域名、改名或换分组保留授权。公网地址转到未授权私网时显示 DNS_POLICY_BLOCKED，撤销活动探测并保留上次 IP 供诊断。

解析使用锁定版本 dnspython==2.8.0。每次 A/CNAME 查询共享 2 秒总预算，CNAME 最多 8 跳，每次最多 64 个 A 地址。Platform API 单进程调度最多 8 个在途解析，每目标最多 1 个。到期目标按等待顺序获得槽位，等待队列仅保留仍到期的目标 ID；开始解析时使用当前 ticket。超出处理能力时，已过 TTL 的目标仍然失效。成功刷新间隔为有效 TTL 的 80%，限制在 30–300 秒。失败退避为 30、60、120、300 秒。TTL 取 A 与 CNAME 链的最小值，从整次 DNS 查询开始保守计时。单调时钟覆盖后续余量，墙钟到期时间用于 API 与历史；inventory、锁和文件写入都不会延长 TTL。进程重启后，保存的旧 IP 只供诊断，重新解析并校验后才发布。低于刷新下限的 TTL 到期后会先成为 DNS_STALE，等待下一次成功解析。

DNS 错误与 ICMP 状态分别返回。列表含 dnsStatus、resolvedIp、aCount、resolutionGeneration、resolvedAt、expiresAt、sampleNotBefore、dnsError。发现接口按当前可发布的端点集合确认，dnsTargets 返回 discoveredGeneration、discoveryStatus 与 pingStatus。清单的 activatedAt 表示探测意图生效。sampleNotBefore 表示 Platform API 首次确认该代次实际发现的时间，确认前为 null。只有严格晚于此时间且足够新的同一次抓取，才用 up=1 和有效 ICMP 样本判 UP/DOWN。并发旧响应和重启前的样本不能通过当前代次门槛。DNS 状态写入失败时立即将受影响的 DNS 目标标记为 DNS_ERROR（dnsError=storage）与 UNKNOWN，尽可能撤回发现文件中的端点。withdrawalPending 表示 Prometheus 仍报告应撤回的端点。已完成的 DNS 结果若无法获取存储锁，会有限地记录原 ticket。下次取得锁时，只有仍匹配配置和运行快照的 ticket 才会撤销旧健康证据。运行卷目录 fsync 失败时，对账可在可写的发现卷中仅删除失效 DNS 项；正常发布仍需运行卷恢复。其他正常端点继续采集。质量阈值和历史统计由既有页面逻辑处理。

发现文件通过仅用于发现的 __meta_custom_ping_resolution_generation 携带解析代次，Prometheus 自动移除该元标签。时序保持 custom_id、instance、真实 target_ip。改名/分组不增加新的时序标签。IP 生效时段用 /platform-api/ping/targets/<UUID>/resolutions?cursor=<generation> 分页只读查询，每页最多 200 条，删除后的 UUID 也可查询。记录至少保留最近 7 天，整体最多 100000 段、运行文件最多 32 MiB。按实际 JSON 字节和段数预留运行行、关闭段及采样门槛字段容量。容量耗尽时暂停新地址激活并返回明确原因，保留已有来源。段的 start 是意图生效时间，sampleNotBefore 是实际发现后的采样下界，end 是时段结束。没有采样下界的旧段或未被发现的段不能纳入按代次统计，原始数据仍可查询。前端按真实 IP 和采样边界读取历史，原始 UTC CSV 与旧端点深链继续使用 TSDB。

运行状态、配置和发现文件均需随现有状态卷持久化。DNS 后端与域名前端在同一维护窗口上线，需构建含解析依赖的 Platform API 镜像。将 PLATFORM_CUSTOM_PING_DNS_ENABLED=false 并重建 Platform API，可用兼容 Schema 2 的版本暂停 DNS 投影，保留配置、IP 时段、回执和 TSDB，IPv4 仍按原有逻辑运行。恢复启用后重新解析并等待新代次与采样。已有 Schema 2 数据的环境应先使用该兼容暂停路径，再评估后续回退版本。


### 核心交换机直连 AP

拓扑默认不把 AP 挂在核心下面，因为核心也会学到下游 AP 的 MAC。确实直连时，先核对物理接线，再在部署目录的 `.env` 填写 `TOPOLOGY_CORE_AP_PORTS`，例如 `192.0.2.1:Gi1/0/37`。多项用逗号分隔，最多 128 项、8192 字符。IP 必须是已配置的核心地址，接口名填写采集到的 `ifName`（忽略大小写，不混用全称和缩写）。不要填写接交换机的互联口或聚合成员；改接线时也要更新这项。

留空或配置无效时，核心 AP 不连线。名单里的端口仍需通过当前 MAC/VLAN 查询；命中其它口、已发现的互联口、聚合成员、异常口，或存在多个接入位置时，不会误画为核心直连。普通接入交换机的 AP 定位不受这项配置影响。修改或删除此项后，重建 `topology-collector` 容器才会生效。

### DHCP 地址池页面

打开 `http://服务器IP:8088/dhcp` 可查看核心交换机上的 Cisco DHCP 地址池、已租用/剩余地址、使用率和冲突地址。页面直接复用基础配置里的“核心 IP”，不会重复维护设备地址。

登录赛事控制台后，在基础配置的“核心 / 防火墙”区域中找到“核心交换机 Telnet”，填写核心 IP、用户名、登录密码、Enable 密码和端口，点击“保存核心配置并测试”。该按钮会先保存当前基础配置，再保存 Telnet 信息并使用刚填写的核心 IP 测试，不需要分两次保存。密码单独保存在本机 Docker 状态卷中，页面不会回显明文，也不会随赛事配置导出。

旧安装仍可继续用 `librenms+grafana/.env` 作为默认值：

```text
PLATFORM_DHCP_SWITCH_USERNAME=
PLATFORM_DHCP_SWITCH_PASSWORD=交换机登录密码
PLATFORM_DHCP_SWITCH_ENABLE_PASSWORD=
```

支持“用户名 + 密码”和仅密码两种 Telnet 登录。页面打开且浏览器标签可见时才采集，默认每 60 秒使用一个会话读取一次；切换页面或隐藏标签后停止，手动连续点击也不会突破 30 秒的后台保护间隔。为了降低核心负荷，面板不定期读取完整的 `show ip dhcp binding` 列表。

DHCP 页面只负责显示地址池和立即刷新，不重复放置账号密码或连接测试。未配置时点击“去赛事控制台配置”会直接定位到核心交换机 Telnet 区域。

### iPerf3 出口测速

赛事控制台的“赛前工具”提供手动 iPerf3 TCP 上传/下载测试。默认使用香港公共节点，并提供香港、新加坡、土耳其伊斯坦布尔、印度尼西亚和自定义选项。每个公共地区可从公共列表中的多个服务器继续选择，地址和端口由预设自动填写并锁定；只有选择“自定义”时才可手工填写服务器和端口。开始前使用页面内确认面板，不会弹出浏览器原生确认框。

iPerf3 客户端已经包含在 `monitor-platform-api:local` Docker 镜像中，服务器宿主机不需要单独安装，也不再依赖容器内执行 Docker 命令。正常双向测试约 20 秒；公共节点繁忙时会自动尝试同组其他端口，页面会显示当前方向、端口、已用时间和进度，整次任务默认最多 60 秒。完成后显示接收端全程平均速率、总传输量、TCP 重传、发送端/接收端总计，以及默认每秒一个区间的传输量和平均速率。测试会真实占用出口带宽，只应在赛前手动运行。

平台 API 已使用 Python 3.13 镜像，并在镜像内自动安装 `telnetlib3` 兼容库。服务器宿主机升级 Python 不需要新建服务或额外手工安装 Telnet 组件。

基础配置按钮：

| 按钮 | 作用 |
|---|---|
| 验证 | 只检查配置，不写文件 |
| 保存 | 写入 `event-config.yml` |
| 应用配置 | 写入 `event-config.yml`，生成 `.env`，并自动执行 `apply-env.sh` 重建需要读取环境变量的容器 |
| 回滚 | 恢复上一次配置 |
| 导入配置 | 导入 `event-config.yml` |
| 导出配置 | 下载当前 `event-config.yml` |

点 `应用配置` 后，控制台会自动让 Prometheus、LibreNMS、飞书桥接、大屏等相关容器重新读取新的 `.env`。如果页面提示自动应用失败，再在服务器执行：

```bash
cd /root/monitor-autoconfig/librenms+grafana
./apply-env.sh
```

## 基础配置怎么填

| 字段 | 建议 |
|---|---|
| 默认赛制 | 控制 `/control` 里默认座位布局 |
| 队号位置 | 64 人赛保持 `2层 / 233 / 332` 的舞台结构不变，只按本场实际位置调整第 1–16 队；比赛状态和选手延迟趋势会使用相同顺序 |
| SNMP Community | 交换机、防火墙、LibreNMS/Prometheus 采集共用，默认 `global` |
| 选手 VLAN / 无线 VLAN | 默认 `40 / 41` |
| 选手网关 | 留空时复用核心 IP |
| 普通交换机自动发现范围 | 给通用大屏、拓扑和 LibreNMS 自动发现用，例如 `192.168.10.0/24` 或 `192.168.10.1-100,192.168.10.254`；不会参与赛事座位识别。只有能确认交换机身份的设备才会进入监控：ICMP 只表示可达，SNMP 不可达、sysDescr/sysObjectID 指向主机或打印机、或厂商/型号无法识别的地址只记为“未验证”（不写 `switch_targets.json`、不进入 Ping/SNMP/拓扑目标）。已确认设备临时 SNMP 失败仍保留 Ping 监控，离线保留 24 小时；状态文件升级时，旧的 IP 占位不会升级为已确认交换机，旧的主机名条目只作为“待 SNMP 复核”继续监控，默认 7 天内由 SNMP 证明身份（`SWITCH_DISCOVERY_LEGACY_GRACE_SECONDS`，0 表示不保留），到期仍未证明自动退役并在日志与状态文件标注 `legacy-grace-expired`。分类是保守启发式：sysServices 只是 RFC3418 的层服务位，作为辅助证据而非绝对设备类型；状态分 SNMP-verified、legacy pending、unverified、offline retained 四类，日志与 `switch-discovery-state.json` 分开呈现；修改 `SWITCH_DISCOVERY_LEGACY_GRACE_SECONDS` 不会延长已持久化的 deadline。需要强制纳入无法自动识别的交换机时，用“固定普通交换机”手工登记 |
| 防火墙管理网段 | 默认 `192.168.9.0/24`；需要发现其它防火墙管理地址时再改 |
| 核心 IP | 三层核心或网关交换机管理 IP |
| 防火墙 IP | 同时用于判断防火墙在线和 WAN 流量 SNMP；多个 IP 用逗号或换行，不用 `/` |
| 防火墙名称 | 选填；大屏和拓扑的显示名。留空用设备 SNMP sysName（HA 防火墙的 sysName 可能是 Member1 这种，建议手填） |
| 物理防火墙 SNMP IP | HA 两台物理机分别采集时填写，多个 IP 用逗号或换行 |
| 赛事交换机 | 每个项目只填承载选手电脑的固定交换机，用于座位识别和赛事选手监控；支持 `192.168.10.45-46` 这种范围写法 |
| 固定普通交换机 | 通常留空，由管理范围自动发现；仅需要固定显示名时填写，不参与赛事识别 |
| 服务器 | 默认空；需要监控游戏服务器时再添加名称和 IP |
| ISP 自动发现 | 通过防火墙 SNMP 的默认路由、接口地址和 WAN 口名称识别运营商链路；公网地址变化后自动替换旧 Ping 目标 |
| WAN 口识别关键词 | 自动发现 WAN 口时匹配接口名/描述，默认 `telecom,telcom,unicom,isp,WAN`；防火墙 SNMP 只报 `eth0/eth1` 物理名时（如 WatchGuard）直接加物理口名，如 `...,eth0,eth1`，以数字结尾的关键词按边界匹配、不会误配 eth10 |
| WAN 口名/别名 | 与防火墙 SNMP 返回的接口名或别名一致；网关由路由表自动发现，不需要填写公网 IP 或网关 |
| 默认/单链路带宽 | 用于饱和判断；对称线路填一个 Mbps 数值，不对称线路固定按“下载/上传”填写（如 `1000/100`）；饱和阈值默认 90% |
| UniFi | 使用 UniFi AP 时填控制器地址和只读账号 |
| 飞书机器人 Token | 留空则不推飞书；多台监控可以复用同一个 token，但会推到同一个群且可能重复告警 |
| 飞书应用 App ID / App Secret | 审批通过的企业自建应用凭据；普通告警优先使用应用机器人，旧 Token 作为失败回退 |
| 告警及巡检群名称或 Chat ID | 多个独立赛事 VM 可以填写同一个群名称；也可填写 `oc_` 开头的 Chat ID，跳过群列表解析 |

飞书企业自建应用审批通过后，在 `/control` 的“告警”区展开“飞书应用高级配置”，填写 App ID、App Secret，
把应用机器人加入告警群，再点“应用配置”。旧版只写在 `.env` 的凭据会自动带入
后台输入框。正式模型是“一场比赛 = 一台 Monitoring VM = 一套独立现场网络”；不在
不同 VM 间共享告警状态或重复监控同一批设备。多个赛事 VM 可以共用 App ID、App Secret
和 Chat ID，但每台必须配置唯一“赛事名称”（`EVENT_NAME`），例如 `Singapore`、
`Shanghai`、`IEM Chengdu`。普通告警标题会自动带 `【EVENT_NAME】` 前缀。

共享群命令必须带赛事名称，例如 `@机器人 Singapore 网络巡检`、
`@机器人 Shanghai 网络巡检`、`@机器人 IEM Chengdu 网络巡检`。每台 VM 只执行与自己
赛事名称匹配的命令，其它赛事及未带赛事名称的群命令会静默忽略。可靠的共享群命令路由
依赖 `im:chat`、`im:message:readonly` 和 `im:message.group_msg` 的消息轮询；主动发消息还需
`im:message:send_as_bot`。权限不足时保留长连接 fallback，但日志会明确提示
`shared-group event routing is degraded`，不能把多个长连接客户端当作可靠广播总线。
自建应用群解析的错误码、直接 Chat ID 配置和现场验证方法见
[`librenms+grafana/docs/feishu-app-chat-troubleshooting.md`](../librenms+grafana/docs/feishu-app-chat-troubleshooting.md)。
所有部署支持 `网络巡检`、`光功率巡检`、`上联冗余巡检` 和 `帮助`；公司模式另外支持
`待删除设备`。
光功率读取 LibreNMS 已采集的 dBm 传感器及阈值，不会额外轮询交换机。
`im.message.receive_v1` 是在“事件与回调”里添加的事件类型，不是权限管理页里的权限名。

Pending Delete 是部署级可选能力，同一套代码不区分分支：

- 比赛 Golden VM / appliance 保持 `DEVICE_PENDING_DELETE_ENABLED=false`（默认），不生成
  48 小时待删除状态、飞书卡片、查询命令或控制台面板；比赛结束直接撤掉整台 VM。
- 公司长期 Monitoring VM 如需原有设备生命周期管理，必须在 `.env` 明确设置
  `DEVICE_PENDING_DELETE_ENABLED=true`。此时 48 小时检测、持久化状态、飞书确认删除/保留
  按钮和控制台入口均启用；删除仍需人工确认，并继续执行在线复核与 token 校验。

公司长期 Monitoring VM 还可选择清理 LibreNMS 中持续离线的历史设备。该能力只有
`DEVICE_PENDING_DELETE_ENABLED=true` 和 `DEVICE_AUTO_DELETE_ENABLED=true` 同时成立才运行，
默认阈值为 7 天、每小时检查一次，并保持 `DEVICE_AUTO_DELETE_DRY_RUN=true`。关闭 DRY RUN 必须先核对日志并得到明确批准；删除前会重新通过 blackbox ICMP 确认，探测异常时一律跳过。
核心、赛事交换机、防火墙 VIP/HA 物理节点、服务器、ISP 和选手网关等显式目标永久保护，
但 discovery range 不会整体受保护。真正删除成功或 DELETE 失败会按检查周期聚合发送一条
飞书通知；DRY RUN 通知默认关闭，需要时设置 `DEVICE_AUTO_DELETE_DRY_RUN_NOTIFY=true`，每轮
也只发送一条候选摘要。比赛 Golden VM 的 auto-delete 默认关闭。

旧安装无法仅凭遗留参数可靠判断用途；公司服务器升级前必须补上上述 `true`，已有
bridge-state 会继续沿用。公司模式的飞书按钮还需订阅 `card.action.trigger`。

## 交换机侧配置

先确认交换机启用了 SNMP，团体字与平台配置一致，服务器可以访问它的 UDP 161。只填写平台团体字不会开启交换机上的 SNMP。

Cisco IOS 可以用 `show snmp` 检查。提示 `SNMP agent not enabled` 时，在交换机配置只读访问；将 `<团体字>` 换成平台配置的值：

```text
configure terminal
snmp-server community <团体字> RO
end
write memory
```

如果绑定了访问控制列表，还需允许监控服务器。自动添加超时后，当前交换机流程没有定期重试；启用 SNMP 后需要核对 LibreNMS 是否已纳管，不能只凭 ping 在线判断。

需要接收交换机日志时，再配置 syslog：

```text
logging host <监控服务器IP>
logging trap informational
service timestamps log datetime msec
```

接入口建议：

```text
spanning-tree portfast edge
spanning-tree bpduguard enable
storm-control broadcast level 1.00 0.50
storm-control action shutdown
```

核心/上联口不要乱开 BPDU Guard。DHCP Snooping 的 trust 只放 DHCP 服务器方向或上联方向，普通终端/AP 口不要 trust。

## 交付方式

赛事现场的最终离线交付方式计划使用经过验证的标准 VM/OVA 模板；仓库继续保留普通在线部署方式。

## 常用排障

查看服务：

```bash
docker compose ps
docker compose logs -f platform-api bigscreen prometheus alertmanager-feishu-bridge
```

只重启控制台：

```bash
docker compose up -d --force-recreate platform-api bigscreen
```

旧容器名冲突时，只删容器，不删数据：

```bash
docker rm -f librenms grafana prometheus bigscreen rsyslog blackbox-exporter snmp-exporter loki promtail-syslog alertmanager-feishu-bridge player-targets topology-collector platform-api grafana-setup init-permissions 2>/dev/null || true
docker compose up -d
```

`librenms-data`、`librenms-db-data`、`librenms-rrdcached-journal` 是宿主机目录；Prometheus、Grafana、Loki 等使用 Docker 命名卷。两类数据都不要随便删除，尤其不要在保留数据时执行 `docker compose down -v`。


LibreNMS 无时区 SQL 时间及 naive Python datetime 按 `LIBRENMS_TIMEZONE`（可选 IANA 名称）优先、运行时 `TZ` 次之解释；没有可用命名时区时采用该日期的运行时本地时区，不默认为 UTC。命名时区的重复小时经 UTC 往返校验后取较晚时刻，避免年龄提前增加；不存在的本地时间返回未知。带 Z/偏移/时区的时间及 Unix epoch 保持绝对时间含义；未来时间仍不能通过 freshness 或删除年龄门槛。Bridge 与 topology-collector 只读挂载主机 `/usr/share/zoneinfo`，部署前须确认主机数据库完整且容器能解析有效来源时区。共享模块是单文件 bind mount，更新代码后须定向 recreate 使用年龄判断的这两个服务并验证源码/容器 SHA；普通 Git 更新或 restart 不保证既有挂载 inode/进程模块已更新。platform-api 不使用共享时间年龄判断，无需因此刷新。轮询/发现间隔、离线阈值及 Auto Delete 四开关不变。
