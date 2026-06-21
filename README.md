# 赛事网络监控

这套仓库是给现场网络值守用的。

它把 LibreNMS、Prometheus、Grafana、blackbox、SNMP exporter 和一个免登录大屏放在一起：设备自动发现，ISP 流量和设备状态自动告警，现场电视打开 8088 就能看。目标很简单：到场、改 `.env`、启动、自检、比赛、赛后清理。

## 看什么

| 入口 | 默认端口 | 用途 |
|---|---:|---|
| 大屏 | 8088 | 给现场电视 / 投屏看，不需要登录 |
| Grafana | 3000 | 运维看图、查历史、临时排查 |
| LibreNMS | 8002 | 设备发现、端口流量、告警规则 |

默认账号：

| 服务 | 默认账号 |
|---|---|
| Grafana | `admin / root` |
| LibreNMS | `admin / librenms123` |

正式用之前建议在 `.env` 里改掉 `GRAFANA_PASSWORD`、`LIBRENMS_ADMIN_PASSWORD` 和 `SNMP_COMMUNITY`。8088 大屏没有登录，机器别直接暴露到公网。

## 第一次部署

如果服务器已经装好 Docker 和 git，可以直接跳到“拉代码”。

### 装 Docker

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

如果上次 Docker 装到一半失败，先清一下旧源再重试：

```bash
sudo rm -f /etc/apt/sources.list.d/docker.list
sudo apt-get update
```

### 国内拉不动镜像

`./deploy.sh` 会拉 Docker Hub 镜像。国内服务器如果一直 `i/o timeout`，推荐让 Docker daemon 走你本机 Clash 代理。Clash 先打开“允许局域网”，把下面的 `电脑IP:端口` 换成实际值：

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

用完后想清掉代理：

```bash
sudo rm -f /etc/systemd/system/docker.service.d/http-proxy.conf
sudo systemctl daemon-reload
sudo systemctl restart docker
```

### 拉代码

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

如果 Gitee 不是最新，去 Gitee 页面手动同步一次镜像。

### 写 `.env`

```bash
cp .env.example .env
vi .env
```

先把这几类填对，系统就能跑起来。

#### 服务器和密码

```bash
SERVER_IP=192.168.10.200
EVENT_NAME=某某比赛
SNMP_COMMUNITY=public
GRAFANA_PASSWORD=改一个密码
LIBRENMS_ADMIN_PASSWORD=改一个密码
FEISHU_ROBOT_TOKEN=
```

`SERVER_IP` 留空时，`deploy.sh` 会自动探测并写回 `.env`。飞书 token 留空也能用，只是告警不会推到群里。

#### 基础设施

```bash
CORE_SWITCH_PING=Core:192.168.10.254
DIST_SWITCH_PING=SW:192.168.10.11-22
FIREWALL_PING=FW1:192.168.11.2,FW2:192.168.11.3
SERVER_PING=

FIREWALL_SNMP_TARGETS=FW:192.168.11.4
LIBRENMS_DISCOVERY_TARGETS=192.168.10.1-100,192.168.10.254
FIREWALL_DISCOVERY_RANGE=192.168.11.2-4
```

几个容易混的点：

- `FIREWALL_PING` 只写物理防火墙、而且要写能 ping 通的地址。HA 逻辑地址不要放这里。
- `FIREWALL_SNMP_TARGETS` 可以写 HA 逻辑地址，用来抓端口流量和运行时长。
- 没有业务服务器就让 `SERVER_PING=` 留空，大屏不会显示“服务器”这一块。
- `DIST_SWITCH_PING=SW:192.168.10.11-22` 会自动展开成 `SW1`、`SW2`，不用每台手写。

#### ISP 和带宽告警

```bash
BIGSCREEN_ISP_AUTO_DISCOVER=true
FIREWALL_WAN_IF_FILTER=telecom,telcom,unicom,isp,WAN
BIGSCREEN_ISP_MAX_BANDWIDTH=300
ISP_SATURATION_PERCENT=90
```

默认推荐自动发现 ISP：脚本会从防火墙 WAN 口的接口名、描述、别名里匹配 `telecom/unicom/WAN` 这些词。你的端口描述已经写了 `telecom`、`unicom`，就不需要手动写 `BIGSCREEN_ISP_NAMES`。

告警按你写的运营商带宽算，不按物理口速率算。比如：

- `BIGSCREEN_ISP_MAX_BANDWIDTH=300`
- `ISP_SATURATION_PERCENT=90`
- 告警线就是 `300 Mbps * 90% = 270 Mbps`

如果写 `1000`，告警线就是 `900 Mbps`。`apply-env.sh` 会把这个速率同步到 LibreNMS 的防火墙 WAN 口，LibreNMS 里的 `port_usage_perc` 才会按运营商带宽算。

多条 ISP 不同带宽也可以：

```bash
BIGSCREEN_ISP_MAX_BANDWIDTH=telecom:300,unicom:500
```

非对称专线写下行/上行：

```bash
BIGSCREEN_ISP_MAX_BANDWIDTH=telecom:1000/100,unicom:500/50
```

#### 选手监控

不接选手就留空。要按座位看选手延迟，交换机端口描述要写成 `team X-Y` 或项目里支持的同类格式。

```bash
TOURNAMENT_SWITCHES=192.168.10.11-22
PLAYER_SUBNETS=192.168.40.0/24
WIRELESS_SUBNETS=192.168.41.0/24
PLAYER_GATEWAYS=192.168.10.254
PLAYER_VLAN_IDS=40
```

怎么理解：

- `TOURNAMENT_SWITCHES` 是选手接入交换机，支持范围，不用一台台写。
- 舞台交换机如果自己就是三层网关，`PLAYER_GATEWAYS` 可以留空。
- 舞台交换机如果只是二层，`PLAYER_GATEWAYS` 要写核心 / 防火墙网关 IP，脚本靠它拿 `IP -> MAC`，再回到接入交换机反查座位端口。
- Cisco / H3C 查非 VLAN 1 的 MAC 表时，通常要填 `PLAYER_VLAN_IDS`。只填有线选手 VLAN，别把无线 VLAN 全塞进去，弱 CPU 交换机会吃不消。
- 选手换电脑或手机拿到新 IP，不想等默认 300 秒发现周期，就去 `/seat-check` 点“立即重扫”。

WiFi-only 比赛如果没法从交换机端口映射座位，可以手写静态目标：

```bash
PLAYER_STATIC_TARGETS=1-1=192.168.12.101,1-2=192.168.12.102,2-1=192.168.12.201
PLAYER_STATIC_NETWORK=wireless
```

### 启动

```bash
chmod +x *.sh
./deploy.sh
```

第一次会比较久，主要是拉镜像和初始化数据库。`deploy.sh` 会串行拉镜像并自动重试，尽量避开 Docker Hub/CDN 偶发失败。

启动后跑一次自检：

```bash
./pre-match-check.sh
```

能自动补的项目可以加 `--fix`：

```bash
./pre-match-check.sh --fix
```

## 平时怎么用

常用地址：

```text
http://服务器IP:8088        大屏首页
http://服务器IP:8088/infra  网络总览
http://服务器IP:8088/topology  网络拓扑
http://服务器IP:8088/latency   延迟查询
http://服务器IP:8088/incident  卡顿分析
http://服务器IP:8088/seat-check  座位核对 / 立即重扫
http://服务器IP:3000        Grafana
http://服务器IP:8002        LibreNMS
```

比赛中一般看 8088。运维排查再进 Grafana 和 LibreNMS。

大屏几个页面的用途：

| 页面 | 用途 |
|---|---|
| `/infra` | 核心、接入、防火墙、ISP、丢包热力图 |
| `/topology` | ISP 到防火墙、核心、接入的拓扑状态 |
| `/latency` | 按队伍、座位、时间查单个选手延迟，可导出 CSV |
| `/incident` | 输入卡顿时间点，自动关联 ISP、同台选手、基础设施 |
| `/heatmap` | 一段时间内的离线率和平均延迟 |
| `/wireless` | 只看无线选手 |
| `/seat-check` | 赛前核对座位，选手换设备后可立即重扫 |
| `/match-5v5`、`/tournament-6` 等 | 比赛实时大屏 |

## 加设备不用每台都手工加

现场最麻烦的是加交换机，这里尽量用范围解决。

如果新交换机在已有范围里：

- SNMP community 对
- IP 在 `LIBRENMS_DISCOVERY_TARGETS` 范围内
- IP 在 `DIST_SWITCH_PING` 或 `TOURNAMENT_SWITCHES` 范围内
- LLDP 开着，能被核心发现

那就不需要去 LibreNMS 一台台 Add Device。LibreNMS 会自动发现，大屏和 Prometheus 也会按范围生成目标。

如果新设备不在范围里，改 `.env` 后跑：

```bash
./apply-env.sh
```

选手交换机或选手网段改了，但想立刻看到结果，去：

```text
http://服务器IP:8088/seat-check
```

点“立即重扫”。

## 改了 `.env` 后跑什么

日常改配置，优先跑：

```bash
./apply-env.sh
```

它不重新拉镜像，会重建需要读取 `.env` 的容器，并重跑 LibreNMS 自动配置、飞书 transport、告警规则、Grafana provisioning。

什么时候跑 `./deploy.sh`：

- 第一次部署
- `git pull` 后代码升级
- 镜像需要重新拉
- 想完整重启整个栈

什么时候不用重启：

- `TOURNAMENT_SWITCHES`、`PLAYER_GATEWAYS`、`PLAYER_SUBNETS`、`WIRELESS_SUBNETS` 这类选手发现配置，`player-targets` 会按发现周期自动读；急用就点 `/seat-check` 的立即重扫。
- 只是看选手断线，已发现目标由 blackbox 每 5 秒 ping 一次，不受 300 秒发现周期影响。

看配置有没有生效：

```bash
docker logs -f librenms-config
docker logs -f player-targets
docker logs -f topology-collector
```

## 常见问题

### deploy 一直卡住

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

新版已经不靠运行时 `apk add jq` 渲染 Grafana provisioning，正常不应该再卡在这个容器。

### ISP 已经跑满，为什么没告警

按下面四项看：

1. `.env` 里 `FEISHU_ROBOT_TOKEN` 有没有填。填完跑 `./apply-env.sh`，LibreNMS 规则页的 `Transports` 不应该是 `none`。
2. `BIGSCREEN_ISP_MAX_BANDWIDTH` 有没有写运营商真实带宽。300M 就写 `300`，不是看物理口 1G。
3. `ISP_SATURATION_PERCENT` 是阈值。写 `90` 就是带宽的 90%。
4. 防火墙 WAN 口有没有被匹配到。端口描述里写 `telecom`、`unicom` 最稳；如果没描述，就把真实端口名加进 `FIREWALL_WAN_IF_FILTER`。

改完后跑：

```bash
./apply-env.sh
docker logs -f librenms-config
```

LibreNMS poller 有周期，规则不是毫秒级触发，等一两个轮询周期再看。

### 防火墙明明能 ping，拓扑还是红

看 `.env` 里的 `FIREWALL_PING`。这里要写“监控服务器能 ping 通的物理防火墙地址”，不是 SNMP 逻辑地址，也不是别的网段旧地址。

例如物理节点是 `192.168.11.2/11.3`：

```bash
FIREWALL_PING=FW1:192.168.11.2,FW2:192.168.11.3
FIREWALL_SNMP_TARGETS=FW:192.168.11.4
```

### 拓扑又出现占位 ISP 名称

默认不要手动写 `BIGSCREEN_ISP_NAMES`，让它自动发现：

```bash
BIGSCREEN_ISP_AUTO_DISCOVER=true
FIREWALL_WAN_IF_FILTER=telecom,telcom,unicom,isp,WAN
```

接口描述里有 `telecom`、`unicom`，拓扑和大屏就会显示真实名字。

### LibreNMS 发现 0 个设备

先直接测 SNMP：

```bash
docker exec librenms snmpwalk -v2c -c public 192.168.10.254 sysName.0
```

不通就查设备 SNMP、ACL、防火墙策略、community。通了再看：

```bash
docker logs -f librenms-config
```

### 选手页面 No data

先看生成了多少 target：

```bash
./pre-match-check.sh
docker logs -f player-targets
```

重点看这几项：

- `TOURNAMENT_SWITCHES` 有没有覆盖选手接入交换机。
- 交换机端口描述是不是按座位写了。
- 二层舞台有没有填 `PLAYER_GATEWAYS`。
- Cisco / H3C 非 VLAN 1 有没有填 `PLAYER_VLAN_IDS`。
- 日志里有没有 `gateway-ARP join: matched N IPs`。

手动排 SNMP：

```bash
docker exec player-targets snmpwalk -v2c -c public <STAGE_IP> 1.3.6.1.2.1.17.4.3.1.2 | head
docker exec player-targets snmpwalk -v2c -c public <STAGE_IP> 1.3.6.1.2.1.17.7.1.2.2.1.2 | head
docker exec player-targets snmpwalk -v2c -c public <GATEWAY_IP> 1.3.6.1.2.1.4.22.1.2 | head
```

## 赛后清理

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

## 文件说明

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
