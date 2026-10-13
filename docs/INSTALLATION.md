# 安装、更新和排障说明

第一次安装可以先看 [README](../README.md)。这里保留完整步骤、软件源恢复、本机检查和配置版本说明。遇到报错先停下，已有配置和数据不要覆盖。

## 部署

### 0. 服务器要求

- Linux x86_64，可选 Ubuntu 24.04；其它 Debian/Ubuntu 版本先确认仍受 Docker 官方支持
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

浏览器打开 `http://服务器IP:8088/control`，用 [README 中的默认账号](../README.md#打开网页并填写现场配置) 登录后直接进入控制台，在“基础配置”里填核心 IP、普通交换机自动发现范围、赛事交换机 IP 和 SNMP Community，点“应用配置”。

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

普通更新使用 `main`。以下操作会部署并可能重启服务，先选择允许操作的时间。仓库假设在 `/root/monitor-autoconfig`；其它安装目录按实际修改。已跟踪文件有修改时先停下来检查，不自动覆盖，也不清理未跟踪文件。

```bash
(
set -e
cd /root/monitor-autoconfig
test "$(git branch --show-current)" = main
test -z "$(git status --porcelain --untracked-files=no)" || {
  echo "已有文件修改，停止更新，请先检查 git status --short"
  exit 1
}
git fetch origin
git pull --ff-only origin main
cd librenms+grafana
./deploy.sh
./deploy-check.sh configured
)
```

`deploy.sh` 会负责已有安装的配置兼容和部署验证。`git pull` 不会自动删除 `event-config.yml`、`.env` 或 Docker 数据卷。项目暂未建立正式的 release/tag 流程；完成后会另行更新推荐的固定版本部署方式。

### 版本与配置兼容

仓库根目录的 `VERSION` 是平台软件版本，可用 `cat VERSION` 查看。`event-config.yml` 使用顶层 `schema_version` 标记配置版本；没有该字段的旧配置会以内存兼容方式读取，不会在普通部署或打开控制台时自动改写，并会在下一次“保存”或“应用配置”时安全升级。

需要单独检查迁移时，可先 dry-run；确认后再写入：

```bash
python3 librenms+grafana/platform_config.py migrate librenms+grafana/event-config.yml
python3 librenms+grafana/platform_config.py migrate librenms+grafana/event-config.yml --write
```
