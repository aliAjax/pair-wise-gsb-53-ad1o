# 移民案件期限与材料管理

纯Python标准库实现的移民案件期限与材料管理原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/calendar.py`：期限日历版本、自然日推算与休息日顺延（含计算明细）。
- `src/rules.py`：状态转换、期限计算、补件送达顺延、材料完整性和冲突检查。
- `src/repository.py`：SQLite建表、事务、日历版本、计算批次与滞留输入。
- `src/service.py`：用例编排、日历冻结/回填、计算批次、乐观并发、恢复和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算、失败场景与日历一致性测试。

## 期限一致性约定

- 案件分两种模式：带`received_date`创建的案件按**自然日 + 日历版本**计算；仅带旧字段`received_day`的历史案件走整数日序号兼容路径。
- 受理时冻结当天适用（`effective_from <= 受理日`的最新版本）的日历完整快照写入案件；主管之后发布新版本只影响新案，旧案的决定日、补件期限、上诉窗口一律按冻结快照计算，**原承诺不被改写**。
- 期限按自然日累加，末日若落在周末或节假日则顺延到第一个工作日；每一次顺延（日期、原因、跳到哪一天）都保存在`*_schedule.steps`和回执的`roll_steps`中。
- 送达回执（动作`record_service`）按送达日的日历顺延确定"视为送达日"，再起算补件回应窗口；同一`receipt_id`重复提交幂等返回，不重复顺延。
- 两名经办并发提交（或主管并发发布日历）时，先完成的计算结果生效；后到一方收到409版本冲突，其**原始输入原样保留**在滞留输入中（`GET /api/retained-inputs`），对应计算批次标记为`conflict`。
- 写入失败时计算批次保留完整结果（状态`write_failed`），可经`POST /api/recover`从最近完整计算批次恢复落地，恢复按批次目标版本幂等执行。
- 旧案缺日历版本时由主管按受理日期回填（无受理日期的历史案件锚定最新版本），只补版本锚点和快照，不改写既有期限。

## 启动

```bash
python3 app.py --db ./data.db --port 8329
```

默认端口为`8329`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。自然日模式`data`带`received_date`（YYYY-MM-DD），历史兼容模式仍用`received_day`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。自然日模式动作包括`submit`、`request_evidence`、`record_service`（补件送达回执，数据为`receipt_id`+`service_date`）、`respond`、`decide`（产出上诉窗口）、`appeal`、`close`。
- `GET /api/calendars`：列日历版本；`POST /api/calendars`（仅主管）发布新版本，请求体为`{"expected_version":N,"data":{"name","effective_from","holidays":[],"weekend_days":[5,6]}}`，冲突时草稿进滞留输入。
- `POST /api/records/{id}/backfill-calendar`（仅主管）：按受理日期回填日历版本，请求体为`{"expected_version":1,"calendar_version":可选}`。
- `POST /api/recover`：从最近完整计算批次恢复，请求体为`{"data":{"record_id":1}}`或`{"data":{"batch_ref":"..."}}`。
- `GET /api/batches?record_id=&status=`：查询计算批次；`GET /api/retained-inputs?record_id=`：查询冲突时保留的输入。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝、版本冲突、日历冻结与顺延明细、并发回执先到先得、写入失败批次恢复、重复回执幂等和旧案回填。
