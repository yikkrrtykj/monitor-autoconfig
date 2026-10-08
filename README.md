# 赛事网络监控

一套面向电竞现场的网络监控平台：LibreNMS 负责设备和端口，Prometheus/Grafana 负责采集和图表，8088 大屏负责现场看板、基础配置、问题清单、事故记录和交换机配置巡检。

开发协作从 [AGENTS.md](AGENTS.md) 开始；工程、Git、文档规范及历史交接索引见 [工程文档](docs/README.md)。活跃 TODO 和验收进度见 [GitHub Issues](https://github.com/yikkrrtykj/monitor-autoconfig/issues)，代码改动和 CI 见关联 PR；旧 `todo.md`、STATUS 和迭代记录已归档到 [历史 Issue](https://github.com/yikkrrtykj/monitor-autoconfig/issues/16)。

UniFi AP 的部署通知与 AP 掉线/恢复以 MAC 作为物理身份；同 MAC 换 IP、改名或 Bridge 重启不会创建新的部署通知生命周期。Controller 不可用时使用持久 AP inventory；已知 AP 但 MAC 无法唯一确定时暂缓通知。历史通知记录保留并按唯一归属迁移，新 MAC 仍正常通知。A-02.1 的升级、兼容边界及验收见 [本次操作手册](docs/runbooks/a-02.1-unifi-mac-hotfix.md)。

链路聚合告警仅在聚合及成员状态明确恢复时发送恢复通知。采集数据消失、接口被关闭或聚合配置被移除时保留已有故障状态及起点，不把缺失数据当作恢复。

已有登录凭据文件损坏或超限时，登录与需要认证的接口明确失败并保留原文件，不自动恢复默认密码。配置回滚先检查快照的文件存在标记及全部所需备份；快照不完整时停止，不删除当前配置或先恢复其中一份。

## 入口

| 服务 | 默认地址 | 用途 |
|---|---|---|
| 大屏 / 控制台 | `http://服务器IP:8088` | 现场展示，`/control` 做配置、问题清单和事故记录 |
| Grafana | `http://服务器IP:3000` | 查图、查历史、临时排障 |
| LibreNMS | `http://服务器IP:8002` | 设备发现、端口流量、告警规则 |

默认账号：

| 服务 | 默认账号 |
|---|---|
| Grafana | `admin / global123!@#` |
| LibreNMS | `admin / global123!@#` |
| 赛事控制台 | `admin / global123!@#` |

该 appliance 面向受信任内网的赛事现场快速部署，三个入口可直接使用以上默认账号。需要自定义时可修改 `.env` 里的 `GRAFANA_PASSWORD`、`LIBRENMS_ADMIN_PASSWORD` 和 `PLATFORM_ADMIN_PASSWORD`；`SNMP_COMMUNITY` 是独立的设备采集凭据。即使启用了 8088 控制台登录，也仍不建议把 appliance 直接暴露到公网。

## 部署

### 0. 服务器要求

- Linux x86_64，推荐 Ubuntu 20.04+ / Debian 11+（其它能跑 Docker 的发行版也可以）
- 建议 4 核 / 8G 内存 / 100G 磁盘起步（Prometheus 默认保留 15 天数据）
- 监控服务器要和交换机管理网段互通（SNMP 出向采集），交换机能把 syslog 发到它

需要预装：Docker（含 compose v2 插件）、git、python3。下面从零开始装。

### 1. 先选适合你的安装方式

下面两种方式选一种，从头按顺序做即可，不需要两边来回找命令。命令在 Linux 服务器的 root Bash 终端执行，适用于 Docker 官方支持的 Debian/Ubuntu（例如 Ubuntu 24.04）。其它系统请看 [Docker 官方安装说明](https://docs.docker.com/engine/install/)。

这些步骤用于第一次安装。已有 Docker 可以跳过安装；已经下载过本项目，也不要重复 `git clone`，更不要用示例文件覆盖已经填好的 `.env` 和 `event-config.yml`。每个代码块成功后再做下一步，有报错先停下来。

### 2. 能直接访问外网：按这套安装

服务器能直接访问 Docker、GitHub 和软件下载网站时，用这一套，不需要 Clash。

**先安装 Docker 和 Compose：**

```bash
curl -fL --connect-timeout 10 --max-time 120 \
  https://get.docker.com -o /tmp/get-docker.sh &&
  test -s /tmp/get-docker.sh &&
  bash /tmp/get-docker.sh &&
  systemctl daemon-reload &&
  systemctl enable --now docker &&
  docker --version &&
  docker compose version &&
  systemctl is-active docker
```

正常情况下会看到 Docker 和 Compose 的版本号，最后是 `active`。如果下载连接被重置或超时，可以改用下面第 3 节的 Clash 方式。安装失败后出现 `docker.service does not exist`，是因为 Docker 还没装好，不要反复执行启动命令。

**再安装 git 和 python3：**

```bash
apt-get -o APT::Update::Error-Mode=any update &&
apt-get install -y git python3
```

**第一次下载项目并启动：**在准备保存项目的目录执行，例如 `/root`。如果该目录下已经有 `monitor-autoconfig`，不要再次复制这一块。

```bash
cd /root &&
git clone https://github.com/yikkrrtykj/monitor-autoconfig.git &&
cd monitor-autoconfig/librenms+grafana &&
cp .env.example .env &&
cp event-config.example.yml event-config.yml &&
chmod +x *.sh &&
./deploy.sh
```

看到 `Platform bootstrap completed successfully` 后，按第 4 节打开网页。若容器已经启动但最后检查失败，先看第 5 节，不必马上重装。

### 3. 需要 Clash 代理：按这套安装

这一套适用于服务器能访问运行 Clash 的电脑，例如电脑与服务器在同一个局域网。安装和下载期间保持电脑、Clash 在线。

#### 3.1 让服务器连到 Clash

在 Clash 开启“允许局域网连接 / Allow LAN”，找到 HTTP 或 Mixed 端口。不要使用控制/API 端口或纯 SOCKS 端口。在 Windows PowerShell 输入 `ipconfig`，找到电脑的局域网 IPv4；防火墙允许服务器访问代理端口，不把代理开放到公网。

下面的 `192.168.1.100:7890` 只是例子，换成你的电脑 IP 和 Clash 端口。后面所有出现这个地址的地方都要一起替换。

在服务器执行：

```bash
export http_proxy="http://192.168.1.100:7890"
export https_proxy="$http_proxy"
export HTTP_PROXY="$http_proxy"
export HTTPS_PROXY="$http_proxy"
export no_proxy="localhost,127.0.0.1,::1"
export NO_PROXY="$no_proxy"
```

`https_proxy` 仍写成 `http://`，因为这里连接的是 HTTP 代理。不要把电脑的 `127.0.0.1` 填成服务器的代理地址。若 Clash 就装在服务器本机，可以用它的本机 HTTP/Mixed 地址。设置只在当前终端有效，换终端后需要重新设置。[Clash 配置说明](https://wiki.metacubex.one/config/general/)

#### 3.2 安装 Docker、git 和 python3

保持当前终端打开，执行：

```bash
curl -fL --connect-timeout 10 --max-time 120 \
  https://get.docker.com -o /tmp/get-docker.sh &&
  test -s /tmp/get-docker.sh &&
  bash /tmp/get-docker.sh &&
  systemctl daemon-reload &&
  systemctl enable --now docker &&
  docker --version &&
  docker compose version &&
  systemctl is-active docker
```

两个版本命令都有输出、最后显示 `active`，就可以继续。下载仍然失败时，检查 Clash、IP、端口、Allow LAN 和防火墙，先不要往下做。

```bash
apt-get -o APT::Update::Error-Mode=any update &&
apt-get install -y git python3
```

#### 3.3 让 Docker 拉镜像时也走代理

刚才的终端设置不会自动传给 Docker 服务，所以还要做这一步。它会重启 Docker，第一次安装时可以直接做；已有服务在运行时，选允许重启的时间再做。

如果 `http-proxy.conf` 已存在，先备份并修改原文件，不要直接覆盖。新安装执行下面这块，记得换掉代理地址：

```bash
mkdir -p /etc/systemd/system/docker.service.d &&
cat > /etc/systemd/system/docker.service.d/http-proxy.conf <<'EOF'
[Service]
Environment="HTTP_PROXY=http://192.168.1.100:7890"
Environment="HTTPS_PROXY=http://192.168.1.100:7890"
Environment="NO_PROXY=localhost,127.0.0.1,::1"
EOF
systemctl daemon-reload &&
systemctl restart docker &&
docker pull hello-world
```

最后一条只下载镜像，不运行容器。下载成功再继续。[Docker 服务代理说明](https://docs.docker.com/engine/daemon/proxy/)

#### 3.4 下载项目，并给构建过程设置代理

第一次安装，在服务器执行下面这块。已有项目时，只进入已有的 `librenms+grafana` 目录，不要重新下载，也不要再复制示例配置。

```bash
cd /root &&
git clone https://github.com/yikkrrtykj/monitor-autoconfig.git &&
cd monitor-autoconfig/librenms+grafana &&
cp .env.example .env &&
cp event-config.example.yml event-config.yml &&
chmod +x *.sh
```

Docker 拉镜像和镜像里下载软件是两回事。构建时的 `apt`、`apk`、`pip` 也需要代理，否则可能等几十分钟，还不断出现下载中断、重试。先执行下面的构建命令，换成你的代理地址：

```bash
docker compose --progress plain build \
  --build-arg HTTP_PROXY=http://192.168.1.100:7890 \
  --build-arg HTTPS_PROXY=http://192.168.1.100:7890 \
  --build-arg http_proxy=http://192.168.1.100:7890 \
  --build-arg https_proxy=http://192.168.1.100:7890
```

构建成功后再继续。不要加 `--no-cache`，已经完成的部分通常可以复用。[Docker 构建代理说明](https://docs.docker.com/build/building/variables/#proxy-arguments)

#### 3.5 清掉终端代理，再启动项目

软件下载完了，启动前先清掉当前终端的代理，让本机检查直接访问本机。这不会移除第 3.3 节给 Docker 设置的拉镜像代理。

```bash
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY all_proxy
export no_proxy="localhost,127.0.0.1,::1"
export NO_PROXY="$no_proxy"
./deploy.sh
```

`deploy.sh` 会再检查镜像和构建结果，刚才完成的构建通常会显示 `CACHED`。看到 `Platform bootstrap completed successfully` 后，按下一节打开网页。代理只解决下载问题，不能代替服务器与现场设备之间的网络连接。

### 4. 启动后检查

`deploy.sh` 会下载镜像、生成配置、构建本地镜像并启动服务。`monitor-*:local` 是在服务器上构建的镜像；拉取这些镜像时出现 pull access denied，不代表它们不存在，后续还要看构建和最终检查是否成功。

第一次安装的示例配置没有现场网段和设备；在网页里填写网络之前，不会扫描示例地址。


```bash
docker compose ps
./deploy-check.sh bootstrap
```

浏览器打开 `http://服务器IP:8088/control`，用 `admin / global123!@#` 登录后直接进入控制台，在“基础配置”里填核心 IP、普通交换机自动发现范围、赛事交换机 IP 和 SNMP Community，点“应用配置”。

第一次启动时没有填写现场网络，也可以完成基础检查；填好并应用后，再执行下面两条，检查配置和现场设备：

```bash
./deploy-check.sh configured
./pre-match-check.sh
```

### 5. 安装时遇到问题

#### 容器都启动了，但最后检查全部超时

如果看到 `Prometheus healthy timed out`、`Grafana API healthy timed out`、`Bigscreen reachable timed out` 等报错，而容器仍在运行，先检查终端代理。有时访问本机的请求也绕到了 Clash，导致网页检查失败；一个检查耗尽等待时间后，后面的检查也可能一起失败，不一定是所有服务都坏了。

先执行下面这块，不需要重新构建或重启服务：

```bash
cd /root/monitor-autoconfig/librenms+grafana || exit 1
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY all_proxy
export no_proxy="localhost,127.0.0.1,::1"
export NO_PROXY="$no_proxy"

curl --noproxy '*' -fsS --max-time 10 http://127.0.0.1:9090/-/healthy
curl --noproxy '*' -fsS --max-time 10 http://127.0.0.1:3000/api/health
curl --noproxy '*' -sS -o /dev/null -w '大屏 HTTP %{http_code}\n' \
  --max-time 10 http://127.0.0.1:8088/
./deploy-check.sh bootstrap
```

正常时会看到 Prometheus 的 Healthy、Grafana 的 `database: ok`、大屏 HTTP 200，最后是 `Result: PASS`。尚未配置 UniFi 或飞书时，对应的 `SKIP` 是正常的。如果仍然失败，保留新的报错和 `docker compose ps` 输出再排查，不要反复重装或覆盖配置。

#### Docker 没装好，或 Compose 命令不可用

`docker.service does not exist`、`docker: command not found` 表示安装还没成功，先看前面的下载或安装报错。已有 Docker 但 `docker compose version` 不可用时，在已配置 Docker CE 软件源的 Debian/Ubuntu 上执行 `apt-get install -y docker-compose-plugin`。旧的 `docker-compose` v1 不能代替 Compose v2。[Docker 安装说明](https://docs.docker.com/engine/install/ubuntu/)

#### 阿里云软件源报 “File has unexpected size”

如果 `apt-get update` 报阿里云 Docker 源的文件大小或哈希不匹配，说明索引与下载内容不一致，可能是镜像同步或缓存问题；不能据此认定代理失效，也不要跳过校验。先等待后重试，或在代理可用时切换到官方源。

以下恢复命令仅适用于 **Debian/Ubuntu 上已有阿里云 Docker CE 源的情况**。先找出配置位置（没有匹配时不要继续）：

```bash
grep -nH -E 'mirrors\.aliyun\.com/docker-ce/linux/(ubuntu|debian)' \
  /etc/apt/sources.list /etc/apt/sources.list.d/*.list \
  /etc/apt/sources.list.d/*.sources 2>/dev/null
```

将下面路径替换成上一条实际找到的文件；每个匹配文件分别处理。先备份，随后只替换 Docker 源域名和路径前缀，保留发行版代号、架构和 `Signed-By` 密钥配置：

```bash
source_file='/etc/apt/sources.list.d/docker.list'
cp -p -- "$source_file" "${source_file}.bak-$(date +%Y%m%d-%H%M%S)" &&
sed -i 's|https\?://mirrors\.aliyun\.com/docker-ce/linux/|https://download.docker.com/linux/|g' "$source_file"
```

如果需要代理，保持当前终端的代理设置，然后刷新软件列表并安装。任一步失败，停止并保留报错，不重复启动未安装的服务：

```bash
apt-get -o APT::Update::Error-Mode=any update &&
apt-get install -y docker-ce docker-ce-cli containerd.io \
  docker-buildx-plugin docker-compose-plugin &&
systemctl daemon-reload &&
systemctl enable --now docker &&
docker --version &&
docker compose version &&
systemctl is-active docker
```

安装时若只出现 `apt-news.service`、`esm-cache.service` 的 unit changed 警告，可执行 `systemctl daemon-reload` 刷新 unit；它不能修复软件源索引错误。

### 6. 端口放行

云服务器记得在安全组放行，现场内网一般不用管：

| 端口 | 方向 | 用途 |
|---|---|---|
| 8088/tcp | 入 | 大屏 / 控制台 |
| 3000/tcp | 入 | Grafana |
| 8002/tcp | 入 | LibreNMS |
| 514/udp+tcp | 入 | 交换机 syslog 上报 |
| 161/udp | 出 | SNMP 采集（出向，无需开入向） |

### 更新

普通更新使用 `main`：

```bash
git checkout main
git pull --ff-only
cd librenms+grafana
./deploy.sh
```

`deploy.sh` 会负责已有安装的配置兼容和部署验证。`git pull` 不会自动删除 `event-config.yml`、`.env` 或 Docker 数据卷。项目暂未建立正式的 release/tag 流程；完成后会另行更新推荐的固定版本部署方式。

### 版本与配置兼容

仓库根目录的 `VERSION` 是平台软件版本，可用 `cat VERSION` 查看。`event-config.yml` 使用顶层 `schema_version` 标记配置版本；没有该字段的旧配置会以内存兼容方式读取，不会在普通部署或打开控制台时自动改写，并会在下一次“保存”或“应用配置”时安全升级。

需要单独检查迁移时，可先 dry-run；确认后再写入：

```bash
python3 librenms+grafana/platform_config.py migrate librenms+grafana/event-config.yml
python3 librenms+grafana/platform_config.py migrate librenms+grafana/event-config.yml --write
```

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

已登录的控制台、`/topology` 与 `/infra` 只使用统一 Network Read API 读取设备、拓扑边和 ISP 清单；API 不可用时显示异常，不用旧清单补成正常结果。匿名 `/topology`、`/infra` 仍使用兼容读取。仅在数据降级、快照陈旧或不可用时显示简短异常提示；正常和匿名浏览不显示内部数据源状态。拓扑探测暂时失败时保留最近成功的节点结构，旧延迟不会继续显示，未获当前 API 状态覆盖的节点显示为未知；首次探测失败会提示拓扑不可用。已登录拓扑的 API 边读取失败时仅保留此前成功的 API 边并提示不可用，首次失败不会显示为正常的零链路。设备状态未知时不会显示为离线；趋势和 ISP 流量图仍读取 Prometheus。

拓扑的“AP: 显示 / 隐藏”默认隐藏，手动选择仅在当前浏览器会话中保存；刷新和页面切换不会重新打开。AP 身份与运行状态沿用 unpoller，LibreNMS FDB 只提供历史候选，只有当前 exact MAC/VLAN SNMP GET 唯一确认后才显示接入交换机叶节点，并显示“AP 已定位数/总数”。缺失、歧义、过期证据不连线。`TOPOLOGY_AP_FDB_CANDIDATE_MAX_AGE_SECONDS` 默认 28800 秒（0–28800），每 AP `TOPOLOGY_AP_FDB_CANDIDATE_CAP` 默认上限 8（0–8；超限整台不定位）。内部 LibreNMS `vlan_id` 按设备 FK 映射为真实 VLAN 后才验证；同交换机移位须重新检查端口，跨交换机移位等待新候选。`TOPOLOGY_AP_FDB_MAX_AGE_SECONDS` 默认 7200 秒（非负整数），用于当前 exact 验证证据在输出和浏览器中的有效期，独立于服务器 FDB 的 900 秒窗口；`ap-attachments.json` 每轮覆盖，无旧所有权和 24 小时保留，AP 验证仅使用有界 GET，计入独立 `ap_snmp_gets` / `ap_snmp_walks`（后者必须为 0）；不新增 FDB walk 或改变既有 SNMP/发现频率。

AP 使用 52 px 圆形叶节点，按已验证父交换机分组并使用避开其他节点的直角分支。上联端口显示在圆形上方；仅名称显示在下方，RTT、真实 VLAN、型号和客户端数保留在 Inspector；AP Inspector 仅展示 Hostname、管理 IP、型号、状态、延迟、VLAN、上联端口和客户端，不展示泛用上联/无线电/端口/连接摘要。单 AP 在父交换机下方居中，安全通道内使用直线，无总线；有真实障碍时使用最短安全直角路线，无安全路线则不绘制。AP 开关固定有线图的坐标、路线、屏幕缩放和位置，仅扩展可滚动空间。独立可视化 ICMP job `infra-ap-ping` 每 5 秒通过现有 blackbox ICMP 模块读取 RTT：collector 将同一轮当前唯一 UniFi 管理 IP 原子写入 `ap-ping-targets.json`（最多 512 个，查询失败清空，不保留旧目标）；Prometheus file_sd 每 60 秒读取。前端按精确 `target_ip` 关联成功的 RTT，缺失/重复/失败保持无延迟，不覆盖 UniFi 在线状态。该 job 不进入 `DEVICE_DOWN_JOBS`，不改变 AP 告警，不增加 SNMP。

拓扑中的核心/接入交换机及未确认类型设备卡片仅显示主名称与状态图标，管理 IP、延迟等资料保留在 Inspector。AP 上联标签复用有线接口标签的字体与样式；分支总线仅覆盖实际子节点锚点，单 AP 行不绘制横向总线。拓扑页的提示条与工具栏使用同一 header，窄屏保持有界 canvas，AP 内容不会参与有线图的容器尺寸计算。

已登录时，点击拓扑中的交换机或防火墙节点可打开右侧轻量详情，显示单节点身份、在线与延迟、端口状态汇总及已发现邻居；UniFi AP 从无线页 AP 卡片或拓扑中已定位的 AP 节点打开同一个详情。详情由经过认证的单节点只读 API 提供，局部数据缺失会标记未知并提示。Hillstone HA 只使用 `sysHAStatus.0`（`1.3.6.1.4.1.28557.2.2.1.8.0`）作为权威：独立 `infra-fw-ha-snmp` 每 60 秒对 `FIREWALL_UNIT_SNMP_TARGETS` 中每台物理机读取一个 scalar，重试 0 次、超时 2 秒，不走接口表。API 精确匹配 `target_ip`，采集时间年龄须在 0–180 秒；缺失、过期、重复或未支持枚举显示未知。0/1/2/3/4/6 分别为 none/init/hello/backup/master/AA-mode；5 保留厂商 `slase` 为未知，不猜测角色。逻辑 VIP 不继承物理机角色；未配置物理目标时 HA 不可用。选中物理机时，“HA 角色”仅展示本机状态，“HA 对端”单独展示其他物理单元；逻辑 VIP 使用“HA 集群”展示物理单元摘要，不赋予 VIP 物理角色。HA 长文本可换行，其他详情行保留紧凑省略样式。完整接口流量与历史继续通过“查看接口”按需打开；不提供策略、NAT、会话或 IPS 数据。匿名拓扑仍保留原有节点详情。

已登录的 Cisco IOS / IOS-XE 等受支持系统的交换机详情可通过“查看端口”打开端口面板。面板按堆叠成员、接口类型和槽位排列可识别的物理端口；逻辑或无法安全识别的接口放在默认折叠的“其他接口”中。悬浮、键盘聚焦和点击可查看当前端口数据。无采集覆盖的指标显示“—”及来源提示；VLAN 仅是观测资料，错误和丢弃数是累计计数，不等于当前故障。面板只在打开时读取一次端口 API；悬浮和聚焦不读取历史。点击固定端口或接口详情时，向现有 Prometheus 查询最近 15 分钟的 RX/TX（30 秒步长，每次选择两个有超时的查询），不自动刷新。历史覆盖独立于当前值来源；任一方向有可用 Prometheus 历史时保留该来源，不混用方向。双向均无可用历史或 Prometheus 查询失败时，再读取一次经鉴权的 LibreNMS RRD 历史（原存储步长，不插值）；标题标明来源。空覆盖、单方向缺失或查询失败保留当前详情，不使用 LibreNMS 当前值补造历史。platform-api 使用 rrdtool 经现有 rrdcached 刷新后只读获取同设备、同 ifIndex 对应的 port RRD，不新增 SNMP job、target 或轮询。

已登录的 Hillstone 防火墙详情可通过“查看接口”打开接口列表。列表保留 LibreNMS 中的全部接口，优先使用现有 `firewall-snmp` 中与该管理 IP 精确匹配的新鲜 IF-MIB 数据。只有 ISP inventory 的目标 IP 与 ifIndex 均精确、唯一匹配时，接口才进入 WAN / ISP 区域。接口流量不用于推断 HA 主备角色，接口列表也不模拟 Cisco 物理面板。

Cisco Small Business（`ciscosb`，兼容旧 `cisco-access`）详情使用“查看接口”和通用交换机接口列表，不模拟堆叠物理布局；其他未支持的 OS 不默认为 Cisco，也不显示详细接口按钮。型号缺失显示“—”。Cisco / Hillstone / Small Business 的接口没有精确 Prometheus 当前指标时，使用同一次 LibreNMS 端口读取中的 poller 统计作为备用：仅接受数字 epoch `poll_time`，年龄须为 0–600 秒（现有约 5 分钟轮询的两个周期）；字节速率乘 8 转为 bps，备用累计错误保留累计语义；丢弃计数只使用 Prometheus，LibreNMS 备用丢弃值保持“—”（device-ports API 不读取 statistics 表）。利用率须有可信速率。缺失、过期或无效数据保持“—”并提示，界面标明 Prometheus / LibreNMS poller 来源与可用的 poller 年龄。数据不跨设备或 HA 节点借用；本功能不增加 SNMP 目标、任务或采集频率。

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
cd librenms+grafana
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
| 普通交换机自动发现范围 | 给通用大屏、拓扑和 LibreNMS 自动发现用，例如 `192.168.10.0/24` 或 `192.168.10.1-100,192.168.10.254`；不会参与赛事座位识别 |
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
[`librenms+grafana/docs/feishu-app-chat-troubleshooting.md`](librenms+grafana/docs/feishu-app-chat-troubleshooting.md)。
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
默认阈值为 7 天、每小时检查一次，并保持 `DEVICE_AUTO_DELETE_DRY_RUN=true`。确认 DRY RUN
日志无误后才可关闭 DRY RUN；删除前会重新通过 blackbox ICMP 确认，探测异常时一律跳过。
核心、赛事交换机、防火墙 VIP/HA 物理节点、服务器、ISP 和选手网关等显式目标永久保护，
但 discovery range 不会整体受保护。真正删除成功或 DELETE 失败会按检查周期聚合发送一条
飞书通知；DRY RUN 通知默认关闭，需要时设置 `DEVICE_AUTO_DELETE_DRY_RUN_NOTIFY=true`，每轮
也只发送一条候选摘要。比赛 Golden VM 的 auto-delete 默认关闭。

旧安装无法仅凭遗留参数可靠判断用途；公司服务器升级前必须补上上述 `true`，已有
bridge-state 会继续沿用。公司模式的飞书按钮还需订阅 `card.action.trigger`。

## 交换机侧配置

至少要让交换机把 syslog 发到监控服务器：

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
