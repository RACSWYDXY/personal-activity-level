# fnOS Docker 安装与故障处理

适用于 Intel 旧款 Mac / 低功耗 N100 / 常见 x86-64 NAS。默认监听宿主机 `8765` 端口。

## 首次安装

1. 将发布压缩包解压到 fnOS 存储目录，例如 `/vol1/1000/questflow`；进入**包含 `Dockerfile` 和 `docker-compose.yml` 的目录**。
2. 执行 `cp .env.example .env`，然后编辑 `.env` 将 `APP_SETUP_KEY` 改为**唯一的长随机值**，不要使用示例值。若使用其他本地时区，按需调整 `TZ`；局域网 HTTP 使用 `COOKIE_SECURE=0`。
3. 检查 `sudo docker compose version`；执行 `sudo docker compose up -d --build`。
4. 使用 `sudo docker compose ps` 检查运行情况，必要时执行 `sudo docker compose logs --tail=100 questflow`。
5. 局域网浏览器打开 `http://<fnOS的LAN_IP>:8765`，用安装密钥创建第一个管理员。Android Chrome、iOS Safari 和 PC 浏览器均可访问。

## 本次实际安装遇到的故障：原因及修复

### 1. 无权限访问 `/var/run/docker.sock`

```text
permission denied while trying to connect to the Docker daemon socket
```

当前 SSH 账户没有 Docker socket 权限。使用 `sudo docker compose up -d --build`，不推荐将 socket `chmod 666`。加入 `docker` 用户组基本等同于授予宿主机 root 权限。

### 2. 飞牛 Docker 加速镜像返回 401

```text
docker.fnnas.com/...python/manifests/3.12-alpine: 401 Unauthorized
```

在 `.env` 中取消注释：

```dotenv
PYTHON_IMAGE=public.ecr.aws/docker/library/python:3.12-alpine
```

然后重新构建。此为 AWS ECR Public Docker 官方 Python 镜像的镜像路径；能否访问取决于当前网络。部分 fnOS 版本也允许调整其 Docker 镜像加速配置。

### 3. 卡在 `apk add --no-cache tzdata`

```text
RUN apk add --no-cache tzdata ...
```

在 `.env` 中取消注释：

```dotenv
ALPINE_MIRROR=mirrors.tuna.tsinghua.edu.cn
```

Dockerfile 将 Alpine 默认软件仓库域名换成指定镜像站。清华 TUNA 是示例，若所在地区访问不佳则删除此设置或换用可靠镜像站。使用 `sudo docker compose --progress=plain build` 可以显示详细构建日志。

### 4. 容器不断 Restarting (2)

```text
python: can't open file '/app/app.py': [Errno 13] Permission denied
```

部分 NAS 文件管理器/解压方式保留了限制性文件权限。新 Dockerfile 的 `RUN chmod -R a+rX /app` 已修复应用源码读取问题。重新执行 `sudo docker compose up -d --build`。

### 5. 容器正在运行，但网页打不开

```bash
sudo docker compose ps
sudo docker compose logs --tail=100 questflow
```

确认显示 `Up` 或 `healthy`，端口映射包含 `8765:8765`，浏览器使用 fnOS 主机的**实际 LAN IP**，设备处于同一局域网，路由器/系统防火墙允许局域网访问该端口。如果端口占用，则调整 `.env` 的 `HOST_PORT`，重建后改用新端口访问。

### 6. 首次安装显示密钥错误或程序拒绝启动

示例 `APP_SETUP_KEY=CHANGE-THIS-TO-A-RANDOM-LONG-SECRET` 故意被程序拒绝。需要在 `.env` 中替换为随机密钥。`.env` 保存后，执行 `sudo docker compose up -d --force-recreate` 更新容器环境。

## 更新、停止及数据保留

```bash
sudo docker compose up -d --build  # 构建并更新
sudo docker compose down           # 停止和移除容器，默认保留命名数据卷
```

**禁止误用 `down -v`**：它将删除命名数据卷，导致丢失所有数据。迁移旧 QuestFlow 到 PAL 前，请先按 `DATABASE.md` 备份。

## 远程访问

不建议将 NAS 的 `8765/TCP` 端口直接开放到公网。使用 Tailscale/WireGuard 等 VPN；如使用 HTTPS 反向代理，请正确处理 TLS、可信反代和 `COOKIE_SECURE=1`。HTTPS 之外的普通 HTTP 不应设置 Secure Cookie 为 1，否则浏览器不会正常携带登录 Cookie。
