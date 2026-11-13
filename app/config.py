"""全局配置，可通过环境变量覆盖。"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    # 上传大小硬上限 8 MiB
    max_upload_bytes: int = 8 * 1024 * 1024
    # 同时执行重算的任务数（超出则排队）
    max_parallel: int = 2
    # 单个任务从启动重算起的硬时限（秒）
    recalc_timeout_seconds: int = 60
    # 连接 LibreOffice UNO 的重试参数（总耗时受 60s 硬时限约束）
    uno_connect_attempts: int = 60
    uno_connect_interval: float = 0.5
    # 存储根目录：工作簿、计算环境与结果均按任务隔离
    storage_root: str = "/data/tasks"
    # LibreOffice 可执行文件（测试时可替换为假实现）
    soffice_bin: str = "soffice"


def load_settings() -> Settings:
    """从环境变量构造 Settings（在应用/测试启动时读取，便于 monkeypatch）。"""
    return Settings(
        max_upload_bytes=_env_int(
            "RECALC_MAX_UPLOAD_BYTES", 8 * 1024 * 1024
        ),
        max_parallel=_env_int("RECALC_MAX_PARALLEL", 2),
        recalc_timeout_seconds=_env_int("RECALC_RECALC_TIMEOUT", 60),
        uno_connect_attempts=_env_int("RECALC_UNO_ATTEMPTS", 60),
        uno_connect_interval=float(
            os.environ.get("RECALC_UNO_INTERVAL", "0.5")
        ),
        storage_root=os.environ.get("RECALC_STORAGE_ROOT", "/data/tasks"),
        soffice_bin=os.environ.get("SOFFICE_BIN", "soffice"),
    )


settings = load_settings()
