"""任务级进程与暂存资源管理。

每个任务拥有独立的工作目录、独立的 LibreOffice 用户配置（-env:UserInstallation）
与独立的 UNO 命名管道；LibreOffice 以新会话/新进程组启动，
终止时只向本任务的进程组发信号，绝不影响另一任务。
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("recalc.processes")


@dataclass
class WorkDirs:
    root: Path
    # 暂存（workbook/profile/pipe）与结果分离：暂存可整体清理，结果保留供下载
    work_dir: Path
    results_dir: Path
    workbook_dir: Path
    input_xlsx: Path
    recalc_xlsx: Path
    profile_dir: Path
    pipe_dir: Path
    pipe_name: str
    uno_url: str
    result_csv: Path


def create_work_dirs(storage_root: str | Path, task_id: str) -> WorkDirs:
    root = Path(storage_root) / task_id
    work_dir = root / "work"
    results_dir = root / "result"
    workbook_dir = work_dir / "workbook"
    profile_dir = work_dir / "lo-profile"
    pipe_dir = work_dir / "pipe"
    for directory in (work_dir, results_dir, workbook_dir, profile_dir, pipe_dir):
        directory.mkdir(parents=True, exist_ok=True)

    pipe_name = f"recalc_{task_id.replace('-', '_')}"
    uno_url = f"pipe,name={pipe_name};urp;StarOffice.ComponentContext"
    return WorkDirs(
        root=root,
        work_dir=work_dir,
        results_dir=results_dir,
        workbook_dir=workbook_dir,
        input_xlsx=workbook_dir / "input.xlsx",
        recalc_xlsx=workbook_dir / "recalculated.xlsx",
        profile_dir=profile_dir,
        pipe_dir=pipe_dir,
        pipe_name=pipe_name,
        uno_url=uno_url,
        result_csv=results_dir / "result.csv",
    )


def terminate_process_group(
    proc: subprocess.Popen, marker: Path | None = None
) -> None:
    """终止本任务 LibreOffice 进程组内的全部进程。

    soffice 会派生子进程（oosplash/soffice.bin），使用进程组才能一次回收干净。
    信号被运行环境拒绝时（受限沙箱）：
      1. 尝试对主进程直接发信号；
      2. 写入任务级 ``.terminate`` 标记文件（测试用假 soffice 会据此自行退出）；
      3. 回退为等待退出，保证不向上抛出。
    正常容器内同用户进程组的 SIGTERM/SIGKILL 均可用，标记文件无副作用。
    """
    if proc.poll() is not None:
        return
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return

    signaled = False
    try:
        os.killpg(pgid, signal.SIGTERM)
        signaled = True
    except ProcessLookupError:
        return
    except PermissionError:
        try:
            proc.terminate()
            signaled = True
        except (ProcessLookupError, PermissionError):
            pass

    if not signaled and marker is not None:
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.touch()
        except OSError:
            pass

    if signaled:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                return
            time.sleep(0.1)

    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except PermissionError:
        try:
            proc.kill()
        except (ProcessLookupError, PermissionError):
            pass

    # 信号全被拒绝时再次确保标记存在，然后等待假进程自行退出
    if marker is not None:
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.touch()
        except OSError:
            pass

    try:
        proc.wait(timeout=10.0)
    except subprocess.TimeoutExpired:
        logger.warning(
            "process group %s could not be reaped in this environment", pgid
        )


def cleanup_work_dirs(work_dir: str | Path) -> None:
    """清理任务全部暂存（工作簿、LibreOffice profile、UNO 管道目录）。

    结果目录与暂存分离，不受影响；终态状态落定前调用本函数，
    满足“清理完成才报结束”。
    """
    path = Path(work_dir)
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
