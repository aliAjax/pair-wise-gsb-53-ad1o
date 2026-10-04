"""移民案件期限与材料管理领域规则与状态转换。

期限有两种计算模式：
- calendar（自然日 + 冻结日历版本）：受理时冻结日历，所有期限按自然日推算，
  末日遇休息日顺延，计算步骤随案件保存；
- legacy（整数自然日序号）：历史案件兼容路径，不参与日历顺延。
"""
from typing import Any, Dict, Iterable, Optional, Tuple

from .calendar import CalendarEngine, parse_iso_date
from .domain import Actor, Conflict, ValidationError, boolean, choice, integer, number, text, text_list


INITIAL_STATE = "draft"
CREATE_ROLES = {'intake_officer'}
ACTION_ROLES = {
    'submit': {'legal_rep', 'case_officer'},
    'request_evidence': {'case_officer'},
    'record_service': {'case_officer'},
    'respond': {'legal_rep'},
    'decide': {'case_officer', 'supervisor'},
    'appeal': {'legal_rep'},
    'close': {'supervisor'},
}
TRANSITIONS = {
    'submit': {'draft': 'submitted'},
    'request_evidence': {'submitted': 'evidence_requested'},
    'record_service': {'evidence_requested': 'evidence_requested'},
    'respond': {'evidence_requested': 'response_received'},
    'decide': {'submitted': 'decided', 'response_received': 'decided'},
    'appeal': {'decided': 'appealed'},
    'close': {'decided': 'closed', 'appealed': 'closed'},
}


class DomainRules:
    INITIAL_STATE = INITIAL_STATE

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or ACTION_ROLES.get(action, set())

    @staticmethod
    def is_calendar_payload(payload: Dict[str, Any]) -> bool:
        return isinstance(payload, dict) and ("received_date" in payload or "decision_schedule" in payload)

    def validate_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        text(p, "applicant_id")
        choice(p, "case_type", ["asylum", "family", "work"])
        boolean(p, "representation_active")
        text_list(p, "required_documents", 1)
        integer(p, "deadline_days", 1)
        if self.is_calendar_payload(p):
            parse_iso_date(p.get("received_date"), "received_date")
        else:
            integer(p, "received_day", 0)
            integer(p, "response_day", 0)
        return p
    def prepare_create(self, payload: Dict[str, Any], engine: Optional[CalendarEngine] = None) -> Dict[str, Any]:
        p = self.validate_create(payload)
        if self.is_calendar_payload(p):
            if engine is None:
                raise ValidationError("自然日案件必须冻结一个日历版本")
            received = parse_iso_date(p["received_date"], "received_date")
            schedule = engine.intake_schedule(received, int(p["deadline_days"]))
            p["mode"] = "calendar"
            p["received_date"] = schedule["start_date"]
            p["decision_schedule"] = schedule
            p["deadline_date"] = schedule["due_date"]
            p["calendar_version"] = engine.version
            p["calendar_snapshot"] = engine.snapshot()
        else:
            p["mode"] = "legacy"
            p["deadline_day"] = int(p["received_day"]) + int(p["deadline_days"])
            p["days_remaining"] = int(p["deadline_day"]) - int(p["response_day"])
            p["overdue"] = p["days_remaining"] < 0
        p["submitted_documents"] = []
        p["missing_documents"] = list(p["required_documents"])
        return p

    def check_create_conflicts(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> None:
        for item in existing:
            if item["state"] not in {"closed", "decided"} and item["payload"].get("applicant_id") == payload.get("applicant_id") and item["payload"].get("case_type") == payload.get("case_type"):
                raise Conflict("同一申请人同类型案件仍在处理中")

    def require_transition(self, record: Dict[str, Any], action: str) -> str:
        allowed = TRANSITIONS.get(action, {}).get(record["state"])
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any], engine: Optional[CalendarEngine] = None) -> Tuple[str, Dict[str, Any], str]:
        new_state = self.require_transition(record, action)
        data = dict(data or {})
        p = dict(record["payload"])
        if p.get("mode") == "calendar":
            if engine is None:
                raise ValidationError("案件缺少冻结日历版本，需先回填日历版本")
            changes, summary = self._apply_calendar(p, action, data, engine)
        else:
            changes, summary = self._apply_legacy(p, action, data)
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action)

    def _apply_legacy(self, p: Dict[str, Any], action: str, data: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
        changes: Dict[str, Any] = {}
        summary = ""
        if action == "submit":
            docs = text_list(data, "documents", 1)
            missing = [doc for doc in p["required_documents"] if doc not in docs]
            if missing and not boolean(data, "supervisor_waiver"):
                raise ValidationError("缺少材料：" + ", ".join(missing))
            if p["overdue"] and not boolean(data, "supervisor_waiver"):
                raise ValidationError("案件已超过提交期限")
            changes["submitted_documents"] = docs
            changes["missing_documents"] = missing
            changes["waiver_used"] = boolean(data, "supervisor_waiver")
            summary = "申请材料已提交"
        elif action == "request_evidence":
            request_day = integer(data, "evidence_request_day", p["response_day"])
            allowed_days = integer(data, "allowed_days", 1)
            changes["evidence_request_day"] = request_day
            changes["evidence_due_day"] = request_day + allowed_days
            changes["evidence_request"] = text(data, "evidence_request")
            summary = "补件要求已发出"
        elif action == "respond":
            docs = text_list(data, "documents", 1)
            if int(data.get("response_day", p["response_day"])) > int(p["evidence_due_day"]):
                raise ValidationError("补件回应超过期限")
            changes["response_day"] = int(data["response_day"])
            changes["evidence_documents"] = docs
            summary = "补件已回应"
        elif action == "decide":
            changes["decision"] = choice(data, "decision", ["granted", "denied", "withdrawn"])
            changes["decision_reason"] = text(data, "decision_reason")
            summary = "案件已作出决定"
        elif action == "appeal":
            appeal_day = integer(data, "appeal_day", 0)
            if appeal_day > int(p["deadline_day"]) + 30:
                raise ValidationError("上诉窗口已关闭")
            changes["appeal_day"] = appeal_day
            changes["appeal_reason"] = text(data, "appeal_reason")
            summary = "上诉已登记"
        elif action == "close":
            changes["closure_note"] = text(data, "closure_note")
            summary = "案件归档"
        return changes, summary

    def _apply_calendar(self, p: Dict[str, Any], action: str, data: Dict[str, Any], engine: CalendarEngine) -> Tuple[Dict[str, Any], str]:
        changes: Dict[str, Any] = {}
        summary = ""
        if action == "submit":
            docs = text_list(data, "documents", 1)
            missing = [doc for doc in p["required_documents"] if doc not in docs]
            submit_date = parse_iso_date(data.get("submit_date", p["received_date"]), "submit_date")
            deadline = parse_iso_date(p["deadline_date"], "deadline_date")
            waiver = boolean(data, "supervisor_waiver")
            if missing and not waiver:
                raise ValidationError("缺少材料：" + ", ".join(missing))
            if submit_date > deadline and not waiver:
                raise ValidationError("案件已超过决定期限（%s）" % p["deadline_date"])
            changes["submit_date"] = submit_date.isoformat()
            changes["submitted_documents"] = docs
            changes["missing_documents"] = missing
            changes["waiver_used"] = waiver
            summary = "申请材料已提交"
        elif action == "request_evidence":
            request_date = parse_iso_date(data.get("evidence_request_date"), "evidence_request_date")
            allowed_days = integer(data, "allowed_days", 1)
            schedule = engine.deadline_after(request_date, allowed_days, kind="evidence_response")
            changes["allowed_days"] = allowed_days
            changes["evidence_request_date"] = schedule["start_date"]
            changes["evidence_schedule"] = schedule
            changes["evidence_due_date"] = schedule["due_date"]
            changes["evidence_request"] = text(data, "evidence_request")
            summary = "补件要求已发出，回应期限%s" % schedule["due_date"]
        elif action == "record_service":
            receipts = list(p.get("service_receipts", []))
            receipt_id = text(data, "receipt_id")
            if any(item.get("receipt_id") == receipt_id for item in receipts):
                raise Conflict("送达回执已存在，不重复顺延：%s" % receipt_id)
            service_date = parse_iso_date(data.get("service_date"), "service_date")
            deemed_date, roll_steps = engine.roll_forward(service_date)
            schedule = engine.deadline_after(deemed_date, int(p["allowed_days"]), kind="evidence_response")
            receipts.append({
                "receipt_id": receipt_id,
                "service_date": service_date.isoformat(),
                "deemed_date": deemed_date.isoformat(),
                "rolled": deemed_date != service_date,
                "roll_steps": roll_steps,
                "response_schedule": schedule,
                "response_due_date": schedule["due_date"],
            })
            changes["service_receipts"] = receipts
            changes["evidence_due_date"] = schedule["due_date"]
            changes["service_schedule"] = {
                "kind": "service_rollforward",
                "calendar": engine.snapshot(),
                "service_date": service_date.isoformat(),
                "deemed_date": deemed_date.isoformat(),
                "steps": roll_steps,
                "response_schedule": schedule,
            }
            summary = "补件送达日%s，按日历视为%s，回应期限%s" % (
                service_date.isoformat(), deemed_date.isoformat(), schedule["due_date"],
            )
        elif action == "respond":
            docs = text_list(data, "documents", 1)
            response_date = parse_iso_date(data.get("response_date"), "response_date")
            if response_date > parse_iso_date(p["evidence_due_date"], "evidence_due_date"):
                raise ValidationError("补件回应超过期限（%s）" % p["evidence_due_date"])
            changes["response_date"] = response_date.isoformat()
            changes["evidence_documents"] = docs
            summary = "补件已回应"
        elif action == "decide":
            decision_date = parse_iso_date(data.get("decision_date"), "decision_date")
            appeal_schedule = engine.appeal_schedule(decision_date)
            changes["decision"] = choice(data, "decision", ["granted", "denied", "withdrawn"])
            changes["decision_reason"] = text(data, "decision_reason")
            changes["decision_date"] = decision_date.isoformat()
            changes["appeal_schedule"] = appeal_schedule
            changes["appeal_due_date"] = appeal_schedule["due_date"]
            summary = "案件已作出决定，上诉窗口至%s" % appeal_schedule["due_date"]
        elif action == "appeal":
            appeal_date = parse_iso_date(data.get("appeal_date"), "appeal_date")
            if appeal_date > parse_iso_date(p["appeal_due_date"], "appeal_due_date"):
                raise ValidationError("上诉窗口已关闭（%s）" % p["appeal_due_date"])
            changes["appeal_date"] = appeal_date.isoformat()
            changes["appeal_reason"] = text(data, "appeal_reason")
            summary = "上诉已登记"
        elif action == "close":
            changes["closure_note"] = text(data, "closure_note")
            summary = "案件归档"
        return changes, summary
