#!/usr/bin/env bash
# 端到端演示脚本：上传 → 状态轮询 → 并行重算 → 取消 → 下载 CSV。
# 用法：
#   ./scripts/demo.sh                 # 使用 samples/stale_cache.xlsx
#   BASE=http://host:8000 ./scripts/demo.sh
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:8000}"
SAMPLE="${1:-samples/stale_cache.xlsx}"

need() { command -v "$1" >/dev/null 2>&1 || { echo "缺少命令: $1" >&2; exit 1; }; }
need curl
need python3

post_task() {
  local file="$1"
  curl -sS -X POST "$BASE/tasks" \
    -F "file=@${file};type=application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
}

wait_status() {
  local tid="$1"
  while :; do
    local body
    body="$(curl -sS "$BASE/tasks/$tid")"
    local st
    st="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])' <<<"$body")"
    echo "  [$tid] $st"
    case "$st" in
      succeeded|failed|cancelled|timed_out) echo "$body"; return ;;
    esac
    sleep 1
  done
}

echo "== 健康检查 =="
curl -sS "$BASE/health"; echo

echo
echo "== 1) 上传过期缓存样例（队列内最多 2 个并行重算） =="
t1="$(post_task "$SAMPLE" | python3 -c 'import json,sys; print(json.load(sys.stdin)["task_id"])')"
echo "task1=$t1"

echo
echo "== 2) 再上传一个任务，观察并行/排队 =="
t2="$(post_task samples/cross_sheet.xlsx | python3 -c 'import json,sys; print(json.load(sys.stdin)["task_id"])')"
echo "task2=$t2"

echo
echo "== 3) 上传可控延迟样例并立即取消（取消先于发布，结果不可下载） =="
t3="$(post_task samples/controllable_delay.xlsx | python3 -c 'import json,sys; print(json.load(sys.stdin)["task_id"])')"
echo "task3=$t3"
curl -sS -X POST "$BASE/tasks/$t3/cancel"; echo

echo
echo "== 等待 task1 结束 =="
wait_status "$t1" >/dev/null

echo
echo "== 4) 下载 task1 的只含值 CSV =="
curl -sS "$BASE/tasks/$t1/download" | tee /tmp/recalc_demo.csv
echo

echo
echo "== task2 / task3 终态 =="
curl -sS "$BASE/tasks/$t2"; echo
curl -sS "$BASE/tasks/$t3"; echo

echo
echo "演示完成。CSV 已保存到 /tmp/recalc_demo.csv"
