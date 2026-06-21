# monitor-autoconfig

Docker Compose 一键部署的赛事网络监控栈，**LibreNMS + Prometheus + Grafana** 自动发现 + 飞书告警，外加一套**无需登录的对外展示大屏**，专为短期出差赛事设计：clone → 改 IP → 起服务 → 自检 → 用一阵子 → 拆机回收。

## 服务

| 服务 | 默认端口 | 用户 / 密码 | 用途 |
|---|---|---|---|
| Grafana | 3000 | `admin / root` | 运维 dashboard，编辑面板、临时排查 |
| 对外展示大屏 | 8088 | 无需登录（只读） | 现场电视 / 投屏，比赛中实时看 |
| LibreNMS | 8002 | `admin / librenms123` | 全网自动发现 + 拓扑图 + 飞书告警 |

### 默认凭据 / 改在哪

| 凭据 | 默认 | 改在哪 |
|---|---|---|
| Grafana 管理员 | `admin / root` | `.env` 里 `GRAFANA_PASSWORD`（首次起服务前改） |
| LibreNMS 管理员 | `admin / librenms123` | `.env` 里 `LIBRENMS_ADMIN_PASSWORD` |
| SNMP community | `public` | `.env` 里 `SNMP_COMMUNITY` + 交换机 / 防火墙 SNMP 配置同步改 |

MariaDB 内部账户（`mysql root` 等）只在容器网络内可达，不暴露到宿主机端口，可以维持默认。

8088 大屏是原生静态页面（nginx 直接托管，无构建步骤），直接读取 Prometheus API 自己渲染，不显示 Grafana 的搜索 / Share / Edit 等后台控件；Grafana 仍然保留给运维编辑 dashboard 和临时排查。机器如果暴露到不可信网络，请只允许现场内网访问 8088 / 3000 端口，并及时修改上面的默认密码。

## 一、装 Docker

脚本本身已含 `docker-compose-plugin`，不必再单独 `apt install docker-compose-plugin`。
国内服务器直连 `download.docker.com` 常超时，会报 `Unable to locate package
docker-compose-plugin` / `group 'docker' does not exist`，用阿里云镜像那套即可。

### Ubuntu（含 24.04）

**国内服务器**（阿里云镜像）：

```bash
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh --mirror Aliyun
sudo apt-get install -y git
sudo usermod -aG docker $USER && newgrp docker
```

**国外服务器**（官方源）：

```bash
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh
sudo apt-get install -y git
sudo usermod -aG docker $USER && newgrp docker
```

> 上次装失败留下的半截仓库会让重试继续报错，先清掉再重跑：
> `sudo rm -f /etc/apt/sources.list.d/docker.list && sudo apt-get update`

### CentOS

**国内服务器**（阿里云镜像）：

```bash
sudo yum install -y yum-utils git
sudo yum-config-manager --add-repo https://mirrors.aliyun.com/docker-ce/linux/centos/docker-ce.repo
sudo yum install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
sudo systemctl enable --now docker
sudo usermod -aG docker $USER && newgrp docker
```

**国外服务器**（官方源）：把上面的 repo 换成
`https://download.docker.com/linux/centos/docker-ce.repo`，其余命令不变。

### 1.5 解决 Docker Hub 国内拉取失败（国内必做）

Docker Hub 在国内被墙，`./deploy.sh` 会报 `i/o timeout`。推荐用本机 Clash 代理：

**方法：让 Docker daemon 走代理**（Clash 开启「允许局域网」，把 `电脑IP:端口` 改成实际值）

```bash
# 清掉 registry-mirrors（用代理不需要）
sudo tee /etc/docker/daemon.json <<'EOF'
{}
EOF

# 配 Docker daemon 走代理
sudo mkdir -p /etc/systemd/system/docker.service.d
sudo tee /etc/systemd/system/docker.service.d/http-proxy.conf <<'EOF'
[Service]
Environment="HTTP_PROXY=http://电脑IP:端口"
Environment="HTTPS_PROXY=http://电脑IP:端口"
Environment="NO_PROXY=localhost,127.0.0.1"
EOF

sudo systemctl daemon-reload
sudo systemctl restart docker
docker info | grep -i proxy   # 验证代理生效
```

> 用完后清代理：`sudo rm /etc/systemd/system/docker.service.d/http-proxy.conf && sudo systemctl daemon-reload && sudo systemctl restart docker`

## 二、部署 checklist

### 1. 拉代码

**国外服务器**（直连 GitHub）：

```bash
git clone https://github.com/yikkrrtykj/monitor-autoconfig.git
cd monitor-autoconfig/librenms+grafana
```

**国内服务器**（GitHub 经常连不上，用 Gitee 镜像）：

```bash
git clone https://gitee.com/yikkrrtykj/monitor-autoconfig.git
cd monitor-autoconfig/librenms+grafana
```

> Gitee 仓库是 GitHub 的镜像，国内访问稳定。如果 Gitee 上不是最新代码，
> 到 Gitee 仓库页面点「管理 → 仓库镜像管理」手动同步一次即可。

### 2. 写 `.env`

```bash
cp .env.example .env
vi .env
```

**必改项**：

```bash
SERVER_IP=                 # 监控服务器自身 IP
EVENT_NAME=                # 赛事名，留空则大屏标题显示“网络监控大屏”
LIBRENMS_BASE_URL=http://${SERVER_IP}:8002

# 给别人看的大屏
BIGSCREEN_TITLE=           # 留空时自动使用“EVENT_NAME 网络监控大屏”
BIGSCREEN_LOGO_TEXT=       # 可选，比如品牌名；留空则不显示左侧 logo
BIGSCREEN_ISP_NAMES=ISP1,ISP2  # 8088 大屏和 Grafana ISP 面板共用这个名单
BIGSCREEN_ISP_IPS=ISP1:1.1.1.1,ISP2:8.8.8.8  # 可选；写了以后 /topology 点击 ISP 也能看延迟
BIGSCREEN_ISP_MAX_BANDWIDTH=ISP1:500,ISP2:500,ISP3:300  # ISP 流量图/卡顿分析上限，并自动覆盖 LibreNMS WAN 口速率；单位 Mbps，也可只写 1000
BIGSCREEN_ISP_AUTO_DISCOVER=false  # true 时自动加入 Prometheus 发现到的 WAN 口
BIGSCREEN_STAGE_DEVICE_FILTER=stage,wutai,舞台  # 大屏只显示这些名字的 Ping / Uptime

# 基础设施 ping（Name:IP 格式，逗号分隔，支持 1-10 范围）
CORE_SWITCH_PING=Core:192.168.10.254
DIST_SWITCH_PING=SW1:192.168.10.11,SW2:192.168.10.12
FIREWALL_PING=FW1:192.168.1.1,FW2:192.168.1.2
SERVER_PING=Server:192.168.10.10
ISP_PING=                 # 可选；留空时自动使用 BIGSCREEN_ISP_IPS

# 防火墙 SNMP（建议用 Name:IP；如果只写 IP，会优先继承 FIREWALL_PING 里同 IP 的名字）
FIREWALL_SNMP_TARGETS=FW1:192.168.1.1,FW2:192.168.1.2

# LibreNMS 自动发现（交换机）
LIBRENMS_DISCOVERY_TARGETS=192.168.10.1-100,192.168.10.254
LIBRENMS_CORE_IP=192.168.10.254

# LibreNMS 自动发现（防火墙）
FIREWALL_DISCOVERY_RANGE=172.25.9.2-253,192.168.9.1-254
FIREWALL_SNMP_COMMUNITY=public   # 与 SNMP_COMMUNITY 不同时在此覆盖

# 飞书告警 token
FEISHU_ROBOT_TOKEN=              # Prometheus 告警 → 团队群（ISP 掉线 / 交换机离线）
LIBRENMS_FEISHU_TOKEN=           # LibreNMS 告警 → 运维群（设备离线 / 接口 down / 高 CPU）
```

> `SNMP_COMMUNITY` 默认 `public`，必须和交换机 / 防火墙上实际配置的 community 一致，否则 LibreNMS 发现 0 个设备、选手 MAC 表查不到。

**赛事监控用**（不接选手就留空）：

```bash
TOURNAMENT_SWITCHES=192.168.10.11,192.168.10.12   # 选手接入交换机 IP
PLAYER_SUBNETS=192.168.11.0/24                    # 选手有线网段（用于有线 / 无线分类）
WIRELESS_SUBNETS=192.168.12.0/24                  # 选手无线（备用，不用就留空）

# 选手网关 IP（L3 设备，承载 player VLAN 网关 / ARP 表）
# 舞台交换机是纯 L2 时必须设置：脚本会查这里的 ARP 表拿 IP→MAC，再用
# 舞台交换机的 MAC 表反推到 team X-Y 端口
# 留空则回退到 LIBRENMS_CORE_IP；两者都为空时只查舞台本机的 ARP 表
PLAYER_GATEWAYS=192.168.10.254
# 网关 SNMP community 不同时在这里覆盖，否则用 SNMP_COMMUNITY
PLAYER_GATEWAY_SNMP_COMMUNITY=

# 选手 VLAN ID（逗号分隔）。Cisco / H3C 默认只在 VLAN 1 暴露 BRIDGE-MIB MAC 表，
# 设上 VLAN ID 后脚本会用 community@vlan_id 的方式查每个 VLAN 的 MAC 表合并起来。
# 不配会导致 Cisco 上 stage MAC 表查到 0 条，gateway-ARP join 永远匹配不到选手。
# 例: PLAYER_VLAN_IDS=11,12   （11=有线选手，12=无线选手 AP 上行）
PLAYER_VLAN_IDS=11,12

# 选手发现周期（秒），默认 300。脚本每周期在每台舞台交换机上走一遍 MAC/ARP 表定位选手座位，
# 这是交换机 SNMP 负载的主要来源。2960 等弱 CPU 交换机拉长间隔能显著减负。
# 只影响“重新发现有哪些选手”，不影响“多久 ping 一次”——已发现的选手由 blackbox 每 5s 探测，
# 掉线 ~5s 内大屏就显示离线，不受此值影响。换设备拿到新 IP 时，可在座位核对页点“立即重扫”秒级触发。
PLAYER_TARGETS_REFRESH_INTERVAL=300

# 选手 ping 采集间隔，默认 5s（基础设施仍是全局 10s）。更快的采样让断线/纠纷复盘的
# 时间分辨率更细，延迟查询和卡顿分析能定位到更精确的时刻。
PLAYER_PING_SCRAPE_INTERVAL=5s

# 端口 link-down 时跳过该 team 位（默认开）。交换机 MAC 老化要 5 分钟、网关 ARP
# 老化要 4 小时，断开后这两个表里残留的条目会让脚本误以为有人，造成“挂着显示红色”。
# 开启后下次脚本运行（默认 300 秒内，或手动重扫立即）就会自动清掉幻影 target。极少数
# 老型号交换机不报 ifOperStatus，可改成 false 关闭这个检查
PLAYER_REQUIRE_LINK_UP=true

# 无线扫描：只按 WIRELESS_SUBNETS ping 扫在线 IP，生成 network=wireless 的选手 targets
# 面板选 wired 只看有线，选 wireless 只看无线扫描，互不影响
# 无线不知道真实座位，左右队和座位只是按 IP 排序临时分配，用来看 WiFi 连接数量/效果
# 默认开启，比赛中可以随时切到 wireless 看有没有人连 WiFi；不需要时改成 false
# LIMIT=0 表示不限制在线 IP 数量，只受 PLAYER_WIRELESS_SCAN_MAX_HOSTS 的扫描保护限制
PLAYER_WIRELESS_SCAN=true
PLAYER_WIRELESS_SCAN_LIMIT=0
PLAYER_WIRELESS_SCAN_EXCLUDE_GATEWAYS=true     # 默认排除 .254 这类网关地址
PLAYER_WIRELESS_SCAN_EXCLUDE=                  # 额外排除 AP/网关/服务器 IP；例: 192.168.12.220-254,192.168.12.10

# WiFi-only 比赛如果无法从交换机端口自动映射选手，可手动指定 10 个选手 IP
PLAYER_STATIC_TARGETS=1-1=192.168.12.101,1-2=192.168.12.102,2-1=192.168.12.201
PLAYER_STATIC_NETWORK=wireless
```

**拓扑链路流量**：

```bash
# 默认关闭。/topology 只显示状态、延迟、LLDP 连接和上联摘要，不采集交换机接口流量。
```

这样不会为了看拓扑反复读取核心交换机接口计数器；需要看吞吐时建议临时到交换机 / LibreNMS / Grafana 单独查。

**两种舞台交换机拓扑都支持：**

- **L3 舞台**（端口描述 `team X-Y` 在舞台、SVI 也在舞台、ARP 表在舞台）：`PLAYER_GATEWAYS` 留空即可，脚本直接查舞台 ARP。
- **L2 舞台**（端口描述 `team X-Y` 在舞台，但 ARP 表在核心 / 防火墙）：把 L3 网关 IP 写入 `PLAYER_GATEWAYS`，脚本会查网关 ARP 拿到 `IP→MAC`，再查舞台 MAC 表 (`dot1dTpFdbPort` / `dot1qTpFdbPort`) 反推到 team 端口。`docker logs player-targets` 会看到每台舞台的 `ifAlias / MAC 表 / bridgePort` 计数。
- 端口已经标了 `team X-Y` 就是真理：即便 IP 不在 `PLAYER_SUBNETS` 也会作为 wired 发出，避免网段写错时静默丢数据。

**Cisco 交换机必须额外配 `PLAYER_VLAN_IDS`**：

Cisco IOS / IOS-XE 的 BRIDGE-MIB 默认只暴露 VLAN 1 的 MAC 表，非 VLAN 1 的数据需要用 community-indexing 形式访问（`community@vlan_id`）。`PLAYER_VLAN_IDS=11,12` 后脚本会同时查 `public@11`、`public@12`，把每个 VLAN 的 MAC 表合并。不配会出现 stage MAC 表显示 0 条但端口实际有人。注意 `PLAYER_VLAN_IDS` 只填**有线选手** VLAN，无线 AP 上行的 VLAN 走 ping 扫描即可，列进来只会徒增每台交换机的 MAC walk 负载。

**幻影 target 防护（默认开）**：

选手拔掉网线后，stage 交换机 MAC 表里的 MAC 要 5 分钟才老化、核心 ARP 表里的 IP 要 4 小时才老化，期间 join 路径会以为还有人，大屏上挂着红色。`PLAYER_REQUIRE_LINK_UP=true`（默认）查 `ifOperStatus`，端口不是 up 就跳过，下次发现周期（默认 300 秒内）幻影自动消失；急用时到座位核对页点 **↻ 立即重扫** 秒级清掉。极少数老型号交换机不报 ifOperStatus，可改 `false` 关闭这个检查。

### 3. 起服务

```bash
chmod +x deploy.sh
./deploy.sh
```

首次 5-8 分钟（拉镜像 + DB 初始化 + 自动配置）。`deploy.sh` 会先串行拉镜像并自动重试，避免 Docker Hub / CDN 偶发 502 导致整次部署中断。

### 3.1 打开给别人看的大屏

```text
http://服务器IP:8088
```

现场电视 / 投屏电脑打开这个地址后按 `F11` 全屏。这个页面直接从 Prometheus 读取数据并自己渲染，运维需要编辑 Grafana dashboard 时仍然进 `http://服务器IP:3000`。页面对手机也做了自适应（窄屏单列布局），临时用手机看一眼也行。

首页会列出所有大屏入口：

| 入口 | 路径 | 用途 |
|---|---|---|
| 网络总览 | `/infra` | 核心 / 接入 ping、运行时长、丢包热力图、ISP 流量 |
| 延迟查询 | `/latency` | 按队伍 / 座位 / 时间查单个选手的延迟和断线，可导出 CSV |
| 卡顿分析 | `/incident` | 输入卡顿时间点，自动关联基础设施 / 同台选手 / ISP 流量给出根因 |
| 质量热图 | `/heatmap` | 按 (队伍, 座位) 显示一段时间的离线率和平均延迟，可导出 CSV |
| 网络拓扑 | `/topology` | ISP → 防火墙 → 核心 → 接入的实时状态拓扑（LLDP 自动发现） |
| 无线总览 | `/wireless` | 只看无线选手，确认有没有人连 WiFi、是否高延迟 / 离线 |
| 座位核对 | `/seat-check` | 赛前按赛制核对队伍座位在线，缺失 / 重复 / 离线直接标出 |
| 比赛大屏 | `/match-5v5` `/tournament-6` 等 | 5v5、6 队、三种 64 人摆法的实时对战面板 |

进入比赛大屏后不会再显示切换按钮，避免现场误点。

**选手换设备（手机 / 电脑坏了临时换）**：换的设备如果拿到**新 IP**，默认要等下一个发现周期（300 秒）才会被扫到。等不及就进 `/seat-check`，点右上角 **↻ 立即重扫**，几秒内就会重新发现有线目标，无须等待。断线本身（IP 不变）由 blackbox 每 5s 探测，~5 秒内大屏就显示离线，不受发现周期影响。

如果选手说“卡了”，打开：

```text
http://服务器IP:8088/latency
```

按 `队伍 + 座位 + 网络 + 查询时间 + 窗口` 查询。延迟查询页会同时显示：

- 延迟趋势：这个时间窗口内该选手的 ping 延迟，显示原始采样、和 Grafana 一致，尖刺即时出现、即时恢复。
- 在线状态：`probe_success` 的在线 / 失败采样；在线率不是 100% 或失败时长大于 0，就说明这个窗口内确实有断线 / 探测失败。
- 汇总卡片：自动汇总平均延迟、最高延迟、在线率、失败时长，适合截图给裁判 / 选手确认。
- **导出 CSV**：把这段时间的延迟 / 在线原始采样导出存证，方便事后复盘或给裁判留底。

窗口含义是“查询时间前后 N 分钟”。如果查询时间接近当前时间，结束时间会自动封顶到当前时间，避免图表画到未来。

Grafana 里手动查同一件事时，用这两类 PromQL：

```promql
probe_icmp_duration_seconds{role="player",team="1",seat="3",network="wired",phase="rtt"} * 1000
probe_success{role="player",team="1",seat="3",network="wired"}
```

`team` / `seat` 对应交换机 description 里的 `teamX-Y`；无线扫描不知道真实座位时，会按 IP 排序临时分配座位，只适合看“有多少人连 WiFi”和大概延迟状态。

### 4. 跑赛前自检

```bash
./pre-match-check.sh
```

输出每条监控链路的状态：容器、Prometheus 抓取目标、设备 ping、选手 targets 注册情况、ISP 链路检测、Grafana 加载情况。绿色 = OK，红色 = 要解决。

自动修复：

```bash
./pre-match-check.sh --fix
```

- 如果 dispatcher 不在跑 → `docker compose up -d librenms-dispatcher`
- 如果没设备 → `docker compose up -d --force-recreate librenms-config`（重跑自动发现，会读 `.env` 里的 `LIBRENMS_DISCOVERY_TARGETS` 扫描）

## 三、常见问题

**服务起不来 / 一直重启**

```bash
docker compose ps
docker compose logs --tail=100 <service-name>
```

**LibreNMS 显示发现 0 个设备**

```bash
docker exec librenms snmpwalk -v2c -c public 192.168.10.254 sysName.0
```

不通 = 防火墙策略 / community 错 / 设备没开 SNMP。注意 community 要和 `.env` 里 `SNMP_COMMUNITY`（默认 `public`）一致。

**选手 dashboard 全是 No data**

1. `./pre-match-check.sh` 看选手 targets 注册了多少
2. 0 个 = 检查 `TOURNAMENT_SWITCHES` 配了没 + 交换机端口 ifAlias 是否按约定命名
3. 舞台是 L2 时一定要配 `PLAYER_GATEWAYS`（核心 / 防火墙 IP），否则脚本拿不到选手 IP→MAC 映射。看 `docker logs player-targets` 是否有 `gateway-ARP join: matched N IPs`
4. 排错 snmpwalk（community 用 `.env` 里的 `SNMP_COMMUNITY`，默认 `public`）：
   ```bash
   # 舞台是否学到选手 MAC（先 BRIDGE-MIB，空就 Q-BRIDGE-MIB）
   docker exec player-targets snmpwalk -v2c -c public <STAGE_IP> 1.3.6.1.2.1.17.4.3.1.2 | head
   docker exec player-targets snmpwalk -v2c -c public <STAGE_IP> 1.3.6.1.2.1.17.7.1.2.2.1.2 | head
   # 网关 ARP 是否含选手 IP
   docker exec player-targets snmpwalk -v2c -c public <GATEWAY_IP> 1.3.6.1.2.1.4.22.1.2 | head
   ```
5. 看 WiFi 连接数量 / 效果 = 确认 `PLAYER_WIRELESS_SCAN=true`，脚本会扫 `WIRELESS_SUBNETS` 并生成 `network="wireless"` targets
6. WiFi-only 比赛通常不能靠交换机端口自动映射每个选手，固定 IP 场景可在 `.env` 填 `PLAYER_STATIC_TARGETS`
7. 注册了但都离线 = 选手电脑 / 手机没接好
8. 选手换了设备拿到新 IP，等不及发现周期 = 到 `/seat-check` 点 **↻ 立即重扫**

**改了 .env 后某些数据不更新**

- 改 `TOURNAMENT_SWITCHES` / `PLAYER_GATEWAYS` / `PLAYER_SUBNETS` / `WIRELESS_SUBNETS` / `PLAYER_STATIC_TARGETS` / `PLAYER_WIRELESS_SCAN` → `player-targets` 每个发现周期（默认 300 秒）自动读取 `.env`；要立刻生效就到大屏 `/seat-check` 点 **↻ 立即重扫**
- 日常改选手交换机、选手有线 / 无线网段、静态选手名单，不需要重建容器；看 `docker logs -f player-targets` 确认日志里的实际值
- 改基础设施 Ping / 防火墙 SNMP / 选手 ping 间隔 / Prometheus 保留时间 → 重启 prometheus
- 改大屏标题、时间、ISP 名称 / 过滤条件 / ISP 自动发现开关 / 舞台设备过滤，或大屏前端代码 → `docker compose up -d --force-recreate bigscreen`
- 改 Grafana ISP 名单 / 自动发现开关 → 重新运行 `./deploy.sh` 渲染 provisioning，然后 `docker compose restart grafana`
- 改 dashboard JSON → Grafana 30 秒自动 reload，或 `docker compose restart grafana`

**重置所有数据从头开始**

```bash
docker compose down -v
rm -rf grafana-data librenms-db-data librenms-data librenms-rrdcached-journal prometheus-data
./deploy.sh
```

## 四、赛后清理

赛事结束、服务器要回收（或换给别人用）。Docker 的两种数据存法要分开清：

**容器 + 镜像 + 命名卷**（grafana-data、prometheus-data、player-targets-data）：

```bash
docker compose down -v        # 停容器、删 monitor 网络、删命名卷
docker system prune -a        # 删镜像（占空间最多）
```

**bind mount 目录**（librenms 数据落在仓库目录里）——`down -v` 不会删，要手动：

```bash
sudo rm -rf librenms-data librenms-db-data librenms-rrdcached-journal
```

要 `sudo` 是因为 `init-permissions` 容器把这些目录 chown 给了容器内 UID（librenms=1000、grafana=472 等），普通用户删不掉。

**仓库本身也删**：

```bash
cd .. && rm -rf monitor-autoconfig
```

到这一步服务器上完全没痕迹，可以放心给别人。

数据要带走的话先备份再清：

```bash
tar czf monitor-backup-$(date +%Y%m%d).tar.gz \
  librenms-data/ librenms-db-data/ \
  .env grafana-provisioning/
```

注意 `prometheus-data/` 和 `grafana-data/` 是命名卷，不在仓库目录里——Prometheus 数据短期赛事不值得带（默认 15d 保留），Grafana dashboard 都在 git 里随 `grafana-provisioning/` 走。

下次新场子直接 `git clone` + `cp .env.example .env` 重头来。

## 五、大屏前端结构（开发者参考）

8088 大屏是无框架、无构建的纯静态页面，nginx 直接托管 `bigscreen/` 目录。前端按职责拆成几个模块，浏览器按顺序加载（见 `index.html`）：

| 文件 | 职责 |
|---|---|
| `config.js` / `pages.js` | 运行时配置（由容器启动时按 `.env` 生成）和页面 / PromQL 定义 |
| `utils.js` | 纯函数：格式化、转义、SVG path、CSV 构造等 |
| `api.js` | Prometheus 数据层：查询、范围缓存、ISP 流量、拓扑数据 |
| `players.js` | 选手去重 / 座位映射 / 状态判定 |
| `incident.js` | 卡顿根因分析的规则与裁决 |
| `topology.js` | 拓扑分层、布局、SVG 渲染 |
| `app.js` | 页面编排：路由、各面板渲染、定时刷新 |

纯逻辑模块带单元测试，跑：

```bash
cd librenms+grafana
for t in tests/test_bigscreen_*.js; do node "$t"; done
```

`player-targets` 容器内另跑一个轻量 HTTP 服务（端口 9199），大屏的“立即重扫”按钮通过 nginx 的 `/player-targets/` 反代 POST 到它，触发一次即时的选手发现。
