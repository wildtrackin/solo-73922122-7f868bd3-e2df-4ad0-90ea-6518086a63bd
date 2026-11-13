"""HTTP 层测试：8 MiB 限制、状态/取消/下载接口语义。"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main as main_module
from app.config import Settings
from app.runner import TaskManager

from .conftest import make_value_workbook


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
            files={"file": (path.name, fh, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        )


def test_upload_status_download_flow(client, tmp_path):
    xlsx = make_value_workbook(tmp_path / "ok.xlsx")
    resp = _upload(client, xlsx)
    assert resp.status_code == 202
    task_id = resp.json()["task_id"]

    final = None
    for _ in range(200):
        r = client.get(f"/tasks/{task_id}")
        assert r.status_code == 200
        if r.json()["status"] in ("succeeded", "failed", "cancelled", "timed_out"):
            final = r.json()
            break
        time.sleep(0.05)
    assert final and final["status"] == "succeeded", final

    dl = client.get(f"/tasks/{task_id}/download")
    assert dl.status_code == 200
    assert "text/csv" in dl.headers["content-type"]
    assert "sample_id,reading,corrected" in dl.text


def test_upload_size_limit(client, tmp_path):
    big = tmp_path / "big.xlsx"
    big.write_bytes(b"PK\x03\x04" + b"0" * (9 * 1024 * 1024))
    resp = _upload(client, big)
    assert resp.status_code == 413


def test_exact_limit_boundary_passes_size_check(client, tmp_path):
    # 恰好 8 MiB 不触发 413；伪 zip 容器会被 415 拦下，
    # 证明大小检查在容器校验之前且边界正确。
    boundary = tmp_path / "boundary.xlsx"
    boundary.write_bytes(b"PK\x03\x04" + b"0" * (8 * 1024 * 1024 - 4))
    resp = _upload(client, boundary)
    assert resp.status_code == 415


def test_reject_non_xlsx(client, tmp_path):
    bad = tmp_path / "note.txt"
    bad.write_bytes(b"hello")
    resp = _upload(client, bad)
    assert resp.status_code == 415


def test_structure_precheck_returns_422(client, storage_root, tmp_path):
    from openpyxl import Workbook

    bad = tmp_path / "nosheet.xlsx"
    wb = Workbook()
    wb.active.title = "其他表"
    wb.active.append(["sample_id", "reading", "corrected"])
    wb.save(bad)

    resp = _upload(client, bad)
    assert resp.status_code == 422
    assert "测量" in resp.json()["detail"]
    # 预校验失败不创建任何任务目录
    assert list(storage_root.iterdir()) == []


def test_unknown_task_404(client):
    assert client.get("/tasks/nope").status_code == 404
    assert client.post("/tasks/nope/cancel").status_code == 404
    assert client.get("/tasks/nope/download").status_code == 404


def test_download_only_when_succeeded(client, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_RECALC_DELAY", "3")
    xlsx = make_value_workbook(tmp_path / "slow.xlsx")
    task_id = _upload(client, xlsx).json()["task_id"]

    # 运行中下载应 409
    time.sleep(0.4)
    assert client.get(f"/tasks/{task_id}/download").status_code == 409

    # 取消后同样不可下载
    r = client.post(f"/tasks/{task_id}/cancel")
    assert r.status_code == 200
    assert r.json()["cancellation_accepted"] is True
    for _ in range(200):
        status = client.get(f"/tasks/{task_id}").json()["status"]
        if status == "cancelled":
            break
        time.sleep(0.05)
    assert client.get(f"/tasks/{task_id}/download").status_code == 409


def test_health(client):
    assert client.get("/health").json() == {"status": "ok", "max_parallel": 2}
