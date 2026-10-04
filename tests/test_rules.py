import unittest
from datetime import date

from src import calendar as cal
from src.domain import ValidationError
from src.rules import DomainRules


CREATE_DATA = {
    'applicant_id': 'A-900', 'case_type': 'family',
    'received_date': '2026-01-05', 'deadline_days': 30,
    'representation_active': True,
    'required_documents': ['passport', 'sponsor_letter'],
}
CAL = cal.baseline("2026-01-01T00:00:00+00:00")


class RulesTest(unittest.TestCase):
    def setUp(self):
        self.rules = DomainRules()

    def test_prepare_create_freezes_calendar_and_natural_day_promise(self):
        prepared = self.rules.prepare_create(CREATE_DATA, CAL)
        self.assertEqual(prepared["calendar_version"], cal.BASELINE_VERSION)
        self.assertEqual(prepared["promised_decision_date"], "2026-02-04")
        self.assertFalse(prepared["promised_decision"]["rolled"])

    def test_weekend_delivery_rolls_forward_with_steps(self):
        # 2026-01-10 周六 -> 顺延至周一 2026-01-12
        detail = cal.service_effective_date(CAL, date(2026, 1, 10))
        self.assertEqual(detail["effective_date"], "2026-01-12")
        self.assertTrue(detail["rolled"])
        self.assertEqual([step["date"] for step in detail["steps"]], ["2026-01-10", "2026-01-11"])
        self.assertEqual([step["reason"] for step in detail["steps"]], ["weekend", "weekend"])

    def test_holiday_roll_uses_calendar_holidays(self):
        holiday_cal = cal.CalendarVersion(
            version=2, name="假日", effective_from=date(2026, 1, 1),
            weekend_days=(5, 6), holidays=frozenset({"2026-01-12"}),
        )
        detail = cal.service_effective_date(holiday_cal, date(2026, 1, 10))
        # 周六、周日、周一(节假日) 均顺延到周二
        self.assertEqual(detail["effective_date"], "2026-01-13")
        reasons = [step["reason"] for step in detail["steps"]]
        self.assertEqual(reasons, ["weekend", "weekend", "holiday"])

    def test_invalid_input(self):
        invalid = dict(CREATE_DATA)
        invalid['case_type'] = 'tourist'
        with self.assertRaises(ValidationError):
            self.rules.prepare_create(invalid, CAL)

    def test_duplicate_same_day_receipt_is_rejected_before_persistence(self):
        record = {"id": 1, "state": "response_received", "payload": dict(
            self.rules.prepare_create(CREATE_DATA, CAL),
            evidence_receipts=[{"receipt_key": "R-1", "raw_date": "2026-01-10", "effective_date": "2026-01-12"}],
        )}
        from src.domain import Conflict
        with self.assertRaises(Conflict):
            self.rules.build_receipt(record, {"receipt_key": "R-2", "served_date": "2026-01-10"}, CAL)
