"""业务用例编排、权限检查、乐观并发、计算批次与审计。"""
from typing import Any, Dict, List, Optional

from . import calendar as cal
from .audit import AuditRecorder
from .domain import Actor, Conflict, PermissionDenied, text
from .repository import Repository
from .rules import DomainRules


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)

    # 测试钩子：置为异常实例时，让下一次落库前模拟写入失败。
    inject_failure: Optional[Exception] = None

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    def _frozen_calendar(self, record: Dict[str, Any]) -> Optional[cal.CalendarVersion]:
        version = self.rules.frozen_calendar_version(record)
        if version is None:
            return None
        return self.repository.get_calendar(version)

    # ------------------------------------------------------------ case intake
    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        data = payload or {}
        received = cal.parse_iso_date(data.get("received_date"), "received_date")
        # 受理时刻冻结当天有效日历版本，之后新发布的版本与本案无关。
        calendar = self.repository.effective_calendar(received)
        prepared = self.rules.prepare_create(data, calendar)
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        record = self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id)
        self.audit.note(
            record["id"],
            actor.user_id,
            "calendar_frozen",
            {"calendar_version": calendar.version, "received_date": cal.iso(received)},
        )
        return record

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get(record_id)

    # --------------------------------------------------------------- calendars
    def publish_calendar(self, actor: Actor, payload: Dict[str, Any], expected_version: Optional[int] = None) -> Dict[str, Any]:
        """主管发布日历新版本；只影响发布后受理的新案。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if actor.role not in {"supervisor", "admin"}:
            raise PermissionDenied("仅主管可以发布节假日日历")
        prepared = cal.validate_payload(payload or {})
        current = self.repository.latest_calendar_version()
        if expected_version is not None and int(expected_version) != current:
            self.repository.retain_calendar_draft(prepared, actor.user_id, int(expected_version), "日历版本冲突，当前最新版本为%s" % current)
            raise Conflict("日历版本冲突，当前最新版本为%s；输入已保留为草稿" % current)
        try:
            return self.repository.publish_calendar(prepared, actor.user_id, expected_version)
        except Conflict:
            # 并发下先完成的一方已提交新版本，后到一方保留输入。
            self.repository.retain_calendar_draft(prepared, actor.user_id, expected_version, "日历版本冲突（并发发布）")
            raise

    def list_calendars(self, actor: Actor) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_calendars()

    def list_calendar_drafts(self, actor: Actor) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if actor.role not in {"supervisor", "admin"}:
            raise PermissionDenied("仅主管可以查看日历草稿")
        return self.repository.list_calendar_drafts()

    # ---------------------------------------------------------------- actions
    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self.repository.get(record_id)
        self.rules.require_transition(record, action)
        calendar = self._frozen_calendar(record)
        new_state, new_payload, summary, extra = self.rules.apply_action(record, action, data or {}, calendar)
        if action == "serve_evidence":
            return self._serve_with_batch(actor, record, int(expected_version), new_payload, data or {}, extra["receipt"], summary)
        return self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details={"summary": summary, "input": data or {}, "from": record["state"], "to": new_state,
                     "calendar_version": self.rules.frozen_calendar_version(record)},
        )

    def _serve_with_batch(self, actor: Actor, record: Dict[str, Any], expected_version: int, new_payload: Dict[str, Any], raw_input: Dict[str, Any], receipt: Dict[str, Any], summary: str) -> Dict[str, Any]:
        """两名经办同时提交回执：先完成计算的一方生效，后到一方看到冲突且输入被保留。"""
        batch_id = self.repository.open_batch(
            record_id=record["id"],
            kind="serve_evidence",
            batch_input={"expected_version": expected_version, "input": raw_input, "receipt": receipt},
            snapshot={"state": record["state"], "version": record["version"], "payload": record["payload"]},
            created_by=actor.user_id,
        )
        failure = self._take_failure()
        if failure is not None:
            self.repository.fail_batch(batch_id, "模拟写入失败：%s" % failure)
            raise failure
        try:
            return self.repository.apply_receipt(
                record_id=record["id"],
                expected_version=expected_version,
                state=record["state"],
                payload=new_payload,
                actor_id=actor.user_id,
                details={"summary": summary, "input": raw_input, "receipt_key": receipt["receipt_key"],
                         "calc_detail": receipt["calc_detail"], "batch_id": batch_id},
                receipt=receipt,
                batch_id=batch_id,
            )
        except Conflict as exc:
            # 后到一方：保留输入供其刷新版本后重试，不改动已生效结果。
            self.repository.retain_receipt(record["id"], receipt["receipt_key"], actor.user_id, raw_input, str(exc))
            self.repository.fail_batch(batch_id, str(exc), terminal=True)
            raise

    def _take_failure(self) -> Optional[Exception]:
        failure = Service.inject_failure
        Service.inject_failure = None
        return failure

    def list_receipts(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self.repository.get(record_id)
        return self.repository.list_receipts(record_id)

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()

    def batches(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self.repository.get(record_id)
        return self.repository.list_batches(record_id)

    # ------------------------------------------------ backfill /  recovery
    def backfill_calendar_version(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        """旧案件缺日历版本时按受理日期回填，重算时使用回填版本但保留原始承诺日期。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if actor.role not in {"supervisor", "admin"}:
            raise PermissionDenied("仅主管可以回填日历版本")
        record = self.repository.get(record_id)
        payload = record["payload"]
        if payload.get("calendar_version") is not None:
            return {"record_id": record_id, "calendar_version": int(payload["calendar_version"]), "backfilled": False}
        received = cal.parse_iso_date(payload.get("received_date"), "received_date")
        calendar = self.repository.effective_calendar(received)
        promised = payload.get("promised_decision_date")
        if promised is None:
            schedule = cal.natural_deadline(calendar, received, int(payload.get("deadline_days", 0)), label="promised_decision")
        else:
            # 原承诺不被改写：回填仅补齐计算依据，承诺日期维持历史值。
            raw = cal.parse_iso_date(promised, "promised_decision_date")
            schedule = cal.natural_deadline(calendar, received, int(payload.get("deadline_days", 0)), label="promised_decision")
            schedule["effective_date"] = promised
            schedule["raw_date"] = promised
            schedule["rolled"] = False
            schedule["steps"] = []
            schedule["preserved"] = True
        new_payload = dict(payload)
        new_payload["calendar_version"] = calendar.version
        new_payload["backfilled_calendar"] = True
        new_payload["promised_decision"] = schedule
        new_payload["promised_decision_date"] = promised if promised is not None else schedule["effective_date"]
        result = self.repository.mutate(
            record_id=record_id,
            expected_version=record["version"],
            state=record["state"],
            payload=new_payload,
            actor_id=actor.user_id,
            action="backfill_calendar",
            details={"calendar_version": calendar.version, "received_date": cal.iso(received), "preserved_promise": promised is not None},
        )
        return {"record_id": record_id, "calendar_version": calendar.version, "backfilled": True, "record": result}

    def recover(self, actor: Optional[Actor] = None) -> Dict[str, Any]:
        """从未完成的计算批次恢复：以最近完整（committed）批次的快照重建案件状态。"""
        if actor is not None:
            actor = self._actor(actor)
            self._ensure_known_role(actor)
            if actor.role not in {"supervisor", "admin"}:
                raise PermissionDenied("仅主管可以执行批次恢复")
        report = {"recovered": [], "orphaned": []}
        for batch in self.repository.pending_batches():
            record_id = int(batch["record_id"])
            # 未完成批次的快照即“最近完整计算批次之后”的状态；缺失时再找最近committed批次。
            snapshot = batch.get("snapshot")
            source = batch["batch_id"]
            if not snapshot:
                last_good = self.repository.latest_committed_batch(record_id)
                if last_good is not None and last_good["snapshot"]:
                    snapshot = last_good["snapshot"]
                    source = last_good["batch_id"]
            if snapshot:
                self.repository.force_state(
                    record_id=record_id,
                    state=snapshot["state"],
                    payload=snapshot["payload"],
                    actor_id=actor.user_id if actor else "system",
                    action="batch_recovered",
                    details={"failed_batch": batch["batch_id"], "restored_from": source},
                )
                self.repository.mark_batch_recovered(batch["batch_id"], source)
                report["recovered"].append({"batch_id": batch["batch_id"], "record_id": record_id, "restored_from": source})
            else:
                # 没有任何完整批次可用于恢复。
                self.repository.fail_batch(batch["batch_id"], "无完整批次可恢复", terminal=True)
                report["orphaned"].append({"batch_id": batch["batch_id"], "record_id": record_id})
        return report
