"""移民案件期限与材料管理领域规则与状态转换。

所有日期计算都使用受理时冻结的日历版本（payload["calendar_version"]），
办事处随后发布的新版本不会改写既有案件的承诺日期。
"""
from datetime import date
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import calendar as cal
from .domain import Conflict, ValidationError, boolean, choice, integer, text, text_list


INITIAL_STATE = "draft"
CREATE_ROLES = {'intake_officer'}
ACTION_ROLES = {
    'submit': {'legal_rep', 'case_officer'},
    'request_evidence': {'case_officer'},
    'respond': {'legal_rep'},
    'serve_evidence': {'case_officer'},
    'decide': {'case_officer', 'supervisor'},
    'appeal': {'legal_rep'},
    'close': {'supervisor'},
}
TRANSITIONS = {
    'submit': {'draft': 'submitted'},
    'request_evidence': {'submitted': 'evidence_requested'},
    'respond': {'evidence_requested': 'response_received'},
    'serve_evidence': {'response_received': 'response_received'},
    'decide': {'submitted': 'decided', 'response_received': 'decided'},
    'appeal': {'decided': 'appealed'},
    'close': {'decided': 'closed', 'appealed': 'closed'},
}
APPEAL_WINDOW_DAYS = 30


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
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    def validate_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        text(p, "applicant_id")
        choice(p, "case_type", ["asylum", "family", "work"])
        cal.parse_iso_date(p.get("received_date"), "received_date")
        integer(p, "deadline_days", 1)
        boolean(p, "representation_active")
        text_list(p, "required_documents", 1)
        return p

    def prepare_create(self, payload: Dict[str, Any], calendar: cal.CalendarVersion) -> Dict[str, Any]:
        """受理：冻结日历版本，按自然日计算承诺决定日。"""
        p = self.validate_create(payload)
        received = cal.parse_iso_date(p["received_date"], "received_date")
        deadline_days = int(p["deadline_days"])
        decision_schedule = cal.natural_deadline(calendar, received, deadline_days, label="promised_decision")
        p["received_date"] = cal.iso(received)
        p["calendar_version"] = calendar.version
        p["deadline_days"] = deadline_days
        p["promised_decision"] = decision_schedule
        p["promised_decision_date"] = decision_schedule["effective_date"]
        p["submitted_documents"] = []
        p["missing_documents"] = list(p["required_documents"])
        return p

    def check_create_conflicts(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> None:
        for item in existing:
            if item["state"] not in {"closed", "decided"} and item["payload"].get("applicant_id") == payload.get("applicant_id") and item["payload"].get("case_type") == payload.get("case_type"):
                raise Conflict("同一申请人同类型案件仍在处理中")

    def frozen_calendar_version(self, record: Dict[str, Any]) -> Optional[int]:
        value = record["payload"].get("calendar_version")
        return int(value) if value is not None else None

    def require_transition(self, record: Dict[str, Any], action: str) -> str:
        allowed = TRANSITIONS.get(action, {}).get(record["state"])
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any], calendar: Optional[cal.CalendarVersion] = None) -> Tuple[str, Dict[str, Any], str, Dict[str, Any]]:
        new_state = self.require_transition(record, action)
        data = dict(data or {})
        p = dict(record["payload"])
        changes: Dict[str, Any] = {}
        summary = ""
        extra: Dict[str, Any] = {}
        if action == "submit":
            docs = text_list(data, "documents", 1)
            missing = [doc for doc in p["required_documents"] if doc not in docs]
            if missing and not boolean(data, "supervisor_waiver"):
                raise ValidationError("缺少材料：" + ", ".join(missing))
            changes["submitted_documents"] = docs
            changes["missing_documents"] = missing
            changes["waiver_used"] = boolean(data, "supervisor_waiver")
            summary = "申请材料已提交"
        elif action == "request_evidence":
            calendar = self._require_calendar(record, calendar)
            request_day = cal.parse_iso_date(data.get("evidence_request_date"), "evidence_request_date")
            allowed_days = integer(data, "allowed_days", 1)
            text(data, "evidence_request")
            # 补件要求送达遇休息日顺延，补件期限自送达生效日按自然日起算。
            service = cal.service_effective_date(calendar, request_day)
            due = cal.natural_deadline(calendar, cal.parse_iso_date(service["effective_date"], "effective_date"), allowed_days, label="evidence_due")
            changes["evidence_request_date"] = cal.iso(request_day)
            changes["evidence_service"] = service
            changes["evidence_effective_date"] = service["effective_date"]
            changes["allowed_days"] = allowed_days
            changes["evidence_due"] = due
            changes["evidence_due_date"] = due["effective_date"]
            changes["evidence_request"] = data["evidence_request"].strip()
            summary = "补件要求已发出"
        elif action == "respond":
            docs = text_list(data, "documents", 1)
            response_day = cal.parse_iso_date(data.get("response_date"), "response_date")
            due_day = cal.parse_iso_date(p["evidence_due_date"], "evidence_due_date")
            if response_day > due_day:
                raise ValidationError("补件回应超过期限（%s）" % p["evidence_due_date"])
            changes["response_date"] = cal.iso(response_day)
            changes["evidence_documents"] = docs
            summary = "补件已回应"
        elif action == "serve_evidence":
            calendar = self._require_calendar(record, calendar)
            receipt = self.build_receipt(record, data, calendar)
            changes["evidence_receipts"] = list(p.get("evidence_receipts", [])) + [
                {"receipt_key": receipt["receipt_key"], **receipt["calc_detail"]}
            ]
            changes["last_evidence_service_date"] = receipt["effective_date"]
            extra["receipt"] = receipt
            summary = "补件送达回执已登记"
        elif action == "decide":
            calendar = self._require_calendar(record, calendar)
            decision_day = cal.parse_iso_date(data.get("decision_date"), "decision_date")
            window = cal.natural_deadline(calendar, decision_day, APPEAL_WINDOW_DAYS, label="appeal_window")
            changes["decision"] = choice(data, "decision", ["granted", "denied", "withdrawn"])
            changes["decision_reason"] = text(data, "decision_reason")
            changes["decision_date"] = cal.iso(decision_day)
            changes["appeal_window"] = window
            changes["appeal_window_date"] = window["effective_date"]
            summary = "案件已作出决定"
        elif action == "appeal":
            appeal_day = cal.parse_iso_date(data.get("appeal_date"), "appeal_date")
            window_day = cal.parse_iso_date(p.get("appeal_window_date"), "appeal_window_date")
            if appeal_day > window_day:
                raise ValidationError("上诉窗口已于%s关闭" % p["appeal_window_date"])
            changes["appeal_date"] = cal.iso(appeal_day)
            changes["appeal_reason"] = text(data, "appeal_reason")
            summary = "上诉已登记"
        elif action == "close":
            changes["closure_note"] = text(data, "closure_note")
            summary = "案件归档"
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action), extra

    def build_receipt(self, record: Dict[str, Any], data: Dict[str, Any], calendar: cal.CalendarVersion) -> Dict[str, Any]:
        """计算补件送达回执，重复键或重复送达日均拒绝重复顺延。"""
        receipt_key = text(data, "receipt_key")
        served = cal.parse_iso_date(data.get("served_date"), "served_date")
        existing: List[Dict[str, Any]] = record["payload"].get("evidence_receipts", [])
        for item in existing:
            if item.get("receipt_key") == receipt_key:
                raise Conflict("回执%s已存在，重复回执不重复顺延" % receipt_key)
            if item.get("raw_date") == cal.iso(served):
                raise Conflict("%s的送达回执已登记，重复回执不重复顺延" % cal.iso(served))
        detail = cal.service_effective_date(calendar, served)
        return {
            "receipt_key": receipt_key,
            "served_date": cal.iso(served),
            "effective_date": detail["effective_date"],
            "calendar_version": calendar.version,
            "calc_detail": detail,
        }

    @staticmethod
    def _require_calendar(record: Dict[str, Any], calendar: Optional[cal.CalendarVersion]) -> cal.CalendarVersion:
        if calendar is None:
            raise ValidationError("案件缺少冻结日历版本，请先按受理日期回填")
        return calendar
