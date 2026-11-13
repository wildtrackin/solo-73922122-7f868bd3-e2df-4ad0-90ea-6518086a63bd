#!/usr/bin/env bash
# 停机收尾演示：两个任务重算中、一个任务排队时发起关闭。
#
# 演示点：
#   1) 开始关闭即拒绝新任务（POST /tasks -> 503）
#   2) 排队任务不再启动；运行任务停止重算、不进入提取
#   3) 任务线程/看门狗/重算进程全部回收后，才清理暂存、结束关闭
#   4) 重复关闭等待同一次收尾（already_completed=true）
#   5) 关闭前已成功的 CSV 保留且可下载
#
# 前置：docker compose up -d（脚本也会尝试自动拉起）
# 用法：./scripts/demo_shutdown.sh
# 环境变量：
#   BASE      服务地址（默认 http://127.0.0.1:8000）
#   DELAY_K   可控延迟样例的重算规模（默认 20000000，越大越慢）
#   COMPOSE   docker compose 命令（默认 "docker compose"）
set -euo pipefail

# 依赖仓库根目录下的 docker-compose.yml 与 samples/，先定位到根目录
cd "$(dirname "$0")/.."

BASE="${BASE:-http://127.0.0.1:8000}"
DELAY_K="${DELAY_K:-20000000}"
COMPOSE="${COMPOSE:-docker compose}"
SERVICE="${SERVICE:-recalc-api}"
MIME="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

need() { command -v "$1" >/dev/null 2>&1 || { echo "缺少命令: $1" >&2; exit 1; }; }
need curl
need python3

json_field() { python3 -c 'import json,sys; print(json.load(sys.stdin)[sys.argv[1]])' "$1"; }

post_task() {
  curl -sS -X POST "$BASE/tasks" -F "file=@$1;type=$MIME"
}

status_of() {
  curl -sS "$BASE/tasks/$1" | json_field status
}

wait_healthy() {
  local health
  for _ in $(seq 1 90); do
    health="$(curl -sS --max-time 2 "$BASE/health" 2>/dev/null || true)"
    if [ -n "$health" ] && python3 -c '
import json, sys
d = json.loads(sys.argv[1])
sys.exit(0 if not d.get("shutting_down") else 1)
' "$health" 2>/dev/null; then
      return 0
    fi
    sleep 1
  done
  echo "服务未就绪（或一直处于关闭状态），请先 docker compose up -d" >&2
  exit 1
}

wait_status() { # <task_id> <期望终态>
  local tid="$1" want="$2" st
  for _ in $(seq 1 120); do
    st="$(status_of "$tid")"
    if [ "$st" = "$want" ]; then return 0; fi
    sleep 0.5
  done
  echo "任务 $tid 未在预期时间内进入 $want（当前 $st）" >&2
  exit 1
}

echo "== 0) 确认服务在线且未处于关闭状态 =="
health="$(curl -sS --max-time 3 "$BASE/health" 2>/dev/null || true)"
if [ -z "$health" ]; then
  echo "服务不可达，尝试拉起：$COMPOSE up -d $SERVICE"
  $COMPOSE up -d "$SERVICE"
elif python3 -c '
import json, sys
d = json.loads(sys.argv[1])
sys.exit(0 if d.get("shutting_down") else 1)
' "$health" 2>/dev/null; then
  echo "服务处于关闭收尾状态（可能是上一轮演示），重启 $SERVICE ..."
  $COMPOSE restart "$SERVICE"
fi
wait_healthy
curl -sS "$BASE/health"; echo

echo
echo "== 1) 生成可控延迟样例（校准!B1 = $DELAY_K；容器内生成后拷回宿主机，避免 bind mount 权限问题）=="
$COMPOSE exec -T "$SERVICE" \
  python3 /samples-src/generate_samples.py --out-dir /tmp/gen --delay-k "$DELAY_K" >/dev/null
$COMPOSE cp "$SERVICE:/tmp/gen/controllable_delay.xlsx" samples/controllable_delay.xlsx

echo
echo "== 2) 先上传一个快速任务并等它成功（验证关闭前成功的 CSV 保留）=="
t0="$(post_task samples/cross_sheet.xlsx | json_field task_id)"
echo "fast task=$t0"
wait_status "$t0" succeeded
echo "fast task 已 succeeded"

echo
echo "== 3) 连续上传 3 个慢任务：两个并行重算、一个排队 =="
t1="$(post_task samples/controllable_delay.xlsx | json_field task_id)"
t2="$(post_task samples/controllable_delay.xlsx | json_field task_id)"
t3="$(post_task samples/controllable_delay.xlsx | json_field task_id)"
echo "slow tasks: $t1 $t2 $t3"
# 等待出现 2 running + 1 queued 的格局
for _ in $(seq 1 60); do
  s1="$(status_of "$t1")"; s2="$(status_of "$t2")"; s3="$(status_of "$t3")"
  running=0; queued=0
  for s in "$s1" "$s2" "$s3"; do
    [ "$s" = "running" ] && running=$((running + 1))
    [ "$s" = "queued" ] && queued=$((queued + 1))
  done
  [ "$running" -eq 2 ] && [ "$queued" -eq 1 ] && break
  sleep 0.5
done
echo "关闭前状态: [$s1] [$s2] [$s3]（预期 running/running/queued）"
if [ "$running" -ne 2 ] || [ "$queued" -ne 1 ]; then
  echo "未形成 2 running + 1 queued，可调大 DELAY_K 后重试" >&2
  exit 1
fi

echo
echo "== 4) 发起停机收尾 POST /admin/shutdown（阻塞至全部回收完成）=="
summary="$(curl -sS -X POST "$BASE/admin/shutdown")"
echo "$summary" | python3 -m json.tool

echo
echo "== 5) 验证关闭语义 =="
echo "-- 5.1 开始关闭即拒绝新任务（预期 503）"
code="$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$BASE/tasks" \
  -F "file=@samples/cross_sheet.xlsx;type=$MIME")"
echo "    POST /tasks -> $code"
[ "$code" = "503" ] || { echo "预期 503，实际 $code" >&2; exit 1; }

echo "-- 5.2 三个在途任务全部 cancelled（排队未启动、运行停止未提取）"
for tid in "$t1" "$t2" "$t3"; do
  body="$(curl -sS "$BASE/tasks/$tid")"
  echo "    $body"
  echo "$body" | json_field status | grep -qx cancelled || {
    echo "任务 $tid 不是 cancelled" >&2; exit 1;
  }
done

echo "-- 5.3 关闭前已成功的 CSV 保留且可下载（预期 200 + 表头）"
dl_code="$(curl -sS -o /tmp/recalc_retained.csv -w '%{http_code}' \
  "$BASE/tasks/$t0/download")"
echo "    GET /tasks/$t0/download -> $dl_code"
[ "$dl_code" = "200" ] || { echo "预期 200，实际 $dl_code" >&2; exit 1; }
head -2 /tmp/recalc_retained.csv

echo "-- 5.4 容器内无重算进程残留（soffice / recalc_uno）"
# 字符类写法避免 pgrep 匹配到执行检查的 sh 进程自身
$COMPOSE exec -T "$SERVICE" sh -c \
  'pgrep -fa "[s]office|[r]ecalc_uno" || echo "    no recalc processes"'

echo "-- 5.5 未完成任务暂存已清理（/data/tasks 下无 work 目录）"
$COMPOSE exec -T "$SERVICE" sh -c \
  'ls -d /data/tasks/*/work 2>/dev/null || echo "    no staging dirs"'

echo "-- 5.6 健康检查报告关闭状态"
curl -sS "$BASE/health"; echo

echo
echo "== 6) 重复关闭：等待同一次收尾（already_completed=true）=="
curl -sS -X POST "$BASE/admin/shutdown" | python3 -m json.tool

echo
echo "演示完成。服务保持在关闭收尾状态：新任务被拒、状态可查、成功 CSV 可下载。"
echo "恢复服务：$COMPOSE restart $SERVICE"
