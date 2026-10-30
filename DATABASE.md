# SQLite database, backups and migration / 数据库说明

## Storage
- SQLite database: **`/data/questflow.db`** (legacy filename retained intentionally for upgrading existing installations).
- Docker named volume: **`questflow_questflow_data`**, when using the included `docker-compose.yml` with `name: questflow`.
- `PRAGMA journal_mode=WAL`, foreign keys and SQLite transactions are used; auxiliary `-wal`/`-shm` files may appear.
- Data includes account credentials (salted hash), sessions, tasks, per-period progress and claims, reward settings, shop items, redemptions, transaction ledger, login attempts.
- Single-user and low concurrent write load: **SQLite is simpler than MySQL/PostgreSQL** for this deployment.

## Consistent backup while running

```bash
sudo docker exec questflow python /app/backup.py /data/backups/backup-20261027.db
sudo docker cp questflow:/data/backups/backup-20261027.db ./pal-backup-20261027.db
```
Use a new output name every time. `backup.py` uses SQLite's backup API, not a live file copy. Also store the backup on a different device or offsite.

## Safe migration / upgrade
1. Create and verify an external backup before replacing any Compose file.
2. The renamed PAL UI deliberately keeps **the `questflow` Compose project, `questflow` container and `questflow.db` filename**. Running in a newly named directory does not create a new data volume if you use this Compose file and no overriding `COMPOSE_PROJECT_NAME`.
3. Verify with `sudo docker volume ls | grep questflow` and `sudo docker inspect questflow` before any volume changes.
4. Rebuild in place with `sudo docker compose up -d --build`.
5. **Never run `docker compose down -v`** unless intentionally deleting all the app's data.

## Restore (advanced and destructive if used incorrectly)
以下流程覆盖现有数据库，使应用回到备份时刻。只适用于**现有容器仍存在、`/data` 使用命名卷**的安装；使用上文 `backup.py` 生成的独立 SQLite 备份，不要使用运行中直接复制的 `.db`。先将备份另存到其他设备。

**日常恢复不要使用 `docker compose down -v`。** 恢复期间禁止其他人或 fnOS 自动任务启动/更新服务；任何命令报错都停止，不要继续重启。

### 1. 设置路径，确认精确卷名并验证备份

在同一个 Bash 终端按顺序执行各代码块。根据实际情况修改项目目录、备份的**绝对路径**和容器名；命令默认需要 `sudo`，已有 Docker 权限时可将函数中的 `sudo docker` 改为 `docker`。路径指 Docker 所在宿主机上的路径。

```bash
set -eu
cd /vol1/1000/questflow  # 改为包含原 docker-compose.yml 和 .env 的目录
BACKUP='/vol1/1000/backups/pal-backup-20261027.db'  # 改为实际备份文件
CONTAINER='questflow'  # 如旧安装自定义了容器名，请修改
d() { sudo docker "$@"; }
test -s "$BACKUP"
IMAGE=$(d inspect -f '{{.Image}}' "$CONTAINER")
PROJECT=$(d inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "$CONTAINER")
test -n "$PROJECT" && test "$PROJECT" != '<no value>'
dc() { d compose -p "$PROJECT" "$@"; }
d inspect -f '{{range .Mounts}}{{println .Type .Name .Source "->" .Destination}}{{end}}' "$CONTAINER"
VOLUME=$(d inspect -f '{{range .Mounts}}{{if eq .Destination "/data"}}{{if eq .Type "volume"}}{{.Name}}{{end}}{{end}}{{end}}' "$CONTAINER")
test -n "$VOLUME"
d volume inspect "$VOLUME"
test "$(dc ps -aq questflow)" = "$(d inspect -f '{{.Id}}' "$CONTAINER")"
printf 'Compose project: %s\n/data volume: %s\n' "$PROJECT" "$VOLUME"
```

核对输出的 `/data` 挂载。默认卷是 `questflow_questflow_data`，但旧卷名或非标准 `-p` / `COMPOSE_PROJECT_NAME` 可能不同，**以实际挂载为准，不要凭名称猜测**。上述 `dc` 固定使用原项目名；保留原来的 Compose 文件、override 文件及环境设置。若容器已被删除、挂载为 bind mount 或身份检查失败，请先找回原部署配置并确认数据位置，不要创建新卷套用本流程。

用现有应用镜像启动无网络临时容器，只读挂载备份；不需要拉取额外镜像。结果必须为 `ok`。完整性检查不能证明备份日期和业务内容正确，还应确认备份来源及时间。

```bash
d run --rm -i --network none --user 0:0 --entrypoint python \
  --mount "type=bind,src=$BACKUP,dst=/backup.db,readonly" "$IMAGE" - <<'PY'
import sqlite3
with sqlite3.connect('file:/backup.db?mode=ro&immutable=1', uri=True) as db:
    result = db.execute('PRAGMA integrity_check').fetchall()
    if result != [('ok',)]:
        raise SystemExit(f'Backup integrity check failed: {result}')
print('ok')
PY
```

### 2. 停止服务并保存恢复前副本

```bash
dc stop questflow
test "$(d inspect -f '{{.State.Running}}' "$CONTAINER")" = false
test -z "$(d ps -q --filter "volume=$VOLUME")"
ROLLBACK_DIR=$(mktemp -d "$PWD/pal-before-restore-XXXXXXXX")
d run --rm --network none --user 0:0 --entrypoint sh \
  --mount "type=volume,src=$VOLUME,dst=/data,readonly" \
  --mount "type=bind,src=$ROLLBACK_DIR,dst=/rollback" "$IMAGE" \
  -eu -c 'test -s /data/questflow.db; tar -cpf /rollback/data.tar -C /data .'
printf '保留此恢复前完整卷副本：%s/data.tar\n' "$ROLLBACK_DIR"
```

归档包含旧数据库和可能存在的 WAL/SHM，需留足整个卷的空间（包括卷内 backups），并保护其中的账户数据。只有归档成功后才继续。若还有其他容器使用此卷，先查明并停止全部写入者。

### 3. 临时容器恢复数据库，清理旧 WAL/SHM

先复制到同卷临时文件，保留原数据库的属主及权限，再替换；不要对整个卷使用 `chmod 777`。清理 WAL/SHM 只能在服务停止、旧数据已归档之后进行。

```bash
d volume inspect "$VOLUME" >/dev/null
test -z "$(d ps -q --filter "volume=$VOLUME")"
d run --rm --network none --user 0:0 --entrypoint sh \
  --mount "type=volume,src=$VOLUME,dst=/data" \
  --mount "type=bind,src=$BACKUP,dst=/backup.db,readonly" "$IMAGE" -eu -c '
    test -s /data/questflow.db
    owner=$(stat -c "%u:%g" /data/questflow.db)
    mode=$(stat -c "%a" /data/questflow.db)
    tmp=$(mktemp /data/questflow.restore.XXXXXXXX)
    trap '\''rm -f "$tmp"'\'' EXIT
    cp /backup.db "$tmp"
    chown "$owner" "$tmp"
    chmod "$mode" "$tmp"
    rm -f /data/questflow.db-wal /data/questflow.db-shm
    mv "$tmp" /data/questflow.db
  '
```

若此步失败，保持服务停止，保留归档并排查；不要把归档中的旧 WAL/SHM 与新数据库混用。

### 4. 验证恢复结果、启动并核对应用状态

先用应用用户检查恢复后的数据库可读且完整，通过后再启动原容器：

```bash
d run --rm -i --network none --entrypoint python \
  --mount "type=volume,src=$VOLUME,dst=/data,readonly" "$IMAGE" - <<'PY'
import sqlite3
with sqlite3.connect('file:/data/questflow.db?mode=ro&immutable=1', uri=True) as db:
    result = db.execute('PRAGMA integrity_check').fetchall()
    if result != [('ok',)]:
        raise SystemExit(f'Restored database integrity check failed: {result}')
print('ok')
PY
dc start questflow
dc ps
dc logs --tail=100 questflow
d inspect -f '{{.State.Health.Status}}' "$CONTAINER"
```

健康检查可能先显示 `starting`，等待约 30–60 秒后重复最后一条命令，应为 `healthy`。用原宿主机端口打开页面，以**备份时的账户密码**登录，核对任务、进度、金币及账本是否与备份时刻相符；若出现首次创建管理员页面或数据为空，停止服务并检查挂载，不要创建新账户。确认无误后仍保留原备份和恢复前归档。

仅迁移宿主机端口不需要恢复或移动数据库，见 [fnOS 宿主机端口迁移](INSTALL-FNOS.md#宿主机端口迁移)。
