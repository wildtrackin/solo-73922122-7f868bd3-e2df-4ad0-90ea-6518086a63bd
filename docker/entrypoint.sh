#!/bin/sh
# 容器入口：确保样例工作簿存在（bind mount 的空目录首次启动时生成），
# 然后启动主进程。
set -eu

if [ ! -f /samples/stale_cache.xlsx ]; then
    echo "[entrypoint] generating sample workbooks into /samples ..."
    python3 /samples-src/generate_samples.py --out-dir /samples
fi

exec "$@"
