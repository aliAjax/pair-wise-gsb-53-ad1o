import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied, ValidationError


CREATE_DATA = {
    'applicant_id': 'A-900', 'case_type': 'family',
    'received_date': '2026-01-05', 'deadline_days': 30,
    'representation_active': True,
    'required_documents': ['passport', 'sponsor_letter'],
}
SUBMIT = ('submit', 'legal_rep', {'documents': ['passport', 'sponsor_letter']})
REQUEST = ('request_evidence', 'case_officer', {'evidence_request_date': '2026-01-10', 'allowed_days': 10, 'evidence_request': '补充收入证明'})
SUPERVISOR = Actor("boss", "supervisor")
OFFICER = Actor("operator", "case_officer")


class FailureTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp.name) / "test.db")
        self.service = build_service(self.db_path)

    def tearDown(self):
        self.temp.cleanup()

    def _submitted_record(self):
        record = self.service.create(Actor("creator", "intake_officer"), "IMM-29001", CREATE_DATA)
        return self.service.act(Actor("operator", SUBMIT[1]), record["id"], record["version"], SUBMIT[0], SUBMIT[2])

    def _responded_record(self):
        record = self._submitted_record()
        record = self.service.act(Actor("operator", REQUEST[1]), record["id"], record["version"], REQUEST[0], REQUEST[2])
        return self.service.act(
            Actor("rep", "legal_rep"), record["id"], record["version"], "respond",
            {"response_date": "2026-01-20", "documents": ["income_proof"]},
        )

    def test_permission_and_duplicate(self):
        with self.assertRaises(PermissionDenied):
            self.service.create(Actor("outsider", "outsider"), "IMM-29001", CREATE_DATA)
        self.service.create(Actor("creator", "intake_officer"), "IMM-29001", CREATE_DATA)
        with self.assertRaises(Conflict):
            self.service.create(Actor("creator", "intake_officer"), "IMM-29001", CREATE_DATA)

    def test_stale_version_is_rejected(self):
        record = self._submitted_record()
        with self.assertRaises(Conflict):
            self.service.act(Actor("operator", REQUEST[1]), record["id"], record["version"] - 1, REQUEST[0], REQUEST[2])

    def test_concurrent_receipts_first_wins_loser_conflicts_and_input_retained(self):
        record = self._responded_record()
        first = self.service.act(
            OFFICER, record["id"], record["version"], "serve_evidence",
            {"receipt_key": "R-A", "served_date": "2026-01-17"},  # 周六 -> 01-19
        )
        self.assertEqual(first["payload"]["last_evidence_service_date"], "2026-01-19")
        # 另一名经办基于同一旧版本提交：先到者已生效，后到者看到版本冲突。
        with self.assertRaises(Conflict):
            self.service.act(
                Actor("officer2", "case_officer"), record["id"], record["version"], "serve_evidence",
                {"receipt_key": "R-B", "served_date": "2026-01-18"},
            )
        receipts = self.service.list_receipts(OFFICER, record["id"])
        applied = [r for r in receipts if r["status"] == "applied"]
        retained = [r for r in receipts if r["status"] == "retained_conflict"]
        self.assertEqual(len(applied), 1)
        self.assertEqual(applied[0]["receipt_key"], "R-A")
        self.assertEqual(len(retained), 1)
        self.assertEqual(retained[0]["receipt_key"], "R-B")
        self.assertEqual(retained[0]["retained_input"]["served_date"], "2026-01-18")
        # 已生效结果不被后到一方改写。
        self.assertEqual(self.service.get_record(OFFICER, record["id"])["payload"]["last_evidence_service_date"], "2026-01-19")

    def test_duplicate_receipt_does_not_roll_again(self):
        record = self._responded_record()
        self.service.act(OFFICER, record["id"], record["version"], "serve_evidence",
                         {"receipt_key": "R-1", "served_date": "2026-01-17"})
        refreshed = self.service.get_record(OFFICER, record["id"])
        with self.assertRaises(Conflict):
            self.service.act(OFFICER, refreshed["id"], refreshed["version"], "serve_evidence",
                             {"receipt_key": "R-1", "served_date": "2026-01-17"})
        applied = [r for r in self.service.list_receipts(OFFICER, record["id"]) if r["status"] == "applied"]
        self.assertEqual(len(applied), 1)

    def test_new_calendar_version_does_not_change_existing_promises(self):
        record = self.service.create(Actor("creator", "intake_officer"), "IMM-29001", CREATE_DATA)
        old_promise = record["payload"]["promised_decision_date"]
        self.service.publish_calendar(
            SUPERVISOR,
            {"name": "2026春节", "effective_from": "2026-02-01",
             "holidays": ["2026-02-02", "2026-02-03"], "weekend": [5, 6]},
            expected_version=1,
        )
        self.assertEqual(self.service.get_record(OFFICER, record["id"])["payload"]["promised_decision_date"], old_promise)
        # 新案受理冻结新版本。
        new_case = self.service.create(
            Actor("creator", "intake_officer"), "IMM-29002",
            {**CREATE_DATA, "applicant_id": "A-901", "received_date": "2026-02-01", "deadline_days": 1},
        )
        self.assertEqual(new_case["payload"]["calendar_version"], 2)
        self.assertEqual(new_case["payload"]["promised_decision_date"], "2026-02-04")

    def test_concurrent_calendar_publish_conflict_keeps_draft(self):
        self.service.publish_calendar(SUPERVISOR, {"name": "v2", "effective_from": "2026-03-01"}, expected_version=1)
        with self.assertRaises(Conflict):
            self.service.publish_calendar(SUPERVISOR, {"name": "v3-late", "effective_from": "2026-04-01"}, expected_version=1)
        drafts = self.service.list_calendar_drafts(SUPERVISOR)
        self.assertTrue(any(d["name"] == "v3-late" for d in drafts))

    def test_failed_write_recovers_from_last_complete_batch(self):
        record = self._responded_record()
        import src.service as service_module
        service_module.Service.inject_failure = RuntimeError("disk full")
        with self.assertRaises(RuntimeError):
            self.service.act(OFFICER, record["id"], record["version"], "serve_evidence",
                             {"receipt_key": "R-X", "served_date": "2026-01-17"})
        # 失败后案件保持送达前状态。
        stale = self.service.get_record(OFFICER, record["id"])
        self.assertNotIn("last_evidence_service_date", stale["payload"])
        # 新进程启动时自动恢复未完成批次，状态与最近完整批次一致。
        rebuilt = build_service(self.db_path)
        recovered_record = rebuilt.get_record(OFFICER, record["id"])
        self.assertEqual(recovered_record["state"], "response_received")
        self.assertNotIn("last_evidence_service_date", recovered_record["payload"])
        timeline = [event["action"] for event in rebuilt.timeline(OFFICER, record["id"])]
        self.assertIn("batch_recovered", timeline)
        # 恢复后可重新提交并生效。
        again = rebuilt.act(OFFICER, record["id"], recovered_record["version"], "serve_evidence",
                            {"receipt_key": "R-X", "served_date": "2026-01-17"})
        self.assertEqual(again["payload"]["last_evidence_service_date"], "2026-01-19")

    def test_legacy_case_backfills_calendar_by_received_date_without_rewriting_promise(self):
        # 模拟旧案件：无 calendar_version，但有历史承诺日期。
        record = self.service.create(Actor("creator", "intake_officer"), "IMM-LEGACY", CREATE_DATA)
        legacy_payload = dict(record["payload"])
        legacy_version = record["version"]
        legacy_payload.pop("calendar_version")
        legacy_payload["promised_decision_date"] = "2026-03-01"
        self.service.repository.force_state(record["id"], record["state"], legacy_payload, "migration",
                                            "legacy_import", {"summary": "导入旧案"})
        legacy = self.service.get_record(OFFICER, record["id"])
        result = self.service.backfill_calendar_version(SUPERVISOR, legacy["id"])
        self.assertTrue(result["backfilled"])
        self.assertEqual(result["calendar_version"], 1)
        updated = self.service.get_record(OFFICER, legacy["id"])
        self.assertEqual(updated["payload"]["calendar_version"], 1)
        # 原承诺日期不被改写。
        self.assertEqual(updated["payload"]["promised_decision_date"], "2026-03-01")
        self.assertTrue(updated["payload"]["promised_decision"].get("preserved"))
        # 重复回填幂等。
        again = self.service.backfill_calendar_version(SUPERVISOR, legacy["id"])
        self.assertFalse(again["backfilled"])

    def test_backfill_requires_supervisor(self):
        record = self.service.create(Actor("creator", "intake_officer"), "IMM-29003", CREATE_DATA)
        with self.assertRaises(PermissionDenied):
            self.service.backfill_calendar_version(OFFICER, record["id"])

    def test_appeal_window_closed_after_frozen_calendar_deadline(self):
        record = self.service.create(Actor("creator", "intake_officer"), "IMM-29004", CREATE_DATA)
        record = self.service.act(Actor("rep", "legal_rep"), record["id"], record["version"], "submit", SUBMIT[2])
        record = self.service.act(OFFICER, record["id"], record["version"], "decide",
                                  {"decision": "denied", "decision_reason": "不符", "decision_date": "2026-01-25"})
        self.assertEqual(record["payload"]["appeal_window_date"], "2026-02-24")
        with self.assertRaises(ValidationError):
            self.service.act(Actor("rep", "legal_rep"), record["id"], record["version"], "appeal",
                             {"appeal_date": "2026-02-25", "appeal_reason": "超期"})
