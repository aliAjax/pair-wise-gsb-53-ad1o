import tempfile
from datetime import date
import unittest
from pathlib import Path

from app import build_service
from src.calendar import CalendarEngine
from src.domain import Actor, Conflict, WriteFailure
from src.rules import DomainRules

INTAKE = Actor("officer-1", "intake_officer")
CASE_OFFICER = Actor("officer-1", "case_officer")
CASE_OFFICER_2 = Actor("officer-2", "case_officer")
LEGAL = Actor("legal-1", "legal_rep")
SUPERVISOR = Actor("supervisor-1", "supervisor")


def calendar_case():
    return {
        "applicant_id": "A-1001",
        "case_type": "asylum",
        "received_date": "2026-09-10",
        "deadline_days": 20,
        "representation_active": True,
        "required_documents": ["passport", "statement"],
    }


class CalendarEngineTest(unittest.TestCase):
    def test_natural_days_and_detail(self):
        engine = CalendarEngine({"name": "default", "version": 1, "effective_from": "1970-01-01", "holidays": [], "weekend_days": [5, 6]})
        schedule = engine.intake_schedule(date(2026, 9, 10), 20)
        self.assertEqual(schedule["raw_due_date"], "2026-09-30")
        self.assertEqual(schedule["due_date"], "2026-09-30")
        self.assertFalse(schedule["rolled"])
        self.assertEqual(schedule["steps"][0]["type"], "natural_days")

    def test_weekend_rollforward_detail(self):
        engine = CalendarEngine({"name": "default", "version": 2, "effective_from": "2026-09-20", "holidays": ["2026-09-28"], "weekend_days": [5, 6]})
        appeal = engine.appeal_schedule(date(2026, 10, 9), 30)
        # v2：2026-09-28是节假日，不影响此处；11-08周日顺延到11-09
        self.assertEqual(appeal["raw_due_date"], "2026-11-08")
        self.assertEqual(appeal["due_date"], "2026-11-09")
        self.assertTrue(appeal["rolled"])
        self.assertEqual([s["date"] for s in appeal["steps"] if s["type"] == "rest_skip"], ["2026-11-08"])

    def test_holiday_chain(self):
        engine = CalendarEngine({"name": "default", "version": 2, "effective_from": "2026-09-20", "holidays": ["2026-09-28"], "weekend_days": [5, 6]})
        deemed, steps = engine.roll_forward(date(2026, 9, 26))
        self.assertEqual(deemed.isoformat(), "2026-09-29")
        self.assertEqual([(s["date"], s["reason"]) for s in steps], [
            ("2026-09-26", "weekend"), ("2026-09-27", "weekend"), ("2026-09-28", "holiday"),
        ])


class CalendarServiceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def _open_case(self):
        record = self.service.create(INTAKE, "IMM-C-1", calendar_case())
        payload = record["payload"]
        self.assertEqual(payload["mode"], "calendar")
        self.assertEqual(payload["calendar_version"], 1)
        self.assertEqual(payload["deadline_date"], "2026-09-30")
        self.assertIn("decision_schedule", payload)
        return record

    def test_intake_freezes_calendar_and_new_version_does_not_move_old_case(self):
        record = self._open_case()
        # 主管在09-20发布v2，把09-28设为节假日
        v2 = self.service.publish_calendar(SUPERVISOR, {
            "name": "default", "effective_from": "2026-09-20", "holidays": ["2026-09-28"], "weekend_days": [5, 6], "note": "新增国庆前假期",
        })
        self.assertEqual(v2["version"], 2)

        # 新案受理于09-25，冻结v2
        new_case = dict(calendar_case())
        new_case.update({"applicant_id": "A-1002", "case_type": "family", "received_date": "2026-09-25", "deadline_days": 5})
        record2 = self.service.create(INTAKE, "IMM-C-2", new_case)
        self.assertEqual(record2["payload"]["calendar_version"], 2)

        # 旧案流程：发出补件（允许10个自然日）
        record = self.service.act(CASE_OFFICER, record["id"], record["version"], "submit", {
            "documents": ["passport", "statement"], "submit_date": "2026-09-10",
        })
        record = self.service.act(CASE_OFFICER, record["id"], record["version"], "request_evidence", {
            "evidence_request_date": "2026-09-11", "allowed_days": 10, "evidence_request": "补充无犯罪记录",
        })
        self.assertEqual(record["payload"]["evidence_due_date"], "2026-09-21")

        # 送达日是周六，按冻结的v1顺延（v1无09-28节假日，视为09-28）
        record = self.service.act(CASE_OFFICER, record["id"], record["version"], "record_service", {
            "receipt_id": "RCP-1", "service_date": "2026-09-26",
        })
        payload = record["payload"]
        self.assertEqual(payload["service_receipts"][0]["deemed_date"], "2026-09-28")
        self.assertEqual(payload["evidence_due_date"], "2026-10-08")
        self.assertTrue(payload["service_schedule"]["steps"])
        # 原承诺决定日不变
        self.assertEqual(payload["deadline_date"], "2026-09-30")

        # 决定与上诉窗口：决定日10-09，30自然日后11-08周日顺延到11-09
        record = self.service.act(LEGAL, record["id"], record["version"], "respond", {
            "documents": ["police_clearance"], "response_date": "2026-10-01",
        })
        record = self.service.act(CASE_OFFICER, record["id"], record["version"], "decide", {
            "decision": "granted", "decision_reason": "符合", "decision_date": "2026-10-09",
        })
        self.assertEqual(record["payload"]["appeal_due_date"], "2026-11-09")
        self.assertEqual(record["payload"]["calendar_version"], 1)
        record = self.service.act(LEGAL, record["id"], record["version"], "appeal", {
            "appeal_date": "2026-11-09", "appeal_reason": "程序问题",
        })
        self.assertEqual(record["state"], "appealed")

    def test_concurrent_receipts_first_wins_second_conflicts_and_input_retained(self):
        record = self._open_case()
        record = self.service.act(CASE_OFFICER, record["id"], record["version"], "submit", {
            "documents": ["passport", "statement"], "submit_date": "2026-09-10",
        })
        record = self.service.act(CASE_OFFICER, record["id"], record["version"], "request_evidence", {
            "evidence_request_date": "2026-09-11", "allowed_days": 10, "evidence_request": "补件",
        })
        base_version = record["version"]
        # 两名经办基于同一版本同时提交不同回执
        first = self.service.act(CASE_OFFICER, record["id"], base_version, "record_service", {
            "receipt_id": "RCP-A", "service_date": "2026-09-26",
        })
        self.assertEqual(first["version"], base_version + 1)
        with self.assertRaises(Conflict):
            self.service.act(CASE_OFFICER_2, record["id"], base_version, "record_service", {
                "receipt_id": "RCP-B", "service_date": "2026-09-25",
            })
        retained = self.service.retained_inputs(Actor("auditor", "intake_officer"), record_id=record["id"])
        self.assertEqual(len(retained), 1)
        self.assertEqual(retained[0]["action"], "record_service")
        self.assertEqual(retained[0]["input"]["receipt_id"], "RCP-B")
        conflict_batches = self.service.list_batches(SUPERVISOR, record_id=record["id"], status="conflict")
        self.assertEqual(len(conflict_batches), 1)
        # 案件只顺延了一次
        self.assertEqual(len(first["payload"]["service_receipts"]), 1)
        self.assertEqual(first["payload"]["evidence_due_date"], "2026-10-08")

    def test_duplicate_receipt_does_not_roll_again(self):
        record = self._open_case()
        record = self.service.act(CASE_OFFICER, record["id"], record["version"], "submit", {
            "documents": ["passport", "statement"], "submit_date": "2026-09-10",
        })
        record = self.service.act(CASE_OFFICER, record["id"], record["version"], "request_evidence", {
            "evidence_request_date": "2026-09-11", "allowed_days": 10, "evidence_request": "补件",
        })
        record = self.service.act(CASE_OFFICER, record["id"], record["version"], "record_service", {
            "receipt_id": "RCP-1", "service_date": "2026-09-26",
        })
        version_before = record["version"]
        due_before = record["payload"]["evidence_due_date"]
        # 同一回执再次提交：幂等，版本不变、不重复顺延
        again = self.service.act(CASE_OFFICER_2, record["id"], record["version"], "record_service", {
            "receipt_id": "RCP-1", "service_date": "2026-09-26",
        })
        self.assertEqual(again["version"], version_before)
        self.assertEqual(again["payload"]["evidence_due_date"], due_before)
        self.assertEqual(len(again["payload"]["service_receipts"]), 1)

    def test_write_failure_then_recover_from_last_complete_batch(self):
        record = self._open_case()
        record = self.service.act(CASE_OFFICER, record["id"], record["version"], "submit", {
            "documents": ["passport", "statement"], "submit_date": "2026-09-10",
        })
        record = self.service.act(CASE_OFFICER, record["id"], record["version"], "request_evidence", {
            "evidence_request_date": "2026-09-11", "allowed_days": 10, "evidence_request": "补件",
        })
        base_version = record["version"]

        original_mutate = self.service.repository.mutate
        calls = {"count": 0}

        def flaky_mutate(*args, **kwargs):
            calls["count"] += 1
            import sqlite3
            raise sqlite3.OperationalError("disk I/O error (simulated)")

        self.service.repository.mutate = flaky_mutate
        with self.assertRaises(WriteFailure):
            self.service.act(CASE_OFFICER, record["id"], base_version, "record_service", {
                "receipt_id": "RCP-1", "service_date": "2026-09-26",
            })
        self.service.repository.mutate = original_mutate

        # 案件仍停留在原状态
        stalled = self.service.get_record(CASE_OFFICER, record["id"])
        self.assertEqual(stalled["state"], "evidence_requested")
        self.assertEqual(stalled["version"], base_version)

        # 从最近完整计算批次恢复
        recovery = self.service.recover(SUPERVISOR, record_id=record["id"])
        self.assertTrue(recovery["applied"])
        recovered = recovery["record"]
        self.assertEqual(recovered["version"], base_version + 1)
        self.assertEqual(recovered["payload"]["evidence_due_date"], "2026-10-08")
        self.assertEqual(recovered["payload"]["service_receipts"][0]["receipt_id"], "RCP-1")
        # 恢复结果与正常计算一致
        batches = self.service.list_batches(SUPERVISOR, record_id=record["id"])
        self.assertIn(batches[0]["status"], ("recovered",))

        # 再次恢复幂等
        second = self.service.recover(SUPERVISOR, batch_ref=batches[0]["batch_ref"])
        self.assertFalse(second["applied"])

    def test_concurrent_calendar_publish_first_wins_draft_retained(self):
        self.service.publish_calendar(SUPERVISOR, {
            "name": "default", "effective_from": "2026-09-20", "holidays": [], "weekend_days": [5, 6],
        })
        draft = {"name": "default", "effective_from": "2026-11-01", "holidays": ["2026-12-25"], "weekend_days": [5, 6]}
        with self.assertRaises(Conflict):
            self.service.publish_calendar(Actor("supervisor-2", "supervisor"), draft, expected_version=1)
        calendars = self.service.list_calendars(SUPERVISOR)
        self.assertEqual([c["version"] for c in calendars], [2, 1])
        retained = self.service.retained_inputs(SUPERVISOR)
        self.assertEqual(retained[0]["scope"], "calendar")
        self.assertEqual(retained[0]["input"]["holidays"], ["2026-12-25"])

    def test_backfill_legacy_case_by_received_date_without_rewriting(self):
        # 旧案（整数日序号模式）没有日历版本
        legacy = {
            "applicant_id": "A-legacy", "case_type": "work", "received_day": 100,
            "deadline_days": 30, "response_day": 110, "representation_active": True,
            "required_documents": ["passport"],
        }
        record = self.service.create(INTAKE, "IMM-L-1", legacy)
        self.assertNotIn("calendar_version", record["payload"])
        self.service.publish_calendar(SUPERVISOR, {
            "name": "default", "effective_from": "2026-01-01", "holidays": [], "weekend_days": [5, 6],
        })
        # 旧案无受理日期时按受理日期回填：无received_date则落最新版本
        filled = self.service.backfill_calendar(SUPERVISOR, record["id"], record["version"])
        self.assertEqual(filled["payload"]["calendar_version"], 2)
        self.assertTrue(filled["payload"]["calendar_backfilled"])
        # 原承诺不被改写
        self.assertEqual(filled["payload"]["deadline_day"], 130)
        self.assertEqual(filled["state"], "draft")
        with self.assertRaises(Conflict):
            self.service.backfill_calendar(SUPERVISOR, record["id"], filled["version"], calendar_version=1)

    def test_backfill_date_case_missing_version_uses_received_date(self):
        record = self._open_case()
        # 发布v2后，人为剥离旧案冻结版本（模拟迁移中的缺口）
        payload = dict(record["payload"])
        payload.pop("calendar_version")
        payload.pop("calendar_snapshot")
        stripped = self.service.repository.patch_payload(
            record["id"], record["version"], payload, "migration", "calendar_backfill", {"note": "模拟缺失"}
        )
        self.service.publish_calendar(SUPERVISOR, {
            "name": "default", "effective_from": "2026-09-20", "holidays": ["2026-09-28"], "weekend_days": [5, 6],
        })
        filled = self.service.backfill_calendar(SUPERVISOR, stripped["id"], stripped["version"])
        # 按受理日期2026-09-10回填，应选v1而不是最新v2
        self.assertEqual(filled["payload"]["calendar_version"], 1)
        self.assertEqual(filled["payload"]["deadline_date"], "2026-09-30")


if __name__ == "__main__":
    unittest.main()
