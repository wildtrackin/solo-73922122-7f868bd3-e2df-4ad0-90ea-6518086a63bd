"""在独立 LibreOffice 进程内执行真正重算的 UNO 脚本。

由 app.runner 以独立子进程方式启动（解释器为 LibreOffice 自带 python），
通过专属命名管道连接到同任务的 soffice 实例：

  python recalc_uno.py <uno_pipe_url> <input_xlsx> <output_xlsx>

脚本只负责：加载工作簿（强制更新链接/文档）→ 打开自动重算 →
全量 calculateAll 两次（覆盖跨表引用链）→ 另存为 xlsx。
缓存校验、有限数值校验与 CSV 生成由 Python 侧的 extractor 完成，
保证“绝不沿用上传缓存”在服务进程内可被独立测试。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path


def _path_to_url(path: str) -> str:
    # as_uri 对中文/空格做标准百分号编码，LibreOffice 可直接加载
    return Path(path).resolve().as_uri()


def _prop(ctx, name, value):
    pv = ctx.ServiceManager.createInstanceWithContext(
        "com.sun.star.beans.PropertyValue", ctx
    )
    pv.Name = name
    pv.Value = value
    return pv


def main(conn_url: str, input_path: str, output_path: str) -> int:
    import uno
    from com.sun.star.beans import PropertyValue  # noqa: F401  (确保 UNO 引导)
    from com.sun.star.frame import UpdateDocMode
    from com.sun.star.sheet import XSpreadsheetDocument
    from com.sun.star.uno import RuntimeException

    local_ctx = uno.getComponentContext()
    resolver = local_ctx.ServiceManager.createInstanceWithContext(
        "com.sun.star.bridge.UnoUrlResolver", local_ctx
    )

    ctx = None
    last_err: Exception | None = None
    # 连接预算由 runner 通过环境变量下发，整体仍受 60s 硬时限约束
    attempts = int(os.environ.get("RECALC_UNO_ATTEMPTS", "60"))
    interval = float(os.environ.get("RECALC_UNO_INTERVAL", "0.5"))
    for _ in range(max(attempts, 1)):
        try:
            ctx = resolver.resolve(conn_url)
            break
        except RuntimeException as exc:  # soffice 尚未就绪
            last_err = exc
            time.sleep(interval)
    if ctx is None:
        print(f"UNO connection failed: {last_err}", file=sys.stderr)
        return 1

    smgr = ctx.ServiceManager
    desktop = smgr.createInstanceWithContext("com.sun.star.frame.Desktop", ctx)

    load_props = (
        _prop(ctx, "Hidden", True),
        _prop(ctx, "ReadOnly", False),
        _prop(ctx, "UpdateDocMode", UpdateDocMode.FULL_UPDATE),
        _prop(ctx, "MacroExecutionMode", 0),  # 不执行任何宏
    )
    doc = desktop.loadComponentFromURL(
        _path_to_url(input_path), "_blank", 0, load_props
    )
    if doc is None:
        print("loadComponentFromURL returned None", file=sys.stderr)
        desktop.terminate()
        return 1

    try:
        sheet_doc = doc.queryInterface(XSpreadsheetDocument)
        # 显式开启自动计算并全量重算两次，确保跨“校准”表的引用链收敛
        sheet_doc.enableAutomaticCalculation(True)
        sheet_doc.calculateAll()
        sheet_doc.calculateAll()

        save_props = (
            _prop(ctx, "Overwrite", True),
            _prop(ctx, "FilterName", "Calc MS Excel 2007 XML"),
        )
        doc.storeToURL(_path_to_url(output_path), save_props)
    finally:
        try:
            doc.close(False)
        except Exception:
            pass
        try:
            desktop.terminate()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("usage: recalc_uno.py <conn_url> <input> <output>", file=sys.stderr)
        sys.exit(2)
    try:
        sys.exit(main(sys.argv[1], sys.argv[2], sys.argv[3]))
    except Exception as exc:  # noqa: BLE001 - 任何异常都转成非零退出
        print(f"recalc error: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
