"""业务用例编排、权限检查与审计。

期限一致性约定：
- 受理时冻结当天适用的日历版本快照写入案件；之后主管发布新版本只影响新案；
- 每个会改变期限的动作先产出"计算批次"（含输入、结果、计算明细），再乐观写库；
- 后到一方写库时看到版本冲突，计算批次标记为冲突，原始输入原样保留到滞留输入表；
- 写入失败时批次保留完整结果，可凭最近完整批次恢复落地；
- 旧案缺日历版本时按受理日期回填版本，原承诺期限不被改写。
"""
import sqlite3
import uuid
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .calendar import CalendarEngine
from .domain import Actor, Conflict, PermissionDenied, WriteFailure, text
from .repository import Repository
from .rules import DomainRules

CALENDAR_ROLE = "supervisor"
TERMINAL_BATCH_STATUSES = ("complete", "recovered", "conflict")


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    def _require_role(self, actor: Actor, role: str) -> None:
        if actor.role != "admin" and actor.role != role:
            raise PermissionDenied("仅%s可执行该操作" % role)

    @staticmethod
    def _engine_for_record(payload: Dict[str, Any]) -> Optional[CalendarEngine]:
        snapshot = payload.get("calendar_snapshot")
        return CalendarEngine(snapshot) if isinstance(snapshot, dict) else None

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        payload = payload or {}
        engine: Optional[CalendarEngine] = None
        if self.rules.is_calendar_payload(payload):
            # 先校验受理日期，再冻结当天适用的日历版本；后续发布不影响本案
            self.rules.validate_create(payload)
            intake = str(payload.get("received_date", ""))
            calendar = self.repository.latest_calendar(on_date=intake)
            engine = CalendarEngine(calendar)
        prepared = self.rules.prepare_create(payload, engine)
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        return self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id)

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get(record_id)

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        data = dict(data or {})
        record = self.repository.get(record_id)
        payload = record["payload"]

        # 重复送达回执：不重复顺延，幂等返回（并发情形由乐观版本在下方兜住）
        if action == "record_service":
            receipt_id = str(data.get("receipt_id", "")).strip()
            if receipt_id and any(item.get("receipt_id") == receipt_id for item in payload.get("service_receipts", [])):
                self.repository.add_audit(record_id, actor.user_id, "duplicate_receipt_ignored", {
                    "receipt_id": receipt_id, "input": data,
                })
                return record

        engine = self._engine_for_record(payload)
        if payload.get("mode") == "calendar" and engine is None:
            raise Conflict("案件缺少冻结日历版本，请先回填后再操作")

        batch_ref = uuid.uuid4().hex
        batch: Dict[str, Any] = {
            "batch_ref": batch_ref,
            "record_id": record_id,
            "action": action,
            "actor_id": actor.user_id,
            "input": data,
            "status": "computed",
        }
        try:
            new_state, new_payload, summary = self.rules.apply_action(record, action, data, engine)
        except Conflict as exc:
            # 计算阶段已发现冲突（如并发写入的回执）：保留输入后抛出
            batch["status"] = "conflict"
            batch["result"] = {}
            batch["detail"] = {"message": str(exc)}
            self.repository.save_batch(batch)
            self.repository.retain_input("record", action, actor.user_id, data, str(exc), record_id=record_id)
            raise

        target_version = int(expected_version) + 1
        details = {"summary": summary, "input": data, "from": record["state"], "to": new_state, "batch_ref": batch_ref}
        batch["result"] = {
            "state": new_state,
            "payload": new_payload,
            "version": target_version,
            "details": details,
        }
        batch["detail"] = {"summary": summary}
        self.repository.save_batch(batch)

        try:
            saved = self.repository.mutate(
                record_id=record_id,
                expected_version=int(expected_version),
                state=new_state,
                payload=new_payload,
                actor_id=actor.user_id,
                action=action,
                details=details,
            )
        except Conflict as exc:
            # 先完成者已生效；后来一方看到版本冲突，原始输入保留
            self.repository.mark_batch(batch_ref, "conflict", str(exc))
            self.repository.retain_input("record", action, actor.user_id, data, str(exc), record_id=record_id)
            raise
        except sqlite3.Error as exc:
            self.repository.mark_batch(batch_ref, "write_failed", str(exc))
            raise WriteFailure("写入失败，可从最近完整计算批次恢复：%s" % batch_ref) from exc
        self.repository.mark_batch(batch_ref, "complete")
        return saved

    def publish_calendar(self, actor: Actor, payload: Dict[str, Any], expected_version: Optional[int] = None) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._require_role(actor, CALENDAR_ROLE)
        payload = dict(payload or {})
        try:
            return self.repository.publish_calendar(payload, actor.user_id, expected_version)
        except Conflict as exc:
            # 两位主管并发发布：先完成的版本生效，后来者草稿原样保留
            self.repository.retain_input("calendar", "publish_calendar", actor.user_id, payload, str(exc))
            raise

    def list_calendars(self, actor: Actor, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_calendars(limit=limit)

    def backfill_calendar(self, actor: Actor, record_id: int, expected_version: int, calendar_version: Optional[int] = None) -> Dict[str, Any]:
        """旧案缺日历版本时按受理日期回填；只补版本锚点，不改写原承诺期限。"""
        actor = self._actor(actor)
        self._require_role(actor, CALENDAR_ROLE)
        record = self.repository.get(record_id)
        payload = dict(record["payload"])
        if payload.get("calendar_version") and isinstance(payload.get("calendar_snapshot"), dict):
            raise Conflict("案件已冻结日历版本%s，无需回填" % payload["calendar_version"])

        if calendar_version is not None:
            calendar = self.repository.get_calendar(int(calendar_version))
        else:
            intake_date = payload.get("received_date")
            calendar = self.repository.latest_calendar(on_date=intake_date)
        engine = CalendarEngine(calendar)
        payload["calendar_version"] = engine.version
        payload["calendar_snapshot"] = engine.snapshot()
        payload["calendar_backfilled"] = True
        saved = self.repository.patch_payload(
            record_id=record_id,
            expected_version=int(expected_version),
            payload=payload,
            actor_id=actor.user_id,
            action="calendar_backfill",
            details={
                "calendar_version": engine.version,
                "by_received_date": calendar_version is None,
                "note": "按受理日期回填日历版本，原承诺期限不变",
            },
        )
        return saved

    def recover(self, actor: Actor, record_id: Optional[int] = None, batch_ref: Optional[str] = None) -> Dict[str, Any]:
        """从最近完整计算批次恢复落地；已是目标版本则幂等返回。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if batch_ref:
            batch = self.repository.get_batch(batch_ref)
            if batch is None:
                raise Conflict("计算批次不存在：%s" % batch_ref)
        elif record_id is not None:
            record = self.repository.get(record_id)
            candidates = self.repository.list_batches(record_id=record_id, limit=50)
            batch = next((item for item in candidates if item["status"] in ("computed", "write_failed") and item["result"]), None)
            if batch is None:
                return {"applied": False, "reason": "没有待恢复的完整计算批次", "record_version": record["version"]}
        else:
            raise Conflict("恢复需要record_id或batch_ref")

        result = batch.get("result") or {}
        target_id = batch.get("record_id")
        if target_id is None or not result.get("payload"):
            raise Conflict("计算批次不完整，无法恢复")
        record = self.repository.get(target_id)
        target_version = int(result.get("version", record["version"] + 1))
        if int(record["version"]) >= target_version and record["state"] == result.get("state"):
            self.repository.mark_batch(batch["batch_ref"], "complete")
            return {"applied": False, "reason": "批次结果已生效", "batch_ref": batch["batch_ref"], "record": record}

        details = dict(result.get("details", {}))
        details["recovered_from"] = batch["batch_ref"]
        saved = self.repository.force_apply(
            record_id=target_id,
            state=result["state"],
            payload=result["payload"],
            actor_id=actor.user_id,
            action="recovered_%s" % batch["action"],
            details=details,
            new_version=target_version,
        )
        self.repository.mark_batch(batch["batch_ref"], "recovered")
        return {"applied": True, "batch_ref": batch["batch_ref"], "record": saved}

    def list_batches(self, actor: Actor, record_id: Optional[int] = None, status: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_batches(record_id=record_id, status=status, limit=limit)

    def retained_inputs(self, actor: Actor, record_id: Optional[int] = None, status: str = "retained", limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_retained_inputs(record_id=record_id, status=status, limit=limit)

    def resolve_retained(self, actor: Actor, retained_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self.repository.resolve_retained_input(int(retained_id))
        return {"id": int(retained_id), "status": "resolved"}

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()
