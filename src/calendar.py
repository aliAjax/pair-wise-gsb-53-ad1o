"""期限日历：按自然日计算期限，遇休息日顺延，每一步都保留计算明细。

日历版本一旦发布即不可变；案件在受理时冻结某一版本的完整快照，
之后所有决定期限、补件期限、送达顺延和上诉窗口都以该快照为唯一计算依据，
后续发布的新版本只影响之后受理的新案。
"""
from datetime import date, timedelta
from typing import Any, Dict, List, Tuple

from .domain import ValidationError, integer, optional_text, text, text_list

DEFAULT_CALENDAR_NAME = "default"
DEFAULT_APPEAL_WINDOW_DAYS = 30
DEFAULT_WEEKEND_DAYS = [5, 6]  # Monday=0 ... Sunday=6


def parse_iso_date(value: Any, key: str = "date") -> date:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("%s必须是YYYY-MM-DD日期" % key)
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise ValidationError("%s必须是YYYY-MM-DD日期" % key) from exc


def iso(day: date) -> str:
    return day.isoformat()


def validate_calendar_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """校验主管发布日历版本的输入。"""
    data = dict(payload or {})
    name = optional_text(data, "name", DEFAULT_CALENDAR_NAME) or DEFAULT_CALENDAR_NAME
    effective_from = iso(parse_iso_date(data.get("effective_from"), "effective_from"))
    raw_holidays = text_list(data, "holidays", 0)
    holidays: List[str] = []
    seen = set()
    for item in raw_holidays:
        day = iso(parse_iso_date(item, "holidays"))
        if day in seen:
            raise ValidationError("节假日存在重复日期：%s" % day)
        seen.add(day)
        holidays.append(day)
    holidays.sort()
    weekend = data.get("weekend_days", DEFAULT_WEEKEND_DAYS)
    if not isinstance(weekend, list) or any(isinstance(x, bool) or not isinstance(x, int) or x < 0 or x > 6 for x in weekend):
        raise ValidationError("weekend_days必须是0到6的整数列表")
    note = optional_text(data, "note")
    return {
        "name": name,
        "effective_from": effective_from,
        "holidays": holidays,
        "weekend_days": sorted(weekend),
        "note": note,
    }


class CalendarEngine:
    """基于某个不可变日历版本的期限计算器。"""

    def __init__(self, calendar: Dict[str, Any]) -> None:
        self.name = str(calendar["name"])
        self.version = int(calendar["version"])
        self.effective_from = str(calendar["effective_from"])
        self.holidays = {date.fromisoformat(item) for item in calendar.get("holidays", [])}
        self.weekend_days = {int(item) for item in calendar.get("weekend_days", DEFAULT_WEEKEND_DAYS)}

    def snapshot(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "effective_from": self.effective_from,
            "holidays": sorted(iso(day) for day in self.holidays),
            "weekend_days": sorted(self.weekend_days),
        }

    def is_rest_day(self, day: date) -> bool:
        return day.weekday() in self.weekend_days or day in self.holidays

    def _rest_reason(self, day: date) -> str:
        return "holiday" if day in self.holidays else "weekend"

    def roll_forward(self, day: date) -> Tuple[date, List[Dict[str, str]]]:
        """把落在休息日的日期顺延到第一个工作日，返回顺延明细。"""
        steps: List[Dict[str, str]] = []
        current = day
        while self.is_rest_day(current):
            steps.append({
                "type": "rest_skip",
                "date": iso(current),
                "reason": self._rest_reason(current),
                "next_date": iso(current + timedelta(days=1)),
            })
            current += timedelta(days=1)
        return current, steps

    def deadline_after(self, start: date, natural_days: int, kind: str = "deadline") -> Dict[str, Any]:
        """从start起算natural_days个自然日，末日遇休息日顺延。"""
        natural_days = integer({"natural_days": natural_days}, "natural_days", 1)
        raw_due = start + timedelta(days=natural_days)
        due, roll_steps = self.roll_forward(raw_due)
        return {
            "kind": kind,
            "calendar": self.snapshot(),
            "start_date": iso(start),
            "natural_days": natural_days,
            "raw_due_date": iso(raw_due),
            "due_date": iso(due),
            "rolled": due != raw_due,
            "steps": [{
                "type": "natural_days",
                "from_date": iso(start),
                "to_date": iso(raw_due),
                "days": natural_days,
            }] + roll_steps,
        }

    def intake_schedule(self, received: date, deadline_days: int) -> Dict[str, Any]:
        return self.deadline_after(received, deadline_days, kind="decision")

    def appeal_schedule(self, decision_day: date, window_days: int = DEFAULT_APPEAL_WINDOW_DAYS) -> Dict[str, Any]:
        return self.deadline_after(decision_day, window_days, kind="appeal")
