#!/usr/bin/env bash
# 可控演示：在“进程创建交接点”发起关闭。
#
# recalc-api-gated 演示服务设置了 RECALC_LAUNCH_GATE=/data/tasks/.launch_gate：
# 任务线程在启动 LibreOffice 之前会等待该“放行文件”，并在等待期间写
# /data/tasks/.launch_gate.waiting 标记。本脚本：
#   1) 上传任务，等线程抵达交接点（.waiting 标记出现，soffice 尚未创建）
#   2) 此时发起关闭（后台阻塞），验证新任务被拒(503)、任务进入 cancelling
#   3) 创建放行文件——进程创建在关闭开始后才完成（迟到进程）
#   4) 收尾把迟到进程回收、清理暂存后才结束；重复关闭等待同一次收尾
#
# 用法：./scripts/demo_shutdown_handoff.sh
# 环境变量：
#   BASE      演示服务地址（默认 http://127.0.0.1:8001）
#   COMPOSE   docker compose 命令（默认 "docker compose"）
set -euo pipefail

# 依赖仓库根目录下的 docker-compose.yml 与 samples/，先定位到根目录
cd "$(dirname "$0")/.."

BASE="${BASE:-http://127.0.0.1:8001}"
COMPOSE="${COMPOSE:-docker compose}"
SERVICE="recalc-api-gated"
GATE="/data/tasks/.launch_gate"
MIME="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
export COMPOSE_PROFILES=gate-demo

need() { command -v "$1" >/dev/null 2>&1 || { echo "缺少命令: $1" >&2; exit 1; }; }
need curl
need python3

json_field() { python3 -c 'import json,sys; print(json.load(sys.stdin)[sys.argv[1]])' "$1"; }

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
  echo "演示服务未就绪" >&2
  exit 1
}

echo "== 0) 启动带交接点闸门的演示服务（profile: gate-demo，端口 8001）=="
$COMPOSE up -d "$SERVICE" >/dev/null
health="$(curl -sS --max-time 3 "$BASE/health" 2>/dev/null || true)"
if [ -n "$health" ] && python3 -c '
import json, sys
d = json.loads(sys.argv[1])
sys.exit(0 if d.get("shutting_down") else 1)
' "$health" 2>/dev/null; then
  echo "演示服务处于关闭状态（上一轮演示），重启 ..."
  $COMPOSE restart "$SERVICE" >/dev/null
fi
wait_healthy
# 清理上一轮可能遗留的放行文件，保证本次一定会在交接点等待
$COMPOSE exec -T "$SERVICE" rm -f "$GATE" "$GATE.waiting"
curl -sS "$BASE/health"; echo

echo
echo "== 1) 上传可控延迟工作簿 =="
tid="$(curl -sS -X POST "$BASE/tasks" \
  -F "file=@samples/controllable_delay.xlsx;type=$MIME" | json_field task_id)"
echo "task=$tid"

echo
echo "== 2) 等待任务线程抵达进程创建交接点（.waiting 标记出现）=="
for _ in $(seq 1 150); do
  if $COMPOSE exec -T "$SERVICE" test -f "$GATE.waiting" 2>/dev/null; then
    break
  fi
  sleep 0.2
done
$COMPOSE exec -T "$SERVICE" test -f "$GATE.waiting" \
  || { echo "线程未抵达交接点" >&2; exit 1; }
echo "线程已在交接点等待放行（soffice 尚未创建）"

echo
echo "== 3) 在交接点发起关闭（后台阻塞等待收尾）=="
summary_file="$(mktemp)"
trap 'rm -f "$summary_file"' EXIT
curl -sS -X POST "$BASE/admin/shutdown" >"$summary_file" &
shutdown_curl=$!
# 等待关闭真正开始（/health 报告 shutting_down）
shutdown_began=0
for _ in $(seq 1 50); do
  h="$(curl -sS --max-time 2 "$BASE/health" 2>/dev/null || true)"
  if [ -n "$h" ] && python3 -c '
import json, sys
d = json.loads(sys.argv[1])
sys.exit(0 if d.get("shutting_down") else 1)
' "$h" 2>/dev/null; then
    shutdown_began=1
    break
  fi
  sleep 0.2
done
[ "$shutdown_began" = "1" ] || { echo "关闭未开始" >&2; exit 1; }

echo
echo "== 4) 关闭已开始：新任务应被拒绝(503)，任务应进入 cancelling =="
code="$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$BASE/tasks" \
  -F "file=@samples/cross_sheet.xlsx;type=$MIME")"
echo "POST /tasks -> $code"
[ "$code" = "503" ] || { echo "预期 503，实际 $code" >&2; exit 1; }
curl -sS "$BASE/tasks/$tid"; echo

echo
echo "== 5) 放行交接点：进程创建在关闭开始后才完成（迟到进程）=="
$COMPOSE exec -T "$SERVICE" touch "$GATE"
wait "$shutdown_curl"
echo "收尾汇总："
python3 -m json.tool <"$summary_file"

echo
echo "== 6) 验证：任务 cancelled、无进程残留、无暂存残留 =="
body="$(curl -sS "$BASE/tasks/$tid")"
echo "$body"
echo "$body" | json_field status | grep -qx cancelled || {
  echo "任务不是 cancelled" >&2; exit 1;
}
$COMPOSE exec -T "$SERVICE" sh -c \
  'pgrep -fa "[s]office|[r]ecalc_uno" || echo "no recalc processes"'
$COMPOSE exec -T "$SERVICE" sh -c \
  'ls -d /data/tasks/*/work 2>/dev/null || echo "no staging dirs"'

echo
echo "== 7) 重复关闭：等待同一次收尾（already_completed=true）=="
curl -sS -X POST "$BASE/admin/shutdown" | python3 -m json.tool

echo
echo "== 交接点关闭演示完成 =="
echo "关键日志（容器内）："
$COMPOSE logs --no-color "$SERVICE" 2>/dev/null | grep -E "交接点|交错|回收|收尾" || true

echo
echo "收尾：停止并移除演示服务"
$COMPOSE stop "$SERVICE" >/dev/null
$COMPOSE rm -f "$SERVICE" >/dev/null
echo "完成。"
