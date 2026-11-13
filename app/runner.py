"""重算任务编排：并发槽位、取消、60s 硬时限、终止回收与停机收尾。

关键保证：
1. 最多 ``RECALC_MAX_PARALLEL`` 个任务真正进入重算，其余排队；
2. 60s 从“启动重算”（取得槽位、LibreOffice 启动）起算，排队时间不计；
3. 取消、超时、失败都会终止本任务进程组并清理全部暂存，再把任务置为终态；
4. 取消一经受理，即便重算随后完成也不会发布结果；
5. 任务间目录、LibreOffice profile、UNO 管道完全隔离；
6. 停机收尾（shutdown）：开始关闭即拒绝新任务；排队任务不再启动；
   运行任务停止重算、不进入提取；关闭与进程创建或重算完成交错时，
   迟到进程同样被回收、未发布 CSV 绝不发布；全部任务线程、看门狗与
   所属重算进程退出并回收后，才清理未完成任务暂存、结束关闭；
   重复关闭等待同一次收尾；关闭前已成功的 CSV 保留。
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
from .models import TERMINAL_STATUSES, TaskRecord, TaskStatus
from .processes import (
    WorkDirs,
    cleanup_work_dirs,
    create_work_dirs,
    terminate_process_group,
)

logger = logging.getLogger("recalc.runner")

RECALC_SCRIPT = Path(__file__).with_name("recalc_uno.py")

# 交接点放行门轮询间隔与默认最长等待（测试/演示钩子）
GATE_POLL_INTERVAL = 0.05
DEFAULT_GATE_TIMEOUT = 30.0


class RecalcError(Exception):
    """重算阶段失败（soffice/UNO 非零退出等）。"""


class TaskCancelled(Exception):
    """取消在管道内部被检测到。"""


class RecalcTimeout(Exception):
    """60s 硬时限到期。"""


class ShutdownInProgress(Exception):
    """停机收尾已开始，拒绝新任务。"""


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


def _gate_timeout() -> float:
    raw = os.environ.get("RECALC_GATE_TIMEOUT", "")
    try:
        return float(raw) if raw.strip() else DEFAULT_GATE_TIMEOUT
    except ValueError:
        return DEFAULT_GATE_TIMEOUT


def _await_handoff_gate(gate: Path, label: str) -> None:
    """测试/演示钩子：在交接点等待“放行文件”出现（有界），随后继续正常流程。

    等待期间在 ``<gate>.waiting`` 放置标记文件，供外部精确同步“线程已
    抵达交接点”。该钩子只把交接窗口拉开，不改变取消/关闭语义——放行后
    既有的 cancel_event 检查点照常生效：关闭与进程创建交错时，迟到进程
    由 finally 回收；关闭与重算完成交错时，不进入提取、结果不发布。
    生产环境不设置对应环境变量，本函数不会被调用。
    """
    waiting_marker = gate.with_name(gate.name + ".waiting")
    timeout = _gate_timeout()
    try:
        waiting_marker.touch()
    except OSError:
        pass
    logger.info("%s：等待放行文件 %s（最长 %.1fs）", label, gate, timeout)
    released = False
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            if gate.exists():
                released = True
                break
            time.sleep(GATE_POLL_INTERVAL)
    finally:
        try:
            waiting_marker.unlink()
        except OSError:
            pass
    if released:
        logger.info("%s：已放行，继续流程", label)
    else:
        logger.warning("%s：等待放行超时（%.1fs），继续流程", label, timeout)


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
        # 演示/测试钩子：把“进程创建交接点”拉开，可控验证关闭与进程创建
        # 交错时迟到进程仍被回收（生产不设置 RECALC_LAUNCH_GATE）。
        launch_gate = os.environ.get("RECALC_LAUNCH_GATE")
        if launch_gate:
            _await_handoff_gate(Path(launch_gate), "进程创建交接点")
        soffice_proc = _launch_soffice(cfg, dirs, log_path)

        if cancel_event.is_set():
            logger.warning(
                "取消/关闭与进程创建交错：回收迟到的 soffice pid=%s",
                soffice_proc.pid,
            )
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
        # 演示/测试钩子：把“重算完成→提取”交接点拉开，可控验证关闭与
        # 重算完成交错时不进入提取、未发布 CSV 绝不发布
        # （生产不设置 RECALC_PRE_EXTRACT_GATE）。
        extract_gate = os.environ.get("RECALC_PRE_EXTRACT_GATE")
        if extract_gate:
            _await_handoff_gate(Path(extract_gate), "提取交接点")
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
        reaped = [p.pid for p in (script_proc, soffice_proc) if p is not None]
        if reaped:
            logger.info(
                "任务 %s：重算进程已全部终止回收 pids=%s",
                dirs.root.name,
                reaped,
            )


class TaskManager:
    """线程安全的任务登记表与重算线程池（信号量限并发）。"""

    def __init__(self, cfg: Settings = default_settings) -> None:
        self._cfg = cfg
        self._records: dict[str, TaskRecord] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(cfg.max_parallel)
        # 停机收尾状态机：
        # - _shutdown_event 一旦置位即拒绝新任务、取消全部在途任务；
        # - _shutdown_lock 串行化收尾执行，重复/并发关闭等待同一次收尾；
        # - _shutdown_done 在全部任务线程、看门狗与重算进程退出回收、
        #   未完成任务暂存清理完成后置位。
        self._shutdown_event = threading.Event()
        self._shutdown_done = threading.Event()
        self._shutdown_lock = threading.Lock()
        self._shutdown_result: dict | None = None

    @property
    def shutting_down(self) -> bool:
        """关闭是否已开始（此时起拒绝新任务）。"""
        return self._shutdown_event.is_set()

    @property
    def shutdown_completed(self) -> bool:
        """收尾是否已完成（线程/进程全部回收、暂存清理完毕）。"""
        return self._shutdown_done.is_set()

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
        thread = threading.Thread(
            target=self._worker,
            args=(task_id, dirs, event),
            name=f"recalc-{task_id}",
            daemon=True,
        )
        with self._lock:
            rejecting = self._shutdown_event.is_set()
            if not rejecting:
                self._records[task_id] = record
                self._cancel_events[task_id] = event
                # 线程先登记再启动：关闭快照要么看到完整登记的线程并 join，
                # 要么先置位令本任务被拒绝——不存在漏登记、漏 join 的线程
                self._threads[task_id] = thread
                thread.start()
        if rejecting:
            # 关闭已开始：拒绝新任务并清掉刚创建的暂存，绝不遗留
            shutil.rmtree(dirs.root, ignore_errors=True)
            raise ShutdownInProgress("服务正在关闭，不再接受新任务")
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

    def _cancel_detail(self, shutdown_text: str, normal_text: str) -> str:
        """取消详情：若因停机收尾触发，使用关闭语义文案。"""
        return shutdown_text if self._shutdown_event.is_set() else normal_text

    def shutdown(self) -> dict:
        """停机收尾：拒绝新任务，取消在途任务并等待全部回收后清理暂存。

        顺序保证：
          1. 置位关闭标记——开始关闭即拒绝新任务；
          2. 置位全部任务的取消事件——排队任务不再启动，运行任务停止
             重算、不进入提取，未发布 CSV 不发布；
          3. join 全部任务线程——线程退出即意味着其看门狗已 join、
             所属 LibreOffice/UNO 进程组已终止回收、暂存已清理；
          4. 兜底核验未完成任务暂存清理完毕（关闭前成功的 CSV 保留）；
          5. 标记收尾完成。
        幂等：重复/并发调用在 ``_shutdown_lock`` 上等待同一次收尾，
        并拿到同一份汇总（``already_completed=True``）。
        """
        with self._shutdown_lock:
            if self._shutdown_result is not None:
                return {**self._shutdown_result, "already_completed": True}

            began = time.monotonic()
            with self._lock:
                self._shutdown_event.set()
                events = list(self._cancel_events.values())
                threads = list(self._threads.values())
                incomplete_ids: list[str] = []
                retained_ids: list[str] = []
                for record in self._records.values():
                    if record.status == TaskStatus.SUCCEEDED:
                        # 关闭前已成功：结果保留，可继续下载
                        retained_ids.append(record.task_id)
                    elif record.status in (
                        TaskStatus.QUEUED,
                        TaskStatus.RUNNING,
                        TaskStatus.CANCELLING,
                    ):
                        incomplete_ids.append(record.task_id)
                        record.status = TaskStatus.CANCELLING
                        record.detail = "服务关闭：等待任务收尾"
                for event in events:
                    event.set()
            logger.info(
                "停机收尾开始：取消 %d 个在途任务，等待 %d 个任务线程退出",
                len(incomplete_ids),
                len(threads),
            )

            # 等待全部任务线程退出。每个线程的 finally 保证：看门狗已
            # join、本任务进程组已终止回收、暂存已清理——join 返回即
            # “线程、看门狗、重算进程全部退出并回收”。
            for thread in threads:
                thread.join()

            # 兜底落定与核验（正常路径下线程已自行完成，此处幂等）：
            # 未完成任务一律无暂存、无可下载结果；成功任务结果保留。
            with self._lock:
                for record in self._records.values():
                    if record.status not in TERMINAL_STATUSES:
                        record.status = TaskStatus.CANCELLED
                        record.detail = "服务关闭：已取消"
                        record.finished_at = time.monotonic()
                records = list(self._records.values())
            staging_cleaned: list[str] = []
            for record in records:
                if record.status == TaskStatus.SUCCEEDED or not record.work_dir:
                    continue
                root = Path(record.work_dir)
                cleanup_work_dirs(root / "work")
                try:
                    csv_path = root / "result" / "result.csv"
                    if csv_path.exists():
                        csv_path.unlink()
                except OSError:
                    pass
                staging_cleaned.append(record.task_id)

            summary = {
                "status": "completed",
                "duration_seconds": round(time.monotonic() - began, 3),
                "threads_joined": len(threads),
                "incomplete_task_ids": incomplete_ids,
                "retained_succeeded_ids": retained_ids,
                "staging_cleaned_ids": staging_cleaned,
            }
            self._shutdown_result = summary
            self._shutdown_done.set()
            logger.info("停机收尾完成: %s", summary)
            return {**summary, "already_completed": False}

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
            # 排队等槽位；排队期间也可被取消/关闭
            while not self._slots.acquire(timeout=0.5):
                if cancel_event.is_set():
                    final_status = TaskStatus.CANCELLED
                    detail = self._cancel_detail(
                        "服务关闭：排队任务不再启动", "排队期间取消"
                    )
                    return
            acquired = True

            if cancel_event.is_set():
                final_status = TaskStatus.CANCELLED
                detail = self._cancel_detail(
                    "服务关闭：排队任务不再启动", "取得槽位前已取消"
                )
                return

            if not self._set_running(record):
                final_status = TaskStatus.CANCELLED
                detail = self._cancel_detail(
                    "服务关闭：任务未启动", "启动前已取消"
                )
                return

            try:
                row_count = run_pipeline(dirs, cancel_event, self._cfg)
            except TaskCancelled as exc:
                final_status = TaskStatus.CANCELLED
                detail = self._cancel_detail(
                    "服务关闭：重算已停止，未进入提取",
                    str(exc) or "任务已取消",
                )
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
                    # 取消/关闭在最后一刻受理：结果永不发布
                    final_status = TaskStatus.CANCELLED
                    detail = self._cancel_detail(
                        "服务关闭：结果不予发布", "取消先于结果发布被受理"
                    )
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
                # 从线程登记表注销，供停机收尾 join 统计
                self._threads.pop(task_id, None)
            logger.info(
                "task %s finished as %s: %s",
                task_id,
                record.status.value,
                record.detail,
            )
