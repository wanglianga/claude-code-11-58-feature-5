#!/bin/sh
set -e
# 等待数据库就绪
python - <<'PY'
import os, time
import psycopg2
url = os.environ["DATABASE_URL"]
# sqlalchemy 风格 -> psycopg2 参数
parts = url.split("://", 1)[1]
auth, rest = parts.split("@")
user, pwd = auth.split(":")
hostport, db = rest.split("/")
host, port = hostport.split(":")
for i in range(60):
    try:
        psycopg2.connect(host=host, port=port, user=user, password=pwd, dbname=db).close()
        break
    except Exception:
        time.sleep(1)
else:
    raise SystemExit("database not ready")
PY
if [ "$SEED_ON_START" = "1" ]; then
  python -m app.seed
fi
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
