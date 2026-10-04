# 移民案件期限与材料管理

纯Python标准库实现的移民案件期限与材料管理原型，使用SQLite持久化，HTTP接口由`http.server`提供。

期限按**自然日**计算，并以**带版本的节假日日历**为统一计算依据。

## 核心规则

- **受理冻结日历版本**：创建案件时按`received_date`选当天生效的日历版本，写入`payload.calendar_version`。承诺决定日（`promised_decision`）、补件期限（`evidence_due`）、上诉窗口（`appeal_window`，决定日后30个自然日）此后都只用该冻结版本计算。
- **新版本只影响新案**：主管发布新日历后，既有案件的承诺日期与计算依据不变；只有发布后受理的案件冻结新版本。
- **休息日送达顺延**：补件要求/补件材料送达日落在周末或节假日时，按冻结日历顺延到下一个工作日；每次顺延的日期与原因（`weekend`/`holiday`）保留在计算明细`steps`中。
- **原承诺不被改写**：旧案件回填日历版本只补齐计算依据，历史承诺日期保持不变（明细标记`preserved: true`）。
- **并发先到先得**：两名经办同时提交送达回执、或主管并发发布日历时，先完成的一方生效；后到一方收到`409 conflict`，其原始输入保留（回执进入`retained_conflict`，日历进入草稿）。
- **重复回执幂等**：同一`receipt_key`或同一送达日重复提交均被拒绝，不重复顺延（数据库唯一约束 + 领域检查）。
- **批次恢复**：送达回执按计算批次（`calc_batches`）登记，记录变更、审计、回执在同一事务提交；写入失败的批次保留`prepared`状态，下次启动自动（或调用`POST /api/recovery/run`手动）从最近完整批次的快照恢复案件状态并写`batch_recovered`审计。
- **旧案回填**：缺少`calendar_version`的旧案件由主管调用回填接口，按受理日期选取当时生效版本。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动时崩溃恢复和服务启动。
- `src/calendar.py`：日历版本类型、发布输入校验、自然日计算与休息日顺延明细。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、冻结日历、补件/上诉期限和回执幂等检查。
- `src/repository.py`：SQLite建表（含日历、回执、批次、草稿）、事务和查询。
- `src/service.py`：用例编排、权限、乐观并发、批次写入/恢复、日历回填和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、日历顺延、并发冲突、写入恢复和旧案回填测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8329
```

默认端口为`8329`。服务启动时自动建表、播种基线日历（版本1，周末为周六/周日），并恢复上次未完成的计算批次。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/calendars`：日历版本列表。
- `POST /api/calendars`：主管发布日历，请求体`{"expected_version":1,"data":{"name":"...","effective_from":"YYYY-MM-DD","weekend":[5,6],"holidays":["YYYY-MM-DD",...]}}`；`expected_version`用于乐观并发，冲突时输入保留为草稿。
- `GET /api/calendars/drafts`：主管查看发布冲突保留的草稿。
- `GET /api/records` / `GET /api/records/{id}`：记录列表/详情。
- `POST /api/records`：创建记录，`data`示例：
  `{"applicant_id":"A-1","case_type":"family","received_date":"2026-01-05","deadline_days":30,"representation_active":true,"required_documents":["passport"]}`。
- `POST /api/records/{id}/actions/{action}`：执行动作，请求体`{"expected_version":1,"data":{...}}`。
  动作包括`submit`、`request_evidence`（`evidence_request_date`/`allowed_days`/`evidence_request`）、
  `respond`（`response_date`/`documents`）、`serve_evidence`（`receipt_key`/`served_date`）、
  `decide`（`decision`/`decision_reason`/`decision_date`）、`appeal`（`appeal_date`/`appeal_reason`）、`close`。
- `GET /api/records/{id}/receipts`：送达回执（含`applied`与`retained_conflict`）。
- `GET /api/records/{id}/batches`：计算批次状态与恢复痕迹。
- `POST /api/records/{id}/backfill-calendar`：主管为旧案件按受理日期回填日历版本。
- `POST /api/recovery/run`：主管手动执行未完成批次恢复。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、日历冻结与休息日顺延、新老案件版本隔离、并发回执/并发发布冲突保留、重复回执幂等、写入失败批次恢复和旧案回填。
