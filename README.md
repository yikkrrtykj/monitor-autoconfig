# 赛事网络监控

这套东西是给现场网络值守用的：LibreNMS 负责设备、端口和告警，Prometheus/Grafana 负责采集和图表，8088 大屏给现场电视或投屏直接看。

按下面顺序做就行。不要一上来钻配置项，先把主链路跑通。

## 0. 先知道看哪里

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
sudo apt-get install -y git
sudo usermod -aG docker $USER && newgrp docker
```

Ubuntu 国外机器：

```bash
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh
sudo apt-get install -y git
sudo usermod -aG docker $USER && newgrp docker
```

CentOS 国内机器：

```bash
sudo yum install -y yum-utils git
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

`./deploy.sh` 会拉 Docker Hub 镜像。国内服务器如果一直 `i/o timeout`，让 Docker daemon 走你本机 Clash 代理。Clash 先打开“允许局域网”，把 `电脑IP:端口` 换成实际值：

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

第一次可以先只改最必要的几项：

```bash
SERVER_IP=192.168.10.200
EVENT_NAME=某某比赛
SNMP_COMMUNITY=public
GRAFANA_PASSWORD=改一个密码
LIBRENMS_ADMIN_PASSWORD=改一个密码
FEISHU_ROBOT_TOKEN=
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

这一段是最重要的。每到一个新场地，基本就是改这里。

### 3.1 基础设施 Ping 和 SNMP

```bash
CORE_SWITCH_PING=Core:192.168.10.254
DIST_SWITCH_PING=SW:192.168.10.11-22
FIREWALL_PING=FW1:192.168.11.2,FW2:192.168.11.3
SERVER_PING=

FIREWALL_SNMP_TARGETS=FW:192.168.11.4
LIBRENMS_DISCOVERY_TARGETS=192.168.10.1-100,192.168.10.254
FIREWALL_DISCOVERY_RANGE=192.168.11.2-4
```

规则很简单：

1. `FIREWALL_PING` 只写物理防火墙，而且必须是监控服务器能 ping 通的地址。
2. HA 逻辑地址不要放 `FIREWALL_PING`，放 `FIREWALL_SNMP_TARGETS` 抓端口流量。
3. 没有业务服务器就让 `SERVER_PING=` 留空，大屏不会显示服务器区域。
4. 范围可以直接写，`SW:192.168.10.11-22` 会自动展开，不用一台台写。

### 3.2 ISP 和带宽告警

```bash
BIGSCREEN_ISP_AUTO_DISCOVER=true
FIREWALL_WAN_IF_FILTER=telecom,telcom,unicom,isp,WAN
BIGSCREEN_ISP_MAX_BANDWIDTH=300
ISP_SATURATION_PERCENT=90
```

这里的逻辑：

1. 防火墙 WAN 口描述里写 `telecom`、`unicom` 最稳。
2. `FIREWALL_WAN_IF_FILTER` 用来匹配 WAN 口的 `ifAlias / ifName / ifDescr`。
3. `BIGSCREEN_ISP_MAX_BANDWIDTH=300` 表示这条 ISP 按 300 Mbps 算，不按物理口 1G 算。
4. `ISP_SATURATION_PERCENT=90` 表示 300M 的 90%，也就是 270 Mbps 告警。

多条 ISP 不同带宽：

```bash
BIGSCREEN_ISP_MAX_BANDWIDTH=telecom:300,unicom:500
```

非对称专线写下行/上行：

```bash
BIGSCREEN_ISP_MAX_BANDWIDTH=telecom:1000/100,unicom:500/50
```

`./apply-env.sh` 会把这个速率写进 LibreNMS 的 WAN 口，让 `port_usage_perc` 按运营商带宽算。告警也只走 LibreNMS，不走 Prometheus 推送。

### 3.3 选手监控

不接选手就留空。要按座位看选手延迟，交换机端口描述要写成 `team X-Y` 或项目支持的同类格式。

```bash
TOURNAMENT_SWITCHES=192.168.10.11-22
PLAYER_SUBNETS=192.168.40.0/24
WIRELESS_SUBNETS=192.168.41.0/24
PLAYER_GATEWAYS=192.168.10.254
PLAYER_VLAN_IDS=40
```

怎么填：

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

### 3.4 新增设备不用每台手工加

如果新交换机满足这些条件，通常不用去 LibreNMS 一台台 Add Device：

1. SNMP community 对。
2. IP 在 `LIBRENMS_DISCOVERY_TARGETS` 范围内。
3. IP 在 `DIST_SWITCH_PING` 或 `TOURNAMENT_SWITCHES` 范围内。
4. LLDP 开着，能被核心发现。

如果新设备不在范围里，改 `.env` 后跑 `./apply-env.sh`。

## 4. 改完配置怎么生效

### 4.1 日常改 `.env`

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

新版已经不靠运行时 `apk add jq` 渲染 Grafana provisioning。

### 6.2 ISP 超过阈值但没告警

按这个顺序看：

1. `.env` 里 `FEISHU_ROBOT_TOKEN` 是否填写。
2. 跑过 `./apply-env.sh` 没有。
3. LibreNMS 规则页里 `ISP 带宽饱和告警` 的 `Transports` 是否不是 `none`。
4. `BIGSCREEN_ISP_MAX_BANDWIDTH` 是否写运营商真实带宽，比如 300M 就写 `300`。
5. `ISP_SATURATION_PERCENT` 是否是你想要的阈值，比如 90。
6. 防火墙 WAN 口是否能被 `FIREWALL_WAN_IF_FILTER` 匹配到。

改完后跑：

```bash
./apply-env.sh
docker logs -f librenms-config
docker logs -f alertmanager-feishu-bridge
```

注意：LibreNMS poller 有周期，不是毫秒级触发。但规则页必须显示 Feishu transport，否则只会在 LibreNMS 内记录，不会推飞书。

### 6.3 防火墙能 ping，拓扑还是红

看 `.env` 里的 `FIREWALL_PING`。这里要写“监控服务器能 ping 通的物理防火墙地址”，不是 SNMP 逻辑地址，也不是旧网段地址。

例如：

```bash
FIREWALL_PING=FW1:192.168.11.2,FW2:192.168.11.3
FIREWALL_SNMP_TARGETS=FW:192.168.11.4
```

### 6.4 拓扑又出现占位 ISP 名称

不要手动写 `BIGSCREEN_ISP_NAMES`，让它自动发现：

```bash
BIGSCREEN_ISP_AUTO_DISCOVER=true
FIREWALL_WAN_IF_FILTER=telecom,telcom,unicom,isp,WAN
```

接口描述里有 `telecom`、`unicom`，拓扑和大屏就会显示真实名字。

### 6.5 LibreNMS 发现 0 个设备

先直接测 SNMP：

```bash
docker exec librenms snmpwalk -v2c -c public 192.168.10.254 sysName.0
```

不通就查设备 SNMP、ACL、防火墙策略、community。通了再看：

```bash
docker logs -f librenms-config
```

### 6.6 选手页面 No data

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
