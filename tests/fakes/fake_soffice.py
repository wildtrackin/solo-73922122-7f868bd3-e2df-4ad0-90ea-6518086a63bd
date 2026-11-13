#!/usr/bin/env python3
"""测试用假 LibreOffice：模拟重算进程的进程组语义。

行为由环境变量控制（真实 soffice 不读取这些变量）：
  FAKE_SOFFICE_DELAY        驻留秒数（模拟重算耗时），默认 30s
  FAKE_SOFFICE_FAIL        设为 1 时睡完延迟后以非零码退出
  FAKE_SOFFICE_IGNORE_TERM 设为 1 时忽略 SIGTERM（验证 SIGKILL 回收）

真实环境里 runner 通过进程组 SIGTERM/SIGKILL 回收；受限沙箱可能
禁止信号，因此本脚本额外监听任务管道目录下的 .terminate 标记文件
（runner 在信号不可用时写入），收到后自行退出，保证测试可在沙箱内
验证“终止—回收—清理—报结束”的完整顺序。
"""

import os
import signal
import sys
import time
from pathlib import Path


def _terminate_marker() -> Path | None:
    # 参数中 --accept=pipe,name=xxx;urp;... 含管道名，
    # 工作目录即任务 work 目录（runner 以 cwd=work_dir 启动）
    cwd = Path.cwd()
    marker = cwd / "pipe" / ".terminate"
    return marker if marker.parent.is_dir() else None


def main() -> int:
    if os.environ.get("FAKE_SOFFICE_IGNORE_TERM") != "1":
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))

    delay = float(os.environ.get("FAKE_SOFFICE_DELAY", "30"))
    fail = os.environ.get("FAKE_SOFFICE_FAIL") == "1"
    marker = _terminate_marker()

    end = time.monotonic() + delay
    while time.monotonic() < end:
        time.sleep(0.05)
        if marker is not None and marker.exists():
            return 143

    if fail:
        print("fake soffice failure", file=sys.stderr)
        return 1

    # 成功模式：有界驻留，等待 runner 终止进程组/写终止标记。
    # 有界而非无限，保证在信号被禁止且标记缺失的极端环境下也能自行退出。
    end = time.monotonic() + float(
        os.environ.get("FAKE_SOFFICE_HOLD", "20")
    )
    while time.monotonic() < end:
        time.sleep(0.1)
        if marker is not None and marker.exists():
            return 143
    return 0


if __name__ == "__main__":
    sys.exit(main())
