"""停机收尾测试：拒绝新任务、排队不启动、运行停止、交错回收、幂等关闭。

覆盖的停机保证：
1. 开始关闭即拒绝新任务（manager 层抛 ShutdownInProgress，API 层 503）；
2. 排队任务不再启动，运行任务停止重算、不进入提取；
3. 关闭与进程创建交错（RECALC_LAUNCH_GATE 拉开的交接窗口）时，
   迟到进程被创建后照样回收；
4. 关闭与重算完成交错（RECALC_PRE_EXTRACT_GATE）时，不进入提取、
   未发布 CSV 不发布；
5. 任务线程/看门狗/重算进程全部回收后才清理暂存、结束关闭；
6. 重复/并发关闭等待同一次收尾；
7. 关闭前已成功的 CSV 保留且可下载。
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main as main_module
from app.config import Settings
from app.models import TaskStatus
from app.runner import ShutdownInProgress, TaskManager

from .conftest import make_value_workbook


def _wait_for(predicate, timeout=10.0, interval=0.02, what="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    raise AssertionError(f"timed out waiting for {what}")


def _wait_terminal(manager, task_id, timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = manager.get(task_id)
        if record.status in (
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
            TaskStatus.TIMED_OUT,
            TaskStatus.CANCELLED,
        ):
            return record
        time.sleep(0.02)
    raise AssertionError(f"task {task_id} did not finish within {timeout}s")


def _leftover_fake_processes(storage_root: Path) -> list[str]:
    """扫描 /proc，找出仍引用本测试存储根目录的假重算进程（应为空）。"""
    leftovers: list[str] = []
    needle = str(storage_root)
    proc = Path("/proc")
    if not proc.is_dir():
        return leftovers
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (
                (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode()
            )
        except OSError:
            continue
        if (
            "fake_soffice.py" in cmdline or "fake_recalc.py" in cmdline
        ) and needle in cmdline:
            leftovers.append(cmdline.strip())
    return leftovers


def test_shutdown_rejects_new_tasks(fake_env, storage_root, make_manager, tmp_path):
    manager = make_manager(storage_root)
    summary = manager.shutdown()
    assert summary["status"] == "completed"
    assert manager.shutting_down is True
    assert manager.shutdown_completed is True

    xlsx = make_value_workbook(tmp_path / "late.xlsx")
    with pytest.raises(ShutdownInProgress):
        manager.create_task("late.xlsx", xlsx.read_bytes())
    # 被拒绝的任务不得在磁盘留下任何暂存
    assert list(storage_root.iterdir()) == []


def test_shutdown_cancels_running_and_queued(
    fake_env, storage_root, make_manager, tmp_path, monkeypatch
):
    """两个任务重算中、一个任务排队时关闭：全部取消，进程与暂存回收。"""
    monkeypatch.setenv("FAKE_RECALC_DELAY", "30")
    monkeypatch.setenv("FAKE_SOFFICE_DELAY", "30")
    manager = make_manager(storage_root, parallel=2)

    paths = [make_value_workbook(tmp_path / f"w{i}.xlsx") for i in range(3)]
    records = [manager.create_task(p.name, p.read_bytes()) for p in paths]

    # 等待两个 running、一个 queued
    _wait_for(
        lambda: sum(
            manager.get(r.task_id).status == TaskStatus.RUNNING for r in records
        )
        == 2,
        what="two running tasks",
    )
    assert sum(
        manager.get(r.task_id).status == TaskStatus.QUEUED for r in records
    ) == 1

    summary = manager.shutdown()

    # 三个任务全部取消，详情带关闭语义
    for record in records:
        final = manager.get(record.task_id)
        assert final.status == TaskStatus.CANCELLED, final.detail
        assert "服务关闭" in final.detail
        assert final.result_path is None

    # 汇总如实反映：3 个在途任务被收尾，3 个线程被 join
    assert summary["threads_joined"] == 3
    assert sorted(summary["incomplete_task_ids"]) == sorted(
        r.task_id for r in records
    )
    assert summary["retained_succeeded_ids"] == []

    # 暂存全部清理，无残留假重算进程
    for record in records:
        assert not (storage_root / record.task_id / "work").exists()
    assert _leftover_fake_processes(storage_root) == []


def test_shutdown_at_process_creation_handoff_reaps_late_process(
    fake_env, storage_root, make_manager, tmp_path, monkeypatch
):
    """关闭与进程创建交错：交接点放行后迟到的 soffice 照样被回收。"""
    gate = tmp_path / "launch_gate"
    spawn_log = tmp_path / "spawn.log"
    monkeypatch.setenv("RECALC_LAUNCH_GATE", str(gate))
    monkeypatch.setenv("RECALC_GATE_TIMEOUT", "30")
    monkeypatch.setenv("FAKE_SOFFICE_SPAWN_LOG", str(spawn_log))
    manager = make_manager(storage_root)

    xlsx = make_value_workbook(tmp_path / "handoff.xlsx")
    record = manager.create_task("handoff.xlsx", xlsx.read_bytes())

    # 等待任务线程抵达“进程创建交接点”（.waiting 标记出现）
    waiting_marker = tmp_path / "launch_gate.waiting"
    _wait_for(waiting_marker.exists, what="worker at launch gate")

    # 在交接点发起关闭：此时 soffice 尚未创建
    outcome: list[dict] = []
    shutdown_thread = threading.Thread(
        target=lambda: outcome.append(manager.shutdown()),
        name="shutdown-trigger",
    )
    shutdown_thread.start()
    _wait_for(
        lambda: manager.get(record.task_id).status == TaskStatus.CANCELLING,
        what="shutdown to begin",
    )
    # 关闭已开始：新任务必须被拒绝
    with pytest.raises(ShutdownInProgress):
        manager.create_task("late.xlsx", xlsx.read_bytes())

    # 放行交接点：进程创建在关闭开始后才完成（迟到进程）
    gate.touch()
    shutdown_thread.join(timeout=30)
    assert not shutdown_thread.is_alive(), "shutdown did not finish"

    # 迟到进程确实被创建过（start），并且随后被回收（exit）
    log_lines = spawn_log.read_text(encoding="utf-8").splitlines()
    assert any(line.startswith("start") for line in log_lines)
    assert any(line.startswith("exit") for line in log_lines)

    final = manager.get(record.task_id)
    assert final.status == TaskStatus.CANCELLED
    assert "服务关闭" in final.detail
    assert final.result_path is None
    assert not (storage_root / record.task_id / "work").exists()
    assert _leftover_fake_processes(storage_root) == []

    summary = outcome[0]
    assert summary["incomplete_task_ids"] == [record.task_id]
    assert summary["threads_joined"] == 1


def test_shutdown_at_recalc_completion_never_publishes(
    fake_env, storage_root, make_manager, tmp_path, monkeypatch
):
    """关闭与重算完成交错：不进入提取，未发布 CSV 不得发布。"""
    gate = tmp_path / "extract_gate"
    monkeypatch.setenv("RECALC_PRE_EXTRACT_GATE", str(gate))
    monkeypatch.setenv("RECALC_GATE_TIMEOUT", "30")
    monkeypatch.setenv("FAKE_RECALC_DELAY", "0.2")
    manager = make_manager(storage_root)

    xlsx = make_value_workbook(tmp_path / "complete.xlsx")
    record = manager.create_task("complete.xlsx", xlsx.read_bytes())

    # 等待重算完成、线程停在“提取交接点”
    waiting_marker = tmp_path / "extract_gate.waiting"
    _wait_for(waiting_marker.exists, what="worker at extract gate")

    # 在提取交接点发起关闭
    outcome: list[dict] = []
    shutdown_thread = threading.Thread(
        target=lambda: outcome.append(manager.shutdown()),
        name="shutdown-trigger",
    )
    shutdown_thread.start()
    _wait_for(
        lambda: manager.get(record.task_id).status == TaskStatus.CANCELLING,
        what="shutdown to begin",
    )

    # 放行交接点：不得进入提取，结果不得发布
    gate.touch()
    shutdown_thread.join(timeout=30)
    assert not shutdown_thread.is_alive(), "shutdown did not finish"

    final = manager.get(record.task_id)
    assert final.status == TaskStatus.CANCELLED
    assert "服务关闭" in final.detail
    assert final.result_path is None
    task_root = storage_root / record.task_id
    assert not (task_root / "work").exists()
    # 未发布：结果目录里绝不留 CSV
    assert not (task_root / "result" / "result.csv").exists()
    assert _leftover_fake_processes(storage_root) == []


def test_repeated_shutdown_waits_for_same_cleanup(
    fake_env, storage_root, make_manager, tmp_path, monkeypatch
):
    """重复/并发关闭等待同一次收尾，拿到同一份汇总。"""
    monkeypatch.setenv("FAKE_RECALC_DELAY", "30")
    monkeypatch.setenv("FAKE_SOFFICE_DELAY", "30")
    manager = make_manager(storage_root)
    xlsx = make_value_workbook(tmp_path / "slow.xlsx")
    record = manager.create_task("slow.xlsx", xlsx.read_bytes())
    _wait_for(
        lambda: manager.get(record.task_id).status == TaskStatus.RUNNING,
        what="task running",
    )

    results: list[dict] = []

    def _call():
        results.append(manager.shutdown())

    first = threading.Thread(target=_call, name="shutdown-1")
    first.start()
    _wait_for(lambda: manager.shutting_down, what="shutdown to begin")
    # 收尾进行中并发第二次关闭：必须等待同一次收尾
    second = threading.Thread(target=_call, name="shutdown-2")
    second.start()
    first.join(timeout=30)
    second.join(timeout=30)
    assert not first.is_alive() and not second.is_alive()

    assert len(results) == 2
    flags = sorted(r["already_completed"] for r in results)
    assert flags == [False, True]
    # 同一次收尾：核心汇总一致
    base = {k: v for k, v in results[0].items() if k != "already_completed"}
    for r in results[1:]:
        assert {k: v for k, v in r.items() if k != "already_completed"} == base
    # 第二次调用返回时，任务已经落定终态（等待的是同一次收尾）
    assert manager.get(record.task_id).status == TaskStatus.CANCELLED

    # 收尾完成后再调用：立即返回同一汇总
    again = manager.shutdown()
    assert again["already_completed"] is True
    assert again["incomplete_task_ids"] == base["incomplete_task_ids"]


def test_succeeded_csv_retained_after_shutdown(
    fake_env, storage_root, make_manager, tmp_path, monkeypatch
):
    """关闭前已成功的 CSV 保留；在途任务被取消且不发布。"""
    manager = make_manager(storage_root)

    # 先跑完一个快速任务
    fast = make_value_workbook(tmp_path / "fast.xlsx")
    fast_record = manager.create_task("fast.xlsx", fast.read_bytes())
    fast_final = _wait_terminal(manager, fast_record.task_id)
    assert fast_final.status == TaskStatus.SUCCEEDED
    csv_path = Path(fast_final.result_path)
    assert csv_path.is_file()

    # 再来一个慢任务，运行中发起关闭
    monkeypatch.setenv("FAKE_RECALC_DELAY", "30")
    monkeypatch.setenv("FAKE_SOFFICE_DELAY", "30")
    slow_record = manager.create_task("fast.xlsx", fast.read_bytes())
    _wait_for(
        lambda: manager.get(slow_record.task_id).status == TaskStatus.RUNNING,
        what="slow task running",
    )
    summary = manager.shutdown()

    # 成功的 CSV 保留；慢任务取消
    assert manager.get(fast_record.task_id).status == TaskStatus.SUCCEEDED
    assert csv_path.is_file()
    assert manager.get(slow_record.task_id).status == TaskStatus.CANCELLED
    assert summary["retained_succeeded_ids"] == [fast_record.task_id]
    assert summary["incomplete_task_ids"] == [slow_record.task_id]


@pytest.fixture()
def client(fake_env, storage_root, monkeypatch):
    cfg = Settings(
        storage_root=str(storage_root),
        max_upload_bytes=8 * 1024 * 1024,
        max_parallel=2,
        recalc_timeout_seconds=60,
        uno_connect_attempts=10,
        uno_connect_interval=0.1,
        soffice_bin=os.environ.get("SOFFICE_BIN", "soffice"),
    )
    test_manager = TaskManager(cfg)
    monkeypatch.setattr(main_module, "manager", test_manager)
    with TestClient(main_module.app) as c:
        yield c


def _upload(client, path: Path):
    with path.open("rb") as fh:
        return client.post(
            "/tasks",
            files={
                "file": (
                    path.name,
                    fh,
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            },
        )


def test_api_shutdown_flow(client, tmp_path, monkeypatch):
    """API 层：/admin/shutdown 阻塞至收尾完成，随后新任务 503。"""
    # 先成功一个任务（验证关闭后 CSV 仍可下载）
    ok = make_value_workbook(tmp_path / "ok.xlsx")
    ok_id = _upload(client, ok).json()["task_id"]
    _wait_for(
        lambda: client.get(f"/tasks/{ok_id}").json()["status"] == "succeeded",
        what="first task succeeded",
    )

    # 再上传一个慢任务，运行中触发关闭
    monkeypatch.setenv("FAKE_RECALC_DELAY", "30")
    monkeypatch.setenv("FAKE_SOFFICE_DELAY", "30")
    slow = make_value_workbook(tmp_path / "slow.xlsx")
    slow_id = _upload(client, slow).json()["task_id"]
    _wait_for(
        lambda: client.get(f"/tasks/{slow_id}").json()["status"] == "running",
        what="slow task running",
    )

    resp = client.post("/admin/shutdown")
    assert resp.status_code == 200
    summary = resp.json()
    assert summary["status"] == "completed"
    assert summary["already_completed"] is False
    assert summary["incomplete_task_ids"] == [slow_id]
    assert summary["retained_succeeded_ids"] == [ok_id]

    # 开始关闭即拒绝新任务
    resp = _upload(client, ok)
    assert resp.status_code == 503
    assert "关闭" in resp.json()["detail"]

    # 健康检查暴露关闭状态
    health = client.get("/health").json()
    assert health["shutting_down"] is True
    assert health["shutdown_completed"] is True

    # 在途任务已取消；成功的 CSV 保留可下载
    assert client.get(f"/tasks/{slow_id}").json()["status"] == "cancelled"
    dl = client.get(f"/tasks/{ok_id}/download")
    assert dl.status_code == 200
    assert "sample_id,reading,corrected" in dl.text

    # 重复关闭：等待同一次收尾，返回同一汇总
    again = client.post("/admin/shutdown")
    assert again.status_code == 200
    assert again.json()["already_completed"] is True
    assert again.json()["incomplete_task_ids"] == [slow_id]
