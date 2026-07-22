# 赛事网络监控

一套面向电竞现场的网络监控平台：LibreNMS 负责设备和端口，Prometheus/Grafana 负责采集和图表，8088 大屏负责现场看板、基础配置、问题清单、事故记录和交换机配置巡检。

## 入口

| 服务 | 默认地址 | 用途 |
|---|---|---|
| 大屏 / 控制台 | `http://服务器IP:8088` | 现场展示，`/control` 做配置、问题清单和事故记录 |
| Grafana | `http://服务器IP:3000` | 查图、查历史、临时排障 |
| LibreNMS | `http://服务器IP:8002` | 设备发现、端口流量、告警规则 |

默认账号：

| 服务 | 默认账号 |
|---|---|
| Grafana | `admin / root` |
| LibreNMS | `admin / librenms123` |
| 赛事控制台 | `admin / global`，首次登录后必须改密码 |

正式使用前请修改 `.env` 里的 `GRAFANA_PASSWORD`、`LIBRENMS_ADMIN_PASSWORD`、`SNMP_COMMUNITY`。8088 控制台有登录，但仍建议只在内网或 VPN 后访问。

## 部署

### 0. 服务器要求

- Linux x86_64，推荐 Ubuntu 20.04+ / Debian 11+（其它能跑 Docker 的发行版也可以）
- 建议 4 核 / 8G 内存 / 100G 磁盘起步（Prometheus 默认保留 15 天数据）
- 监控服务器要和交换机管理网段互通（SNMP 出向采集），交换机能把 syslog 发到它

需要预装：Docker（含 compose v2 插件）、git、python3。下面从零开始装。

### 1. 安装 Docker（含 compose 插件）

国内服务器用阿里云安装源：

```bash
curl -fsSL https://get.docker.com | bash -s docker --mirror Aliyun
systemctl enable --now docker
```

能正常访问外网的服务器直接：

```bash
curl -fsSL https://get.docker.com | bash
systemctl enable --now docker
```

验证（两条都要能出版本号）：

```bash
docker --version
docker compose version
```

`docker compose version` 报 “不是 docker 命令” 说明缺 v2 插件（老的 `docker-compose` v1 不行），单独补装：

```bash
apt-get install -y docker-compose-plugin    # Debian/Ubuntu
# yum install -y docker-compose-plugin      # CentOS/RHEL（需先配好 docker-ce 源）
```

### 2. 安装 git 和 python3

`deploy.sh` 渲染 Grafana 配置需要 python3：

```bash
apt-get update && apt-get install -y git python3    # Debian/Ubuntu
# yum install -y git python3                        # CentOS/RHEL
```

### 3. 国内拉镜像加速（拉镜像超时再做）

方法一：给 Docker 配 registry 镜像加速（改成你可用的加速地址）：

```bash
mkdir -p /etc/docker
cat > /etc/docker/daemon.json <<'EOF'
{
  "registry-mirrors": ["https://docker.m.daocloud.io"]
}
EOF
systemctl restart docker
```

方法二：有代理时给 Docker daemon 配代理：

```bash
mkdir -p /etc/systemd/system/docker.service.d
cat > /etc/systemd/system/docker.service.d/http-proxy.conf <<'EOF'
[Service]
Environment="HTTP_PROXY=http://代理IP:端口"
Environment="HTTPS_PROXY=http://代理IP:端口"
Environment="NO_PROXY=localhost,127.0.0.1,::1"
EOF
systemctl daemon-reload
systemctl restart docker
```

### 4. 拉代码并启动

```bash
git clone https://github.com/yikkrrtykj/monitor-autoconfig.git
cd monitor-autoconfig/librenms+grafana
cp .env.example .env
cp event-config.example.yml event-config.yml
chmod +x *.sh
./deploy.sh
```

`deploy.sh` 会自动：探测本机 IP 写入 `SERVER_IP`、逐个拉镜像并自动重试、渲染 Grafana 配置、重新构建本地工具镜像并启动全部服务。仓库里的 Dockerfile 更新后不需要手工执行构建命令。

拉镜像日志里 `monitor-rsyslog:local`、`monitor-player-tools:local`、`monitor-platform-api:local`、`monitor-grafana-setup:local` 这些镜像报 403/pull access denied 是正常的：它们是本地构建镜像，仓库里本来就没有，部署脚本会用仓库里的 Dockerfile 自动构建。

### 5. 启动后检查

```bash
docker compose ps
./pre-match-check.sh
```

浏览器打开 `http://服务器IP:8088/control`，用 `admin / global` 登录（首次登录强制改密码），在"基础配置"里填核心 IP、交换机管理网段、SNMP Community，点"应用配置"。

### 6. 端口放行

云服务器记得在安全组放行，现场内网一般不用管：

| 端口 | 方向 | 用途 |
|---|---|---|
| 8088/tcp | 入 | 大屏 / 控制台 |
| 3000/tcp | 入 | Grafana |
| 8002/tcp | 入 | LibreNMS |
| 514/udp+tcp | 入 | 交换机 syslog 上报 |
| 161/udp | 出 | SNMP 采集（出向，无需开入向） |

### 更新到测试分支

```bash
git fetch origin
git checkout -B codex/remove-quality-heatmap origin/codex/remove-quality-heatmap
cd librenms+grafana
./deploy.sh
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
| 离线部署包 | 查看离线镜像、文件和安装脚本清单 |
| 交换机配置巡检 | 粘贴 Cisco `show run` 片段，检查现场风险 |
| 核心交换机 Telnet | 填写登录信息并执行只读连接测试；密码不随赛事配置导出 |

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
| 导入 | 导入赛事配置包里的 YAML/JSON |
| 导出包 | 下载当前配置、事故和部署清单 |

点 `应用配置` 后，控制台会自动让 Prometheus、LibreNMS、飞书桥接、大屏等相关容器重新读取新的 `.env`。如果页面提示自动应用失败，再在服务器执行：

```bash
cd librenms+grafana
./apply-env.sh
```

## 基础配置怎么填

| 字段 | 建议 |
|---|---|
| 默认赛制 | 控制 `/control` 里默认座位布局 |
| SNMP Community | 交换机、防火墙、LibreNMS/Prometheus 采集共用，默认 `global` |
| 选手 VLAN / 无线 VLAN | 默认 `40 / 41` |
| 选手网关 | 留空时复用核心 IP |
| 交换机管理网段 | 给 LibreNMS 自动发现用，例如 `192.168.10.0/24` 或 `192.168.10.1-100,192.168.10.254` |
| 防火墙管理网段 | 默认 `192.168.9.0/24`；需要发现其它防火墙管理地址时再改 |
| 核心 IP | 三层核心或网关交换机管理 IP |
| 防火墙 IP | 同时用于判断防火墙在线和 WAN 流量 SNMP；多个 IP 用逗号或换行，不用 `/` |
| 防火墙名称 | 选填；大屏和拓扑的显示名。留空用设备 SNMP sysName（HA 防火墙的 sysName 可能是 Member1 这种，建议手填） |
| 物理防火墙 SNMP IP | HA 两台物理机分别采集时填写，多个 IP 用逗号或换行 |
| 舞台交换机 | 用于选手识别和大屏选手监控，支持 `192.168.10.11-14` 这种范围写法 |
| 其它接入交换机 | 不参与选手识别，只用于在线、拓扑和发现 |
| 服务器 | 默认空；需要监控游戏服务器时再添加名称和 IP |
| ISP 自动发现 | 通过防火墙 SNMP 的默认路由、接口地址和 WAN 口名称识别运营商链路；公网地址变化后自动替换旧 Ping 目标 |
| WAN 口识别关键词 | 自动发现 WAN 口时匹配接口名/描述，默认 `telecom,telcom,unicom,isp,WAN`；防火墙 SNMP 只报 `eth0/eth1` 物理名时（如 WatchGuard）直接加物理口名，如 `...,eth0,eth1`，以数字结尾的关键词按边界匹配、不会误配 eth10 |
| WAN 口名/别名 | 与防火墙 SNMP 返回的接口名或别名一致；网关由路由表自动发现，不需要填写公网 IP 或网关 |
| 默认/单链路带宽 | 用于饱和判断；对称线路填一个 Mbps 数值，不对称线路固定按“下载/上传”填写（如 `1000/100`）；饱和阈值默认 90% |
| UniFi | 使用 UniFi AP 时填控制器地址和只读账号 |
| 飞书机器人 Token | 旧 Webhook 回退通道；不同群通常使用不同 Token |
| 飞书应用 App ID / App Secret | 审批通过的企业自建应用凭据；普通告警优先使用应用机器人，旧 Token 作为失败回退 |
| 主动告警群 | 可填群名或 `oc_...` Chat ID；机器人只在一个群时可留空，多群时必须指定，避免告警发错场地 |
| 飞书接入模式 | `local` 单站点；`hub` 多站点唯一长连接中心；`site` 其它监控站点 |
| 项目/比赛名称 | 自动使用页面上方已有的“赛事名称”，无需重复填写；内部鉴权令牌也自动生成 |

飞书企业自建应用审批通过后，在 `/control` 的“告警”区填写 App ID、App Secret，
把应用机器人加入告警群，再点“应用配置”。旧版只写在 `.env` 的凭据会自动带入
后台输入框。长连接同时处理卡片按钮和群内 `@机器人` 查询；查询回复始终回到发起
消息所在的群，不使用“主动告警群”配置。支持 `查设备 <名称/IP>`、
`查光功率 <名称/IP> [接口]`、`查异常光功率 <名称/IP> [接口]` 和 `帮助`。
光功率读取 LibreNMS 已采集的 dBm 传感器及阈值，不会额外轮询交换机。
`im.message.receive_v1` 是在“事件与回调”里添加的事件类型，不是权限管理页里的权限名。

### 一个 LibreBOT 管理多个监控项目

飞书同一应用的多个长连接属于集群而不是广播，一条消息或卡片回调只会交给其中
随机一个客户端。因此不能让上海、海外等每台监控服务器都连接同一个 App ID。
本项目的多站点模式固定只有一台 `hub` 建立飞书长连接，其它服务器使用 `site`：

1. 所有站点填写同一个 `FEISHU_APP_ID/FEISHU_APP_SECRET` 和自己的告警群名称；
   项目名称直接使用已有 `event.name`，内部 API 令牌自动生成。
2. 选一台网络能访问所有站点 Bridge 的服务器设为 `hub`；其它服务器设为 `site`。
3. hub 自身根据上方项目和群名称自动建立本地路由；其它现场只需在中心路由填写
   比赛名称、群名称和现场监控地址。群名称会自动解析成 Chat ID，内部令牌也会自动生成。
4. 站点 Bridge 默认只绑定 `127.0.0.1:5005`。中心需要跨服务器访问时，把该站点
   `.env` 的 `FEISHU_BRIDGE_BIND` 改成 VPN 地址，并在主机防火墙中只放行 hub；
   公网场景应使用 HTTPS 反向代理，不要直接暴露 5005。

示例：

```yaml
event:
  name: 公司监控
alerts:
  feishu_app_id: cli_shared
  feishu_app_secret: shared-secret
  feishu_chat_id: 公司监控告警群
  feishu_mode: hub
  feishu_sites:
    - site_id: 英雄电竞上海站
      chat_id: 英雄电竞上海站告警群
      bridge_url: https://overseas-monitor.example.com/feishu-bridge
```

海外站点只需把 `feishu_mode` 改为 `site`，并设置上方赛事名称和自己的告警群名称；应用配置
时会自动停止该服务器的 `feishu-ws` 容器，但主动告警发送能力仍然保留。

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

## 离线交付

有网络时在一台服务器打包：

```bash
cd librenms+grafana
./offline-package.sh
```

现场离线服务器（注意：`install-offline.sh` 只导入镜像，不安装 Docker 本体，离线服务器需要提前装好 Docker + compose 插件和 python3，可在有网时用上面第 1、2 步装好或做进系统镜像）：

```bash
tar -xf monitor-offline-*.tar.gz
cd monitor-offline-*
./install-offline.sh
docker compose up -d
```

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
