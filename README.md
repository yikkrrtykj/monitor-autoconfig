# 赛事网络监控

这套东西是给现场网络值守用的：LibreNMS 负责设备、端口和告警，Prometheus/Grafana 负责采集和图表，8088 大屏给现场电视或投屏直接看。

## 端口

| 入口 | 默认端口 | 用途 |
|---|---:|---|
| 大屏 | 8088 | 现场电视 / 投屏，不需要登录 |
| Grafana | 3000 | 运维看图、查历史、临时排查 |
| LibreNMS | 8002 | 设备发现、端口流量、告警规则、飞书通知 |

默认账号：

| 服务 | 默认账号 |
|---|---|
| Grafana | `admin / root` |
| LibreNMS | `admin / librenms123` |

正式使用前，在 `.env` 里改掉 `GRAFANA_PASSWORD`、`LIBRENMS_ADMIN_PASSWORD` 和 `SNMP_COMMUNITY`。8088 大屏没有登录，不要直接暴露到公网。

## 1. 准备服务器

### 1.1 安装 Docker 和 git

Ubuntu 国内机器：

```bash
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh --mirror Aliyun
sudo apt-get install -y git python3
sudo usermod -aG docker $USER && newgrp docker
```

Ubuntu 国外机器：

```bash
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh
sudo apt-get install -y git python3
sudo usermod -aG docker $USER && newgrp docker
```

CentOS 国内机器：

```bash
sudo yum install -y yum-utils git python3
sudo yum-config-manager --add-repo https://mirrors.aliyun.com/docker-ce/linux/centos/docker-ce.repo
sudo yum install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
sudo systemctl enable --now docker
sudo usermod -aG docker $USER && newgrp docker
```

国外 CentOS 把 repo 换成 `https://download.docker.com/linux/centos/docker-ce.repo`，其余一样。

如果之前 Docker 装到一半失败，先清旧源：

```bash
sudo rm -f /etc/apt/sources.list.d/docker.list
sudo apt-get update
```

### 1.2 国内机器拉不动镜像时

`./deploy.sh` 会拉 Docker Hub 镜像。国内服务器如果一直 `i/o timeout`，让 Docker daemon 走本机 Clash 代理。Clash 先打开“允许局域网”，把 `电脑IP:端口` 换成实际值：

```bash
sudo tee /etc/docker/daemon.json <<'EOF'
{}
EOF

sudo mkdir -p /etc/systemd/system/docker.service.d
sudo tee /etc/systemd/system/docker.service.d/http-proxy.conf <<'EOF'
[Service]
Environment="HTTP_PROXY=http://电脑IP:端口"
Environment="HTTPS_PROXY=http://电脑IP:端口"
Environment="NO_PROXY=localhost,127.0.0.1"
EOF

sudo systemctl daemon-reload
sudo systemctl restart docker
docker info | grep -i proxy
```

用完清代理：

```bash
sudo rm -f /etc/systemd/system/docker.service.d/http-proxy.conf
sudo systemctl daemon-reload
sudo systemctl restart docker
```

## 2. 首次部署

### 2.1 拉代码

GitHub 能直连：

```bash
git clone https://github.com/yikkrrtykj/monitor-autoconfig.git
cd monitor-autoconfig/librenms+grafana
```

国内机器也可以走 Gitee 镜像：

```bash
git clone https://gitee.com/yikkrrtykj/monitor-autoconfig.git
cd monitor-autoconfig/librenms+grafana
```

如果 Gitee 不是最新，到 Gitee 页面手动同步镜像。

### 2.2 生成 `.env`

```bash
cp .env.example .env
vi .env
```

`SERVER_IP` 留空时，`deploy.sh` 会自动探测并写回 `.env`。飞书 token 留空也能启动，只是不推告警。

### 2.3 启动

```bash
chmod +x *.sh
./deploy.sh
```

第一次会久一点，主要是拉镜像和初始化数据库。

启动后跑自检：

```bash
./pre-match-check.sh
```

能自动补的项目可以加：

```bash
./pre-match-check.sh --fix
```

## 3. 按现场改 `.env`

### 3.1 基础设施 Ping 和 SNMP

规则很简单：

1. `FIREWALL_PING` 只写物理防火墙，而且必须是监控服务器能 ping 通的地址。
2. HA 逻辑地址不要放 `FIREWALL_PING`，放 `FIREWALL_SNMP_TARGETS` 抓端口流量。
3. 没有游戏服务器就让 `SERVER_PING=` 留空，大屏不会显示服务器区域。
4. 范围可以直接写，`SW:192.168.10.11-22` 会自动展开，不用一台台写。

### 3.2 ISP 和带宽告警

`./apply-env.sh` 会重建 LibreNMS transport，也会重启飞书 bridge。ISP 带宽饱和告警由 bridge 从 Prometheus 实时读取防火墙 WAN 口速率：连续超过 `BIGSCREEN_ISP_MAX_BANDWIDTH * ISP_SATURATION_PERCENT` 才推飞书。

ISP 断线不是靠 LibreNMS 丢包规则判断，而是看 `ISP_PING` 生成的 `infra-isp-ping`：

```bash
ISP_PING=telecom:223.5.5.5,telecom_gw:电信网关IP,unicom:119.29.29.29,unicom_gw:联通网关IP
ISP_DOWN_FOR_SECONDS=0
ISP_PING_SCRAPE_INTERVAL=1s
DEVICE_DOWN_SAMPLE_WINDOW_SECONDS=5
BLACKBOX_ICMP_TIMEOUT=1s
```

`ISP_DOWN_FOR_SECONDS=0` 表示只要 Prometheus 采到这条 ISP ping 失败，就马上推飞书。`ISP_PING_SCRAPE_INTERVAL=1s` 是外网探测频率；`DEVICE_DOWN_SAMPLE_WINDOW_SECONDS=5` 会把最近 5 秒内的一次失败保留下来给 bridge 看到，专门用来抓“拔一下马上插回”的短闪断。再短到完全落在两次采样之间的瞬间抖动，ping 方式仍可能漏；要物理口一抖就知道，就让防火墙或交换机把 WAN 口 up/down syslog 发到监控服务器。

多 ISP 时，公网探测目标要在防火墙上做 PBR 钉到对应出口，否则所有探测都走默认线路，断另一条线也可能看不出来。

### 3.3 选手监控

要按座位看选手延迟，交换机端口描述要写成 `team X-Y` 。

```bash
TOURNAMENT_SWITCHES=192.168.10.11-22
PLAYER_SUBNETS=192.168.40.0/24
WIRELESS_SUBNETS=192.168.41.0/24
PLAYER_GATEWAYS=192.168.10.254
PLAYER_VLAN_IDS=40
```

1. `TOURNAMENT_SWITCHES` 是选手接入交换机，支持范围。
2. 舞台交换机如果自己就是三层网关，`PLAYER_GATEWAYS` 可以留空。
3. 舞台交换机如果只是二层，`PLAYER_GATEWAYS` 要写核心 / 防火墙网关 IP。
4. Cisco / H3C 查非 VLAN 1 的 MAC 表时，通常要填 `PLAYER_VLAN_IDS`。
5. `PLAYER_VLAN_IDS` 只填有线选手 VLAN，不要把无线 VLAN 全塞进去。

WiFi-only 比赛可以手写静态目标：

```bash
PLAYER_STATIC_TARGETS=1-1=192.168.12.101,1-2=192.168.12.102,2-1=192.168.12.201
PLAYER_STATIC_NETWORK=wireless
```

### 3.4 UniFi AP 监控

比赛现场默认启用 UniFi 采集。你只需要在 `.env` 里填控制器地址和一个只读账号：

```bash
COMPOSE_PROFILES=unifi
UNIFI_CONTROLLER_URL=https://控制器IP
UNIFI_CONTROLLER_USER=readonly
UNIFI_CONTROLLER_PASS=只读账号密码
UNIFI_CONTROLLER_SITES=all
```

UniFi OS 主机一般填 `https://控制器IP`；独立 Network 控制器一般是 `https://控制器IP:8443`。AP 在线、离线、客户端数这些都从控制器 API 来，不靠 AP 自身 SNMP。SNMP 只用于把在线 AP 自动补进 LibreNMS 设备列表；SNMP 不通不会影响大屏和 AP 掉线告警。AP 默认连续离线 180 秒才推飞书，用来过滤 UniFi 控制器/API 的短闪断。

### 3.5 飞书告警

系统自动配置这些告警，部署后无需手动建规则：

| 告警 | 来源 | 速度 |
|---|---|---|
| 设备离线 / 恢复 | Prometheus blackbox | 约一个 ping 采集周期 |
| 新设备上线 | LibreNMS SNMP 设备列表 | 秒级到 30 秒 |
| ISP 断线 / 恢复 | Prometheus blackbox | 约一个 ping 采集周期 |
| ISP 带宽饱和 | Prometheus 实时 | 10 秒（可调） |
| 互联口断链 / 恢复 | Prometheus SNMP `ifOperStatus` | 约 5-10 秒 |
| AP 掉线 / 恢复 | UniFi 控制器 API | 默认 180 秒确认 |
| 串线 / 接口保护关闭 / 回环 / DHCP Snooping | syslog | 秒级 |

`CORE_SWITCH_PING` / `DIST_SWITCH_PING` 里的范围只是候选目标。bridge 不会给一开始就是 down 的地址发离线告警；候选目标第一次 ping 通后，只会尝试把它按 SNMP 加到 LibreNMS，并纳入后续离线监控。真正的“新设备上线”只等 LibreNMS 发现到 SNMP 设备后发送

syslog 飞书推送默认由 `SYSLOG_ALERT_TYPES=native_vlan_mismatch,errdisable,loopback,dhcp_snooping` 控制。MAC 漂移、BPDU Guard 单独日志、storm-control 单独日志默认只进 Loki/LibreNMS，不推飞书；BPDU Guard 导致接口 err-disable 时，只在“接口被保护关闭”卡片里显示原因为 `bpduguard`。同一设备同一接口在 `SYSLOG_CORRELATION_SECONDS=10` 秒内同时出现串线和 err-disable 时，只推“接入口疑似串线”。

syslog 告警需要交换机配：

```
! Cisco IOS，全局配置模式
logging host <监控服务器IP>
```

宿主机还需放行 514/UDP：

```bash
sudo ufw allow 514/udp    # Ubuntu
# 或
sudo firewall-cmd --add-port=514/udp --permanent && sudo firewall-cmd --reload  # CentOS
```

只有配了 `logging host` 的交换机日志才能收到。其他接入层交换机如果也想告警，同样加一行 `logging host <监控服务器IP>`。

### 3.6 新增设备不用每台手工加

如果新交换机满足这些条件，通常不用去 LibreNMS 一台台 Add Device：

1. SNMP community 对。
2. IP 在 `LIBRENMS_DISCOVERY_TARGETS` 范围内。
3. IP 在 `DIST_SWITCH_PING` 或 `TOURNAMENT_SWITCHES` 范围内。
4. LLDP 开着，能被核心发现。

如果新设备不在范围里，改 `.env` 后跑 `./apply-env.sh`。



## 4. 改完配置怎么生效

### 4.1 改 `.env`

```bash
./apply-env.sh
```

它不拉镜像，会重建需要读取 `.env` 的容器，并重跑 LibreNMS 自动发现、飞书 transport、告警规则、Grafana provisioning。

### 4.2 什么时候才跑 `deploy.sh`

这些情况跑：

1. 第一次部署。
2. `git pull` 后代码升级。
3. 镜像需要重新拉。
4. 想完整重启整个栈。

平时只是改设备 IP、ISP 带宽、飞书 token、选手网段，优先跑 `./apply-env.sh`。

### 4.3 选手换设备

已发现目标由 blackbox 每 5 秒 ping 一次，断线会很快显示。

如果选手换电脑 / 手机后拿到新 IP，不想等默认 300 秒发现周期，打开：

```text
http://服务器IP:8088/seat-check
```

点“立即重扫”。

### 4.4 看配置有没有生效

```bash
docker logs -f librenms-config
docker logs -f alertmanager-feishu-bridge
docker logs -f player-targets
docker logs -f topology-collector
```

## 5. 现场怎么用

常用地址：

```text
http://服务器IP:8088             大屏首页
http://服务器IP:8088/infra       网络总览
http://服务器IP:8088/topology    网络拓扑
http://服务器IP:8088/latency     延迟查询
http://服务器IP:8088/incident    卡顿分析
http://服务器IP:8088/seat-check  座位核对 / 立即重扫
http://服务器IP:3000             Grafana
http://服务器IP:8002             LibreNMS
```

页面用途：

| 页面 | 用途 |
|---|---|
| `/infra` | 核心、接入、防火墙、ISP、丢包热力图 |
| `/topology` | ISP 到防火墙、核心、接入的拓扑状态 |
| `/latency` | 按队伍、座位、时间查单个选手延迟，可导出 CSV |
| `/incident` | 输入卡顿时间点，关联 ISP、同台选手、基础设施 |
| `/heatmap` | 一段时间内的离线率和平均延迟 |
| `/wireless` | 只看无线选手 |
| `/seat-check` | 赛前核对座位，选手换设备后立即重扫 |
| `/match-5v5`、`/tournament-6` 等 | 比赛实时大屏 |

## 6. 按现象排障

### 6.1 deploy 一直卡住

先确认代码是新的：

```bash
git pull
./deploy.sh
```

如果 `docker ps` 还能看到旧的 `grafana-provisioning-render` 容器，清掉一次：

```bash
docker compose rm -sf grafana-provisioning-render
./deploy.sh
```

### 6.2 ISP 超过阈值但没告警

按这个顺序看：

1. `.env` 里 `FEISHU_ROBOT_TOKEN` 是否填写。
2. 跑过 `./apply-env.sh` 没有。
3. `docker logs -f alertmanager-feishu-bridge` 里是否有 `[ISP] realtime bandwidth watcher enabled`。
4. 日志里是否有 `[ISP] rates unicom in=333.0 Mbps/240.0 Mbps` 这种行。前面是当前速率，后面是阈值。
5. `BIGSCREEN_ISP_MAX_BANDWIDTH` 是否写运营商真实带宽，比如 300M 就写 `300`。
6. `ISP_SATURATION_PERCENT` 是否是你想要的阈值，比如 80 表示超过 240 Mbps 告警。
7. `ISP_ALERT_FOR_SECONDS` 是否是你想要的持续时间，比如 10。
8. 防火墙 WAN 口是否能被 `FIREWALL_WAN_IF_FILTER` 匹配到。
9. LibreNMS transport 的 Test 是否 OK；这只证明飞书通，不代表 ISP 实时阈值已经触发。

改完后跑：

```bash
./apply-env.sh
docker logs -f librenms-config
docker logs -f alertmanager-feishu-bridge
```

注意：ISP 实时带宽告警看的是 Prometheus 的 `firewall-snmp` 采集，默认每 5 秒检查一次；LibreNMS poller 不参与这条告警。

### 6.3 ISP 断线但没告警

先看 bridge 是否真的在监控 ISP ping：

```bash
docker logs -f alertmanager-feishu-bridge
```

正常会看到：

```text
[DOWN] device-down watcher enabled (... isp_for=0s ...)
[DOWN] targets ... infra-isp-ping=2 ...
```

如果看到：

```text
[DOWN] no infra-isp-ping targets found
```

说明 `.env` 里 `ISP_PING` 为空，或者改完没有跑 `./apply-env.sh`。填好后执行：

```bash
./apply-env.sh
docker logs -f alertmanager-feishu-bridge
```

多 ISP 一定要确认探测目标从对应出口出去。比如 `telecom:223.5.5.5` 要被防火墙策略路由到电信出口；否则断电信但探测从联通绕出去，系统会认为它还是通的。

### 6.4 范围里的设备没上线却报警

新版默认不会这样做。`SW:192.168.10.11-30` 只是候选池，bridge 看到目标第一次 UP 后才把它纳入离线告警。日志里如果出现下面这行，是正常的，表示这个地址还没上线，不会推飞书：

```text
[DOWN] waiting for first UP before alerting infra-dist-ping SW12 (192.168.10.22)
```

如果你确实想让“已在 LibreNMS 发现过但当前 down”的设备也立刻报警，保持默认即可；bridge 会用 LibreNMS 设备列表识别它，并用 LibreNMS 里的设备名显示。

候选目标第一次 ping 通且不在 LibreNMS 里时，会看到：

```text
[WATCHER] SNMP auto-add requested for 192.168.10.22
```

如果它是交换机候选，bridge 会静默调用 LibreNMS API 按 SNMP 添加设备。等 LibreNMS 拿到真实 hostname 后，才会推一条“新设备上线”。

### 6.5 互联口断了但没有具体接口

互联口告警看的是 `infra-switch-ifmib` 里的 `ifOperStatus`，正常日志类似：

```text
[LINK] interconnect watcher enabled (...)
[LINK] watched port-channels total=4 up=4 down=0
```

如果 `total=0`，说明 Prometheus 没采到 Port-channel/LAG 接口，先检查交换机 SNMP、`SWITCH_IFMIB_SCRAPE_INTERVAL`，以及接口名是否能被 `INTERCONNECT_PORT_FILTER` 匹配。

### 6.6 LibreNMS 里还有旧的接口丢弃 SQL 错误

这类日志一般是老版本规则还留在数据库里，比如 `接口丢弃告警` 引用了当前 LibreNMS 没有的 `ports.ifInDiscards_rate` 字段。最新脚本会删除这些旧规则，跑完后 `docker logs -f librenms-config` 应该能看到：

```text
Alert rule: 接口丢弃告警 - removed
Alert rule: 接口错误告警 - removed
```

注意 LibreNMS 的 eventlog 是历史记录，旧日志不会自动消失；只要时间不再继续新增，就说明规则已经清掉了。如果日志里还是 `updated`，说明服务器没有拉到最新代码。

### 6.7 交换机是黄色

先点进设备详情看端口统计。如果像 `Total: 60 / Up: 8 / Down: 52 / Disabled: 0`，就是大量未接线端口没有 shutdown。脚本默认开启：

```bash
LIBRENMS_IGNORE_DOWN_PORTS=true
```

跑 `./apply-env.sh` 后会把这些 down 口设为 ignore，设备会回到绿色。互联口断链不会因此漏报，因为它走 `alertmanager-feishu-bridge` 的 `[LINK]` 秒级监控。

### 6.8 防火墙能 ping，拓扑还是红

看 `.env` 里的 `FIREWALL_PING`。这里要写“监控服务器能 ping 通的物理防火墙地址”，不是 SNMP 逻辑地址，也不是旧网段地址。

例如：

```bash
FIREWALL_PING=FW1:192.168.11.2,FW2:192.168.11.3
FIREWALL_SNMP_TARGETS=FW:192.168.11.4
```

### 6.9 拓扑又出现占位 ISP 名称

不要手动写 `BIGSCREEN_ISP_NAMES`，让它自动发现：

```bash
BIGSCREEN_ISP_AUTO_DISCOVER=true
FIREWALL_WAN_IF_FILTER=telecom,telcom,unicom,isp,WAN
```

接口描述里有 `telecom`、`unicom`，拓扑和大屏就会显示真实名字。

### 6.10 LibreNMS 发现 0 个设备

先直接测 SNMP：

```bash
docker exec librenms snmpwalk -v2c -c public 192.168.10.254 sysName.0
```

不通就查设备 SNMP、ACL、防火墙策略、community。通了再看：

```bash
docker logs -f librenms-config
```

### 6.11 选手页面 No data

先看生成了多少 target：

```bash
./pre-match-check.sh
docker logs -f player-targets
```

再按顺序查：

1. `TOURNAMENT_SWITCHES` 有没有覆盖选手接入交换机。
2. 交换机端口描述是不是按座位写了。
3. 二层舞台有没有填 `PLAYER_GATEWAYS`。
4. Cisco / H3C 非 VLAN 1 有没有填 `PLAYER_VLAN_IDS`。
5. 日志里有没有 `gateway-ARP join: matched N IPs`。

手动排 SNMP：

```bash
docker exec player-targets snmpwalk -v2c -c public <STAGE_IP> 1.3.6.1.2.1.17.4.3.1.2 | head
docker exec player-targets snmpwalk -v2c -c public <STAGE_IP> 1.3.6.1.2.1.17.7.1.2.2.1.2 | head
docker exec player-targets snmpwalk -v2c -c public <GATEWAY_IP> 1.3.6.1.2.1.4.22.1.2 | head
```

## 7. 赛后清理

只停服务、保留数据：

```bash
docker compose down
```

彻底清掉容器和命名卷：

```bash
docker compose down -v
docker system prune -a
```

LibreNMS 的 bind mount 数据在仓库目录里，`down -v` 不会删。确定不要了再删：

```bash
sudo rm -rf librenms-data librenms-db-data librenms-rrdcached-journal
```

要带走配置和 LibreNMS 数据，先打包：

```bash
tar czf monitor-backup-$(date +%Y%m%d).tar.gz \
  .env grafana-provisioning librenms-data librenms-db-data
```

## 8. 文件说明

| 文件 | 作用 |
|---|---|
| `deploy.sh` | 第一次部署 / 代码升级 / 拉镜像 |
| `apply-env.sh` | 日常改 `.env` 后应用配置 |
| `pre-match-check.sh` | 赛前自检 |
| `librenms-auto-config.sh` | LibreNMS 设备发现、告警规则、飞书 transport |
| `prometheus-gen-config.sh` | 生成 Prometheus 抓取配置 |
| `generate-player-targets.py` | 选手座位发现、无线扫描、静态选手目标 |
| `generate-topology-edges.py` | LLDP 拓扑采集 |
| `bigscreen/` | 8088 大屏前端 |

前端是纯静态页面，没有构建步骤。开发时跑测试：

```bash
cd librenms+grafana
for t in tests/test_bigscreen_*.js; do node "$t"; done
```
