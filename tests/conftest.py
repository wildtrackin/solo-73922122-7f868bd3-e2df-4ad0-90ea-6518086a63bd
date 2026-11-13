"""pytest 公共夹具：假 LibreOffice 环境与工作簿构造器。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from openpyxl import Workbook

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from app.runner import TaskManager  # noqa: E402

FAKES = Path(__file__).parent / "fakes"


@pytest.fixture()
def storage_root(tmp_path: Path) -> Path:
    root = tmp_path / "tasks"
    root.mkdir()
    return root


@pytest.fixture()
def fake_env(monkeypatch) -> dict:
    python = sys.executable
    env = {
        "SOFFICE_BIN": python,
        # 让假 soffice 知道自己要跑哪个脚本
        "RECALC_SCRIPT_OVERRIDE": str(FAKES / "fake_recalc.py"),
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    # fake_soffice 通过 python -m 方式不便，直接把脚本作为 SOFFICE_BIN
    monkeypatch.setenv("SOFFICE_BIN", str(FAKES / "fake_soffice.py"))
    return env


@pytest.fixture()
def make_manager():
    def _factory(storage_root: Path, timeout: int = 60, parallel: int = 2) -> TaskManager:
        cfg = Settings(
            storage_root=str(storage_root),
            max_upload_bytes=8 * 1024 * 1024,
            max_parallel=parallel,
            recalc_timeout_seconds=timeout,
            uno_connect_attempts=10,
            uno_connect_interval=0.1,
            soffice_bin=os.environ.get("SOFFICE_BIN", "soffice"),
        )
        return TaskManager(cfg)

    return _factory


def make_value_workbook(
    path: Path,
    rows: list[tuple[str, float, float]] | None = None,
) -> Path:
    """构造 data_only 可读的纯值工作簿（假重算原样复制后可直接提取）。"""
    wb = Workbook()
    ws = wb.active
    ws.title = "测量"
    ws.append(["sample_id", "reading", "corrected"])
    rows = rows or [
        ("S001", 10.0, 10.5),
        ("S002", 20.0, 21.5),
        ("S003", 30.0, 32.5),
    ]
    for row in rows:
        ws.append(list(row))
    wb.create_sheet("校准")
    wb.save(path)
    return path


def make_formula_workbook(path: Path, bad: bool = False) -> Path:
    """构造跨表公式工作簿（openpyxl 不写缓存 -> data_only 读到 None）。"""
    wb = Workbook()
    ws = wb.active
    ws.title = "测量"
    ws.append(["sample_id", "reading", "corrected"])
    cal = wb.create_sheet("校准")
    cal["A1"] = 2.0
    cal["A2"] = 1.0
    for idx, reading in enumerate((10.0, 20.0, 30.0), start=2):
        ws.cell(row=idx, column=1, value=f"S{idx - 1:03d}")
        ws.cell(row=idx, column=2, value=reading)
        if bad and idx == 3:
            ws.cell(row=idx, column=3, value="=B3*校准!A1/0")  # 除零
        else:
            ws.cell(row=idx, column=3, value=f"=B{idx}*校准!A1+校准!A2")
    wb.save(path)
    return path
