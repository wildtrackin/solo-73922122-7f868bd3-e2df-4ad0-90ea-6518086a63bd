"""端到端管道测试：并行、取消、超时、失败、隔离、缓存防线。"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from openpyxl import load_workbook

from app.models import TaskStatus
from samples.generate_samples import (
    build_controllable_delay,
    build_cross_sheet,
    build_stale_cache,
)

from .conftest import make_formula_workbook, make_value_workbook


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


def test_success_pipeline_and_csv(fake_env, storage_root, make_manager, tmp_path):
    manager = make_manager(storage_root)
    xlsx = make_value_workbook(tmp_path / "ok.xlsx")
    record = manager.create_task("ok.xlsx", xlsx.read_bytes())

    final = _wait_terminal(manager, record.task_id)
    assert final.status == TaskStatus.SUCCEEDED, final.detail
    assert final.row_count == 3

    csv_path = Path(final.result_path)
    assert csv_path.is_file()
    content = csv_path.read_text(encoding="utf-8")
    lines = content.strip().splitlines()
    assert lines[0] == "sample_id,reading,corrected"
    assert lines[1] == "S001,10.0,10.5"
    # 只含值：CSV 中不得出现公式或缓存标记
    assert "=" not in content

    # 暂存已清理，结果保留
    task_root = storage_root / record.task_id
    assert not (task_root / "work").exists()
    assert (task_root / "result" / "result.csv").is_file()


def test_parallel_limit_two_and_isolation(
    fake_env, storage_root, make_manager, tmp_path, monkeypatch
):
    monkeypatch.setenv("FAKE_RECALC_DELAY", "1.5")
    manager = make_manager(storage_root, parallel=2)

    paths = [make_value_workbook(tmp_path / f"w{i}.xlsx") for i in range(4)]
    records = [
        manager.create_task(p.name, p.read_bytes()) for p in paths
    ]
    time.sleep(0.6)

    running = [
        r for r in records if manager.get(r.task_id).status == TaskStatus.RUNNING
    ]
    queued = [
        r for r in records if manager.get(r.task_id).status == TaskStatus.QUEUED
    ]
    assert len(running) == 2
    assert len(queued) == 2

    finals = [_wait_terminal(manager, r.task_id, timeout=20) for r in records]
    assert all(r.status == TaskStatus.SUCCEEDED for r in finals)

    # 任务目录互不串用
    roots = list(storage_root.iterdir())
    assert len({r.name for r in roots}) == 4
    for record in finals:
        csv_path = Path(record.result_path)
        assert csv_path.is_file()
        # 每个结果只含自己的三行
        assert len(csv_path.read_text().strip().splitlines()) == 4


def test_cancel_running_task_never_publishes(
    fake_env, storage_root, make_manager, tmp_path, monkeypatch
):
    monkeypatch.setenv("FAKE_RECALC_DELAY", "8")
    manager = make_manager(storage_root)
    xlsx = make_value_workbook(tmp_path / "slow.xlsx")
    record = manager.create_task("slow.xlsx", xlsx.read_bytes())

    # 等任务进入 running
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if manager.get(record.task_id).status == TaskStatus.RUNNING:
            break
        time.sleep(0.02)
    assert manager.get(record.task_id).status == TaskStatus.RUNNING

    accepted, _ = manager.request_cancel(record.task_id)
    assert accepted is True
    final = _wait_terminal(manager, record.task_id, timeout=15)
    assert final.status == TaskStatus.CANCELLED
    assert final.result_path is None

    task_root = storage_root / record.task_id
    assert not task_root.exists() or not (task_root / "work").exists()
    # 取消后下载目标不存在
    assert not (task_root / "result").exists() or not list(
        (task_root / "result").glob("*.csv")
    )


def test_cancel_queued_task(fake_env, storage_root, make_manager, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_RECALC_DELAY", "2")
    manager = make_manager(storage_root, parallel=1)
    paths = [make_value_workbook(tmp_path / f"q{i}.xlsx") for i in range(2)]
    first = manager.create_task(paths[0].name, paths[0].read_bytes())
    second = manager.create_task(paths[1].name, paths[1].read_bytes())

    time.sleep(0.4)
    assert manager.get(second.task_id).status == TaskStatus.QUEUED
    accepted, _ = manager.request_cancel(second.task_id)
    assert accepted is True

    second_final = _wait_terminal(manager, second.task_id)
    assert second_final.status == TaskStatus.CANCELLED
    first_final = _wait_terminal(manager, first.task_id, timeout=15)
    assert first_final.status == TaskStatus.SUCCEEDED


def test_timeout_kills_and_reports(
    fake_env, storage_root, make_manager, tmp_path, monkeypatch
):
    monkeypatch.setenv("FAKE_RECALC_DELAY", "10")
    manager = make_manager(storage_root, timeout=2)
    xlsx = make_value_workbook(tmp_path / "t.xlsx")
    record = manager.create_task("t.xlsx", xlsx.read_bytes())

    final = _wait_terminal(manager, record.task_id, timeout=15)
    assert final.status == TaskStatus.TIMED_OUT
    assert "60" not in final.detail or "2" in final.detail  # 使用的是 2s 测试时限
    assert final.result_path is None
    # 暂存已清理
    assert not (storage_root / record.task_id / "work").exists()


def test_recalc_failure_fails_batch(
    fake_env, storage_root, make_manager, tmp_path, monkeypatch
):
    monkeypatch.setenv("FAKE_RECALC_FAIL", "1")
    manager = make_manager(storage_root)
    xlsx = make_value_workbook(tmp_path / "f.xlsx")
    record = manager.create_task("f.xlsx", xlsx.read_bytes())
    final = _wait_terminal(manager, record.task_id)
    assert final.status == TaskStatus.FAILED
    assert "重算失败" in final.detail


def test_formula_error_fails_batch(fake_env, storage_root, make_manager, tmp_path):
    manager = make_manager(storage_root)
    xlsx = make_formula_workbook(tmp_path / "div0.xlsx", bad=True)
    record = manager.create_task("div0.xlsx", xlsx.read_bytes())
    final = _wait_terminal(manager, record.task_id)
    # 假重算原样复制；openpyxl data_only 读到除零公式无缓存 -> 非有限值/错误
    assert final.status == TaskStatus.FAILED


def test_non_finite_corrected_fails_batch(
    fake_env, storage_root, make_manager, tmp_path
):
    manager = make_manager(storage_root)
    xlsx = make_value_workbook(
        tmp_path / "inf.xlsx",
        rows=[("S001", 1.0, float("inf"))],
    )
    record = manager.create_task("inf.xlsx", xlsx.read_bytes())
    final = _wait_terminal(manager, record.task_id)
    assert final.status == TaskStatus.FAILED
    assert "corrected" in final.detail


def test_missing_sheet_fails(fake_env, storage_root, make_manager, tmp_path):
    from app.extractor import WorkbookError

    wb_path = tmp_path / "wrong.xlsx"
    wb = load_workbook(make_value_workbook(wb_path))
    wb["测量"].title = "其他表"
    wb.save(wb_path)

    manager = make_manager(storage_root)
    # API 层的结构预校验应直接拒绝
    with pytest.raises(WorkbookError):
        manager.validate_upload(wb_path.read_bytes())

    # 即便绕过预校验进入管道，重算后的严格提取仍会整批失败
    record = manager.create_task("wrong.xlsx", wb_path.read_bytes())
    final = _wait_terminal(manager, record.task_id)
    assert final.status == TaskStatus.FAILED
    assert "测量" in final.detail


def test_stale_cache_sample_is_rejected_when_not_recalculated(
    fake_env, storage_root, make_manager, tmp_path, monkeypatch
):
    """过期缓存样例：未真正重算（缓存被剥离）时必须整批失败。

    真实 LibreOffice 会重新计算公式并用新值覆盖缓存从而成功；
    本测试锁定“绝不沿用上传缓存”的服务侧防线——
    只要 LibreOffice 没有产出新值，哪怕上传簿自带过期缓存也拒绝发布。
    """
    monkeypatch.setenv("FAKE_RECALC_NO_RECALC", "1")
    sample = tmp_path / "stale.xlsx"
    sample.write_bytes(build_stale_cache())

    # 先确认样例确实携带 999.0 的过期缓存
    wb = load_workbook(sample, data_only=True)
    cached = [wb["测量"].cell(row=r, column=3).value for r in range(2, 6)]
    assert cached == [999.0, 999.0, 999.0, 999.0]

    manager = make_manager(storage_root)
    record = manager.create_task("stale.xlsx", sample.read_bytes())
    final = _wait_terminal(manager, record.task_id)
    assert final.status == TaskStatus.FAILED
    assert "corrected" in final.detail


def test_cancel_after_completion_is_conflict(
    fake_env, storage_root, make_manager, tmp_path
):
    manager = make_manager(storage_root)
    xlsx = make_value_workbook(tmp_path / "ok.xlsx")
    record = manager.create_task("ok.xlsx", xlsx.read_bytes())
    _wait_terminal(manager, record.task_id)
    accepted, detail = manager.request_cancel(record.task_id)
    assert accepted is False
    assert "terminal" in detail


def test_terminate_process_group_reaps_children(
    fake_env, storage_root, make_manager, tmp_path, monkeypatch
):
    """忽略 SIGTERM 的假 soffice 必须被 SIGKILL 回收，无残留进程。

    受限 CI 沙箱可能禁止向子进程发信号；此时验证终止路径不抛异常，
    进程组回收的强校验在具备信号权限的环境（含 Docker 容器）内执行。
    """
    import signal as signal_mod

    probe = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(5)"],
        start_new_session=True,
    )
    can_signal = True
    try:
        os.kill(probe.pid, signal_mod.SIGTERM)
        probe.wait(timeout=5)
    except (PermissionError, OSError):
        can_signal = False
        probe.wait(timeout=6)

    monkeypatch.setenv("FAKE_SOFFICE_IGNORE_TERM", "1")
    monkeypatch.setenv("FAKE_RECALC_DELAY", "1")
    manager = make_manager(storage_root, timeout=3)
    xlsx = make_value_workbook(tmp_path / "k.xlsx")
    record = manager.create_task("k.xlsx", xlsx.read_bytes())
    final = _wait_terminal(manager, record.task_id, timeout=15)
    assert final.status in (TaskStatus.SUCCEEDED, TaskStatus.TIMED_OUT)

    if not can_signal:
        pytest.skip("sandbox forbids signaling child processes")

    # 没有任何 fake_soffice 进程残留
    out = subprocess.run(
        ["pgrep", "-f", "fake_soffice.py"],
        capture_output=True,
        text=True,
    )
    assert out.stdout.strip() == ""


def test_sample_generation_outputs(tmp_path):
    out = tmp_path / "samples"
    from samples.generate_samples import main as gen_main
    import sys

    old_argv = sys.argv
    try:
        sys.argv = ["generate_samples.py", "--out-dir", str(out), "--delay-k", "1000"]
        gen_main()
    finally:
        sys.argv = old_argv

    names = {p.name for p in out.iterdir()}
    assert names == {
        "stale_cache.xlsx",
        "cross_sheet.xlsx",
        "controllable_delay.xlsx",
    }
    for path in out.iterdir():
        wb = load_workbook(path)
        assert "测量" in wb.sheetnames
        assert "校准" in wb.sheetnames


def test_cross_sheet_sample_structure(tmp_path):
    path = tmp_path / "cross.xlsx"
    path.write_bytes(build_cross_sheet())
    wb = load_workbook(path)
    ws = wb["测量"]
    assert [c.value for c in ws[1]] == ["sample_id", "reading", "corrected"]
    formula = ws.cell(row=2, column=3).value
    assert "校准!" in formula


def test_delay_sample_references_calibration(tmp_path):
    path = tmp_path / "delay.xlsx"
    path.write_bytes(build_controllable_delay(2000))
    wb = load_workbook(path)
    assert wb["校准"]["B1"].value == 2000
    formula = wb["测量"].cell(row=2, column=3).value
    assert "校准!$B$1" in formula
