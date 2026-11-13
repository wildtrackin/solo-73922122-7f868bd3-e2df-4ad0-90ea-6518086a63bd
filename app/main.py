"""FastAPI 入口：上传、状态、取消、下载与停机收尾。"""

from __future__ import annotations

import asyncio
import io
import logging
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse

from .config import settings
from .extractor import WorkbookError
from .models import TaskStatus
from .runner import ShutdownInProgress, TaskManager

logger = logging.getLogger("recalc.api")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    # 进程收到 SIGTERM/SIGINT（如 docker stop）时执行停机收尾：
    # 拒绝新任务、取消在途任务，等待任务线程/看门狗/重算进程全部
    # 回收并清理未完成任务暂存后才真正退出。
    yield
    logger.info("API 关闭：开始任务线程与重算进程收尾")
    summary = await asyncio.to_thread(manager.shutdown)
    logger.info("停机收尾完成: %s", summary)


app = FastAPI(
    title="测量工作簿重算服务",
    description=(
        "接收最多 8 MiB 的 xlsx，由后台 LibreOffice **真正重新计算** "
        "“测量”表（corrected 公式可引用同簿“校准”表），"
        "成功后下载只含值的 CSV。最多两个任务并行，"
        "支持状态查询、取消与 60 秒重算硬时限。"
        "维护停机走 `POST /admin/shutdown`：开始关闭即拒绝新任务，"
        "在途任务回收、暂存清理完成后才结束关闭。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)

manager = TaskManager(settings)

XLSX_MAGIC = b"PK\x03\x04"  # xlsx 即 zip 容器
TERMINAL_DOWNLOADABLE = {TaskStatus.SUCCEEDED}


def _read_limited(file: UploadFile, limit: int) -> bytes:
    """流式读取上传内容，超过 8 MiB 立即拒绝，避免大文件占内存。"""
    buffer = io.BytesIO()
    total = 0
    while True:
        chunk = file.file.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise HTTPException(
                status_code=413,
                detail=f"工作簿超过 {limit} 字节（8 MiB）上限",
            )
        buffer.write(chunk)
    return buffer.getvalue()


def _validate_xlsx_container(data: bytes, filename: str) -> None:
    if not filename.lower().endswith(".xlsx"):
        raise HTTPException(status_code=415, detail="仅接受 .xlsx 工作簿")
    if not data.startswith(XLSX_MAGIC):
        raise HTTPException(status_code=415, detail="文件不是有效的 xlsx (zip) 容器")
    if not zipfile.is_zipfile(io.BytesIO(data)):
        raise HTTPException(status_code=415, detail="xlsx 容器已损坏")


@app.post("/tasks", status_code=202, summary="上传工作簿并创建重算任务")
async def create_task(file: UploadFile) -> dict:
    # 开始关闭即拒绝新任务（读取上传体之前就拒绝）
    if manager.shutting_down:
        raise HTTPException(
            status_code=503, detail="服务正在关闭，不再接受新任务"
        )
    data = _read_limited(file, settings.max_upload_bytes)
    if not data:
        raise HTTPException(status_code=400, detail="上传内容为空")
    _validate_xlsx_container(data, file.filename or "")
    try:
        # 先做结构预校验，不合格返回 422 且不在磁盘创建任何任务目录
        manager.validate_upload(data)
        record = manager.create_task(file.filename or "upload.xlsx", data)
    except WorkbookError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ShutdownInProgress as exc:
        # 校验期间关闭开始：同样拒绝，不留下任何任务目录
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return record.to_public() | {"links": _task_links(record.task_id)}


@app.get("/tasks/{task_id}", summary="查询任务状态")
async def get_task(task_id: str) -> dict:
    record = manager.get(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    payload = record.to_public()
    if record.status in TERMINAL_DOWNLOADABLE:
        payload["links"] = _task_links(task_id)
    return payload


@app.post("/tasks/{task_id}/cancel", summary="取消任务")
async def cancel_task(task_id: str) -> dict:
    record = manager.get(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    accepted, detail = manager.request_cancel(task_id)
    if not accepted and detail.startswith("task already terminal"):
        raise HTTPException(
            status_code=409,
            detail=f"任务已处于终态 ({record.status.value})，无法取消",
        )
    refreshed = manager.get(task_id)
    return {
        "task_id": task_id,
        "cancellation_accepted": True,
        "detail": refreshed.detail if refreshed else detail,
        "note": "取消一经受理，即使重算随后完成也不会发布结果",
    }


@app.get("/tasks/{task_id}/download", summary="下载只含值的结果 CSV")
async def download_result(task_id: str):
    record = manager.get(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if record.status != TaskStatus.SUCCEEDED:
        raise HTTPException(
            status_code=409,
            detail=(
                f"任务当前状态为 {record.status.value}，"
                "仅 succeeded 状态可下载"
            ),
        )
    result_path = Path(record.result_path or "")
    if not result_path.is_file():
        # 结果文件缺失视为服务端异常，绝不回退到上传缓存
        raise HTTPException(status_code=410, detail="结果文件已不可用")
    return FileResponse(
        result_path,
        media_type="text/csv; charset=utf-8",
        filename=f"{task_id}_recalculated.csv",
    )


@app.get("/health", summary="健康检查")
async def health() -> dict:
    return {
        "status": "ok",
        "max_parallel": settings.max_parallel,
        "shutting_down": manager.shutting_down,
        "shutdown_completed": manager.shutdown_completed,
    }


@app.post(
    "/admin/shutdown",
    summary="停机收尾：拒绝新任务，回收在途任务并清理暂存",
)
async def admin_shutdown() -> dict:
    """维护窗口入口（幂等，可重复调用）。

    - 开始关闭即拒绝新任务（`POST /tasks` 返回 503）；
    - 排队任务不再启动；运行任务停止重算、不进入提取；
    - 关闭与进程创建/重算完成交错时，迟到进程同样回收，
      未发布 CSV 不发布；
    - 全部任务线程、看门狗与重算进程退出并回收后，清理未完成
      任务暂存，收尾才结束（本请求阻塞至收尾完成并返回汇总）；
    - 重复调用等待同一次收尾；关闭前已成功的 CSV 保留、可下载。
    """
    logger.info("收到停机请求，开始收尾")
    return await asyncio.to_thread(manager.shutdown)


def _task_links(task_id: str) -> dict:
    return {
        "status": f"/tasks/{task_id}",
        "cancel": f"/tasks/{task_id}/cancel",
        "download": f"/tasks/{task_id}/download",
    }
