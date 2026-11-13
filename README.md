# 测量工作簿重算服务（FastAPI + LibreOffice headless）

实验室数据工程师收到的测量工作簿常携带**过期的公式缓存**。本服务在入仓前用
LibreOffice 在后台**真正重新计算**工作簿，校验“测量”表的 `sample_id`、
`reading`、`corrected` 三列，并产出**只含值**的 CSV；绝不沿用上传文件自带的
缓存，校正值非有限数值或任何公式报错都会让**整批失败**。

## 能力与保证

| 需求 | 实现方式 |
| --- | --- |
| 上传上限 8 MiB | 流式读取，超限立即 `413`；同时校验 xlsx(zip) 魔数与容器完整性 |
| 读取“测量”表三列 | `app/extractor.py` 强制列名校验，`corrected` 公式可跨表引用“校准”表 |
| 后台真正重算 | 每个任务启动独立 LibreOffice，经 UNO 命名管道连接，`FULL_UPDATE` 加载 + `calculateAll()` 两次 + 另存 xlsx（`app/recalc_uno.py`） |
| 不信任上传缓存 | 只读取 LibreOffice 重算后另存的新文件；公式单元格无新值、错误类型或非有限值一律整批失败 |
| 最多 2 任务并行 | `BoundedSemaphore(2)`，第 3 个任务保持 `queued`，排队不计入 60s |
| 任务互不串用 | 每任务独立工作目录、独立 `-env:UserInstallation` profile、独立 UNO 管道名（含任务 UUID） |
| 状态 / 取消 / 下载 | `GET /tasks/{id}`、`POST /tasks/{id}/cancel`、`GET /tasks/{id}/download` |
| 取消不发布结果 | 取消事件在管道多个检查点生效；即使重算随后完成，结果也不会发布，终态为 `cancelled` |
| 60 秒硬时限 | 从取得槽位、启动 LibreOffice 起算（`started_at`），看门狗 + 等待循环双保险 |
| 终止并回收 | 进程组 `SIGTERM`→等待→`SIGKILL`，并按任务专属 profile 路径 `pkill -f` 兜底；只影响本任务 |
| 清理完成才报结束 | 工作簿/profile/管道暂存先清理，之后才把任务置为终态；结果 CSV 位于独立 `result/` 目录 |

## 目录结构

```
app/
  config.py        环境变量与并发/超时/大小配置
  models.py        任务状态机与对外字段
  processes.py     任务目录、进程组终止、暂存清理
  recalc_uno.py    LibreOffice 内执行的 UNO 重算脚本
  extractor.py     重算结果校验与只含值 CSV 生成
  runner.py        线程编排、信号量、取消、看门狗、回收
  main.py          FastAPI 路由
samples/
  generate_samples.py  样例工作簿生成器
docker/entrypoint.sh
tests/             21 个 pytest 用例（含假 soffice/UNO 进程）
scripts/demo.sh    curl 端到端演示
```

## 启动（Docker Compose）

```bash
docker compose build
docker compose up -d
# 打开交互式文档并直接演示上传/并行/取消/下载
xdg-open http://127.0.0.1:8000/docs   # 或浏览器手动打开
```

样例工作簿在容器首次启动时自动生成到挂载目录 `./samples/`：

- `stale_cache.xlsx`：**过期缓存**样例。`corrected` 是引用“校准”表的公式，
  文件内缓存被故意写成 `999.0`。真正重算后 `S001` 应为 `10*1.1-0.5=10.5`；
  若结果里出现 `999.0`，说明服务误用了上传缓存（本服务不会）。
- `cross_sheet.xlsx`：逐行跨表公式 `reading * 校准!factor + 校准!offset`。
- `controllable_delay.xlsx`：`corrected` 内嵌由 **“校准”!B1** 控制的
  `SUMPRODUCT(ROW(INDIRECT("1:"&校准!$B$1))...)` 大规模数组运算。
  调大 B1 即可拖慢重算，用于演示并行、取消与 60 秒超时。

重新生成样例（可调延迟参数）：

```bash
python3 samples/generate_samples.py --out-dir samples --delay-k 20000000
```

> 容器内重算耗时与 CPU 相关；默认 `K=3,000,000` 通常为亚秒到数秒，
> 调到 `20,000,000~50,000,000` 可稳定制造超过取消窗口/60 秒的任务。

## API 速览

```bash
# 创建任务（202 Accepted）
curl -X POST http://127.0.0.1:8000/tasks \
  -F 'file=@samples/stale_cache.xlsx;type=application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'

# 查状态：queued | running | cancelling | succeeded | failed | timed_out | cancelled
curl http://127.0.0.1:8000/tasks/<task_id>

# 取消（终态任务返回 409）
curl -X POST http://127.0.0.1:8000/tasks/<task_id>/cancel

# 仅 succeeded 可下载，否则 409
curl -OJ http://127.0.0.1:8000/tasks/<task_id>/download
```

CSV 格式（UTF-8、无公式、无缓存标记）：

```csv
sample_id,reading,corrected
S001,10.0,10.5
S002,20.0,21.5
```

## 失败语义（整批拒绝）

以下任一情况任务终态为 `failed`（超时为 `timed_out`，取消为 `cancelled`），
**不会产出可下载结果**，并在 `detail` 中给出定位信息：

- 工作簿缺少“测量”表或三列中任意一列；
- 任一行 `corrected`/`reading` 不是有限数值（`NaN`/`Inf`/文本/空值）；
- 单元格为公式错误类型（如 `#DIV/0!`、`#REF!`）；
- `sample_id` 为空或数据行为零；
- LibreOffice/UNO 非零退出或未产出重算文件。

## 本地开发与测试

无需 Docker 也能测试业务逻辑：测试用假的 `soffice` 与 UNO 脚本
（`tests/fakes/`）模拟进程组、延迟、失败与“未真正重算”。

```bash
pip install -r requirements.txt
PYTHONPATH=. pytest -q
```

测试覆盖：成功 CSV、并行上限与目录隔离、运行中/排队取消且不发布、
60s（测试中缩短为 2s）超时回收、重算失败、公式错误、非有限值、
缺表缺列、过期缓存防线、终态取消冲突、进程组回收与三个样例结构。

## 配置（环境变量）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `RECALC_MAX_UPLOAD_BYTES` | `8388608` | 上传硬上限 |
| `RECALC_MAX_PARALLEL` | `2` | 同时重算任务数 |
| `RECALC_RECALC_TIMEOUT` | `60` | 单任务重算硬时限（秒） |
| `RECALC_STORAGE_ROOT` | `/data/tasks` | 任务隔离存储根目录 |
| `SOFFICE_BIN` | `soffice` | LibreOffice 可执行文件（测试钩子） |
| `RECALC_SCRIPT_OVERRIDE` | 未设置 | 替换 UNO 脚本（测试钩子，生产不设置） |
