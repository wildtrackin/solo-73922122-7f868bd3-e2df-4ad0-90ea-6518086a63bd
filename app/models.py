"""任务状态模型。"""

from __future__ import annotations

import enum
import time
from dataclasses import dataclass, field
from typing import Optional


class TaskStatus(str, enum.Enum):
    # 已受理，等待重算槽位
    QUEUED = "queued"
    # 已取得槽位，LibreOffice 正在重算（60s 计时从此状态开始）
    RUNNING = "running"
    # 取消请求已受理（终态前的内部标记，外部可见为 cancelling）
    CANCELLING = "cancelling"
    # 成功，CSV 可下载
    SUCCEEDED = "succeeded"
    # 业务/系统失败，整批拒绝
    FAILED = "failed"
    # 60s 硬时限到期，已终止回收
    TIMED_OUT = "timed_out"
    # 取消完成，结果永不发布
    CANCELLED = "cancelled"


# 一旦进入以下状态，任务不得再发布结果
TERMINAL_STATUSES = frozenset(
    {
        TaskStatus.SUCCEEDED,
        TaskStatus.FAILED,
        TaskStatus.TIMED_OUT,
        TaskStatus.CANCELLED,
    }
)


@dataclass
class TaskRecord:
    task_id: str
    original_name: str
    status: TaskStatus = TaskStatus.QUEUED
    detail: str = ""
    created_at: float = field(default_factory=time.monotonic)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    # 重算完成的行数（成功时填充）
    row_count: int = 0
    # 工作目录（workbook / profile / scratch / result.csv 均在其中）
    work_dir: Optional[str] = None
    result_path: Optional[str] = None

    def to_public(self) -> dict:
        data = {
            "task_id": self.task_id,
            "status": self.status.value,
            "detail": self.detail,
            "original_name": self.original_name,
            "row_count": self.row_count,
        }
        if self.started_at is not None and self.finished_at is not None:
            data["elapsed_seconds"] = round(
                self.finished_at - self.started_at, 3
            )
        return data
