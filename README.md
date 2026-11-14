# 园区电表累计读数还原（metercalc）

园区能源组抄表员月底核对用的小工具：电表是**累计读数**，中间换过表、表跑满回绕过，
还要保证峰/谷与总量对得上、缺口不能悄悄插值。系统把读数 CSV 还原成**月度用能表**，
并给出每条数字的依据（区间起止 + 分摊公式）与复核清单。

## 能力

- **读数导入**：CSV 严格校验（列契约、ISO 日期真实性、数值、电量类型、重复读数），任一行不合法即整体拒绝并给出行号/列名/原因。
- **换表/回绕识别**：`后读数 < 前读数` 时按换表记录分段求和，或按量程还原回绕量；两者都不成立就隔离为异常。
- **用量还原**：逐区间给出前/后读数、区间天数、用量与算式。
- **月度分摊**：区间跨月按天数比例分摊，保留 2 位小数，用最大余数法保证分摊合计与区间用量**完全相等**。
- **分时校验**：`peak + valley ≈ total`，容差 0.5%，超差记异常但仍输出数值。
- **复核清单**：月度台账（每表每月 total/peak/valley/缺口天数/异常数）+ P1/P2/P3 复核项 + CSV 导出。

## 核心规则

1. **区间用量**：`usage = 后读数 − 前读数`。
   - 后 < 前 且区间被换表记录覆盖（`start < 换表日 ≤ end`）→ 两表分段：
     `旧表尾段 = 拆表读数 − 前读数`，`新表首段 = 后读数 − 装表读数`，合计为区间用量。
   - 否则若 `前读数 ≥ 量程 × 0.8` 且 `后读数 ≤ 量程 × 0.2` → 回绕：`量程 − 前读数 + 后读数`。
   - 两者都不成立（或换表记录与读数不自洽）→ `anomaly`，该区间用量为 null，**不参与汇总**。
   - 量程默认 `99999.9`，可按表台账配置或用请求参数覆盖。
2. **月度分摊**：`[起始读数日, 结束读数日)` 按日历月切桶，份额 = `用量 × 该月天数 / 区间总天数`；
   以「分」为整数单位用最大余数法分配，`sum(分摊) == round(区间用量, 2)`，尾差落在余数最大的月份。
3. **分时一致性**：`|peak + valley − total| ≤ 0.5% × total`，超差进复核清单，数值照常输出。
4. **缺口**：相邻读数间隔 > 31 天 → `gap`，输出缺口区间、缺失天数、建议抄表日（区间中点），**禁止插值**。
5. **汇总**：每表每月 total/peak/valley/缺口天数/异常数（异常数 = 隔离异常 + 分时超差）+ 复核清单。

## 启动

```bash
cd batch-20261114b/projects/gb-628
cp .env.example .env          # 可选，默认端口 18217
docker compose up -d --build
docker compose ps             # 等 app 变 healthy
curl -s http://127.0.0.1:18217/health
```

仪表盘页面：<http://127.0.0.1:18217/>

SQLite 使用命名卷 `metercalc-data:/app/data`，启动时按样例版本幂等 seed（重复启动不产生脏数据）。
重建数据库：`docker compose down -v` 后重新 `up`。

## 演示步骤

1. 打开 `/` 看场景、输入与样例概览（4 块表、4140 行读数、2025-04 ~ 2026-03、换表/回绕/缺口/超差/无法判定各几处）。
2. 进 `/workbench`，月份选 `2026-03`，点「执行还原」，进度依次走 读数校验 → 换表/回绕识别 → 用量还原 → 月度分摊 → 分时校验 → 台账生成。
3. 结果页 `/result/{run_id}` 看：总用量 2225.82 kWh；EM-01 分时超差（峰+谷比总量少 49.2）；EM-03 跨月区间按 26/35 天分摊；EM-04 读数回退被隔离；EM-03 缺口 35 天、建议抄表日 2026-03-09。
4. 换表月份：工作台选 `2025-09`（EM-02 于 09-15 换表，区间用量 28.0 = 13.4 + 14.6）。回绕月份：选 `2025-12`（EM-03 用量 17.4）。
5. `/review` 看 12 个月全量台账与复核清单，导出 `/api/ledger.csv`、`/api/review.csv`。

命令行复现：

```bash
curl -s -X POST http://127.0.0.1:18217/api/import/sample
curl -s -X POST http://127.0.0.1:18217/api/restore \
  -H 'Content-Type: application/json' -d '{"month":"2026-03"}' | python3 -m json.tool | head -40
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:18217/api/restore \
  -H 'Content-Type: application/json' -d '{"month":"2026-03","meter_ids":["EM-99"]}'   # 400
```

## API / 页面

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 + 读数行数/表数 |
| GET | `/` | 首页：场景、输入契约、样例概览、演示步骤 |
| GET | `/workbench` | 工作台：选表选月份、导入读数、执行还原（带进度） |
| GET | `/result/{run_id}` | 结果页：月度用能表、分摊依据、缺口、隔离异常、复核清单 |
| GET | `/review` | 月度台账（全期）+ 复核清单 + 导出 |
| POST | `/api/import` | multipart 上传读数 CSV（UTF-8/GBK），严格校验，整体替换 |
| POST | `/api/import/sample` | 重新导入样例（幂等） |
| POST | `/api/restore` | `{month, meter_ids?, full_scale?, max_gap_days?}` → 201 + run_id + 台账 + 依据 |
| GET | `/api/runs`, `/api/runs/{run_id}` | 批次列表 / 批次详情 |
| GET | `/api/runs/{run_id}/ledger.csv`、`/api/runs/{run_id}/review.csv` | 本月台账 / 复核清单 CSV |
| GET | `/api/ledger.csv`、`/api/review.csv` | 全期台账 / 复核清单 CSV |

错误输入返回 4xx 且带中文原因：未知表号（400 + 可选表号列表）、月份格式错误（400）、
该月无读数覆盖（400）、量程非正（400）、缺口阈值 < 2（400）、CSV 行级错误（400 + 逐行问题）。

## 目录

```text
app/
  main.py          # 路由（薄）：页面 + JSON API
  settings.py      # 路径/常量（量程、缺口阈值、容差）
  readings.py      # 读数解析/校验/分组/缺口检测
  restore.py       # 区间用量、换表分段、回绕、异常隔离
  proration.py     # 跨月按天分摊（最大余数法守恒）
  validation.py    # 峰谷容差、缺口月份重叠、覆盖说明
  summary.py       # 月度台账、复核清单、CSV 导出
  pipeline.py      # 编排：一次还原 → 全月份汇总 + 指定月份聚焦
  store.py         # SQLite（读数/换表/台账/批次/事件）
  seed.py          # 幂等 seed
  templates/       # base/index/workbench/result/review
  static/          # app.js, style.css（原生 JS/CSS，无框架无 CDN）
sample_data/
  readings.csv         # 4 块表 × 每日读数 × 12 个月（total/peak/valley）= 4140 行
  meter_changes.json   # 换表记录（旧表号/新表号/换表日/拆表读数/装表读数，按类型分开）
  meters.json          # 表计台账（名称/位置/量程）
tests/                 # test_restore / test_proration / test_validation / test_api
```

样例数据里刻意埋了：1 次换表（EM-02 2025-09-15）、1 次满量程回绕（EM-03 2025-12-17）、
2 段 >31 天缺口（EM-04 通讯中断 46 天、EM-03 连续漏抄 35 天）、1 组峰谷超差（EM-01 2026-03）、
1 处无法判定异常（EM-04 2026-03-17 读数回退且无换表记录）。

## 已知限制

- 换表记录的拆表/装表读数需由人工签认后登记，系统不做 OCR 或现场采集。
- 回绕判定依赖量程配置（默认 99999.9）；量程配错会把真实回绕判成异常，或反之。
- 缺口期间没有读数，系统不做任何插值；缺口越大，该月日均值参考意义越小。
- 表计倍率（CT 变比）在台账里只登记不参与换算（样例倍率均为 1.0）。
- 单租户单进程演示：无登录、无权限、无并发写保护，SQLite 为本地文件。
