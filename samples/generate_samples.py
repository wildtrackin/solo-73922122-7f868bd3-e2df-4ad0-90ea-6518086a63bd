"""生成演示用工作簿：过期缓存、跨表公式、可控重算延迟。

三个样例都写入 samples/（容器内 /samples）：

1. stale_cache.xlsx
   “测量”表 corrected 列是引用“校准”表的公式，文件里携带的缓存值
   被故意写成错误的 999.0（过期缓存）。LibreOffice 真正重算后
   才能得到正确的校正值；若服务沿用上传缓存，下载结果会出现 999.0。

2. cross_sheet.xlsx
   普通跨表公式样例（校准表含逐行增益/偏置），用于验证
   同簿“校准”表引用链。

3. controllable_delay.xlsx
   corrected 公式内嵌由“校准”表!B1 控制的大规模数组运算，
   调节 B1 即可控制 LibreOffice 的重算耗时，方便演示
   并行、取消与 60 秒超时（例如把 B1 调到 40000000 以上）。
"""

from __future__ import annotations

import argparse
import io
import re
import zipfile
from pathlib import Path

from openpyxl import Workbook

MEASURE_HEADERS = ["sample_id", "reading", "corrected"]
CAL_HEADERS = ["factor", "offset"]

# 缓存注入时用公式文本定位单元格（每个公式在簿内唯一）
STALE_CACHE_VALUE = 999.0


def _build_workbook() -> Workbook:
    wb = Workbook()
    ws = wb.active
    ws.title = "测量"
    ws.append(MEASURE_HEADERS)
    cal = wb.create_sheet("校准")
    cal.append(CAL_HEADERS)
    return wb, ws, cal


def build_stale_cache() -> bytes:
    wb, ws, cal = _build_workbook()
    # 校准表：全局增益 1.1、偏置 -0.5
    cal["D1"] = 1.1
    cal["D2"] = -0.5
    readings = [10.0, 20.0, 30.0, 40.0]
    formulas: list[str] = []
    for idx, reading in enumerate(readings, start=2):
        ws.cell(row=idx, column=1, value=f"S{idx - 1:03d}")
        ws.cell(row=idx, column=2, value=reading)
        formula = f"B{idx}*校准!$D$1+校准!$D$2"
        ws.cell(row=idx, column=3, value=f"={formula}")
        formulas.append(formula)
    raw = save_via_buffer(wb)
    # 注入过期缓存：每个 corrected 单元格的缓存值都写成 999.0
    return inject_stale_cached_values(raw, formulas, STALE_CACHE_VALUE)


def build_cross_sheet() -> bytes:
    wb, ws, cal = _build_workbook()
    rows = [
        ("S001", 12.5, 1.02, 0.3),
        ("S002", 18.0, 0.98, 1.1),
        ("S003", 25.25, 1.05, -0.7),
        ("S004", 7.75, 1.10, 0.0),
        ("S005", 33.0, 0.95, 2.2),
    ]
    for idx, (sample_id, reading, factor, offset) in enumerate(rows, start=2):
        ws.cell(row=idx, column=1, value=sample_id)
        ws.cell(row=idx, column=2, value=reading)
        ws.cell(
            row=idx,
            column=3,
            value=f"=B{idx}*校准!A{idx}+校准!B{idx}",
        )
        cal.cell(row=idx, column=1, value=factor)
        cal.cell(row=idx, column=2, value=offset)
    return save_via_buffer(wb)


def build_controllable_delay(delay_k: int) -> bytes:
    wb, ws, cal = _build_workbook()
    # 校准!B1 = 数组运算规模 K；K 越大重算越慢（容器内典型值见 README）
    cal["A1"] = "delay_k"
    cal["B1"] = delay_k
    readings = [11.0, 22.0, 33.0]
    for idx, reading in enumerate(readings, start=2):
        ws.cell(row=idx, column=1, value=f"D{idx - 1:03d}")
        ws.cell(row=idx, column=2, value=reading)
        # SUMPRODUCT 对 1..K 数组做强制求值，再用 MOD 折叠；
        # 引用“校准”表!$B$1，改一个单元格即可控制每一行的重算耗时。
        formula = (
            f"=MOD(SUMPRODUCT(ROW(INDIRECT(\"1:\"&校准!$B$1))*1),1000)"
            f"+B{idx}"
        )
        ws.cell(row=idx, column=3, value=formula)
    return save_via_buffer(wb)


def save_via_buffer(wb: Workbook) -> bytes:
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def inject_stale_cached_values(
    xlsx_bytes: bytes, formula_texts: list[str], cached: float
) -> bytes:
    """向公式单元格注入过期数值缓存（模拟未重算的旧工作簿）。

    openpyxl 写公式时会带一个空的 ``<v />``；这里把它替换为
    ``<v>cached</v>``，并在缺少时补上，保证单元格带数值类型缓存。
    关系解析不依赖 XML 属性顺序。
    """
    with zipfile.ZipFile(io.BytesIO(xlsx_bytes)) as zin:
        workbook_xml = zin.read("xl/workbook.xml").decode("utf-8")
        rels_xml = zin.read("xl/_rels/workbook.xml.rels").decode("utf-8")

        sheet_match = re.search(
            r'<sheet\b[^>]*name="测量"[^>]*r:id="(rId\d+)"', workbook_xml
        )
        if sheet_match is None:
            raise RuntimeError("工作簿中找不到“测量”表关系")
        sheet_rid = sheet_match.group(1)

        rel_match = re.search(
            r"<Relationship\b[^>]*?\bId="
            + rf'"{re.escape(sheet_rid)}"'
            r"[^>]*?\bTarget=\"([^\"]+)\"",
            rels_xml,
        )
        if rel_match is None:
            # 兼容 Target 出现在 Id 之前的属性顺序
            rel_match = re.search(
                r"<Relationship\b[^>]*?\bTarget=\"([^\"]+)\""
                r"[^>]*?\bId="
                + rf'"{re.escape(sheet_rid)}"',
                rels_xml,
            )
        if rel_match is None:
            raise RuntimeError(f"找不到关系 {sheet_rid} 的 Target")
        sheet_target = rel_match.group(1).lstrip("/")
        sheet_path = (
            sheet_target
            if sheet_target.startswith("xl/")
            else "xl/" + sheet_target
        )

        sheet_xml = zin.read(sheet_path).decode("utf-8")
        for formula in formula_texts:
            pattern = re.compile(
                r"(<c\b[^>]*>)\s*<f>"
                + re.escape(formula)
                + r"</f>\s*(?:<v\s*/>|<v>[^<]*</v>)?\s*(</c>)"
            )
            sheet_xml, count = pattern.subn(
                rf"\g<1><f>{formula}</f><v>{cached}</v>\g<2>",
                sheet_xml,
                count=1,
            )
            if count != 1:
                raise RuntimeError(f"未能为公式 {formula} 注入缓存")

        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                payload = (
                    sheet_xml.encode("utf-8")
                    if item.filename == sheet_path
                    else zin.read(item.filename)
                )
                zout.writestr(item, payload)
    return out.getvalue()


def main() -> None:
    parser = argparse.ArgumentParser(description="生成重算服务演示工作簿")
    parser.add_argument(
        "--out-dir", default="samples", help="输出目录（默认 samples/）"
    )
    parser.add_argument(
        "--delay-k",
        type=int,
        default=3_000_000,
        help="controllable_delay.xlsx 的重算延迟参数 K（校准!B1）",
    )
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    artifacts = {
        "stale_cache.xlsx": build_stale_cache(),
        "cross_sheet.xlsx": build_cross_sheet(),
        "controllable_delay.xlsx": build_controllable_delay(args.delay_k),
    }
    for name, payload in artifacts.items():
        target = out_dir / name
        target.write_bytes(payload)
        print(f"wrote {target} ({len(payload)} bytes)")


if __name__ == "__main__":
    main()
