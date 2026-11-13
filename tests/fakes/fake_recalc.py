#!/usr/bin/env python3
"""测试用假 UNO 重算脚本（替代 app/recalc_uno.py）。

参数与真实脚本一致：<conn_url> <input> <output>。

环境变量：
  FAKE_RECALC_DELAY  重算耗时（秒）
  FAKE_RECALC_FAIL   1 = 非零退出，模拟公式报错/重算失败
  FAKE_RECALC_NO_RECALC  1 = 剥离公式单元格的缓存值，模拟
                     “LibreOffice 没有真正重算”——用于证明服务端
                     绝不沿用上传工作簿携带的过期缓存（无新值即整批失败）
"""

import io
import os
import re
import shutil
import sys
import time
import zipfile


def _strip_formula_caches(input_path: str, output_path: str) -> None:
    """把所有公式单元格的 <v> 缓存清空，保留公式本身。"""
    with zipfile.ZipFile(input_path) as zin, zipfile.ZipFile(
        output_path, "w", zipfile.ZIP_DEFLATED
    ) as zout:
        for item in zin.infolist():
            payload = zin.read(item.filename)
            if item.filename.startswith("xl/worksheets/") and item.filename.endswith(
                ".xml"
            ):
                xml = payload.decode("utf-8")
                # 仅清理紧邻 </f> 之后的 <v>...</v> / <v/>
                xml = re.sub(
                    r"(</f>)\s*<v\s*(?:>[^<]*</v>|/>)",
                    r"\g<1><v />",
                    xml,
                )
                payload = xml.encode("utf-8")
            zout.writestr(item, payload)


def main() -> int:
    _conn_url, input_path, output_path = sys.argv[1], sys.argv[2], sys.argv[3]
    delay = float(os.environ.get("FAKE_RECALC_DELAY", "0"))
    end = time.monotonic() + delay
    while time.monotonic() < end:
        time.sleep(0.05)

    if os.environ.get("FAKE_RECALC_FAIL") == "1":
        print("fake recalc formula error", file=sys.stderr)
        return 1

    if os.environ.get("FAKE_RECALC_NO_RECALC") == "1":
        _strip_formula_caches(input_path, output_path)
    else:
        shutil.copyfile(input_path, output_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
