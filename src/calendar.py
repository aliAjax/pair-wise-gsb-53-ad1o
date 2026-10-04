"""节假日日历版本与自然日顺延计算。

受理时冻结具体日历版本，之后所有期限（承诺决定日、补件期限、上诉窗口）
都只依据冻结版本计算；新发布版本只影响发布后受理的新案。
"""
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

from .domain import ValidationError, integer

# 基线日历：保证任意受理日期都能选到一个有效版本。
BASELINE_VERSION = 1
BASELINE_NAME = "基线日历"
BASELINE_EFFECTIVE_FROM = "2000-01-01"
DEFAULT_WEEKEND = [5, 6]
# 顺延上限，避免出现“全年皆休息日”的异常日历造成死循环。
MAX_ROLL_DAYS = 366


def parse_iso_date(value: Any, key: str) -> date:
    if not isinstance(value, str):
        raise ValidationError("%s必须是YYYY-MM-DD日期" % key)
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise ValidationError("%s必须是YYYY-MM-DD日期" % key) from exc


def optional_iso_date(data: Dict[str, Any], key: str) -> Optional[date]:
    value = data.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return parse_iso_date(value, key)


def iso(day: date) -> str:
    return day.isoformat()


@dataclass(frozen=True)
class CalendarVersion:
    version: int
    name: str
    effective_from: date
    weekend_days: Tuple[int, ...]
    holidays: frozenset
    published_by: str = ""
    published_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "name": self.name,
            "effective_from": iso(self.effective_from),
            "weekend_days": list(self.weekend_days),
            "holidays": sorted(self.holidays),
            "published_by": self.published_by,
            "published_at": self.published_at,
        }


def from_dict(row: Dict[str, Any]) -> CalendarVersion:
    return CalendarVersion(
        version=int(row["version"]),
        name=str(row["name"]),
        effective_from=parse_iso_date(row["effective_from"], "effective_from"),
        weekend_days=tuple(int(day) for day in row["weekend_days"]),
        holidays=frozenset(row["holidays"]),
        published_by=str(row.get("published_by", "")),
        published_at=str(row.get("published_at", "")),
    )


def baseline(published_at: str) -> CalendarVersion:
    return CalendarVersion(
        version=BASELINE_VERSION,
        name=BASELINE_NAME,
        effective_from=parse_iso_date(BASELINE_EFFECTIVE_FROM, "effective_from"),
        weekend_days=tuple(DEFAULT_WEEKEND),
        holidays=frozenset(),
        published_by="system",
        published_at=published_at,
    )


def validate_payload(data: Dict[str, Any]) -> Dict[str, Any]:
    """校验主管发布日历的输入，返回规范化后的字典。"""
    from .domain import text

    payload = dict(data or {})
    name = text(payload, "name")
    effective_from = parse_iso_date(payload.get("effective_from"), "effective_from")
    weekend = payload.get("weekend", DEFAULT_WEEKEND)
    if not isinstance(weekend, list) or any(isinstance(day, bool) or not isinstance(day, int) for day in weekend):
        raise ValidationError("weekend必须是0-6整数列表")
    days = sorted(set(weekend))
    if any(day < 0 or day > 6 for day in days):
        raise ValidationError("weekend取值只能是0(周一)到6(周日)")
    raw_holidays = payload.get("holidays", [])
    if not isinstance(raw_holidays, list) or any(not isinstance(day, str) for day in raw_holidays):
        raise ValidationError("holidays必须是YYYY-MM-DD日期列表")
    holidays: List[str] = []
    for item in raw_holidays:
        day = iso(parse_iso_date(item, "holidays"))
        if day not in holidays:
            holidays.append(day)
    return {
        "name": name,
        "effective_from": iso(effective_from),
        "weekend_days": days,
        "holidays": sorted(holidays),
    }


def rest_reason(calendar: CalendarVersion, day: date) -> Optional[str]:
    if day.weekday() in calendar.weekend_days:
        return "weekend"
    if iso(day) in calendar.holidays:
        return "holiday"
    return None


def roll_forward(calendar: CalendarVersion, day: date) -> Tuple[date, List[Dict[str, str]]]:
    """休息日送达顺延到下一个工作日，逐天记录顺延原因。"""
    steps: List[Dict[str, str]] = []
    current = day
    for _ in range(MAX_ROLL_DAYS + 1):
        reason = rest_reason(calendar, current)
        if reason is None:
            return current, steps
        steps.append({"date": iso(current), "reason": reason})
        current = current + timedelta(days=1)
    raise ValidationError("日历在一年内没有工作日，无法顺延")


def natural_deadline(calendar: CalendarVersion, start: date, days: int, label: str = "") -> Dict[str, Any]:
    """按自然日计算到期日，落点为休息日时按日历顺延并保留明细。"""
    days = integer({"days": days}, "days", 0)
    raw = start + timedelta(days=days)
    effective, steps = roll_forward(calendar, raw)
    return {
        "label": label,
        "calendar_version": calendar.version,
        "start_date": iso(start),
        "natural_days": days,
        "raw_date": iso(raw),
        "effective_date": iso(effective),
        "rolled": effective != raw,
        "steps": steps,
    }


def service_effective_date(calendar: CalendarVersion, served: date) -> Dict[str, Any]:
    """补件送达回执：送达日遇休息日顺延，返回计算明细。"""
    effective, steps = roll_forward(calendar, served)
    return {
        "label": "evidence_service",
        "calendar_version": calendar.version,
        "raw_date": iso(served),
        "effective_date": iso(effective),
        "rolled": effective != served,
        "steps": steps,
    }
