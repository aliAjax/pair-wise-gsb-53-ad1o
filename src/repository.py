"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import Conflict, NotFound, WriteFailure
from .calendar import CalendarEngine, validate_calendar_payload


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE TABLE IF NOT EXISTS calendars (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    version INTEGER NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    effective_from TEXT NOT NULL,
                    holidays TEXT NOT NULL,
                    weekend_days TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    published_by TEXT NOT NULL,
                    published_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS calc_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_ref TEXT NOT NULL UNIQUE,
                    record_id INTEGER,
                    action TEXT NOT NULL,
                    status TEXT NOT NULL,
                    input TEXT NOT NULL,
                    result TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_batches_record ON calc_batches(record_id, id);
                CREATE TABLE IF NOT EXISTS retained_inputs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER,
                    scope TEXT NOT NULL,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    input TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'retained',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_retained_record ON retained_inputs(record_id, id);
                """
            )
            self._ensure_default_calendar(connection)

    def _ensure_default_calendar(self, connection: sqlite3.Connection) -> None:
        existing = connection.execute("SELECT version FROM calendars ORDER BY version DESC LIMIT 1").fetchone()
        if existing is not None:
            return
        now = _now()
        payload = validate_calendar_payload({
            "name": "default",
            "effective_from": "1970-01-01",
            "holidays": [],
            "weekend_days": [5, 6],
        })
        connection.execute(
            "INSERT INTO calendars(version,name,effective_from,holidays,weekend_days,note,published_by,published_at) VALUES(?,?,?,?,?,?,?,?)",
            (1, payload["name"], payload["effective_from"], json.dumps(payload["holidays"]), json.dumps(payload["weekend_days"]), payload["note"], "system", now),
        )

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (reference, state, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "created", actor_id, 1, json.dumps({"state": state}, ensure_ascii=False, sort_keys=True), now),
                )
                row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("reference已存在") from exc
        return self._row(row)

    def get(self, record_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        return self._row(row)

    def list_records(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM records WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(row) for row in rows]

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any]) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    def patch_payload(self, record_id: int, expected_version: int, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any]) -> Dict[str, Any]:
        """仅改写payload并推进版本（用于旧案回填日历版本），状态保持不变。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT state, version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET payload=?,version=?,updated_by=?,updated_at=? WHERE id=?",
                (json.dumps(payload, ensure_ascii=False, sort_keys=True), version, actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    def force_apply(self, record_id: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any], new_version: Optional[int] = None) -> Dict[str, Any]:
        """按已完成的计算批次落地结果，恢复流程专用。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            current_version = int(row["version"])
            version = int(new_version) if new_version is not None else current_version + 1
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, int(row["version"]), json.dumps(details, ensure_ascii=False, sort_keys=True), _now()),
            )
            connection.commit()

    def audit_timeline(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM audit_events WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    # ---- 期限日历版本 ----
    @staticmethod
    def _calendar_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["holidays"] = json.loads(item["holidays"])
        item["weekend_days"] = json.loads(item["weekend_days"])
        return item

    def latest_calendar(self, on_date: Optional[str] = None) -> Dict[str, Any]:
        with self._connect() as connection:
            if on_date:
                row = connection.execute(
                    "SELECT * FROM calendars WHERE effective_from <= ? ORDER BY version DESC LIMIT 1",
                    (on_date,),
                ).fetchone()
            else:
                row = connection.execute("SELECT * FROM calendars ORDER BY version DESC LIMIT 1").fetchone()
        if row is None:
            raise NotFound("尚无可用期限日历")
        return self._calendar_row(row)

    def get_calendar(self, version: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM calendars WHERE version=?", (int(version),)).fetchone()
        if row is None:
            raise NotFound("日历版本不存在：%s" % version)
        return self._calendar_row(row)

    def list_calendars(self, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM calendars ORDER BY version DESC LIMIT ?", (limit,)).fetchall()
        return [self._calendar_row(row) for row in rows]

    def publish_calendar(self, payload: Dict[str, Any], actor_id: str, expected_version: Optional[int] = None) -> Dict[str, Any]:
        payload = validate_calendar_payload(payload)
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT MAX(version) AS version FROM calendars").fetchone()
            current_version = int(row["version"]) if row and row["version"] is not None else 0
            if expected_version is not None and int(expected_version) != current_version:
                connection.rollback()
                raise Conflict("日历版本冲突，请基于最新版本重试")
            version = current_version + 1
            try:
                connection.execute(
                    "INSERT INTO calendars(version,name,effective_from,holidays,weekend_days,note,published_by,published_at) VALUES(?,?,?,?,?,?,?,?)",
                    (version, payload["name"], payload["effective_from"], json.dumps(payload["holidays"]), json.dumps(payload["weekend_days"]), payload["note"], actor_id, now),
                )
                connection.commit()
            except sqlite3.Error:
                connection.rollback()
                raise
        return self.get_calendar(version)

    # ---- 计算批次与恢复 ----
    def save_batch(self, batch: Dict[str, Any]) -> Dict[str, Any]:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO calc_batches(batch_ref,record_id,action,status,input,result,detail,actor_id,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    batch["batch_ref"], batch.get("record_id"), batch["action"], batch["status"],
                    json.dumps(batch.get("input", {}), ensure_ascii=False, sort_keys=True),
                    json.dumps(batch.get("result", {}), ensure_ascii=False, sort_keys=True),
                    json.dumps(batch.get("detail", {}), ensure_ascii=False, sort_keys=True),
                    batch["actor_id"], _now(),
                ),
            )
            connection.commit()
            row = connection.execute("SELECT * FROM calc_batches WHERE batch_ref=?", (batch["batch_ref"],)).fetchone()
        return self._batch_row(row)

    @staticmethod
    def _batch_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        for key in ("input", "result", "detail"):
            item[key] = json.loads(item[key])
        return item

    def get_batch(self, batch_ref: str) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM calc_batches WHERE batch_ref=?", (batch_ref,)).fetchone()
        return self._batch_row(row) if row else None

    def list_batches(self, record_id: Optional[int] = None, status: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        clauses = []
        params: List[Any] = []
        if record_id is not None:
            clauses.append("record_id=?")
            params.append(record_id)
        if status:
            clauses.append("status=?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM calc_batches%s ORDER BY id DESC LIMIT ?" % where, params).fetchall()
        return [self._batch_row(row) for row in rows]

    def mark_batch(self, batch_ref: str, status: str, error: str = "") -> None:
        with self._connect() as connection:
            connection.execute("UPDATE calc_batches SET status=?, error=? WHERE batch_ref=?", (status, error, batch_ref))
            connection.commit()

    def latest_complete_batch(self, record_id: int, action: Optional[str] = None) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            if action:
                row = connection.execute(
                    "SELECT * FROM calc_batches WHERE record_id=? AND action=? AND status='complete' ORDER BY id DESC LIMIT 1",
                    (record_id, action),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT * FROM calc_batches WHERE record_id=? AND status='complete' ORDER BY id DESC LIMIT 1",
                    (record_id,),
                ).fetchone()
        return self._batch_row(row) if row else None

    # ---- 滞留输入（版本冲突时保留） ----
    def retain_input(self, scope: str, action: str, actor_id: str, data: Dict[str, Any], reason: str, record_id: Optional[int] = None) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO retained_inputs(record_id,scope,action,actor_id,input,reason,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (record_id, scope, action, actor_id, json.dumps(data or {}, ensure_ascii=False, sort_keys=True), reason, "retained", now),
            )
            connection.commit()
            row = connection.execute("SELECT * FROM retained_inputs WHERE id=?", (cursor.lastrowid,)).fetchone()
        return self._retained_row(row)

    @staticmethod
    def _retained_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["input"] = json.loads(item["input"])
        return item

    def list_retained_inputs(self, record_id: Optional[int] = None, status: str = "retained", limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        clauses = ["status=?"]
        params: List[Any] = [status]
        if record_id is not None:
            clauses.append("record_id=?")
            params.append(record_id)
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM retained_inputs WHERE " + " AND ".join(clauses) + " ORDER BY id DESC LIMIT ?",
                params,
            ).fetchall()
        return [self._retained_row(row) for row in rows]

    def resolve_retained_input(self, retained_id: int) -> None:
        with self._connect() as connection:
            connection.execute("UPDATE retained_inputs SET status='resolved' WHERE id=?", (retained_id,))
            connection.commit()

    def stats(self) -> Dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT state, COUNT(*) AS total FROM records GROUP BY state").fetchall()
        return {str(row["state"]): int(row["total"]) for row in rows}

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False
