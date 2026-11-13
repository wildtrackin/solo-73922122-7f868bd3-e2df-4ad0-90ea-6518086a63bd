"""重算任务编排：并发槽位、取消、60s 硬时限与终止回收。

关键保证：
1. 最多 ``RECALC_MAX_PARALLEL`` 个任务真正进入重算，其余排队；
2. 60s 从“启动重算”（取得槽位、LibreOffice 启动）起算，排队时间不计；
3. 取消、超时、失败都会终止本任务进程组并清理全部暂存，再把任务置为终态；
4. 取消一经受理，即便重算随后完成也不会发布结果；
5. 任务间目录、LibreOffice profile、UNO 管道完全隔离。
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

from .config import Settings, settings as default_settings
from .extractor import WorkbookError, extract_to_csv, validate_structure
from .models import TaskRecord, TaskStatus
from .processes import (
    WorkDirs,
    cleanup_work_dirs,
    create_work_dirs,
    terminate_process_group,
)

logger = logging.getLogger("recalc.runner")

RECALC_SCRIPT = Path(__file__).with_name("recalc_uno.py")


class RecalcError(Exception):
    """重算阶段失败（soffice/UNO 非零退出等）。"""


class TaskCancelled(Exception):
    """取消在管道内部被检测到。"""


class RecalcTimeout(Exception):
    """60s 硬时限到期。"""


def _resolve_lo_python() -> str:
    # 允许通过环境变量显式指定 LibreOffice 自带 python；
    # Debian 镜像里系统 python3 即可 import uno。
    env_python = os.environ.get("LO_PYTHON_BIN")
    if env_python:
        return env_python
    return shutil.which("python3") or "python3"


def _launch_soffice(
    cfg: Settings, dirs: WorkDirs, log_path: Path
) -> subprocess.Popen:
    profile_url = "file://" + str(dirs.profile_dir.resolve())
    cmd = [
        cfg.soffice_bin,
        f"-env:UserInstallation={profile_url}",
        "--headless",
        "--invisible",
        "--nodefault",
        "--norestore",
        "--nolockcheck",
        "--nologo",
        "--nofirststartwizard",
        f"--accept={dirs.uno_url}",
    ]
    log_fh = log_path.open("ab")
    # start_new_session：独立进程组/会话，回收时只杀本任务
    return subprocess.Popen(
        cmd,
        cwd=str(dirs.work_dir),
        stdin=subprocess.DEVNULL,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )


def _recalc_script_path() -> Path:
    # 测试钩子：指向假 UNO 脚本；生产环境不设置该变量
    override = os.environ.get("RECALC_SCRIPT_OVERRIDE")
    return Path(override) if override else RECALC_SCRIPT


def _run_uno_script(
    cfg: Settings, dirs: WorkDirs, log_path: Path
) -> subprocess.Popen:
    cmd = [
        _resolve_lo_python(),
        str(_recalc_script_path()),
        dirs.uno_url,
        str(dirs.input_xlsx.resolve()),
        str(dirs.recalc_xlsx.resolve()),
    ]
    log_fh = log_path.open("ab")
    env = dict(os.environ)
    env["RECALC_UNO_ATTEMPTS"] = str(cfg.uno_connect_attempts)
    env["RECALC_UNO_INTERVAL"] = str(cfg.uno_connect_interval)
    return subprocess.Popen(
        cmd,
        cwd=str(dirs.work_dir),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )


def _pkill_profile(profile_dir: Path) -> None:
    """按任务专属 profile 路径兜底回收残留 soffice（含 oosplash/soffice.bin）。"""
    needle = f"UserInstallation=file://{profile_dir.resolve()}"
    try:
        result = subprocess.run(
            ["pkill", "-TERM", "-f", needle],
            capture_output=True,
            timeout=5,
            check=False,
        )
        if result.returncode == 0:
            time.sleep(0.5)
            subprocess.run(
                ["pkill", "-KILL", "-f", needle],
                capture_output=True,
                timeout=5,
                check=False,
            )
    except (FileNotFoundError, subprocess.TimeoutExpired, PermissionError):
        # 无 pkill 或受限环境：进程组信号与标记文件已经覆盖
        pass


def run_pipeline(
    dirs: WorkDirs,
    cancel_event: threading.Event,
    cfg: Settings = default_settings,
) -> int:
    """执行单个任务的重算管道，成功返回数据行数。

    调用方负责最终清理与状态落定；本函数在取消/超时/失败时抛出
    TaskCancelled / RecalcTimeout / RecalcError / WorkbookError。
    """
    log_path = dirs.work_dir / "soffice.log"
    deadline = time.monotonic() + cfg.recalc_timeout_seconds
    timeout_fired = threading.Event()

    def _watchdog() -> None:
        remaining = deadline - time.monotonic()
        if remaining > 0 and not cancel_event.wait(remaining):
            timeout_fired.set()
            cancel_event.set()  # 统一通过 cancel_event 打断等待

    watcher = threading.Thread(
        target=_watchdog, name=f"watchdog-{dirs.root.name}", daemon=True
    )
    watcher.start()

    soffice_proc: subprocess.Popen | None = None
    script_proc: subprocess.Popen | None = None
    try:
        if cancel_event.is_set():
            raise TaskCancelled("cancelled before recalc start")

        # —— 启动重算（60s 计时的起点已在调用方标记）——
        soffice_proc = _launch_soffice(cfg, dirs, log_path)

        if cancel_event.is_set():
            raise TaskCancelled("cancelled while starting LibreOffice")

        script_proc = _run_uno_script(cfg, dirs, log_path)

        # 等待 UNO 脚本结束，期间响应取消/超时
        while True:
            try:
                rc = script_proc.wait(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                if cancel_event.is_set():
                    if timeout_fired.is_set():
                        raise RecalcTimeout(
                            f"重算超过 {cfg.recalc_timeout_seconds}s 硬时限"
                        )
                    raise TaskCancelled("任务已取消")
                if time.monotonic() >= deadline:
                    timeout_fired.set()
                    cancel_event.set()
                    raise RecalcTimeout(
                        f"重算超过 {cfg.recalc_timeout_seconds}s 硬时限"
                    )

        if cancel_event.is_set():
            if timeout_fired.is_set():
                raise RecalcTimeout(
                    f"重算超过 {cfg.recalc_timeout_seconds}s 硬时限"
                )
            raise TaskCancelled("任务已取消")
        if rc != 0:
            raise RecalcError(f"LibreOffice 重算失败，退出码 {rc}")
        if not dirs.recalc_xlsx.is_file():
            raise RecalcError("LibreOffice 未产出重算工作簿")

        # —— 重算成功后做严格校验，任何不合格值整批失败 ——
        # 60s 硬时限同样覆盖提取阶段
        if cancel_event.is_set():
            if timeout_fired.is_set():
                raise RecalcTimeout(
                    f"重算超过 {cfg.recalc_timeout_seconds}s 硬时限"
                )
            raise TaskCancelled("任务已取消")
        try:
            row_count = extract_to_csv(dirs.recalc_xlsx, dirs.result_csv)
        except WorkbookError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise WorkbookError(f"结果提取失败: {exc}") from exc

        if cancel_event.is_set():
            # 临界窗口：取消与完成同时发生，取消优先，结果不得发布
            if timeout_fired.is_set():
                raise RecalcTimeout(
                    f"重算超过 {cfg.recalc_timeout_seconds}s 硬时限"
                )
            raise TaskCancelled("取消先于结果发布被受理")
        return row_count
    finally:
        watcher.join(timeout=1.0)
        # 取消/超时/失败：终止并回收本任务全部 LibreOffice 进程
        terminate_marker = dirs.pipe_dir / ".terminate"
        for proc in (script_proc, soffice_proc):
            if proc is not None:
                terminate_process_group(proc, terminate_marker)
        # 兜底：按本任务专属 UserInstallation 路径匹配，只杀本任务的 soffice，
        # 绝不影响另一任务（路径中含本任务 UUID）。
        _pkill_profile(dirs.profile_dir)
        if soffice_proc is not None:
            try:
                soffice_proc.wait(timeout=10.0)
            except subprocess.TimeoutExpired:
                logger.warning("soffice did not exit within grace period")


class TaskManager:
    """线程安全的任务登记表与重算线程池（信号量限并发）。"""

    def __init__(self, cfg: Settings = default_settings) -> None:
        self._cfg = cfg
        self._records: dict[str, TaskRecord] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(cfg.max_parallel)

    def validate_upload(self, workbook_bytes: bytes) -> None:
        """结构预校验（临时文件，随用随删），不合格抛 WorkbookError。"""
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
            tmp.write(workbook_bytes)
            tmp_path = Path(tmp.name)
        try:
            validate_structure(tmp_path)
        finally:
            try:
                tmp_path.unlink()
            except OSError:
                pass

    def create_task(self, original_name: str, workbook_bytes: bytes) -> TaskRecord:
        task_id = uuid.uuid4().hex
        dirs = create_work_dirs(self._cfg.storage_root, task_id)
        dirs.input_xlsx.write_bytes(workbook_bytes)
        record = TaskRecord(
            task_id=task_id,
            original_name=original_name,
            work_dir=str(dirs.root),
        )
        event = threading.Event()
        with self._lock:
            self._records[task_id] = record
            self._cancel_events[task_id] = event
        thread = threading.Thread(
            target=self._worker,
            args=(task_id, dirs, event),
            name=f"recalc-{task_id}",
            daemon=True,
        )
        thread.start()
        return record

    def get(self, task_id: str) -> TaskRecord | None:
        with self._lock:
            return self._records.get(task_id)

    def request_cancel(self, task_id: str) -> tuple[bool, str]:
        """受理取消。返回 (是否新受理, 说明)。终态任务不可取消。"""
        with self._lock:
            record = self._records.get(task_id)
            if record is None:
                return False, "task not found"
            if record.status in (
                TaskStatus.SUCCEEDED,
                TaskStatus.CANCELLED,
                TaskStatus.FAILED,
                TaskStatus.TIMED_OUT,
            ):
                return False, f"task already terminal: {record.status.value}"
            event = self._cancel_events[task_id]
            already = event.is_set()
            event.set()
            if record.status == TaskStatus.QUEUED:
                record.status = TaskStatus.CANCELLING
                record.detail = "取消已受理，等待回收"
            elif record.status == TaskStatus.RUNNING:
                record.status = TaskStatus.CANCELLING
                record.detail = "取消已受理，正在终止 LibreOffice"
            return not already, record.detail

    def _set_running(self, record: TaskRecord) -> bool:
        """标记运行中并开始 60s 计时；若取消已受理则返回 False。"""
        with self._lock:
            if record.status == TaskStatus.CANCELLING or self._cancel_events[
                record.task_id
            ].is_set():
                return False
            record.status = TaskStatus.RUNNING
            record.started_at = time.monotonic()
            record.detail = "LibreOffice 正在重算"
            return True

    def _worker(
        self, task_id: str, dirs: WorkDirs, cancel_event: threading.Event
    ) -> None:
        record = self._records[task_id]
        acquired = False
        final_status: TaskStatus | None = None
        detail = ""
        try:
            # 排队等槽位；排队期间也可被取消
            while not self._slots.acquire(timeout=0.5):
                if cancel_event.is_set():
                    final_status = TaskStatus.CANCELLED
                    detail = "排队期间取消"
                    return
            acquired = True

            if cancel_event.is_set():
                final_status = TaskStatus.CANCELLED
                detail = "取得槽位前已取消"
                return

            if not self._set_running(record):
                final_status = TaskStatus.CANCELLED
                detail = "启动前已取消"
                return

            try:
                row_count = run_pipeline(dirs, cancel_event, self._cfg)
            except TaskCancelled as exc:
                final_status = TaskStatus.CANCELLED
                detail = str(exc) or "任务已取消"
                return
            except RecalcTimeout as exc:
                final_status = TaskStatus.TIMED_OUT
                detail = str(exc)
                return
            except RecalcError as exc:
                final_status = TaskStatus.FAILED
                detail = str(exc)
                return
            except WorkbookError as exc:
                final_status = TaskStatus.FAILED
                detail = str(exc)
                return
            except Exception as exc:  # noqa: BLE001
                logger.exception("unexpected pipeline error")
                final_status = TaskStatus.FAILED
                detail = f"内部错误: {type(exc).__name__}: {exc}"
                return

            with self._lock:
                if cancel_event.is_set() or record.status == TaskStatus.CANCELLING:
                    # 取消在最后一刻受理：结果永不发布
                    final_status = TaskStatus.CANCELLED
                    detail = "取消先于结果发布被受理"
                    return
                record.status = TaskStatus.SUCCEEDED
                record.row_count = row_count
                record.result_path = str(dirs.result_csv)
                record.detail = f"重算完成，共 {row_count} 行"
                final_status = TaskStatus.SUCCEEDED
        finally:
            # 先终止回收进程（run_pipeline 已处理其自身进程，
            # 排队取消等路径同样保证没有残留），再清理全部暂存，
            # 清理完成后才报结束。
            if acquired:
                self._slots.release()
            cleanup_work_dirs(dirs.work_dir)
            with self._lock:
                if final_status is not None and final_status != TaskStatus.SUCCEEDED:
                    record.status = final_status
                    record.detail = detail
                record.finished_at = time.monotonic()
                if final_status in (
                    TaskStatus.CANCELLED,
                    TaskStatus.FAILED,
                    TaskStatus.TIMED_OUT,
                ):
                    # 非成功终态绝不保留任何可下载产物
                    record.result_path = None
                    try:
                        if dirs.result_csv.exists():
                            dirs.result_csv.unlink()
                    except OSError:
                        pass
            logger.info(
                "task %s finished as %s: %s",
                task_id,
                record.status.value,
                record.detail,
            )
