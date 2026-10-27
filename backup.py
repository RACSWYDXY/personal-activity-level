#!/usr/bin/env python3
"""Make a consistent online SQLite backup, even while PAL is running."""
import datetime as dt
import os
import sqlite3
import sys
from pathlib import Path

folder = Path(os.environ.get('DATA_DIR','/data'))
source = folder/'questflow.db'
if not source.is_file():
    raise SystemExit(f'Database not found: {source}')
output = Path(sys.argv[1]) if len(sys.argv)>1 else folder/'backups'/f'questflow-{dt.datetime.now():%Y%m%d-%H%M%S}.db'
output.parent.mkdir(parents=True,exist_ok=True)
if output.exists():
    raise SystemExit(f'Backup destination exists, refusing to overwrite: {output}')
with sqlite3.connect(source) as live, sqlite3.connect(output) as backup:
    live.backup(backup)
with sqlite3.connect(output) as restored:
    status = restored.execute('PRAGMA integrity_check').fetchone()[0]
if status != 'ok':
    raise SystemExit(f'Backup integrity check failed: {status}')
print(f'Backup OK: {output} ({output.stat().st_size} bytes)')
