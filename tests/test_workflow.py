import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict


CREATE_DATA = {
    'applicant_id': 'A-900', 'case_type': 'family',
    'received_date': '2026-01-05', 'deadline_days': 30,
    'representation_active': True,
    'required_documents': ['passport', 'sponsor_letter'],
}
# request_evidence在周六送达：按基线日历顺延到周一2026-01-12，补件10个自然日到期2026-01-22。
FLOW = [
    ('submit', 'legal_rep', {'documents': ['passport', 'sponsor_letter']}, 'submitted'),
    ('request_evidence', 'case_officer', {'evidence_request_date': '2026-01-10', 'allowed_days': 10, 'evidence_request': '补充收入证明'}, 'evidence_requested'),
    ('respond', 'legal_rep', {'response_date': '2026-01-20', 'documents': ['income_proof']}, 'response_received'),
    ('decide', 'case_officer', {'decision': 'granted', 'decision_reason': '材料充分', 'decision_date': '2026-01-25'}, 'decided'),
]


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def test_complete_workflow_and_audit(self):
        record = self.service.create(Actor("creator", "intake_officer"), "IMM-29001", CREATE_DATA)
        self.assertEqual(record["state"], "draft")
        self.assertEqual(record["payload"]["calendar_version"], 1)
        self.assertEqual(record["payload"]["promised_decision_date"], "2026-02-04")
        for action, role, data, expected_state in FLOW:
            record = self.service.act(Actor("operator", role), record["id"], record["version"], action, data)
            self.assertEqual(record["state"], expected_state)
        self.assertEqual(record["payload"]["evidence_effective_date"], "2026-01-12")
        self.assertTrue(record["payload"]["evidence_service"]["rolled"])
        self.assertEqual(record["payload"]["appeal_window_date"], "2026-02-24")
        timeline = self.service.timeline(Actor("creator", "intake_officer"), record["id"])
        actions = [event["action"] for event in timeline]
        self.assertEqual(actions[-1], FLOW[-1][0])
        self.assertIn("calendar_frozen", actions)
