"""SQLite 表结构与事务访问。

除案件记录与审计外，还持久化：
- calendar_versions/calendar_drafts：节假日日历版本与冲突时保留的发布输入；
- evidence_receipts：补件送达回执（幂等，唯一约束防重复顺延）；
- calc_batches：计算批次，写入失败后据此从最近完整批次恢复。
"""
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from . import calendar as cal
from .domain import Conflict, NotFound


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _batch_id() -> str:
    return uuid.uuid4().hex


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
                CREATE TABLE IF NOT EXISTS calendar_versions (
                    version INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    effective_from TEXT NOT NULL,
                    weekend_days TEXT NOT NULL,
                    holidays TEXT NOT NULL,
                    published_by TEXT NOT NULL,
                    published_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS calendar_drafts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    expected_version INTEGER,
                    name TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    submitted_by TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    submitted_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS evidence_receipts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    receipt_key TEXT NOT NULL,
                    served_date TEXT NOT NULL,
                    effective_date TEXT,
                    calendar_version INTEGER,
                    calc_detail TEXT,
                    submitted_by TEXT NOT NULL,
                    submitted_at TEXT NOT NULL,
                    batch_id TEXT,
                    status TEXT NOT NULL,
                    retained_input TEXT,
                    UNIQUE(record_id, receipt_key)
                );
                CREATE TABLE IF NOT EXISTS calc_batches (
                    batch_id TEXT PRIMARY KEY,
                    record_id INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    input TEXT NOT NULL,
                    snapshot TEXT,
                    result TEXT,
                    error TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    finished_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_calendar_effective ON calendar_versions(effective_from, version);
                CREATE INDEX IF NOT EXISTS idx_receipts_record ON evidence_receipts(record_id, status);
                CREATE INDEX IF NOT EXISTS idx_batches_record ON calc_batches(record_id, status, created_at);
                """
            )
            row = connection.execute("SELECT COUNT(*) AS total FROM calendar_versions").fetchone()
            if int(row["total"]) == 0:
                seeded = cal.baseline(_now())
                connection.execute(
                    "INSERT INTO calendar_versions(version,name,effective_from,weekend_days,holidays,published_by,published_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        seeded.version,
                        seeded.name,
                        cal.iso(seeded.effective_from),
                        json.dumps(list(seeded.weekend_days)),
                        json.dumps(sorted(seeded.holidays)),
                        seeded.published_by,
                        seeded.published_at,
                    ),
                )

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    # --------------------------------------------------------------- records
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

    def force_state(self, record_id: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any]) -> Dict[str, Any]:
        """按快照恢复记录，版本号继续向前，不掩盖恢复事实。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            version = int(row["version"]) + 1
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

    def stats(self) -> Dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT state, COUNT(*) AS total FROM records GROUP BY state").fetchall()
        return {str(row["state"]): int(row["total"]) for row in rows}

    # -------------------------------------------------------------- calendars
    @staticmethod
    def _calendar_row(row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "version": int(row["version"]),
            "name": row["name"],
            "effective_from": row["effective_from"],
            "weekend_days": json.loads(row["weekend_days"]),
            "holidays": json.loads(row["holidays"]),
            "published_by": row["published_by"],
            "published_at": row["published_at"],
        }

    def latest_calendar_version(self) -> int:
        with self._connect() as connection:
            row = connection.execute("SELECT MAX(version) AS v FROM calendar_versions").fetchone()
        return int(row["v"] or cal.BASELINE_VERSION)

    def publish_calendar(self, payload: Dict[str, Any], actor_id: str, expected_version: Optional[int] = None) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT MAX(version) AS v FROM calendar_versions").fetchone()
            current = int(row["v"] or cal.BASELINE_VERSION)
            if expected_version is not None and int(expected_version) != current:
                connection.rollback()
                raise Conflict("日历版本冲突，当前最新版本为%s" % current)
            cursor = connection.execute(
                "INSERT INTO calendar_versions(name,effective_from,weekend_days,holidays,published_by,published_at) VALUES(?,?,?,?,?,?)",
                (
                    payload["name"],
                    payload["effective_from"],
                    json.dumps(payload["weekend_days"]),
                    json.dumps(payload["holidays"]),
                    actor_id,
                    now,
                ),
            )
            result = connection.execute("SELECT * FROM calendar_versions WHERE version=?", (int(cursor.lastrowid),)).fetchone()
            connection.commit()
        return self._calendar_row(result)

    def retain_calendar_draft(self, payload: Dict[str, Any], actor_id: str, expected_version: Optional[int], reason: str) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO calendar_drafts(expected_version,name,payload,submitted_by,reason,submitted_at) VALUES(?,?,?,?,?,?)",
                (expected_version, payload["name"], json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, reason, _now()),
            )
            return int(cursor.lastrowid)

    def list_calendar_drafts(self) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM calendar_drafts ORDER BY id").fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item["payload"])
            result.append(item)
        return result

    def list_calendars(self) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM calendar_versions ORDER BY version").fetchall()
        return [self._calendar_row(row) for row in rows]

    def get_calendar(self, version: int) -> cal.CalendarVersion:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM calendar_versions WHERE version=?", (version,)).fetchone()
        if row is None:
            raise NotFound("日历版本不存在")
        return cal.from_dict(self._calendar_row(row))

    def effective_calendar(self, on_day: cal.date) -> cal.CalendarVersion:
        """受理日冻结：取生效日不晚于该日的最新版本，否则回退基线。"""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM calendar_versions WHERE effective_from <= ? ORDER BY version DESC LIMIT 1",
                (cal.iso(on_day),),
            ).fetchone()
            if row is None:
                row = connection.execute("SELECT * FROM calendar_versions ORDER BY version LIMIT 1").fetchone()
        return cal.from_dict(self._calendar_row(row))

    # ---------------------------------------------------------------- receipts
    @staticmethod
    def _receipt_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["calc_detail"] = json.loads(item["calc_detail"]) if item["calc_detail"] else None
        item["retained_input"] = json.loads(item["retained_input"]) if item["retained_input"] else None
        return item

    def find_receipt(self, record_id: int, receipt_key: str) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM evidence_receipts WHERE record_id=? AND receipt_key=?",
                (record_id, receipt_key),
            ).fetchone()
        return self._receipt_row(row) if row else None

    def list_receipts(self, record_id: int) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM evidence_receipts WHERE record_id=? ORDER BY id",
                (record_id,),
            ).fetchall()
        return [self._receipt_row(row) for row in rows]

    def retain_receipt(self, record_id: int, receipt_key: str, submitted_by: str, retained_input: Dict[str, Any], reason: str) -> int:
        """版本冲突时先保留后到一方的输入，独立事务，不覆盖任何已生效结果。"""
        now = _now()
        with self._connect() as connection:
            try:
                cursor = connection.execute(
                    "INSERT INTO evidence_receipts(record_id,receipt_key,served_date,submitted_by,submitted_at,status,retained_input) VALUES(?,?,?,?,?,?,?)",
                    (
                        record_id,
                        receipt_key,
                        retained_input.get("served_date", ""),
                        submitted_by,
                        now,
                        "retained_conflict",
                        json.dumps(retained_input, ensure_ascii=False, sort_keys=True),
                    ),
                )
            except sqlite3.IntegrityError:
                # 同键回执已经存在（先到一方已生效），冲突输入挂到审计即可，不重复插入。
                existing = connection.execute(
                    "SELECT id FROM evidence_receipts WHERE record_id=? AND receipt_key=?",
                    (record_id, receipt_key),
                ).fetchone()
                return int(existing["id"])
            return int(cursor.lastrowid)

    def apply_receipt(
        self,
        record_id: int,
        expected_version: int,
        state: str,
        payload: Dict[str, Any],
        actor_id: str,
        details: Dict[str, Any],
        receipt: Dict[str, Any],
        batch_id: str,
    ) -> Dict[str, Any]:
        """回执生效：记录状态变更、审计、回执落库在同一事务内提交。"""
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
            duplicate = connection.execute(
                "SELECT id FROM evidence_receipts WHERE record_id=? AND receipt_key=?",
                (record_id, receipt["receipt_key"]),
            ).fetchone()
            if duplicate is not None:
                connection.rollback()
                raise Conflict("回执已存在，重复回执不重复顺延")
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, "serve_evidence", actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            connection.execute(
                "INSERT INTO evidence_receipts(record_id,receipt_key,served_date,effective_date,calendar_version,calc_detail,submitted_by,submitted_at,batch_id,status,retained_input) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record_id,
                    receipt["receipt_key"],
                    receipt["served_date"],
                    receipt["effective_date"],
                    receipt["calendar_version"],
                    json.dumps(receipt["calc_detail"], ensure_ascii=False, sort_keys=True),
                    actor_id,
                    now,
                    batch_id,
                    "applied",
                    None,
                ),
            )
            connection.execute("UPDATE calc_batches SET status=?, finished_at=? WHERE batch_id=?", ("committed", now, batch_id))
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    # ----------------------------------------------------------------- batches
    def open_batch(self, record_id: int, kind: str, batch_input: Dict[str, Any], snapshot: Dict[str, Any], created_by: str) -> str:
        batch_id = _batch_id()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO calc_batches(batch_id,record_id,kind,status,input,snapshot,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    batch_id,
                    record_id,
                    kind,
                    "prepared",
                    json.dumps(batch_input, ensure_ascii=False, sort_keys=True),
                    json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
                    created_by,
                    _now(),
                ),
            )
        return batch_id

    def fail_batch(self, batch_id: str, error: str, result: Dict[str, Any] = None, terminal: bool = False) -> None:
        # terminal=True（版本冲突）：先到一方已生效，无需恢复，批次记为aborted；
        # 否则保留prepared（待恢复）状态，仅登记错误，等待恢复流程处理。
        with self._connect() as connection:
            connection.execute(
                "UPDATE calc_batches SET status=?,error=?,result=?,finished_at=? WHERE batch_id=?",
                ("aborted" if terminal else "prepared", error, json.dumps(result, ensure_ascii=False, sort_keys=True) if result is not None else None, _now() if terminal else None, batch_id),
            )

    def list_batches(self, record_id: int) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM calc_batches WHERE record_id=? ORDER BY rowid", (record_id,)).fetchall()
        return [self._batch_row(row) for row in rows]

    def pending_batches(self) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM calc_batches WHERE status='prepared' ORDER BY rowid").fetchall()
        return [self._batch_row(row) for row in rows]

    def latest_committed_batch(self, record_id: int, kind: Optional[str] = None) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            if kind:
                row = connection.execute(
                    "SELECT * FROM calc_batches WHERE record_id=? AND kind=? AND status='committed' ORDER BY rowid DESC LIMIT 1",
                    (record_id, kind),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT * FROM calc_batches WHERE record_id=? AND status='committed' ORDER BY rowid DESC LIMIT 1",
                    (record_id,),
                ).fetchone()
        return self._batch_row(row) if row else None

    def mark_batch_recovered(self, batch_id: str, restored_batch_id: Optional[str]) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE calc_batches SET status=?,result=?,finished_at=? WHERE batch_id=?",
                ("recovered", json.dumps({"restored_from": restored_batch_id}, ensure_ascii=False), _now(), batch_id),
            )

    @staticmethod
    def _batch_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        for key in ("input", "snapshot", "result"):
            item[key] = json.loads(item[key]) if item[key] else None
        return item

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False
