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
Stop the application, save the old database/volume, and restore an already verified backup using a temporary container or trusted volume manager. If replacing the database, do so only when stopped and ensure that the `-wal`/`-shm` companion files are not conflicting. Do not overwrite the volume until you have identified its exact name. The README covers safe backup commands; request guided assistance for restoration.
