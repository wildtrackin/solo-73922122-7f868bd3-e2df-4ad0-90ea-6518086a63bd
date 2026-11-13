"""FastAPI 入口：上传、状态、取消、下载。"""

from __future__ import annotations

import io
import logging
import zipfile
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse

from .config import settings
from .extractor import WorkbookError
from .models import TaskStatus
from .runner import TaskManager

logger = logging.getLogger("recalc.api")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

app = FastAPI(
    title="测量工作簿重算服务",
    description=(
        "接收最多 8 MiB 的 xlsx，由后台 LibreOffice **真正重新计算** "
        "“测量”表（corrected 公式可引用同簿“校准”表），"
        "成功后下载只含值的 CSV。最多两个任务并行，"
        "支持状态查询、取消与 60 秒重算硬时限。"
    ),
    version="1.0.0",
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
    return {"status": "ok", "max_parallel": settings.max_parallel}


def _task_links(task_id: str) -> dict:
    return {
        "status": f"/tasks/{task_id}",
        "cancel": f"/tasks/{task_id}/cancel",
        "download": f"/tasks/{task_id}/download",
    }
