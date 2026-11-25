#!/usr/bin/env python3
"""
Auditable integer allocation for cumulative meter-reading settlement.

The solver never uses floating point for energy or money.  Every intersection is
stored as a fractions.Fraction.  Rounding is modelled as a binary choice between
floor(q) and ceil(q), and solved as an integral max-flow/b-matching problem.

No third-party packages are required.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any, Iterable, Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore

ENGINE_RULE_VERSION = "integer-slice-flow-1.0.0"
HALF_OPEN_NOTE = "Intervals are [start_utc, end_utc); the end instant belongs to the next interval."


class InputError(Exception):
    """Raised when the input contract cannot be parsed safely."""


# ---------------------------------------------------------------------------
# Exact time / rational helpers
# ---------------------------------------------------------------------------

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
INSTANT_RE = __import__("re").compile(
    r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})"
    r"(?:\.(\d+))?(Z|[+-]\d{2}:?\d{2})?$"
)


@dataclass(frozen=True)
class Instant:
    text: str
    sec: Fraction
    dt: datetime


def parse_instant(value: Any, path: str = "timestamp") -> Instant:
    if not isinstance(value, str):
        raise InputError(f"{path}: expected ISO-8601 string with UTC offset, got {type(value).__name__}")
    m = INSTANT_RE.match(value.strip())
    if not m:
        raise InputError(f"{path}: {value!r} is not a supported ISO-8601 timestamp")
    year, month, day, hour, minute, second = (int(m.group(i)) for i in range(1, 7))
    frac_digits = m.group(7)
    offset_text = m.group(8)
    if offset_text is None:
        raise InputError(f"{path}: {value!r} must include a UTC offset; naive local time is not allowed")

    frac = Fraction(int(frac_digits), 10 ** len(frac_digits)) if frac_digits else Fraction(0)

    if offset_text == "Z":
        offset_seconds = 0
    else:
        sign = 1 if offset_text[0] == "+" else -1
        body = offset_text[1:].replace(":", "")
        offset_seconds = sign * (int(body[:2]) * 3600 + int(body[2:4]) * 60)

    tz = timezone(timedelta(seconds=offset_seconds))
    dt = datetime(year, month, day, hour, minute, second, tzinfo=tz)
    elapsed = dt - EPOCH
    whole_seconds = elapsed.days * 86400 + elapsed.seconds
    return Instant(value, Fraction(whole_seconds) + frac, dt)


def parse_rational(value: Any, path: str) -> Fraction:
    if isinstance(value, bool):
        raise InputError(f"{path}: boolean is not a rational price")
    if isinstance(value, int):
        return Fraction(value, 1)
    if isinstance(value, str):
        try:
            return Fraction(value.strip())
        except (ValueError, ZeroDivisionError) as exc:
            raise InputError(f"{path}: {value!r} is not a valid rational/decimal") from exc
    if isinstance(value, dict):
        if "num" not in value or "den" not in value:
            raise InputError(f"{path}: rational object requires num and den")
        num = require_int(value["num"], path + ".num")
        den = require_int(value["den"], path + ".den")
        if den == 0:
            raise InputError(f"{path}: denominator must not be zero")
        return Fraction(num, den)
    if isinstance(value, float):
        raise InputError(
            f"{path}: JSON float {value!r} is forbidden because it is binary-imprecise; "
            "quote the decimal as a string or use {{'num': n, 'den': d}}"
        )
    raise InputError(f"{path}: unsupported rational value {value!r}")


def require_int(value: Any, path: str, minimum: Optional[int] = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InputError(f"{path}: expected integer Wh, got {value!r} ({type(value).__name__})")
    if minimum is not None and value < minimum:
        raise InputError(f"{path}: expected integer >= {minimum}, got {value}")
    return value


def frac_str(value: Fraction | int | None) -> Optional[str]:
    if value is None:
        return None
    f = Fraction(value)
    if f.denominator == 1:
        return str(f.numerator)
    return f"{f.numerator}/{f.denominator}"


def finite_decimal_digits(f: Fraction) -> Optional[str]:
    """Return exact fractional digits if denominator has only factors 2 and 5."""
    if f == 0:
        return ""
    sign = "-" if f < 0 else ""
    f = abs(f)
    den = f.denominator
    p2 = p5 = 0
    while den % 2 == 0:
        den //= 2
        p2 += 1
    while den % 5 == 0:
        den //= 5
        p5 += 1
    if den != 1:
        return None
    scale = max(p2, p5)
    digits = str(f.numerator * (10 ** scale) // f.denominator).zfill(scale)
    return sign + digits


def abs_to_utc_iso(sec: Fraction) -> str:
    whole = sec.numerator // sec.denominator
    rem = sec - whole
    dt = EPOCH + timedelta(seconds=int(whole))
    base = dt.strftime("%Y-%m-%dT%H:%M:%S")
    if rem:
        digits = finite_decimal_digits(rem)
        if digits is not None:
            base += "." + digits.rstrip("0").rstrip(".")
    return base + "Z"


def parse_hms(value: Any, path: str) -> int:
    if not isinstance(value, str):
        raise InputError(f"{path}: expected HH:MM or HH:MM:SS")
    parts = value.split(":")
    if len(parts) not in (2, 3):
        raise InputError(f"{path}: expected HH:MM or HH:MM:SS")
    try:
        h = int(parts[0])
        m = int(parts[1])
        s = int(parts[2]) if len(parts) == 3 else 0
    except ValueError as exc:
        raise InputError(f"{path}: invalid clock time {value!r}") from exc
    if not (0 <= h <= 24 and 0 <= m < 60 and 0 <= s < 60) or (h == 24 and (m or s)):
        raise InputError(f"{path}: invalid clock time {value!r}")
    return h * 3600 + m * 60 + s


# ---------------------------------------------------------------------------
# Calendar / tariffs / domain records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BillingDay:
    day: str
    start: Fraction
    end: Fraction


@dataclass
class BillingCalendar:
    calendar_id: str
    tz_name: str
    tz: Any
    day_start_second: int = 0
    holidays: set[date] = field(default_factory=set)
    extra_workdays: set[date] = field(default_factory=set)
    workweek: set[int] = field(default_factory=lambda: {1, 2, 3, 4, 5})
    boundary_fold: int = 0
    raw: dict = field(default_factory=dict)

    def period_at(self, sec: Fraction) -> BillingDay:
        whole = sec.numerator // sec.denominator
        z = (EPOCH + timedelta(seconds=int(whole))).astimezone(self.tz)
        tod = z.hour * 3600 + z.minute * 60 + z.second
        local_day = z.date()
        billing_day = local_day - timedelta(days=1) if tod < self.day_start_second else local_day
        start = wall_clock_to_abs(billing_day, self.day_start_second, self.tz, self.boundary_fold)
        end = wall_clock_to_abs(billing_day + timedelta(days=1), self.day_start_second, self.tz, self.boundary_fold)
        if end <= start:
            raise InputError(f"calendar {self.calendar_id}: non-positive billing day {billing_day}")
        return BillingDay(billing_day.isoformat(), start, end)

    def day_type(self, d: date) -> str:
        if d in self.holidays:
            return "holiday"
        if d in self.extra_workdays:
            return "workday"
        return "workday" if d.isoweekday() in self.workweek else "weekend"


def parse_date_set(values: Any, path: str) -> set[date]:
    out: set[date] = set()
    if values is None:
        return out
    if not isinstance(values, list):
        raise InputError(f"{path}: expected list of YYYY-MM-DD")
    for v in values:
        if not isinstance(v, str):
            raise InputError(f"{path}: invalid date {v!r}")
        try:
            out.add(date.fromisoformat(v))
        except ValueError as exc:
            raise InputError(f"{path}: invalid date {v!r}") from exc
    return out


def wall_clock_to_abs(d: date, tod_seconds: int, tz: Any, fold: int = 0) -> Fraction:
    if tod_seconds >= 86400:
        d = d + timedelta(days=tod_seconds // 86400)
        tod_seconds %= 86400
    h, rem = divmod(tod_seconds, 3600)
    m, s = divmod(rem, 60)
    dt = datetime(d.year, d.month, d.day, h, m, s, tzinfo=tz, fold=fold)
    elapsed = dt - EPOCH
    return Fraction(elapsed.days * 86400 + elapsed.seconds)


def resolve_wall_clock(d: date, tod_seconds: int, tz: Any, preferred_fold: Optional[int]) -> tuple[Fraction, str]:
    """
    Resolve a local wall-clock boundary.

    Returns (absolute_seconds, resolution).  Resolution is one of:
    normal, ambiguous-fold-0, ambiguous-fold-1, invalid-fold-0, invalid-fold-1.
    The caller decides whether ambiguity/nonexistence is an error.
    """
    if tod_seconds >= 86400:
        d = d + timedelta(days=tod_seconds // 86400)
        tod_seconds %= 86400
    h, rem = divmod(tod_seconds, 3600)
    m, s = divmod(rem, 60)

    candidates = []
    for fold in (0, 1):
        dt = datetime(d.year, d.month, d.day, h, m, s, tzinfo=tz, fold=fold)
        elapsed = dt - EPOCH
        abs_sec = Fraction(elapsed.days * 86400 + elapsed.seconds)
        whole = abs_sec.numerator // abs_sec.denominator
        back = (EPOCH + timedelta(seconds=int(whole))).astimezone(tz)
        roundtrip_ok = (
            back.year == d.year and back.month == d.month and back.day == d.day
            and back.hour == h and back.minute == m and back.second == s
        )
        candidates.append((abs_sec, roundtrip_ok, dt.utcoffset(), fold))

    valid = [c for c in candidates if c[1]]
    if not valid:
        return candidates[0][0], "nonexistent"
    abs0, ok0, off0, _ = candidates[0]
    abs1, ok1, off1, _ = candidates[1]
    if ok0 and ok1 and off0 != off1 and abs0 != abs1:
        if preferred_fold == 1:
            return abs1, "ambiguous-fold-1"
        if preferred_fold == 0:
            return abs0, "ambiguous-fold-0"
        # No preference: report ambiguity through a special marker.
        return abs0, "ambiguous"
    chosen = valid[preferred_fold] if preferred_fold is not None and preferred_fold < len(valid) else valid[0]
    return chosen[0], "normal"


@dataclass
class Binding:
    binding_id: str
    meter_id: str
    scope_id: str
    start: Fraction
    end: Fraction
    point_id: Optional[str] = None
    calendar_id: Optional[str] = None
    priority: int = 0
    raw: dict = field(default_factory=dict)


@dataclass
class TariffInterval:
    scope_id: str
    rate_id: str
    tariff_version: str
    price: Fraction
    start: Fraction
    end: Fraction
    basis: dict = field(default_factory=dict)


@dataclass
class Anchor:
    sec: Fraction
    text: str
    value: Optional[int]
    kind: str
    ref_id: str
    evidence: dict = field(default_factory=dict)


@dataclass
class Segment:
    segment_id: str
    meter_id: str
    start: Fraction
    end: Fraction
    delta_wh: Optional[int]
    status: str
    issues: list[dict] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)


@dataclass
class Atom:
    atom_id: str
    segment_id: str
    key: tuple[str, str, str]
    start: Fraction
    end: Fraction
    duration: Fraction
    segment_duration: Fraction
    rational_wh: Fraction
    floor_wh: int
    ceil_wh: int
    price: Fraction
    tariff_version: str
    basis: dict = field(default_factory=dict)


@dataclass
class Control:
    key: tuple[str, str, str]
    total_wh: int
    control_id: Optional[str]
    raw: dict
    issues: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Input loading
# ---------------------------------------------------------------------------


def load_calendars(root: dict) -> dict[str, BillingCalendar]:
    raw_cals = root.get("billing_calendars")
    if raw_cals is None:
        raw_cals = [root.get("billing_calendar")] if isinstance(root.get("billing_calendar"), dict) else []
    if not isinstance(raw_cals, list) or not raw_cals:
        raise InputError("billing_calendar or billing_calendars is required")

    out: dict[str, BillingCalendar] = {}
    for idx, rc in enumerate(raw_cals):
        if not isinstance(rc, dict):
            raise InputError(f"billing_calendars[{idx}] must be an object")
        cid = str(rc.get("calendar_id") or rc.get("id") or f"calendar-{idx+1}")
        if cid in out:
            raise InputError(f"duplicate billing calendar_id {cid}")
        tz_name = rc.get("iana_timezone") or rc.get("timezone")
        if not tz_name:
            raise InputError(f"billing_calendars[{idx}].timezone is required (IANA name)")
        if ZoneInfo is None:
            raise InputError("zoneinfo is unavailable; Python 3.9+ with system tzdata is required")
        try:
            tz = ZoneInfo(str(tz_name))
        except Exception as exc:
            raise InputError(f"unknown IANA timezone {tz_name!r}: {exc}") from exc
        day_start = parse_hms(rc.get("billing_day_start", rc.get("day_start", "00:00")), f"billing_calendars.{cid}.billing_day_start")
        fold = require_int(rc.get("boundary_fold", 0), f"billing_calendars.{cid}.boundary_fold")
        if fold not in (0, 1):
            raise InputError("boundary_fold must be 0 or 1")
        cal = BillingCalendar(
            calendar_id=cid,
            tz_name=str(tz_name),
            tz=tz,
            day_start_second=day_start,
            holidays=parse_date_set(rc.get("holidays"), f"billing_calendars.{cid}.holidays"),
            extra_workdays=parse_date_set(rc.get("extra_workdays"), f"billing_calendars.{cid}.extra_workdays"),
            workweek=set(rc.get("workweek", [1, 2, 3, 4, 5])),
            boundary_fold=fold,
            raw=rc,
        )
        bad_days = [x for x in cal.workweek if not isinstance(x, int) or not 1 <= x <= 7]
        if bad_days:
            raise InputError(f"billing_calendars.{cid}.workweek values must be ISO weekdays 1..7")
        out[cid] = cal

    default_id = root.get("default_calendar_id")
    if default_id is None:
        default_id = next(iter(out))
    if default_id not in out:
        raise InputError(f"default_calendar_id {default_id!r} not found")
    # Stash default through a special key while retaining explicit ids.
    out["*default*"] = out[str(default_id)]
    return out


def load_bindings(root: dict) -> list[Binding]:
    raw = root.get("bindings", [])
    if not isinstance(raw, list):
        raise InputError("bindings must be a list")
    bindings: list[Binding] = []
    for i, b in enumerate(raw):
        p = f"bindings[{i}]"
        if not isinstance(b, dict):
            raise InputError(f"{p} must be an object")
        bid = str(b.get("binding_id") or b.get("id") or f"B{i+1}")
        meter_id = b.get("meter_id")
        scope_id = b.get("scope_id") or b.get("settlement_scope_id") or b.get("account_id")
        if not meter_id or not scope_id:
            raise InputError(f"{p}: meter_id and scope_id are required")
        start_raw = b.get("valid_from", b.get("start", b.get("from")))
        end_raw = b.get("valid_to", b.get("end", b.get("to")))
        if start_raw is None or end_raw is None:
            raise InputError(f"{p}: valid_from and valid_to are required half-open bounds")
        start = parse_instant(start_raw, p + ".valid_from").sec
        end = parse_instant(end_raw, p + ".valid_to").sec
        if end <= start:
            raise InputError(f"{p}: binding interval must have positive duration")
        priority = require_int(b.get("priority", 0), p + ".priority")
        bindings.append(Binding(
            binding_id=bid,
            meter_id=str(meter_id),
            scope_id=str(scope_id),
            point_id=(str(b["point_id"]) if b.get("point_id") is not None else None),
            calendar_id=(str(b["calendar_id"]) if b.get("calendar_id") is not None else None),
            start=start,
            end=end,
            priority=priority,
            raw=b,
        ))
    return bindings


def get_default_calendar(cals: dict[str, BillingCalendar]) -> BillingCalendar:
    return cals["*default*"]


def calendar_for_binding(b: Optional[Binding], cals: dict[str, BillingCalendar]) -> BillingCalendar:
    if b and b.calendar_id:
        if b.calendar_id not in cals:
            raise InputError(f"binding {b.binding_id} references unknown calendar {b.calendar_id}")
        return cals[b.calendar_id]
    return get_default_calendar(cals)


def expand_tariffs(root: dict, cals: dict[str, BillingCalendar]) -> tuple[list[TariffInterval], list[dict]]:
    raw_versions = root.get("tariff_versions", [])
    if not isinstance(raw_versions, list):
        raise InputError("tariff_versions must be a list")
    intervals: list[TariffInterval] = []
    config_errors: list[dict] = []

    for vi, tv in enumerate(raw_versions):
        p = f"tariff_versions[{vi}]"
        if not isinstance(tv, dict):
            raise InputError(f"{p} must be an object")
        version = str(tv.get("tariff_version") or tv.get("version") or tv.get("id") or f"tariff-{vi+1}")
        scope_id = str(tv.get("scope_id", "*"))
        rate_id = str(tv.get("rate_id") or tv.get("rate") or "")
        if not rate_id:
            raise InputError(f"{p}.rate_id is required")
        price = parse_rational(tv.get("unit_price", tv.get("price")), p + ".unit_price")
        if price < 0:
            raise InputError(f"{p}.unit_price must be non-negative")
        eff_from = parse_instant(tv.get("effective_from", tv.get("from")), p + ".effective_from")
        eff_to = parse_instant(tv.get("effective_to", tv.get("to")), p + ".effective_to")
        if eff_to.sec <= eff_from.sec:
            raise InputError(f"{p}: effective interval must be positive and half-open")
        tz_name = tv.get("timezone") or get_default_calendar(cals).tz_name
        try:
            tz = ZoneInfo(str(tz_name)) if ZoneInfo else None
        except Exception as exc:
            raise InputError(f"{p}.timezone invalid: {exc}") from exc
        if tz is None:
            raise InputError("ZoneInfo unavailable")

        windows = tv.get("windows")
        if windows is None:
            intervals.append(TariffInterval(
                scope_id=scope_id, rate_id=rate_id, tariff_version=version, price=price,
                start=eff_from.sec, end=eff_to.sec,
                basis={"tariff_version": version, "rate_id": rate_id, "all_day": True,
                       "effective_from": eff_from.text, "effective_to": eff_to.text,
                       "unit_price": frac_str(price)},
            ))
            continue

        if not isinstance(windows, list):
            raise InputError(f"{p}.windows must be a list")

        from_d = eff_from.dt.astimezone(tz).date()
        to_d = eff_to.dt.astimezone(tz).date()
        day_cursor = from_d - timedelta(days=1)
        last_d = to_d + timedelta(days=1)
        wi = 0
        while day_cursor <= last_d:
            for wi, w in enumerate(windows):
                wp = f"{p}.windows[{wi}]"
                if not isinstance(w, dict):
                    raise InputError(f"{wp} must be an object")
                weekdays = w.get("weekdays", w.get("days"))
                if weekdays is not None:
                    if not isinstance(weekdays, list) or any((not isinstance(x, int)) or not 1 <= x <= 7 for x in weekdays):
                        raise InputError(f"{wp}.weekdays must be ISO weekdays 1..7")
                    if day_cursor.isoweekday() not in weekdays:
                        continue
                day_types = w.get("day_types")
                if day_types is not None:
                    cal_id = tv.get("billing_calendar_id")
                    cal = cals.get(str(cal_id)) if cal_id else get_default_calendar(cals)
                    if cal is None:
                        cal = get_default_calendar(cals)
                    if not isinstance(day_types, list) or cal.day_type(day_cursor) not in [str(x) for x in day_types]:
                        continue

                start_tod = parse_hms(w.get("start", w.get("from")), wp + ".start")
                end_tod = parse_hms(w.get("end", w.get("to")), wp + ".end")
                fold_start = w.get("fold_start", tv.get("ambiguous_fold"))
                fold_end = w.get("fold_end", tv.get("ambiguous_fold"))
                fold_start = require_int(fold_start, wp + ".fold_start") if fold_start is not None else None
                fold_end = require_int(fold_end, wp + ".fold_end") if fold_end is not None else None
                nonexistent_policy = str(w.get("nonexistent_policy", tv.get("nonexistent_policy", "fail")))

                ws, start_res = resolve_wall_clock(day_cursor, start_tod, tz, fold_start)
                end_day = day_cursor
                if end_tod <= start_tod:
                    # Overnight window. Equal times are not interpreted as 24h;
                    # use 00:00-24:00 for all day.
                    end_day = day_cursor + timedelta(days=1)
                we, end_res = resolve_wall_clock(end_day, end_tod, tz, fold_end)

                if start_res == "nonexistent" or end_res == "nonexistent":
                    msg = f"{wp}: local boundary does not exist due to spring-forward ({start_res}/{end_res})"
                    if nonexistent_policy == "fail":
                        config_errors.append({"scope_id": scope_id if scope_id != "*" else None,
                                              "tariff_version": version, "path": wp, "code": "nonexistent_local_tariff_boundary",
                                              "message": msg + "; no duration fabricated"})
                        continue
                    raise InputError(msg + " and unsupported policy " + nonexistent_policy)
                if start_res == "ambiguous" or end_res == "ambiguous":
                    config_errors.append({"scope_id": scope_id if scope_id != "*" else None,
                                          "tariff_version": version, "path": wp, "code": "ambiguous_local_tariff_boundary",
                                          "message": f"{wp}: local boundary is repeated at fall-back; specify fold_start/fold_end or explicit offset interval"})
                    continue
                if we <= ws:
                    config_errors.append({"scope_id": scope_id if scope_id != "*" else None,
                                          "tariff_version": version, "path": wp, "code": "non_positive_tariff_window",
                                          "message": f"{wp}: resolved half-open window has non-positive UTC duration"})
                    continue

                s = max(ws, eff_from.sec)
                e = min(we, eff_to.sec)
                if e > s:
                    intervals.append(TariffInterval(
                        scope_id=scope_id, rate_id=rate_id, tariff_version=version, price=price,
                        start=s, end=e,
                        basis={
                            "tariff_version": version,
                            "rate_id": rate_id,
                            "window_index": wi,
                            "local_date": day_cursor.isoformat(),
                            "window": w,
                            "start_resolution": start_res,
                            "end_resolution": end_res,
                            "timezone": tz_name,
                            "unit_price": frac_str(price),
                        },
                    ))
            day_cursor += timedelta(days=1)

    intervals.sort(key=lambda x: (x.scope_id, x.start, x.end, x.rate_id, x.tariff_version))
    return intervals, config_errors


def load_controls(root: dict) -> dict[tuple[str, str, str], Control]:
    raw = root.get("control_totals", root.get("controls", []))
    if not isinstance(raw, list):
        raise InputError("control_totals must be a list")
    controls: dict[tuple[str, str, str], Control] = {}
    for i, c in enumerate(raw):
        p = f"control_totals[{i}]"
        if not isinstance(c, dict):
            raise InputError(f"{p} must be an object")
        scope = str(c.get("scope_id") or c.get("settlement_scope_id") or "")
        day = str(c.get("billing_day") or c.get("day") or "")
        rate = str(c.get("rate_id") or c.get("rate") or "")
        if not scope or not day or not rate:
            raise InputError(f"{p}: scope_id, billing_day and rate_id are required")
        total = require_int(c.get("total_wh", c.get("wh", c.get("control_total_wh"))), p + ".total_wh", minimum=0)
        key = (scope, day, rate)
        cid = c.get("control_id") or c.get("id")
        control = Control(key=key, total_wh=total, control_id=(str(cid) if cid is not None else None), raw=c)
        if key in controls:
            prior = controls[key]
            same = prior.total_wh == total
            control.issues.append({
                "code": "duplicate_control_total",
                "message": "duplicate billing_day x rate control row; refusing to choose",
                "control_ids": [prior.control_id, control.control_id],
                "totals_wh": [prior.total_wh, total],
                "identical_value": same,
            })
            prior.issues.extend(control.issues)
            # Keep the newly parsed row too via issue raw evidence; dictionary retains prior.
        else:
            controls[key] = control
    return controls


# ---------------------------------------------------------------------------
# Readings / vouchers -> meter segments
# ---------------------------------------------------------------------------


def build_segments(root: dict) -> tuple[list[Segment], list[dict], list[dict]]:
    raw_readings = root.get("readings", [])
    if not isinstance(raw_readings, list):
        raise InputError("readings must be a list")

    readings_by_id: dict[str, dict] = {}
    anchors_by_meter: dict[str, list[Anchor]] = defaultdict(list)
    excluded: list[dict] = []
    global_issues: list[dict] = []

    for i, r in enumerate(raw_readings):
        p = f"readings[{i}]"
        if not isinstance(r, dict):
            raise InputError(f"{p} must be an object")
        rid = str(r.get("reading_id") or r.get("id") or f"reading-{i+1}")
        if rid in readings_by_id:
            raise InputError(f"duplicate reading_id {rid}")
        readings_by_id[rid] = r
        valid = r.get("valid", True)
        status = str(r.get("status", "valid")).lower()
        if valid is False or status in {"void", "invalid", "cancelled", "excluded"}:
            excluded.append({"reading_id": rid, "reason": "marked invalid/void", "raw": r})
            continue
        meter_id = r.get("meter_id")
        if not meter_id:
            raise InputError(f"{p}.meter_id is required")
        meter_id = str(meter_id)
        ts_raw = r.get("ts", r.get("timestamp", r.get("time")))
        instant = parse_instant(ts_raw, p + ".ts")
        value = require_int(r.get("value_wh", r.get("value", r.get("cumulative_wh"))), p + ".value_wh")
        anchors_by_meter[meter_id].append(Anchor(
            sec=instant.sec, text=instant.text, value=value, kind="valid_reading", ref_id=rid,
            evidence=raw_evidence(r, p),
        ))

    vouchers = root.get("exchange_vouchers", root.get("meter_exchanges", []))
    if not isinstance(vouchers, list):
        raise InputError("exchange_vouchers must be a list")

    for vi, v in enumerate(vouchers):
        p = f"exchange_vouchers[{vi}]"
        if not isinstance(v, dict):
            raise InputError(f"{p} must be an object")
        vid = str(v.get("voucher_id") or v.get("id") or f"exchange-{vi+1}")
        at_raw = v.get("exchange_at", v.get("at", v.get("ts")))
        at = parse_instant(at_raw, p + ".exchange_at") if at_raw is not None else None
        old_meter = str(v.get("old_meter_id", ""))
        new_meter = str(v.get("new_meter_id", ""))
        if not old_meter or not new_meter:
            raise InputError(f"{p}: old_meter_id and new_meter_id are required")

        for side, meter_id in (("old_end", old_meter), ("new_start", new_meter)):
            ep = v.get(side)
            value: Optional[int] = None
            ts: Optional[Instant] = at
            ref = f"{vid}:{side}"
            evidence: dict[str, Any] = {"voucher_id": vid, "side": side, "exchange_raw": v}
            path = f"{p}.{side}"

            if isinstance(ep, dict):
                if ep.get("reading_id"):
                    rid = str(ep["reading_id"])
                    rr = readings_by_id.get(rid)
                    if rr is None:
                        global_issues.append({"code": "voucher_reading_not_found", "path": path, "reading_id": rid, "voucher_id": vid})
                    else:
                        value = require_int(rr.get("value_wh", rr.get("value", rr.get("cumulative_wh"))), f"referenced reading {rid}.value_wh")
                        ts = parse_instant(rr.get("ts", rr.get("timestamp", rr.get("time"))), f"referenced reading {rid}.ts")
                        ref = rid
                        evidence["endpoint_reading_id"] = rid
                else:
                    value = ep.get("value_wh", ep.get("value"))
                    if value is not None:
                        value = require_int(value, path + ".value_wh")
                    ts_raw = ep.get("ts", ep.get("timestamp"))
                    if ts_raw is not None:
                        ts = parse_instant(ts_raw, path + ".ts")
                if value is None and ep.get("value_wh") is None and ep.get("value") is None:
                    value = None
            else:
                rid = v.get(side + "_reading_id")
                if rid:
                    rr = readings_by_id.get(str(rid))
                    if rr is None:
                        global_issues.append({"code": "voucher_reading_not_found", "path": p, "reading_id": rid, "voucher_id": vid})
                    else:
                        value = require_int(rr.get("value_wh", rr.get("value", rr.get("cumulative_wh"))), f"referenced reading {rid}.value_wh")
                        ts = parse_instant(rr.get("ts", rr.get("timestamp", rr.get("time"))), f"referenced reading {rid}.ts")
                        ref = str(rid)
                        evidence["endpoint_reading_id"] = str(rid)
                else:
                    val = v.get(side + "_value_wh", v.get(side + "_value"))
                    if val is not None:
                        value = require_int(val, p + "." + side + "_value_wh")
                    ts_raw = v.get(side + "_ts")
                    if ts_raw is not None:
                        ts = parse_instant(ts_raw, p + "." + side + "_ts")

            if ts is None:
                raise InputError(f"{p}: exchange_at or {side}.ts is required")
            if value is None:
                anchors_by_meter[meter_id].append(Anchor(
                    sec=ts.sec, text=ts.text, value=None, kind="missing_exchange_endpoint",
                    ref_id=ref, evidence={**evidence, "missing": True},
                ))
            else:
                anchors_by_meter[meter_id].append(Anchor(
                    sec=ts.sec, text=ts.text, value=value,
                    kind=("voucher_old_end" if side == "old_end" else "voucher_new_start"),
                    ref_id=ref, evidence=evidence,
                ))

    segments: list[Segment] = []
    for meter_id in sorted(anchors_by_meter):
        anchors = sorted(anchors_by_meter[meter_id], key=lambda a: (a.sec, a.kind, a.ref_id))
        groups: list[list[Anchor]] = []
        for a in anchors:
            if not groups or groups[-1][0].sec != a.sec:
                groups.append([a])
            else:
                groups[-1].append(a)

        point_values: list[Optional[int]] = []
        point_issues: list[list[dict]] = []
        for g in groups:
            vals = {a.value for a in g if a.value is not None}
            issues: list[dict] = []
            if not vals:
                point_values.append(None)
                missing = [a for a in g if a.value is None]
                issues.append({
                    "code": "missing_exchange_endpoint",
                    "message": "cross-exchange interval cannot be certified because old-end/new-start Wh is missing",
                    "refs": [a.ref_id for a in g],
                    "voucher_ids": sorted({a.evidence.get("voucher_id") for a in g if a.evidence.get("voucher_id")}),
                })
            elif len(vals) > 1:
                point_values.append(None)
                issues.append({
                    "code": "conflicting_anchor_values",
                    "message": "multiple valid endpoint values at the same UTC instant disagree",
                    "anchors": [{"ref": a.ref_id, "kind": a.kind, "value_wh": a.value, "ts": a.text} for a in g],
                })
            else:
                point_values.append(next(iter(vals)))
                missing_markers = [a for a in g if a.value is None]
                if missing_markers:
                    issues.append({
                        "code": "voucher_endpoint_supplied_by_reading",
                        "severity": "warning",
                        "message": "voucher field omitted; a same-instant valid reading supplies the endpoint",
                        "reading_refs": [a.ref_id for a in g if a.value is not None],
                        "voucher_ids": sorted({a.evidence.get("voucher_id") for a in missing_markers if a.evidence.get("voucher_id")}),
                    })
            point_issues.append(issues)

        for k in range(len(groups) - 1):
            g0, g1 = groups[k], groups[k + 1]
            start_sec, end_sec = g0[0].sec, g1[0].sec
            if end_sec <= start_sec:
                continue
            sid = f"SEG-{meter_id}-{k+1:03d}"
            issues = [i for i in point_issues[k] + point_issues[k + 1] if i.get("severity") != "warning"]
            warnings = [i for i in point_issues[k] + point_issues[k + 1] if i.get("severity") == "warning"]
            v0, v1 = point_values[k], point_values[k + 1]
            evidence = {
                "meter_id": meter_id,
                "start_anchors": [anchor_audit(a) for a in g0],
                "end_anchors": [anchor_audit(a) for a in g1],
                "start_utc": abs_to_utc_iso(start_sec),
                "end_utc": abs_to_utc_iso(end_sec),
                "duration_seconds": frac_str(end_sec - start_sec),
                "interval_note": HALF_OPEN_NOTE,
                "warnings": warnings,
            }
            if v0 is None or v1 is None:
                status = "pending_verification"
                delta = None
                if not issues:
                    issues.append({"code": "pending_verification", "message": "endpoint value is unavailable"})
            else:
                delta = v1 - v0
                if delta < 0:
                    status = "negative_drop_quarantined"
                    issues.append({
                        "code": "unvouchered_negative_drop",
                        "message": "cumulative Wh decreased; isolated instead of treating the decrease as negative consumption",
                        "start_value_wh": v0,
                        "end_value_wh": v1,
                        "raw_difference_wh": delta,
                    })
                else:
                    status = "valid"
            segments.append(Segment(
                segment_id=sid, meter_id=meter_id, start=start_sec, end=end_sec,
                delta_wh=delta, status=status, issues=issues, evidence=evidence,
            ))

    return segments, excluded, global_issues


def raw_evidence(r: dict, path: str) -> dict:
    return {"reading_id": r.get("reading_id") or r.get("id"), "path": path, "raw": r}


def anchor_audit(a: Anchor) -> dict:
    return {
        "ref_id": a.ref_id,
        "kind": a.kind,
        "declared_ts": a.text,
        "utc": abs_to_utc_iso(a.sec),
        "value_wh": a.value,
        "evidence": a.evidence,
    }


# ---------------------------------------------------------------------------
# Exact intersection -> rational atoms
# ---------------------------------------------------------------------------


def active_bindings_at(bindings: list[Binding], meter_id: str, sec: Fraction) -> list[Binding]:
    return [b for b in bindings if b.meter_id == meter_id and b.start <= sec < b.end]


def active_tariffs_at(tariffs: list[TariffInterval], scope_id: str, sec: Fraction) -> list[TariffInterval]:
    return [
        t for t in tariffs
        if (t.scope_id == scope_id or t.scope_id == "*") and t.start <= sec < t.end
    ]


def collect_breakpoints(
    seg: Segment,
    bindings: list[Binding],
    tariffs: list[TariffInterval],
    calendars: dict[str, BillingCalendar],
) -> set[Fraction]:
    points = {seg.start, seg.end}
    for b in bindings:
        if b.meter_id != seg.meter_id:
            continue
        if b.end <= seg.start or b.start >= seg.end:
            continue
        lo = max(b.start, seg.start)
        hi = min(b.end, seg.end)
        points.add(lo)
        points.add(hi)

        cal = calendar_for_binding(b, calendars)
        cur = lo
        while cur < hi:
            period = cal.period_at(cur)
            if period.end <= cur:
                break
            if lo < period.end < hi:
                points.add(period.end)
            cur = period.end

        # Only tariff boundaries applicable to this binding's scope may split
        # the segment.  Tariffs for unrelated scopes must not create artificial
        # rounding slices.
        for t in tariffs:
            if t.scope_id != "*" and t.scope_id != b.scope_id:
                continue
            if t.end <= lo or t.start >= hi:
                continue
            points.add(max(t.start, lo))
            points.add(min(t.end, hi))
    return points


@dataclass
class BuildResult:
    atoms: list[Atom]
    atom_by_id: dict[str, Atom]
    blocked_incidences: list[dict]
    segment_atom_ids: dict[str, list[str]]


def build_atoms(
    segments: list[Segment],
    bindings: list[Binding],
    tariffs: list[TariffInterval],
    calendars: dict[str, BillingCalendar],
    controls: dict[tuple[str, str, str], Control],
) -> BuildResult:
    atoms: list[Atom] = []
    atom_by_id: dict[str, Atom] = {}
    blocked_incidences: list[dict] = []
    segment_atom_ids: dict[str, list[str]] = defaultdict(list)
    atom_index = 0

    for seg in segments:
        if seg.status != "valid" or seg.delta_wh is None:
            inc = diagnose_blocked_segment(seg, bindings, tariffs, calendars, controls)
            blocked_incidences.append(inc)
            continue

        points = sorted(collect_breakpoints(seg, bindings, tariffs, calendars))
        seg_duration = seg.end - seg.start
        if seg_duration <= 0:
            seg.status = "input_error"
            seg.issues.append({"code": "non_positive_segment_duration", "message": "valid segment has non-positive UTC duration"})
            blocked_incidences.append({"segment_id": seg.segment_id, "keys": [], "issues": seg.issues})
            continue

        segment_has_blocker = False
        for a, b in zip(points, points[1:]):
            if b <= a:
                continue
            active_b = active_bindings_at(bindings, seg.meter_id, a)
            interval_issue: Optional[dict] = None
            candidate_keys: list[tuple[str, str, str]] = []

            if len(active_b) != 1:
                if len(active_b) == 0:
                    interval_issue = {
                        "code": "unbound_interval",
                        "message": "segment interval has no unique metering-point binding",
                        "start_utc": abs_to_utc_iso(a), "end_utc": abs_to_utc_iso(b),
                    }
                else:
                    interval_issue = {
                        "code": "ambiguous_binding",
                        "message": "multiple binding rows cover the same half-open interval",
                        "start_utc": abs_to_utc_iso(a), "end_utc": abs_to_utc_iso(b),
                        "binding_ids": [x.binding_id for x in active_b],
                    }
                    for cand_b in active_b:
                        cal = calendar_for_binding(cand_b, calendars)
                        day = cal.period_at(a).day
                        cand_tariffs = active_tariffs_at(tariffs, cand_b.scope_id, a)
                        if len(cand_tariffs) == 1:
                            candidate_keys.append((cand_b.scope_id, day, cand_tariffs[0].rate_id))
                        else:
                            candidate_keys.extend(all_scope_day_keys(controls, cand_b.scope_id, day))
            else:
                binding = active_b[0]
                cal = calendar_for_binding(binding, calendars)
                period = cal.period_at(a)
                active_t = active_tariffs_at(tariffs, binding.scope_id, a)
                if len(active_t) != 1:
                    interval_issue = {
                        "code": "ambiguous_or_missing_tariff",
                        "message": "interval does not have exactly one applicable tariff version/rate",
                        "start_utc": abs_to_utc_iso(a), "end_utc": abs_to_utc_iso(b),
                        "binding_id": binding.binding_id,
                        "active_tariff_versions": [
                            {"tariff_version": x.tariff_version, "rate_id": x.rate_id,
                             "start_utc": abs_to_utc_iso(x.start), "end_utc": abs_to_utc_iso(x.end)}
                            for x in active_t
                        ],
                    }
                    candidate_keys.extend(all_scope_day_keys(controls, binding.scope_id, period.day))
                else:
                    tariff = active_t[0]
                    if tariff.scope_id == "*":
                        # Wildcard tariff is applicable; key remains binding scope.
                        pass
                    key = (binding.scope_id, period.day, tariff.rate_id)
                    duration = b - a
                    rational = Fraction(seg.delta_wh) * duration / seg_duration
                    floor_q = rational.numerator // rational.denominator
                    ceil_q = -((-rational.numerator) // rational.denominator)
                    atom_index += 1
                    aid = f"A{atom_index:05d}"
                    atom = Atom(
                        atom_id=aid,
                        segment_id=seg.segment_id,
                        key=key,
                        start=a,
                        end=b,
                        duration=duration,
                        segment_duration=seg_duration,
                        rational_wh=rational,
                        floor_wh=floor_q,
                        ceil_wh=ceil_q,
                        price=tariff.price,
                        tariff_version=tariff.tariff_version,
                        basis={
                            "rule_version": ENGINE_RULE_VERSION,
                            "allocation_convention": "uniform by actual UTC duration",
                            "formula_wh": f"{seg.delta_wh} * ({frac_str(duration)}) / ({frac_str(seg_duration)})",
                            "segment_id": seg.segment_id,
                            "meter_id": seg.meter_id,
                            "binding_id": binding.binding_id,
                            "point_id": binding.point_id,
                            "scope_id": binding.scope_id,
                            "calendar_id": cal.calendar_id,
                            "timezone": cal.tz_name,
                            "billing_day": period.day,
                            "billing_day_start_utc": abs_to_utc_iso(period.start),
                            "billing_day_end_utc": abs_to_utc_iso(period.end),
                            "rate_id": tariff.rate_id,
                            "tariff_version": tariff.tariff_version,
                            "unit_price": frac_str(tariff.price),
                            "start_utc": abs_to_utc_iso(a),
                            "end_utc": abs_to_utc_iso(b),
                            "duration_seconds": frac_str(duration),
                            "segment_duration_seconds": frac_str(seg_duration),
                            "half_open": HALF_OPEN_NOTE,
                            "binding_raw": binding.raw,
                            "tariff_basis": tariff.basis,
                        },
                    )
                    atoms.append(atom)
                    atom_by_id[aid] = atom
                    segment_atom_ids[seg.segment_id].append(aid)
                    continue

            segment_has_blocker = True
            seg.issues.append(interval_issue or {"code": "intersection_error"})
            blocked_incidences.append({
                "segment_id": seg.segment_id,
                "start_utc": abs_to_utc_iso(a),
                "end_utc": abs_to_utc_iso(b),
                "keys": [key_object(k) for k in sorted(set(candidate_keys))],
                "_keys": sorted(set(candidate_keys)),
                "issue": interval_issue,
            })

        if segment_has_blocker:
            seg.status = "intersection_ambiguous"

    return BuildResult(atoms, atom_by_id, blocked_incidences, dict(segment_atom_ids))


def all_scope_day_keys(
    controls: dict[tuple[str, str, str], Control], scope_id: str, day: str
) -> list[tuple[str, str, str]]:
    return [k for k in controls if k[0] == scope_id and k[1] == day]


def diagnose_blocked_segment(
    seg: Segment,
    bindings: list[Binding],
    tariffs: list[TariffInterval],
    calendars: dict[str, BillingCalendar],
    controls: dict[tuple[str, str, str], Control],
) -> dict:
    """Find candidate control cells touched by a segment whose Wh is not usable."""
    candidate_keys: set[tuple[str, str, str]] = set()
    diagnostic_intervals: list[dict] = []
    try:
        points = sorted(collect_breakpoints(seg, bindings, tariffs, calendars))
        for a, b in zip(points, points[1:]):
            if b <= a:
                continue
            active_b = active_bindings_at(bindings, seg.meter_id, a)
            info: dict[str, Any] = {
                "start_utc": abs_to_utc_iso(a), "end_utc": abs_to_utc_iso(b),
                "duration_seconds": frac_str(b - a),
                "binding_ids": [x.binding_id for x in active_b],
            }
            if len(active_b) == 1:
                bind = active_b[0]
                cal = calendar_for_binding(bind, calendars)
                day = cal.period_at(a).day
                info["scope_id"] = bind.scope_id
                info["billing_day"] = day
                active_t = active_tariffs_at(tariffs, bind.scope_id, a)
                info["tariff_versions"] = [x.tariff_version for x in active_t]
                info["rate_ids"] = [x.rate_id for x in active_t]
                if len(active_t) == 1:
                    candidate_keys.add((bind.scope_id, day, active_t[0].rate_id))
                else:
                    candidate_keys.update(all_scope_day_keys(controls, bind.scope_id, day))
            elif len(active_b) > 1:
                for bind in active_b:
                    cal = calendar_for_binding(bind, calendars)
                    day = cal.period_at(a).day
                    active_t = active_tariffs_at(tariffs, bind.scope_id, a)
                    if len(active_t) == 1:
                        candidate_keys.add((bind.scope_id, day, active_t[0].rate_id))
                    else:
                        candidate_keys.update(all_scope_day_keys(controls, bind.scope_id, day))
            diagnostic_intervals.append(info)
    except InputError as exc:
        seg.issues.append({"code": "diagnostic_calendar_error", "message": str(exc)})

    return {
        "segment_id": seg.segment_id,
        "keys": [key_object(k) for k in sorted(candidate_keys)],
        "_keys": sorted(candidate_keys),
        "issues": seg.issues,
        "diagnostic_intervals": diagnostic_intervals,
    }


def key_object(key: tuple[str, str, str]) -> dict:
    return {"scope_id": key[0], "billing_day": key[1], "rate_id": key[2]}


# ---------------------------------------------------------------------------
# DSU connected ranges and max-flow rounding
# ---------------------------------------------------------------------------


class DSU:
    def __init__(self) -> None:
        self.parent: dict[Any, Any] = {}
        self.rank: dict[Any, int] = {}

    def add(self, x: Any) -> None:
        if x not in self.parent:
            self.parent[x] = x
            self.rank[x] = 0

    def find(self, x: Any) -> Any:
        self.add(x)
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != x:
            nxt = self.parent[x]
            self.parent[x] = root
            x = nxt
        return root

    def union(self, a: Any, b: Any) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self.rank[ra] < self.rank[rb]:
            ra, rb = rb, ra
        self.parent[rb] = ra
        if self.rank[ra] == self.rank[rb]:
            self.rank[ra] += 1


class FlowEdge:
    __slots__ = ("to", "rev", "cap", "initial_cap", "atom_id")

    def __init__(self, to: int, rev: int, cap: int, atom_id: Optional[str] = None) -> None:
        self.to = to
        self.rev = rev
        self.cap = cap
        self.initial_cap = cap
        self.atom_id = atom_id


class Dinic:
    def __init__(self, n: int) -> None:
        self.n = n
        self.g: list[list[FlowEdge]] = [[] for _ in range(n)]

    def add_edge(self, fr: int, to: int, cap: int, atom_id: Optional[str] = None) -> FlowEdge:
        forward = FlowEdge(to, len(self.g[to]), cap, atom_id)
        backward = FlowEdge(fr, len(self.g[fr]), 0, None)
        self.g[fr].append(forward)
        self.g[to].append(backward)
        return forward

    def bfs(self, s: int, t: int) -> list[int]:
        level = [-1] * self.n
        q = deque([s])
        level[s] = 0
        while q:
            v = q.popleft()
            for e in self.g[v]:
                if e.cap > 0 and level[e.to] < 0:
                    level[e.to] = level[v] + 1
                    q.append(e.to)
        return level

    def dfs(self, v: int, t: int, pushed: int, level: list[int], it: list[int]) -> int:
        if v == t:
            return pushed
        while it[v] < len(self.g[v]):
            e = self.g[v][it[v]]
            if e.cap > 0 and level[e.to] == level[v] + 1:
                tr = self.dfs(e.to, t, min(pushed, e.cap), level, it)
                if tr:
                    e.cap -= tr
                    self.g[e.to][e.rev].cap += tr
                    return tr
            it[v] += 1
        return 0

    def max_flow(self, s: int, t: int) -> int:
        flow = 0
        INF = 10**30
        while True:
            level = self.bfs(s, t)
            if level[t] < 0:
                break
            it = [0] * self.n
            while True:
                pushed = self.dfs(s, t, INF, level, it)
                if pushed == 0:
                    break
                flow += pushed
        return flow

    def reachable(self, s: int) -> set[int]:
        seen = {s}
        q = deque([s])
        while q:
            v = q.popleft()
            for e in self.g[v]:
                if e.cap > 0 and e.to not in seen:
                    seen.add(e.to)
                    q.append(e.to)
        return seen


@dataclass
class ComponentResult:
    component_id: str
    root: Any
    segment_ids: set[str] = field(default_factory=set)
    keys: set[tuple[str, str, str]] = field(default_factory=set)
    status: str = "settled"
    issues: list[dict] = field(default_factory=list)
    allocations: dict[str, int] = field(default_factory=dict)
    evidence: dict = field(default_factory=dict)


def solve_all(
    root_input: dict,
    segments: list[Segment],
    build: BuildResult,
    controls: dict[tuple[str, str, str], Control],
    tariff_config_errors: list[dict],
) -> tuple[list[ComponentResult], dict[tuple[str, str, str], str], dict[str, str]]:
    dsu = DSU()
    segment_by_id = {s.segment_id: s for s in segments}

    for key in controls:
        dsu.add(("C", key))

    # Every intersection slice is an audit edge and joins its reading segment to
    # the billing_day x rate control cell.  Integer-q slices have no binary
    # choice, but they still belong to the same settlement-connected range.
    for atom in build.atoms:
        dsu.add(("S", atom.segment_id))
        dsu.add(("C", atom.key))
        dsu.union(("S", atom.segment_id), ("C", atom.key))

    # Blocked/unknown segments hyperconnect every candidate control cell.  Use
    # the same ("S", segment_id) node as their valid fractional slices so a
    # partially intersected segment cannot be split into a fake settled part.
    for inc in build.blocked_incidences:
        sid = inc["segment_id"]
        seg_node = ("S", sid)
        dsu.add(seg_node)
        for key in inc.get("_keys", []):
            dsu.add(("C", key))
            dsu.union(seg_node, ("C", key))

    # Tariff configuration errors block matching scope/day controls when scope known.
    for err in tariff_config_errors:
        if err.get("scope_id"):
            for key in controls:
                if key[0] == err["scope_id"]:
                    dsu.add(("T", err.get("path", "tariff-config")))
                    dsu.union(("T", err.get("path", "tariff-config")), ("C", key))

    groups: dict[Any, ComponentResult] = {}
    for node in list(dsu.parent.keys()):
        root = dsu.find(node)
        if root not in groups:
            groups[root] = ComponentResult(component_id="", root=root)
        comp = groups[root]
        if node[0] == "S":
            comp.segment_ids.add(node[1])
        elif node[0] == "C":
            comp.keys.add(node[1])

    # Ensure every control belongs to a component even if DSU iteration changed roots.
    for key in controls:
        root = dsu.find(("C", key))
        if root not in groups:
            groups[root] = ComponentResult(component_id="", root=root)
        groups[root].keys.add(key)

    # Assign stable IDs sorted by readable key sets.
    def group_sort_key(comp: ComponentResult) -> str:
        keys = sorted("|".join(k) for k in comp.keys)
        segs = sorted(comp.segment_ids)
        return (keys[0] if keys else "~") + "::" + (segs[0] if segs else "~")

    ordered = sorted(groups.values(), key=group_sort_key)
    key_to_component: dict[tuple[str, str, str], str] = {}
    segment_to_component: dict[str, str] = {}
    results: list[ComponentResult] = []

    for idx, comp in enumerate(ordered, 1):
        cid = f"R{idx:03d}"
        comp.component_id = cid
        results.append(comp)
        for key in comp.keys:
            key_to_component[key] = cid
        # A decision segment has one component; blocked nodes may include IDs too.
        for sid in comp.segment_ids:
            segment_to_component.setdefault(sid, cid)

    # Attach segment/control issues.
    for comp in results:
        for sid in sorted(comp.segment_ids):
            seg = segment_by_id.get(sid)
            if seg and seg.issues:
                comp.issues.extend({"segment_id": sid, **iss} for iss in seg.issues)
                if seg.status != "valid":
                    comp.status = "blocked_evidence"
        for key in sorted(comp.keys):
            ctl = controls.get(key)
            if ctl and ctl.issues:
                comp.issues.extend({"control": key_object(key), **iss} for iss in ctl.issues)
                comp.status = "input_error"
            if key not in controls:
                comp.issues.append({
                    "code": "missing_control_total",
                    "control": key_object(key),
                    "message": "intersection produces this billing_day x rate cell but settlement control total is absent; zero must be explicit",
                })
                if comp.status == "settled":
                    comp.status = "input_error"
        for err in tariff_config_errors:
            # Attach only if path node is in this DSU root or scope intersects.
            path = err.get("path", "tariff-config")
            if ("T", path) in dsu.parent and dsu.find(("T", path)) == comp.root:
                comp.issues.append(err)
                comp.status = "input_error"

    atoms_by_key: dict[tuple[str, str, str], list[Atom]] = defaultdict(list)
    for atom in build.atoms:
        atoms_by_key[atom.key].append(atom)

    for comp in results:
        if comp.status != "settled":
            continue
        solve_component_flow(comp, build.atoms, atoms_by_key, controls, segment_by_id)

    # Map valid segments with no fractional edge to component via their first key,
    # only for output convenience.  Their row check itself is global.
    for atom in build.atoms:
        cid = key_to_component.get(atom.key)
        if cid and atom.segment_id not in segment_to_component:
            segment_to_component[atom.segment_id] = cid

    return results, key_to_component, segment_to_component


def solve_component_flow(
    comp: ComponentResult,
    all_atoms: list[Atom],
    atoms_by_key: dict[tuple[str, str, str], list[Atom]],
    controls: dict[tuple[str, str, str], Control],
    segment_by_id: dict[str, Segment],
) -> None:
    keys = sorted(comp.keys)
    # A component could theoretically contain a segment node from blocked incidence
    # but status would already be blocked. Here all status settled.
    segment_ids = sorted({a.segment_id for k in keys for a in atoms_by_key.get(k, [])})
    comp.segment_ids.update(segment_ids)

    comp.evidence["baseline_convention"] = (
        "allocated_wh = floor(q_wh) + z_e, z_e in {0,1} for non-integer q; integer q has z=0"
    )

    floor_segment: dict[str, int] = defaultdict(int)
    fractional_by_segment: dict[str, list[Atom]] = defaultdict(list)
    # Each atom has exactly one key; keys are unique across component iteration.
    segment_atoms: list[Atom] = []
    seen_aids: set[str] = set()
    for k in keys:
        for a in atoms_by_key.get(k, []):
            if a.atom_id not in seen_aids:
                seen_aids.add(a.atom_id)
                segment_atoms.append(a)
                floor_segment[a.segment_id] += a.floor_wh
                if a.rational_wh.denominator != 1:
                    fractional_by_segment[a.segment_id].append(a)

    r_by_seg: dict[str, int] = {}
    prechecks: list[dict] = []
    local_failure = False
    for sid in segment_ids:
        seg = segment_by_id[sid]
        assert seg.delta_wh is not None
        r = seg.delta_wh - floor_segment[sid]
        fcount = len(fractional_by_segment.get(sid, []))
        r_by_seg[sid] = r
        ok = 0 <= r <= fcount and isinstance(r, int)
        # Exact partition identity gives the stronger invariant when data are complete.
        prechecks.append({
            "segment_id": sid,
            "target_delta_wh": seg.delta_wh,
            "sum_floor_wh": floor_segment[sid],
            "required_ceil_increments": r,
            "fractional_slice_count": fcount,
            "local_bound_ok": ok,
        })
        if not ok:
            local_failure = True
            comp.issues.append({"code": "segment_rounding_bound_conflict", "segment_id": sid,
                                "required": r, "fractional_count": fcount})

    floor_control: dict[tuple[str, str, str], int] = {}
    fractional_by_key: dict[tuple[str, str, str], list[Atom]] = defaultdict(list)
    c_by_key: dict[tuple[str, str, str], int] = {}
    for key in keys:
        atoms = atoms_by_key.get(key, [])
        floor_ctl = sum(a.floor_wh for a in atoms)
        floor_control[key] = floor_ctl
        fracs = [a for a in atoms if a.rational_wh.denominator != 1]
        fractional_by_key[key] = fracs
        ctl = controls.get(key)
        if ctl is None:
            comp.status = "input_error"
            return
        c = ctl.total_wh - floor_ctl
        c_by_key[key] = c
        ok = 0 <= c <= len(fracs)
        prechecks.append({
            "control": key_object(key),
            "target_total_wh": ctl.total_wh,
            "sum_floor_wh": floor_ctl,
            "required_ceil_increments": c,
            "fractional_slice_count": len(fracs),
            "local_bound_ok": ok,
        })
        if not ok:
            local_failure = True
            if c < 0:
                code = "control_floor_baseline_exceeds_target"
                msg = "sum of mandatory floor Wh already exceeds settlement control total"
            else:
                code = "control_ceil_capacity_insufficient"
                msg = "even rounding every slice up cannot reach settlement control total"
            comp.issues.append({"code": code, "control": key_object(key), "target_wh": ctl.total_wh,
                                "floor_sum_wh": floor_ctl, "required": c,
                                "fractional_count": len(fracs), "message": msg})

    R = sum(r_by_seg.values())
    C = sum(c_by_key.values())
    comp.evidence["prechecks"] = prechecks
    comp.evidence["required_segment_increments"] = R
    comp.evidence["required_control_increments"] = C

    if local_failure:
        comp.status = "infeasible"
        comp.evidence["witness_type"] = "local_bound"
        return
    if R != C:
        comp.status = "infeasible"
        comp.evidence["witness_type"] = "component_total_mismatch"
        comp.evidence["gap_segment_minus_control_increments"] = R - C
        comp.issues.append({
            "code": "connected_range_total_conflict",
            "message": "required ceil increments differ between reading segments and control cells in this connected range",
            "segment_required_increments": R,
            "control_required_increments": C,
            "gap_wh": R - C,
            "verification": "sum(target segment Wh) - sum(all slice floor Wh) vs sum(control Wh) - sum(all slice floor Wh)",
        })
        return
    if R == 0:
        # All residual increments zero; every fractional edge must round down.
        for a in segment_atoms:
            comp.allocations[a.atom_id] = a.floor_wh
        comp.evidence["flow_wh"] = 0
        return

    seg_list = sorted(r_by_seg)
    key_list = keys
    idx = {"S": 0, "T": 1}
    for i, sid in enumerate(seg_list, start=2):
        idx[("S", sid)] = i
    row_base = 2 + len(seg_list)
    for j, key in enumerate(key_list):
        idx[("C", key)] = row_base + j
    sink = row_base + len(key_list)
    mf = Dinic(sink + 1)
    for sid in seg_list:
        mf.add_edge(0, idx[("S", sid)], r_by_seg[sid])

    edge_refs: dict[str, FlowEdge] = {}
    for sid in seg_list:
        for a in fractional_by_segment.get(sid, []):
            e = mf.add_edge(idx[("S", sid)], idx[("C", a.key)], 1, a.atom_id)
            edge_refs[a.atom_id] = e
    for key in key_list:
        mf.add_edge(idx[("C", key)], sink, c_by_key[key])

    flow = mf.max_flow(0, sink)
    comp.evidence["max_flow_increments"] = flow
    comp.evidence["target_flow_increments"] = R
    if flow != R:
        comp.status = "infeasible"
        reachable = mf.reachable(0)
        reachable_segments = [sid for sid in seg_list if idx[("S", sid)] in reachable]
        # Strong generalized Hall witness:
        # r(S) > sum_j min(c_j, number_of_fractional_edges_from_S_to_j).
        edge_count: dict[tuple[str, str, str], int] = defaultdict(int)
        witness_atom_ids: dict[tuple[str, str, str], list[str]] = defaultdict(list)
        for sid in reachable_segments:
            for a in fractional_by_segment.get(sid, []):
                edge_count[a.key] += 1
                witness_atom_ids[a.key].append(a.atom_id)
        capacity_rows = []
        capacity_sum = 0
        for key in key_list:
            ecount = edge_count.get(key, 0)
            cap = min(c_by_key[key], ecount)
            capacity_sum += cap
            if ecount:
                capacity_rows.append({
                    "control": key_object(key),
                    "fractional_edges_from_witness_segments": ecount,
                    "control_required_increments": c_by_key[key],
                    "usable_capacity_min": cap,
                    "atom_ids": witness_atom_ids[key],
                })
        required_sum = sum(r_by_seg[sid] for sid in reachable_segments)
        comp.evidence["witness_type"] = "generalized_hall_min_cut"
        comp.evidence["hall_witness"] = {
            "subset_segments": reachable_segments,
            "required_ceil_increments_rS": required_sum,
            "maximum_increments_absorbable": capacity_sum,
            "strict_inequality": f"{required_sum} > {capacity_sum}",
            "columns": capacity_rows,
            "verification": (
                "For subset S, each control j can receive at most min(c_j, e(S,j)) selected slices "
                "from S. If sum r_i exceeds that sum, no 0/1 rounding exists."
            ),
        }
        comp.issues.append({
            "code": "no_integer_allocation_in_connected_range",
            "message": "max-flow cannot satisfy all segment and control integer totals; generalized Hall witness attached",
            "max_flow": flow,
            "required_flow": R,
        })
        return

    for aid, e in edge_refs.items():
        # Initial capacity 1; final forward cap 0 means the binary increment is selected.
        used = 1 if e.cap == 0 else 0
        atom = atom_by_id_global(all_atoms, aid)
        comp.allocations[aid] = atom.floor_wh + used
    for a in segment_atoms:
        if a.atom_id not in comp.allocations:
            comp.allocations[a.atom_id] = a.floor_wh


def atom_by_id_global(atoms: list[Atom], aid: str) -> Atom:
    for a in atoms:
        if a.atom_id == aid:
            return a
    raise KeyError(aid)


# ---------------------------------------------------------------------------
# Audit report
# ---------------------------------------------------------------------------


def build_report(
    input_path: Optional[Path],
    raw_bytes: Optional[bytes],
    root: dict,
    segments: list[Segment],
    excluded_readings: list[dict],
    global_issues: list[dict],
    build: BuildResult,
    controls: dict[tuple[str, str, str], Control],
    components: list[ComponentResult],
    key_to_component: dict[tuple[str, str, str], str],
    tariff_config_errors: list[dict],
) -> dict:
    atom_by_id = build.atom_by_id
    component_by_id = {c.component_id: c for c in components}

    # Final atom values. Fixed integer atoms have a unique forced value even in
    # an otherwise infeasible component; fractional values remain null there.
    final_allocation: dict[str, Optional[int]] = {}
    for atom in build.atoms:
        cid = key_to_component.get(atom.key)
        comp = component_by_id.get(cid) if cid else None
        if comp and comp.status == "settled" and atom.atom_id in comp.allocations:
            final_allocation[atom.atom_id] = comp.allocations[atom.atom_id]
        elif atom.rational_wh.denominator == 1:
            final_allocation[atom.atom_id] = atom.floor_wh
        else:
            final_allocation[atom.atom_id] = None

    slices: list[dict] = []
    amount_total = Fraction(0)
    allocated_energy_total = 0
    for atom in sorted(build.atoms, key=lambda a: a.atom_id):
        value = final_allocation[atom.atom_id]
        amount = Fraction(value) * atom.price if value is not None else None
        if value is not None and amount is not None:
            amount_total += amount
            allocated_energy_total += value
        rounding = "unresolved"
        if value is not None:
            if atom.rational_wh.denominator == 1:
                rounding = "exact_integer"
            elif value == atom.floor_wh:
                rounding = "floor"
            elif value == atom.ceil_wh:
                rounding = "ceil"
        slices.append({
            "atom_id": atom.atom_id,
            "component_id": key_to_component.get(atom.key),
            "segment_id": atom.segment_id,
            "control": key_object(atom.key),
            "interval_start_utc": abs_to_utc_iso(atom.start),
            "interval_end_utc": abs_to_utc_iso(atom.end),
            "duration_seconds": frac_str(atom.duration),
            "rational_wh": frac_str(atom.rational_wh),
            "floor_wh": atom.floor_wh,
            "ceil_wh": atom.ceil_wh,
            "allocated_wh": value,
            "rounding": rounding,
            "unit_price": frac_str(atom.price),
            "amount_fraction": frac_str(amount),
            "basis": atom.basis,
        })

    segment_checks = build_segment_checks(segments, build, atom_by_id, final_allocation)
    component_status = {cid: comp.status for cid, comp in component_by_id.items()}
    control_checks = build_control_checks(
        build, controls, atom_by_id, final_allocation, key_to_component, component_status
    )

    serialized_components = []
    bad_component_count = 0
    settled_component_count = 0
    for comp in components:
        if comp.status == "settled":
            settled_component_count += 1
        else:
            bad_component_count += 1
        comp_atoms = [a for a in build.atoms if key_to_component.get(a.key) == comp.component_id]
        comp_amount = Fraction(0)
        comp_wh = 0
        all_have_values = True
        for a in comp_atoms:
            v = final_allocation.get(a.atom_id)
            if v is None:
                all_have_values = False
            else:
                comp_wh += v
                comp_amount += Fraction(v) * a.price
        serialized_components.append({
            "component_id": comp.component_id,
            "status": comp.status,
            "segment_ids": sorted(comp.segment_ids),
            "control_keys": [key_object(k) for k in sorted(comp.keys)],
            "atom_ids": [a.atom_id for a in sorted(comp_atoms, key=lambda x: x.atom_id)],
            "slice_count": len(comp_atoms),
            "all_allocated": all_have_values and comp.status == "settled",
            "allocated_wh_if_complete": comp_wh if all_have_values else None,
            "amount_fraction_if_complete": frac_str(comp_amount) if all_have_values else None,
            "issues": comp.issues,
            "evidence": comp.evidence,
        })

    quarantined_segments = []
    for seg in segments:
        if seg.status != "valid":
            inc = next((x for x in build.blocked_incidences if x["segment_id"] == seg.segment_id), None)
            quarantined_segments.append({
                "segment_id": seg.segment_id,
                "meter_id": seg.meter_id,
                "status": seg.status,
                "start_utc": abs_to_utc_iso(seg.start),
                "end_utc": abs_to_utc_iso(seg.end),
                "duration_seconds": frac_str(seg.end - seg.start),
                "raw_difference_wh": seg.delta_wh,
                "issues": seg.issues,
                "evidence": seg.evidence,
                "candidate_control_keys": (inc or {}).get("keys", []),
                "diagnostic_intervals": (inc or {}).get("diagnostic_intervals", []),
            })

    verification = {
        "half_open_interval_rule": HALF_OPEN_NOTE,
        "uniform_distribution_rule": "q_atom = delta_wh * overlap_UTC_seconds / segment_UTC_seconds",
        "rounding_rule": "each atom is allocated floor(q) or ceil(q); integer q is fixed",
        "money_rule": "amount = allocated_wh * input_unit_price; Fraction arithmetic, no extra rounding",
        "segment_checks": segment_checks,
        "control_checks": control_checks,
        "all_segment_sums_ok": all(x["status"] == "verified" for x in segment_checks if x["segment_status"] == "valid"),
        "all_control_sums_ok": all(x["status"] == "verified" for x in control_checks),
        "global_issues": global_issues + tariff_config_errors,
    }

    if global_issues or tariff_config_errors:
        overall = "input_error"
    elif bad_component_count == 0 and verification["all_segment_sums_ok"] and verification["all_control_sums_ok"]:
        overall = "settled"
    elif settled_component_count > 0:
        overall = "partial_settlement"
    elif bad_component_count > 0:
        overall = "not_settleable"
    else:
        overall = "verification_failed"

    report = {
        "overall_status": overall,
        "currency": root.get("currency"),
        "rule_version": {
            "input_rule_version": root.get("rule_version", "unspecified"),
            "engine_rule_version": ENGINE_RULE_VERSION,
            "calendar_rule_version": root.get("billing_calendar", {}).get("rule_version") if isinstance(root.get("billing_calendar"), dict) else None,
        },
        "input_audit": {
            "path": str(input_path) if input_path else None,
            "sha256": hashlib.sha256(raw_bytes).hexdigest() if raw_bytes else None,
            "original_readings": root.get("readings", []),
            "exchange_vouchers": root.get("exchange_vouchers", root.get("meter_exchanges", [])),
            "excluded_readings": excluded_readings,
        },
        "totals": {
            "slice_count": len(slices),
            "allocated_wh_where_known": allocated_energy_total,
            "amount_fraction_where_known": frac_str(amount_total),
            "settled_components": settled_component_count,
            "rejected_or_blocked_components": bad_component_count,
        },
        "components": serialized_components,
        "slices": slices,
        "quarantined_segments": quarantined_segments,
        "verification": verification,
    }
    return report


def build_segment_checks(
    segments: list[Segment],
    build: BuildResult,
    atom_by_id: dict[str, Atom],
    final_allocation: dict[str, Optional[int]],
) -> list[dict]:
    out = []
    for seg in segments:
        ids = build.segment_atom_ids.get(seg.segment_id, [])
        atoms = [atom_by_id[aid] for aid in ids]
        rational_sum = sum((a.rational_wh for a in atoms), Fraction(0))
        floor_sum = sum(a.floor_wh for a in atoms)
        values = [final_allocation.get(a.atom_id) for a in atoms]
        known = [v for v in values if v is not None]
        complete = len(known) == len(values) and seg.status == "valid"
        alloc_sum = sum(known) if complete else None
        bounds_ok = all(
            v is not None and a.floor_wh <= v <= a.ceil_wh
            for a, v in zip(atoms, values) if v is not None
        )
        rational_partition_ok = seg.delta_wh is not None and rational_sum == seg.delta_wh
        sum_ok = complete and alloc_sum == seg.delta_wh
        status = "not_applicable_quarantined" if seg.status != "valid" else ("verified" if sum_ok and bounds_ok and rational_partition_ok else "not_settled")
        out.append({
            "segment_id": seg.segment_id,
            "meter_id": seg.meter_id,
            "segment_status": seg.status,
            "target_delta_wh": seg.delta_wh,
            "atom_ids": ids,
            "rational_sum_wh": frac_str(rational_sum),
            "floor_sum_wh": floor_sum,
            "allocated_sum_wh": alloc_sum,
            "rational_partition_ok": rational_partition_ok,
            "floor_ceil_bounds_ok_for_known_values": bounds_ok,
            "sum_matches_original_difference": sum_ok,
            "status": status,
        })
    return out


def build_control_checks(
    build: BuildResult,
    controls: dict[tuple[str, str, str], Control],
    atom_by_id: dict[str, Atom],
    final_allocation: dict[str, Optional[int]],
    key_to_component: dict[tuple[str, str, str], str],
    component_status: dict[str, str],
) -> list[dict]:
    by_key: dict[tuple[str, str, str], list[Atom]] = defaultdict(list)
    for a in build.atoms:
        by_key[a.key].append(a)
    out = []
    all_keys = set(controls) | set(by_key)
    for key in sorted(all_keys):
        atoms = by_key.get(key, [])
        ctl = controls.get(key)
        target = ctl.total_wh if ctl else None
        floor_sum = sum(a.floor_wh for a in atoms)
        ceil_sum = sum(a.ceil_wh for a in atoms)
        rational_sum = sum((a.rational_wh for a in atoms), Fraction(0))
        vals = [final_allocation.get(a.atom_id) for a in atoms]
        complete = all(v is not None for v in vals) and ctl is not None
        alloc_sum = sum(v for v in vals if v is not None) if complete else None
        ok = complete and alloc_sum == target
        cid = key_to_component.get(key)
        cstatus = component_status.get(cid) if cid else None
        if ctl is None:
            status = "missing_control"
        elif cstatus is not None and cstatus != "settled":
            # An empty/forced arithmetic sum may coincide with the target, but
            # the connected range has no certifiable complete settlement.
            status = f"component_{cstatus}"
        elif ok:
            status = "verified"
        else:
            status = "not_settled"
        out.append({
            "control": key_object(key),
            "component_id": cid,
            "component_status": cstatus,
            "control_id": ctl.control_id if ctl else None,
            "target_total_wh": target,
            "atom_ids": [a.atom_id for a in sorted(atoms, key=lambda x: x.atom_id)],
            "rational_sum_wh": frac_str(rational_sum),
            "floor_sum_wh": floor_sum,
            "ceil_sum_wh": ceil_sum,
            "minimum_integer_wh": floor_sum,
            "maximum_integer_wh": ceil_sum,
            "allocated_sum_wh": alloc_sum,
            "sum_matches_control": ok,
            "status": status,
            "issues": ctl.issues if ctl else [{"code": "missing_control_total"}],
        })
    return out


def write_slices_csv(report: dict, path: Path) -> None:
    import csv
    fields = [
        "atom_id", "component_id", "rounding", "allocated_wh", "rational_wh",
        "floor_wh", "ceil_wh", "segment_id", "scope_id", "billing_day", "rate_id",
        "interval_start_utc", "interval_end_utc", "duration_seconds",
        "unit_price", "amount_fraction", "tariff_version", "binding_id",
    ]
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for s in report["slices"]:
            w.writerow({
                "atom_id": s["atom_id"],
                "component_id": s["component_id"],
                "rounding": s["rounding"],
                "allocated_wh": "" if s["allocated_wh"] is None else s["allocated_wh"],
                "rational_wh": s["rational_wh"],
                "floor_wh": s["floor_wh"],
                "ceil_wh": s["ceil_wh"],
                "segment_id": s["segment_id"],
                "scope_id": s["control"]["scope_id"],
                "billing_day": s["control"]["billing_day"],
                "rate_id": s["control"]["rate_id"],
                "interval_start_utc": s["interval_start_utc"],
                "interval_end_utc": s["interval_end_utc"],
                "duration_seconds": s["duration_seconds"],
                "unit_price": s["unit_price"],
                "amount_fraction": s["amount_fraction"] or "",
                "tariff_version": s["basis"].get("tariff_version"),
                "binding_id": s["basis"].get("binding_id"),
            })


# ---------------------------------------------------------------------------
# Planned collection-window reconciliation
# ---------------------------------------------------------------------------
#
# This section is independent from the settlement pipeline above.  It audits
# planned collection windows against actual readings and the binding rows that
# were effective at each reading instant.  It does not require tariffs or
# control totals, does not alter any settlement result, and never selects a
# reading by nearest time.

RECONCILIATION_RULE_VERSION = "planned-window-reconciliation-1.0.0"
WINDOW_HALF_OPEN_NOTE = (
    "Planned windows and binding intervals are half-open [start, end); "
    "a reading exactly at a window end belongs to the next window, if any."
)
RECONCILIATION_STATUS_LABELS = {
    "matched": "实到",
    "pending_review": "待核",
    "missed": "漏采",
}
RECONCILIATION_STATUS_MEANINGS = {
    "matched": "窗口恰有一条有效候选读数，且该读数只可能归属这一个计划窗口",
    "pending_review": "窗口有多条候选读数，或读数/窗口归属不能唯一判定；不做静默挑选",
    "missed": "窗口没有任何有效候选读数",
}
RECONCILIATION_MATCHING_RULE = (
    "A reading is eligible for a window only if its UTC instant is inside the "
    "half-open window AND at that instant the reading's meter has a valid binding "
    "whose point_id equals the window point_id. A reading whose active bindings name "
    "several points is eligible for each of those points' windows but is never "
    "selected, because its attribution is ambiguous. A window with two or more "
    "eligible readings, or with an ambiguous-binding reading, is pending_review. A "
    "window with exactly one unambiguous eligible reading is matched only when that "
    "reading is eligible for no other window; otherwise every window sharing the "
    "reading is pending_review. A window with no eligible reading is missed. A "
    "reading can be selected for at most one window. Nearest-time selection is "
    "never performed."
)
WINDOW_START_KEYS = (
    "expected_from", "expected_start", "planned_from", "window_from", "start", "from",
)
WINDOW_END_KEYS = (
    "expected_to", "expected_end", "planned_to", "window_to", "end", "to",
)
POINT_KEYS = ("point_id", "metering_point_id", "metering_point")
METER_KEYS = ("meter_id", "meter")
READING_STATUS_EXCLUDED = {"void", "invalid", "cancelled", "excluded"}

UNMATCHED_REASON_MEANINGS = {
    "selected_for_no_window": "no window could take this reading",
    "no_effective_binding_at_reading_time": "no valid binding row covers this meter at the reading instant",
    "binding_without_point_id": "the effective binding does not identify a metering point",
    "ambiguous_binding_at_reading_time": "multiple active bindings map this meter to different points at the reading instant",
    "binding_point_not_in_containing_window": "the reading is inside a planned window, but its bound point is not the window point",
    "candidate_but_not_uniquely_assigned": "the reading is eligible for more than one window, or its window is not uniquely resolvable",
    "not_inside_any_planned_window": "the reading instant is outside every planned window",
}


@dataclass
class PlannedWindow:
    window_id: str
    point_id: str
    start: Fraction
    end: Fraction
    start_text: str
    end_text: str
    path: str
    raw: dict


@dataclass
class ReconcileBinding:
    binding_id: str
    meter_id: str
    point_id: Optional[str]
    scope_id: Optional[str]
    start: Fraction
    end: Fraction
    start_text: str
    end_text: str
    path: str
    raw: dict


@dataclass
class ReconcileReading:
    reading_id: str
    meter_id: Optional[str]
    sec: Optional[Fraction]
    ts_text: Optional[str]
    value: Optional[int]
    declared_point_id: Optional[str]
    path: str
    raw: dict
    excluded_reason: Optional[str] = None
    warnings: list[dict] = field(default_factory=list)


def _first_present(raw: dict, keys: Iterable[str]) -> tuple[Optional[Any], Optional[str]]:
    for key in keys:
        if key in raw and raw[key] is not None and raw[key] != "":
            return raw[key], key
    return None, None


def _load_planned_windows(root: dict) -> tuple[list[PlannedWindow], str]:
    source_key: Optional[str] = None
    raw_windows: Any = None
    for key in ("planned_windows", "collection_windows", "planned_collection_windows"):
        if key in root:
            source_key, raw_windows = key, root[key]
            break
    if source_key is None:
        raise InputError(
            "planned_windows (or collection_windows/planned_collection_windows) is required for reconciliation"
        )
    if not isinstance(raw_windows, list):
        raise InputError(f"{source_key} must be a list")

    windows: list[PlannedWindow] = []
    seen_ids: set[str] = set()
    for i, raw in enumerate(raw_windows):
        path = f"{source_key}[{i}]"
        if not isinstance(raw, dict):
            raise InputError(f"{path} must be an object")
        wid_raw = raw.get("window_id", raw.get("id"))
        wid = str(wid_raw) if wid_raw is not None and str(wid_raw) != "" else f"W{i + 1}"
        if wid in seen_ids:
            raise InputError(f"{path}: duplicate planned window_id {wid}")
        seen_ids.add(wid)

        point_raw, _ = _first_present(raw, POINT_KEYS)
        if point_raw is None:
            raise InputError(f"{path}: point_id (metering point) is required")
        start_raw, start_key = _first_present(raw, WINDOW_START_KEYS)
        end_raw, end_key = _first_present(raw, WINDOW_END_KEYS)
        if start_raw is None or end_raw is None:
            raise InputError(f"{path}: expected_from and expected_to are required half-open bounds")
        start = parse_instant(start_raw, f"{path}.{start_key}")
        end = parse_instant(end_raw, f"{path}.{end_key}")
        if end.sec <= start.sec:
            raise InputError(f"{path}: planned window must have positive UTC duration")

        windows.append(PlannedWindow(
            window_id=wid,
            point_id=str(point_raw),
            start=start.sec,
            end=end.sec,
            start_text=start.text,
            end_text=end.text,
            path=path,
            raw=raw,
        ))
    return windows, source_key


def _load_reconcile_bindings(root: dict) -> tuple[list[ReconcileBinding], list[dict]]:
    raw_bindings = root.get("bindings", [])
    if not isinstance(raw_bindings, list):
        raise InputError("bindings must be a list")

    bindings: list[ReconcileBinding] = []
    excluded: list[dict] = []
    id_counts: Counter[str] = Counter()
    for i, raw in enumerate(raw_bindings):
        path = f"bindings[{i}]"
        if not isinstance(raw, dict):
            raise InputError(f"{path} must be an object")
        bid_raw = raw.get("binding_id", raw.get("id"))
        bid = str(bid_raw) if bid_raw is not None and str(bid_raw) != "" else f"B{i + 1}"
        id_counts[bid] += 1

        valid = raw.get("valid", True)
        status = str(raw.get("status", "valid")).lower()
        if valid is False or status in READING_STATUS_EXCLUDED:
            excluded.append({
                "binding_id": bid,
                "path": path,
                "reason": "binding_marked_invalid_or_void",
                "status": status,
                "raw": raw,
            })
            continue

        meter_raw, _ = _first_present(raw, METER_KEYS)
        if meter_raw is None:
            raise InputError(f"{path}: meter_id is required")
        point_raw, _ = _first_present(raw, POINT_KEYS)
        start_raw, start_key = _first_present(raw, ("valid_from", "start", "from"))
        end_raw, end_key = _first_present(raw, ("valid_to", "end", "to"))
        if start_raw is None or end_raw is None:
            raise InputError(f"{path}: valid_from and valid_to are required half-open bounds")
        start = parse_instant(start_raw, f"{path}.{start_key}")
        end = parse_instant(end_raw, f"{path}.{end_key}")
        if end.sec <= start.sec:
            raise InputError(f"{path}: binding interval must have positive UTC duration")

        bindings.append(ReconcileBinding(
            binding_id=bid,
            meter_id=str(meter_raw),
            point_id=(str(point_raw) if point_raw is not None else None),
            scope_id=(str(raw["scope_id"]) if raw.get("scope_id") is not None else None),
            start=start.sec,
            end=end.sec,
            start_text=start.text,
            end_text=end.text,
            path=path,
            raw=raw,
        ))

    for bid, count in sorted(id_counts.items()):
        if count > 1:
            excluded.append({
                "binding_id": bid,
                "path": None,
                "reason": "duplicate_binding_id",
                "severity": "warning",
                "occurrences": count,
                "message": "binding_id is not unique; every matching row is reported separately in binding evidence",
            })
    return bindings, excluded


def _load_reconcile_readings(root: dict) -> tuple[list[ReconcileReading], list[dict]]:
    raw_readings = root.get("readings", [])
    if not isinstance(raw_readings, list):
        raise InputError("readings must be a list")

    readings: list[ReconcileReading] = []
    excluded: list[dict] = []
    seen_ids: set[str] = set()
    for i, raw in enumerate(raw_readings):
        path = f"readings[{i}]"
        if not isinstance(raw, dict):
            raise InputError(f"{path} must be an object")
        rid_raw = raw.get("reading_id", raw.get("id"))
        rid = str(rid_raw) if rid_raw is not None and str(rid_raw) != "" else f"reading-{i + 1}"
        if rid in seen_ids:
            raise InputError(f"{path}: duplicate reading_id {rid}")
        seen_ids.add(rid)

        valid = raw.get("valid", True)
        status = str(raw.get("status", "valid")).lower()
        excluded_reason: Optional[str] = None
        if valid is False or status in READING_STATUS_EXCLUDED:
            excluded_reason = f"reading_marked_{status if status != 'valid' else 'invalid'}"
            excluded.append({
                "reading_id": rid,
                "path": path,
                "reason": excluded_reason,
                "raw": raw,
            })

        meter_raw, _ = _first_present(raw, METER_KEYS)
        ts_raw, ts_key = _first_present(raw, ("ts", "timestamp", "time"))
        value_raw, _ = _first_present(raw, ("value_wh", "value", "cumulative_wh"))
        point_raw, _ = _first_present(raw, POINT_KEYS)

        if excluded_reason is None:
            if meter_raw is None:
                raise InputError(f"{path}: meter_id is required")
            if ts_raw is None:
                raise InputError(f"{path}: ts is required")
            instant = parse_instant(ts_raw, f"{path}.{ts_key}")
            sec: Optional[Fraction] = instant.sec
            ts_text: Optional[str] = instant.text
            value: Optional[int] = require_int(value_raw, f"{path}.value_wh")
            warnings: list[dict] = []
        else:
            # Invalid/void rows remain visible as evidence but are never candidates.
            warnings = []
            sec = None
            ts_text = str(ts_raw) if ts_raw is not None else None
            if ts_raw is not None:
                try:
                    instant = parse_instant(ts_raw, f"{path}.{ts_key}")
                    sec, ts_text = instant.sec, instant.text
                except InputError:
                    warnings.append({
                        "code": "excluded_reading_timestamp_unparsable",
                        "severity": "warning",
                        "message": "excluded reading timestamp is not parseable; raw row retained",
                    })
            value = value_raw if isinstance(value_raw, int) and not isinstance(value_raw, bool) else None

        readings.append(ReconcileReading(
            reading_id=rid,
            meter_id=(str(meter_raw) if meter_raw is not None else None),
            sec=sec,
            ts_text=ts_text,
            value=value,
            declared_point_id=(str(point_raw) if point_raw is not None else None),
            path=path,
            raw=raw,
            excluded_reason=excluded_reason,
            warnings=warnings,
        ))
    return readings, excluded


def _active_bindings(
    bindings: list[ReconcileBinding], meter_id: Optional[str], sec: Fraction
) -> list[ReconcileBinding]:
    if meter_id is None:
        return []
    return [b for b in bindings if b.meter_id == meter_id and b.start <= sec < b.end]


def _binding_state(active: list[ReconcileBinding]) -> tuple[str, list[str]]:
    if not active:
        return "no_active_binding", []
    points = sorted({b.point_id for b in active if b.point_id is not None})
    if not points:
        return "binding_without_point_id", []
    if len(points) > 1:
        return "ambiguous_binding", points
    return "unique_point", points


def _binding_evidence(b: ReconcileBinding) -> dict:
    return {
        "binding_id": b.binding_id,
        "meter_id": b.meter_id,
        "point_id": b.point_id,
        "scope_id": b.scope_id,
        "declared_valid_from": b.start_text,
        "declared_valid_to": b.end_text,
        "valid_from_utc": abs_to_utc_iso(b.start),
        "valid_to_utc": abs_to_utc_iso(b.end),
        "path": b.path,
        "raw": b.raw,
    }


def _reading_evidence(
    reading: ReconcileReading,
    active: list[ReconcileBinding],
    contained: list[PlannedWindow],
    candidates: list[PlannedWindow],
    state: str,
    bound_points: list[str],
) -> dict:
    warnings = list(reading.warnings)
    if (
        reading.declared_point_id is not None
        and bound_points
        and reading.declared_point_id not in bound_points
    ):
        warnings.append({
            "code": "declared_point_differs_from_binding",
            "severity": "warning",
            "message": "reading row declares a point that differs from its effective binding; binding evidence governs",
            "declared_point_id": reading.declared_point_id,
            "bound_point_ids": bound_points,
        })
    if len(active) > 1 and state == "unique_point":
        warnings.append({
            "code": "multiple_active_binding_rows_same_point",
            "severity": "warning",
            "message": "more than one active binding row maps this meter to the same point; all rows are retained as evidence",
            "binding_ids": [b.binding_id for b in active],
        })
    return {
        "reading_id": reading.reading_id,
        "meter_id": reading.meter_id,
        "declared_ts": reading.ts_text,
        "ts_utc": abs_to_utc_iso(reading.sec) if reading.sec is not None else None,
        "value_wh": reading.value,
        "path": reading.path,
        "binding_state": state,
        "bound_point_ids": bound_points,
        "attribution_ambiguous": state == "ambiguous_binding",
        "active_bindings": [_binding_evidence(b) for b in active],
        "contained_window_ids": [w.window_id for w in contained],
        "candidate_window_ids": [w.window_id for w in candidates],
        "warnings": warnings,
        "raw": reading.raw,
    }


def _unmatched_reason(
    state: str,
    contained: list[PlannedWindow],
    candidates: list[PlannedWindow],
) -> str:
    if state == "no_active_binding":
        return "no_effective_binding_at_reading_time"
    if state == "binding_without_point_id":
        return "binding_without_point_id"
    if state == "ambiguous_binding":
        return "ambiguous_binding_at_reading_time"
    if candidates:
        return "candidate_but_not_uniquely_assigned"
    if contained:
        return "binding_point_not_in_containing_window"
    return "not_inside_any_planned_window"


def _overlapping_point_bindings(
    bindings: list[ReconcileBinding], window: PlannedWindow
) -> list[dict]:
    out: list[dict] = []
    for b in bindings:
        if b.point_id != window.point_id:
            continue
        overlap_start = max(b.start, window.start)
        overlap_end = min(b.end, window.end)
        if overlap_end <= overlap_start:
            continue
        out.append({
            "binding_id": b.binding_id,
            "meter_id": b.meter_id,
            "point_id": b.point_id,
            "scope_id": b.scope_id,
            "declared_valid_from": b.start_text,
            "declared_valid_to": b.end_text,
            "valid_from_utc": abs_to_utc_iso(b.start),
            "valid_to_utc": abs_to_utc_iso(b.end),
            "overlap_start_utc": abs_to_utc_iso(overlap_start),
            "overlap_end_utc": abs_to_utc_iso(overlap_end),
            "path": b.path,
            "raw": b.raw,
        })
    out.sort(key=lambda x: (x["valid_from_utc"], x["binding_id"], x["meter_id"]))
    return out


def reconcile_windows(
    root: dict,
    input_path: Optional[Path] = None,
    raw_bytes: Optional[bytes] = None,
) -> dict:
    """Audit planned collection windows against readings and effective bindings.

    Pure read-only analysis: no settlement data is modified or recomputed.
    """
    windows, windows_source = _load_planned_windows(root)
    bindings, excluded_bindings = _load_reconcile_bindings(root)
    readings, excluded_readings = _load_reconcile_readings(root)

    active_by_reading: dict[str, list[ReconcileBinding]] = {}
    state_by_reading: dict[str, str] = {}
    points_by_reading: dict[str, list[str]] = {}
    contained_by_reading: dict[str, list[PlannedWindow]] = {}
    candidates_by_reading: dict[str, list[PlannedWindow]] = {}

    for reading in readings:
        if reading.excluded_reason is not None or reading.sec is None:
            continue
        active = _active_bindings(bindings, reading.meter_id, reading.sec)
        state, points = _binding_state(active)
        contained = [w for w in windows if w.start <= reading.sec < w.end]
        if state in ("unique_point", "ambiguous_binding"):
            candidates = [w for w in contained if w.point_id in points]
        else:
            candidates = []
        active_by_reading[reading.reading_id] = active
        state_by_reading[reading.reading_id] = state
        points_by_reading[reading.reading_id] = points
        contained_by_reading[reading.reading_id] = contained
        candidates_by_reading[reading.reading_id] = candidates

    reading_ids_by_window: dict[str, list[str]] = {w.window_id: [] for w in windows}
    for reading in readings:
        for w in candidates_by_reading.get(reading.reading_id, []):
            reading_ids_by_window[w.window_id].append(reading.reading_id)
    for ids in reading_ids_by_window.values():
        ids.sort()

    window_status: dict[str, str] = {}
    selected_by_window: dict[str, str] = {}
    window_issues: dict[str, list[dict]] = {}
    selected_reading_ids: set[str] = set()

    for window in windows:
        candidate_ids = reading_ids_by_window[window.window_id]
        issues: list[dict] = []
        ambiguous_ids = [
            rid for rid in candidate_ids
            if state_by_reading.get(rid) == "ambiguous_binding"
        ]
        if not candidate_ids:
            status = "missed"
            issues.append({
                "code": "no_candidate_reading",
                "message": "no reading both falls inside the window and is bound to the window point at its instant",
            })
        elif ambiguous_ids:
            status = "pending_review"
            issues.append({
                "code": "ambiguous_binding_at_reading_time",
                "message": "a reading inside the window is bound to several points at its instant; attribution is not unique",
                "reading_ids": ambiguous_ids,
                "candidate_reading_ids": candidate_ids,
            })
        elif len(candidate_ids) > 1:
            status = "pending_review"
            issues.append({
                "code": "multiple_candidate_readings",
                "message": "more than one eligible reading falls inside the window; no reading is selected by proximity",
                "candidate_reading_ids": candidate_ids,
            })
        else:
            rid = candidate_ids[0]
            other_window_ids = [
                w.window_id for w in candidates_by_reading.get(rid, [])
                if w.window_id != window.window_id
            ]
            if other_window_ids:
                status = "pending_review"
                issues.append({
                    "code": "reading_shared_across_windows",
                    "message": "the only eligible reading is also eligible for other planned windows; attribution is not unique",
                    "reading_id": rid,
                    "other_window_ids": other_window_ids,
                })
            else:
                status = "matched"
                selected_by_window[window.window_id] = rid
                selected_reading_ids.add(rid)
        window_status[window.window_id] = status
        window_issues[window.window_id] = issues

    window_results: list[dict] = []
    readings_by_id = {r.reading_id: r for r in readings}
    for window in windows:
        candidate_ids = reading_ids_by_window[window.window_id]
        candidate_evidence = []
        for rid in candidate_ids:
            reading = readings_by_id[rid]
            candidate_evidence.append(_reading_evidence(
                reading,
                active_by_reading.get(rid, []),
                contained_by_reading.get(rid, []),
                candidates_by_reading.get(rid, []),
                state_by_reading.get(rid, "no_active_binding"),
                points_by_reading.get(rid, []),
            ))
        selected_id = selected_by_window.get(window.window_id)
        selected = next((x for x in candidate_evidence if x["reading_id"] == selected_id), None)
        status = window_status[window.window_id]
        # Every valid reading physically inside the window, even when its
        # point/binding makes it ineligible.  This lets a missed window be
        # traced to the raw rows that were collected but could not be
        # attributed, without ever promoting them to candidates.
        inside: list[dict] = []
        excluded_inside: list[dict] = []
        for reading in readings:
            if reading.sec is None or not (window.start <= reading.sec < window.end):
                continue
            if reading.excluded_reason is not None:
                excluded_inside.append({
                    "reading_id": reading.reading_id,
                    "meter_id": reading.meter_id,
                    "ts_utc": abs_to_utc_iso(reading.sec),
                    "value_wh": reading.value,
                    "exclusion_reason": reading.excluded_reason,
                    "path": reading.path,
                    "raw": reading.raw,
                })
                continue
            state = state_by_reading.get(reading.reading_id, "no_active_binding")
            bound_points = points_by_reading.get(reading.reading_id, [])
            if reading.reading_id in candidate_ids:
                attribution = "candidate_for_this_window"
            elif state == "no_active_binding":
                attribution = "no_effective_binding_at_reading_time"
            elif state == "binding_without_point_id":
                attribution = "binding_without_point_id"
            elif state == "ambiguous_binding":
                attribution = "ambiguous_binding_at_reading_time"
            elif window.point_id not in bound_points:
                attribution = "bound_to_other_point"
            else:
                attribution = "not_eligible"
            row = {
                "reading_id": reading.reading_id,
                "meter_id": reading.meter_id,
                "ts_utc": abs_to_utc_iso(reading.sec),
                "value_wh": reading.value,
                "binding_state": state,
                "bound_point_ids": bound_points,
                "attribution": attribution,
            }
            if attribution != "candidate_for_this_window":
                row["active_bindings"] = [_binding_evidence(b) for b in active_by_reading.get(reading.reading_id, [])]
            inside.append(row)
        window_results.append({
            "window_id": window.window_id,
            "point_id": window.point_id,
            "status": status,
            "status_label": RECONCILIATION_STATUS_LABELS[status],
            "status_meaning": RECONCILIATION_STATUS_MEANINGS[status],
            "expected_from": window.start_text,
            "expected_to": window.end_text,
            "start_utc": abs_to_utc_iso(window.start),
            "end_utc": abs_to_utc_iso(window.end),
            "duration_seconds": frac_str(window.end - window.start),
            "half_open": WINDOW_HALF_OPEN_NOTE,
            "selected_reading_id": selected_id,
            "selected_reading": selected,
            "candidate_reading_ids": candidate_ids,
            "candidate_readings": candidate_evidence,
            "readings_inside_window": inside,
            "excluded_readings_inside_window": excluded_inside,
            "issues": window_issues[window.window_id],
            "overlapping_point_bindings": _overlapping_point_bindings(bindings, window),
            "raw": window.raw,
        })

    unmatched_readings: list[dict] = []
    for reading in readings:
        if reading.excluded_reason is not None or reading.sec is None:
            continue
        if reading.reading_id in selected_reading_ids:
            continue
        contained = contained_by_reading.get(reading.reading_id, [])
        candidates = candidates_by_reading.get(reading.reading_id, [])
        state = state_by_reading.get(reading.reading_id, "no_active_binding")
        candidate_statuses = {
            w.window_id: window_status[w.window_id] for w in candidates
        }
        reason = _unmatched_reason(state, contained, candidates)
        evidence = _reading_evidence(
            reading,
            active_by_reading.get(reading.reading_id, []),
            contained,
            candidates,
            state,
            points_by_reading.get(reading.reading_id, []),
        )
        evidence.update({
            "reason": reason,
            "reason_detail": UNMATCHED_REASON_MEANINGS.get(reason),
            "candidate_window_statuses": candidate_statuses,
        })
        unmatched_readings.append(evidence)

    matched_count = sum(1 for w in windows if window_status[w.window_id] == "matched")
    pending_count = sum(1 for w in windows if window_status[w.window_id] == "pending_review")
    missed_count = sum(1 for w in windows if window_status[w.window_id] == "missed")
    valid_readings = [r for r in readings if r.excluded_reason is None and r.sec is not None]
    has_window_attention = pending_count > 0 or missed_count > 0

    if not windows:
        overall_status = "no_planned_windows"
    elif has_window_attention:
        overall_status = "attention_required"
    else:
        overall_status = "all_windows_matched"

    return {
        "report_type": "planned_window_reconciliation",
        "overall_status": overall_status,
        "attention_required": has_window_attention or bool(unmatched_readings),
        "window_attention_required": has_window_attention,
        "unmatched_readings_present": bool(unmatched_readings),
        "rule_version": {
            "input_rule_version": root.get("rule_version", "unspecified"),
            "engine_rule_version": RECONCILIATION_RULE_VERSION,
        },
        "input_audit": {
            "path": str(input_path) if input_path else None,
            "sha256": hashlib.sha256(raw_bytes).hexdigest() if raw_bytes else None,
            "planned_windows_source": windows_source,
            "reading_count": len(readings),
            "valid_reading_count": len(valid_readings),
            "binding_count": len(bindings),
            "excluded_reading_count": len(excluded_readings),
            "excluded_binding_count": len(excluded_bindings),
        },
        "rules": {
            "half_open": WINDOW_HALF_OPEN_NOTE,
            "candidate_rule": RECONCILIATION_MATCHING_RULE,
            "status_meaning": RECONCILIATION_STATUS_MEANINGS,
            "selection_rule": "a reading is selected only when it is the single unambiguous candidate of exactly one window",
            "duplicate_reading_rule": "one reading may be selected for at most one window",
        },
        "summary": {
            "planned_window_count": len(windows),
            "matched_window_count": matched_count,
            "pending_review_window_count": pending_count,
            "missed_window_count": missed_count,
            "valid_reading_count": len(valid_readings),
            "selected_reading_count": len(selected_reading_ids),
            "unmatched_reading_count": len(unmatched_readings),
            "excluded_reading_count": len(excluded_readings),
            "excluded_binding_count": len(excluded_bindings),
        },
        "windows": window_results,
        "unmatched_readings": unmatched_readings,
        "excluded_readings": excluded_readings,
        "excluded_bindings": excluded_bindings,
        "bindings_considered": [_binding_evidence(b) for b in bindings],
    }


def write_reconciliation_csv(report: dict, path: Path) -> None:
    import csv
    fields = [
        "window_id", "point_id", "status", "status_label",
        "expected_from", "expected_to", "start_utc", "end_utc", "duration_seconds",
        "selected_reading_id", "selected_reading_utc", "selected_meter_id", "selected_value_wh",
        "candidate_reading_ids", "candidate_count", "issue_codes",
    ]
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for w in report["windows"]:
            selected = w["selected_reading"]
            writer.writerow({
                "window_id": w["window_id"],
                "point_id": w["point_id"],
                "status": w["status"],
                "status_label": w["status_label"],
                "expected_from": w["expected_from"],
                "expected_to": w["expected_to"],
                "start_utc": w["start_utc"],
                "end_utc": w["end_utc"],
                "duration_seconds": w["duration_seconds"],
                "selected_reading_id": w["selected_reading_id"] or "",
                "selected_reading_utc": (selected or {}).get("ts_utc") or "",
                "selected_meter_id": (selected or {}).get("meter_id") or "",
                "selected_value_wh": "" if not selected or selected.get("value_wh") is None else selected["value_wh"],
                "candidate_reading_ids": ";".join(w["candidate_reading_ids"]),
                "candidate_count": len(w["candidate_reading_ids"]),
                "issue_codes": ";".join(x["code"] for x in w["issues"]),
            })

# ---------------------------------------------------------------------------
# Revision impact analysis (baseline settlement vs. candidate revision)
# ---------------------------------------------------------------------------
#
# This section is additive: it never changes settlement or planned-window
# reconciliation results.  Both inputs are run through the unchanged
# settlement pipeline and compared using semantic identities only:
#
#   * control cell     -> (scope_id, billing_day, rate_id)
#   * reading segment  -> (meter_id, start_utc, end_utc)
#
# Generated atom ids, component ids and JSON list positions are never used as
# identity, so re-ordering input records cannot create an added/removed/changed
# result on its own.
#
# A scope is comparable only when each side either has a complete, verified
# settlement for every control cell and every reading segment touching that
# scope, or has no data for that scope at all while the other side is complete.
# A blocked segment, an unsettled connected range, a missing/duplicate control
# total, an ambiguous binding/tariff, or a scope-less parser issue makes the
# scope not_comparable: both sides' conflict evidence is retained and no
# numeric delta is emitted.  Failure is never coerced to zero and is never
# silently paired with the other side.

REVISION_IMPACT_RULE_VERSION = "revision-impact-1.0.0"

REVISION_IMPACT_STATUS_LABELS = {
    "all_scopes_comparable": "全部结算范围可比",
    "partial_comparability": "部分结算范围可比",
    "not_comparable": "没有可比较的结算范围",
    "no_scopes": "没有结算范围",
    "fatal_input_error": "输入契约错误",
}

REVISION_COMPARABILITY_RULE = (
    "影响只在同一结算范围(scope_id)的两份输入都能形成完整、已复核的结算时报告；"
    "任一输入在该范围存在未结算的连通范围、阻断的读数段、缺失/重复控制总量、"
    "绑定或费率歧义时，该范围标为 not_comparable，保留双方冲突证据且不输出数值差。"
    "失败绝不当作零值，也不与其他侧数据静默配对。某侧完全不存在该范围的数据时，"
    "仅在另一侧完整可结算的前提下记为 added/removed；absence 不等同于 0。"
    "比较键为 (scope_id,billing_day,rate_id) 与 (meter_id,start_utc,end_utc)，"
    "不使用输入下标或生成的 atom_id，因此输入记录顺序变化不会制造变化。"
)

REVISION_IDENTITY_RULE = (
    "控制格身份=(scope_id,billing_day,rate_id)；读数段身份=(meter_id,start_utc,end_utc)。"
    "读数数值、凭证、绑定、费率版本、控制总量和分配结果只作为可比较的数值/证据，"
    "不参与身份键。读数时间戳改变会产生旧段 removed 与新段 added，这是真实变化而非顺序变化。"
)

REVISION_SETTLEMENT_BASIS_RULES = {
    "half_open_interval_rule": HALF_OPEN_NOTE,
    "uniform_distribution_rule": "q_atom = delta_wh * overlap_UTC_seconds / segment_UTC_seconds",
    "rounding_rule": "each atom is allocated floor(q) or ceil(q); integer q is fixed",
    "money_rule": "amount = allocated_wh * input_unit_price; Fraction arithmetic, no extra rounding",
    "change_categories": {
        "added": "候选修订中出现、基线中不存在的控制格或读数段",
        "removed": "基线中存在、候选修订中不存在的控制格或读数段",
        "changed": "两份输入都存在但数值、目标控制量、费率版本或绑定证据不同",
        "unchanged": "两份输入都存在且上述比较字段完全一致（不受影响）",
    },
}


def _rev_segment_identity(seg: Segment) -> dict:
    return {
        "meter_id": seg.meter_id,
        "start_utc": abs_to_utc_iso(seg.start),
        "end_utc": abs_to_utc_iso(seg.end),
    }


def _rev_segment_key(identity: dict) -> tuple[str, str, str]:
    return (identity["meter_id"], identity["start_utc"], identity["end_utc"])


def _rev_key_tuple(obj: dict) -> tuple[str, str, str]:
    return (obj["scope_id"], obj["billing_day"], obj["rate_id"])


def _rev_key_obj(key: tuple[str, str, str]) -> dict:
    return key_object(key)


def _rev_control_key_from_raw(c: dict) -> tuple[str, str, str]:
    return (
        str(c.get("scope_id") or c.get("settlement_scope_id") or ""),
        str(c.get("billing_day") or c.get("day") or ""),
        str(c.get("rate_id") or c.get("rate") or ""),
    )


def _rev_control_raw_rows(root: dict) -> dict[tuple[str, str, str], list[dict]]:
    raw = root.get("control_totals", root.get("controls", []))
    rows: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    if isinstance(raw, list):
        for i, c in enumerate(raw):
            if isinstance(c, dict):
                rows[_rev_control_key_from_raw(c)].append({"path": f"control_totals[{i}]", "raw": c})
    return rows


def _rev_segment_binding_evidence(
    seg: Segment, bindings: list[Binding]
) -> list[dict]:
    """Overlapping binding rows for a segment, used when no atom carries one."""
    out = []
    for b in sorted(bindings, key=lambda x: (x.binding_id, x.start, x.end)):
        if b.meter_id != seg.meter_id:
            continue
        if b.end <= seg.start or b.start >= seg.end:
            continue
        out.append({
            "binding_id": b.binding_id,
            "meter_id": b.meter_id,
            "point_id": b.point_id,
            "scope_id": b.scope_id,
            "valid_from_utc": abs_to_utc_iso(b.start),
            "valid_to_utc": abs_to_utc_iso(b.end),
            "raw": b.raw,
        })
    return out


def _rev_scope_ids_for_voucher(
    err: dict, root: dict, bindings: list[Binding]
) -> set[str]:
    vid = err.get("voucher_id")
    if not vid:
        return set()
    raw_vouchers = root.get("exchange_vouchers", root.get("meter_exchanges", []))
    if not isinstance(raw_vouchers, list):
        return set()
    scopes: set[str] = set()
    for v in raw_vouchers:
        if not isinstance(v, dict):
            continue
        if str(v.get("voucher_id") or v.get("id") or "") != str(vid):
            continue
        meters = {str(v.get("old_meter_id", "")), str(v.get("new_meter_id", ""))} - {""}
        for b in bindings:
            if b.meter_id in meters:
                scopes.add(b.scope_id)
    return scopes


def _rev_money(amount: Optional[Fraction]) -> Optional[str]:
    return frac_str(amount) if amount is not None else None


def _rev_slice_atom_view(sl: dict) -> dict:
    b = sl.get("basis") or {}
    # A complete but non-duplicated audit view: the volatile boilerplate strings
    # are emitted once per report, while every per-atom decision keeps its rule,
    # binding, tariff, calendar and exact rational evidence.
    return {
        "atom_id": sl["atom_id"],
        "component_id": sl["component_id"],
        "control": sl["control"],
        "segment_id": b.get("segment_id"),
        "rule_version": b.get("rule_version"),
        "allocation_convention": b.get("allocation_convention"),
        "half_open": b.get("half_open"),
        "interval_start_utc": sl["interval_start_utc"],
        "interval_end_utc": sl["interval_end_utc"],
        "duration_seconds": sl["duration_seconds"],
        "formula_wh": b.get("formula_wh"),
        "rational_wh": sl["rational_wh"],
        "floor_wh": sl["floor_wh"],
        "ceil_wh": sl["ceil_wh"],
        "allocated_wh": sl["allocated_wh"],
        "rounding": sl["rounding"],
        "unit_price": sl["unit_price"],
        "amount_fraction": sl["amount_fraction"],
        "binding_id": b.get("binding_id"),
        "point_id": b.get("point_id"),
        "scope_id": b.get("scope_id"),
        "calendar_id": b.get("calendar_id"),
        "timezone": b.get("timezone"),
        "billing_day_start_utc": b.get("billing_day_start_utc"),
        "billing_day_end_utc": b.get("billing_day_end_utc"),
        "tariff_version": b.get("tariff_version"),
        "rate_id": b.get("rate_id"),
        "binding_raw": b.get("binding_raw"),
        "tariff_basis": b.get("tariff_basis"),
    }


def _rev_build_side(
    label: str,
    root: dict,
    input_path: Optional[Path],
    raw_bytes: Optional[bytes],
) -> dict:
    """Run the untouched settlement pipeline and index its evidence semantically."""
    report = settle(root, input_path, raw_bytes)
    segments, _excluded, _reading_issues = build_segments(root)
    bindings = load_bindings(root)
    controls = load_controls(root)

    seg_identity_by_id = {s.segment_id: _rev_segment_identity(s) for s in segments}
    slices_by_seg: dict[str, list[dict]] = defaultdict(list)
    slices_by_key: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for sl in report["slices"]:
        slices_by_seg[sl["segment_id"]].append(sl)
        slices_by_key[_rev_key_tuple(sl["control"])].append(sl)

    control_check_by_key = {
        _rev_key_tuple(x["control"]): x for x in report["verification"]["control_checks"]
    }
    segment_check_by_id = {
        x["segment_id"]: x for x in report["verification"]["segment_checks"]
    }
    quarantined_by_id = {
        q["segment_id"]: q for q in report["quarantined_segments"]
    }
    raw_control_rows = _rev_control_raw_rows(root)

    # ---- control cells -------------------------------------------------
    cells: dict[tuple[str, str, str], dict] = {}
    for key in sorted(set(controls) | set(slices_by_key)):
        sl = sorted(slices_by_key.get(key, []), key=lambda s: s["atom_id"])
        check = control_check_by_key.get(key, {})
        ctl = controls.get(key)
        vals = [s["allocated_wh"] for s in sl]
        resolved = all(v is not None for v in vals)
        amounts = [s["amount_fraction"] for s in sl]
        amount: Optional[Fraction] = None
        if resolved and all(a is not None for a in amounts):
            amount = sum((Fraction(a) for a in amounts), Fraction(0))
        seg_alloc: dict[tuple[str, str, str], Optional[int]] = {}
        for s in sl:
            ident = seg_identity_by_id.get(s["segment_id"])
            if ident is not None:
                seg_alloc[_rev_segment_key(ident)] = s["allocated_wh"]
        cells[key] = {
            "control": _rev_key_obj(key),
            "present": bool(sl) or ctl is not None,
            "target_wh": ctl.total_wh if ctl is not None else None,
            "control_ids": ([ctl.control_id] if ctl is not None and ctl.control_id is not None else []),
            "allocated_wh": sum(vals) if resolved else None,
            "amount": amount,
            "unit_prices": sorted({s["unit_price"] for s in sl if s["unit_price"] is not None}),
            "tariff_versions": sorted({
                s["basis"].get("tariff_version") for s in sl
                if s["basis"].get("tariff_version") is not None
            }),
            "binding_ids": sorted({
                s["basis"].get("binding_id") for s in sl
                if s["basis"].get("binding_id") is not None
            }),
            "status": check.get("status"),
            "component_id": check.get("component_id"),
            "component_status": check.get("component_status"),
            "verified": check.get("status") == "verified",
            "issues": list(check.get("issues", [])) + (list(ctl.issues) if ctl is not None else []),
            "raw_control_rows": raw_control_rows.get(key, []),
            "segments": seg_alloc,
            "slices": [_rev_slice_atom_view(s) for s in sl],
        }

    # ---- reading segments ---------------------------------------------
    records: dict[tuple[str, str, str], dict] = {}
    for seg in segments:
        ident = _rev_segment_identity(seg)
        skey = _rev_segment_key(ident)
        sl = sorted(slices_by_seg.get(seg.segment_id, []), key=lambda s: s["atom_id"])
        check = segment_check_by_id.get(seg.segment_id, {})
        q = quarantined_by_id.get(seg.segment_id)

        scope_alloc: dict[str, dict] = {}
        for s in sl:
            scope = s["control"]["scope_id"]
            entry = scope_alloc.setdefault(scope, {
                "allocated_wh": 0,
                "amount": Fraction(0),
                "per_control": {},
                "complete": True,
            })
            control_key = _rev_key_tuple(s["control"])
            if s["allocated_wh"] is None:
                entry["complete"] = False
            else:
                entry["allocated_wh"] += s["allocated_wh"]
                entry["per_control"][control_key] = entry["per_control"].get(control_key, 0) + s["allocated_wh"]
            if s["amount_fraction"] is None:
                entry["complete"] = False
            else:
                entry["amount"] += Fraction(s["amount_fraction"])

        scope_ids = set(scope_alloc)
        if q is not None:
            for obj in q.get("candidate_control_keys", []):
                scope_ids.add(obj["scope_id"])
        binding_evidence = _rev_segment_binding_evidence(seg, bindings)
        if not scope_ids:
            scope_ids.update(b["scope_id"] for b in binding_evidence)

        complete = check.get("status") == "verified" and seg.status == "valid"
        if not complete:
            for entry in scope_alloc.values():
                entry["complete"] = False

        atom_views = [_rev_slice_atom_view(s) for s in sl]
        binding_evidence_by_id: dict[str, dict] = {}
        tariff_evidence: dict[tuple[str, str, str], dict] = {}
        for s in sl:
            basis = s["basis"]
            bid = basis.get("binding_id")
            if bid is not None and bid not in binding_evidence_by_id:
                binding_evidence_by_id[bid] = {
                    "binding_id": bid,
                    "point_id": basis.get("point_id"),
                    "scope_id": basis.get("scope_id"),
                    "calendar_id": basis.get("calendar_id"),
                    "raw": basis.get("binding_raw"),
                }
            tb = basis.get("tariff_basis") or {}
            tkey = (str(basis.get("tariff_version")), str(basis.get("rate_id")), str(basis.get("unit_price")))
            if tkey not in tariff_evidence:
                tariff_evidence[tkey] = {
                    "tariff_version": basis.get("tariff_version"),
                    "rate_id": basis.get("rate_id"),
                    "unit_price": basis.get("unit_price"),
                    "basis": tb,
                }
        if not binding_evidence_by_id:
            binding_evidence_by_id = {
                b["binding_id"]: {
                    "binding_id": b["binding_id"],
                    "point_id": b["point_id"],
                    "scope_id": b["scope_id"],
                    "calendar_id": None,
                    "raw": b["raw"],
                }
                for b in binding_evidence
            }

        records[skey] = {
            "identity": ident,
            "segment_ids": [seg.segment_id],
            "status": seg.status,
            "delta_wh": seg.delta_wh,
            "duration_seconds": frac_str(seg.end - seg.start),
            "complete": complete,
            "scope_ids": sorted(scope_ids),
            "scope_allocations": scope_alloc,
            "segment_check": check,
            "issues": list(seg.issues) + list((q or {}).get("issues", [])),
            "candidate_controls": list((q or {}).get("candidate_control_keys", [])),
            "diagnostic_intervals": list((q or {}).get("diagnostic_intervals", [])),
            "reading_evidence": {
                "start_anchors": seg.evidence.get("start_anchors", []),
                "end_anchors": seg.evidence.get("end_anchors", []),
                "warnings": seg.evidence.get("warnings", []),
                "interval_note": seg.evidence.get("interval_note"),
            },
            "binding_evidence": sorted(binding_evidence_by_id.values(), key=lambda x: str(x["binding_id"])),
            "tariff_evidence": sorted(tariff_evidence.values(), key=lambda x: (str(x["tariff_version"]), str(x["rate_id"]))),
            "atoms": atom_views,
        }

    # ---- scope-level conflict evidence ---------------------------------
    # A connected range can touch several scopes.  The full range evidence is
    # stored once in the side's catalog and each scope keeps a reference, so a
    # multi-scope conflict is not duplicated into every scope payload.
    range_evidence: dict[str, dict] = {}
    scope_conflicts: dict[str, list[dict]] = defaultdict(list)
    for comp in report["components"]:
        if comp["status"] == "settled":
            continue
        range_id = f"{label}:range:{comp['component_id']}"
        range_evidence[range_id] = {
            "range_id": range_id,
            "side": label,
            "component_id": comp["component_id"],
            "component_status": comp["status"],
            "segment_ids": sorted(comp["segment_ids"]),
            "control_keys": comp["control_keys"],
            "issues": [_rev_stable_issue(i) for i in comp["issues"]],
            "evidence": comp["evidence"],
        }
        for obj in comp["control_keys"]:
            scope_conflicts[obj["scope_id"]].append({
                "code": "connected_range_not_settled",
                "range_id": range_id,
                "component_id": comp["component_id"],
                "component_status": comp["status"],
                "control_keys": comp["control_keys"],
                "segment_ids": sorted(comp["segment_ids"]),
                "issues": [_rev_stable_issue(i) for i in comp["issues"]],
            })

    global_conflicts: list[dict] = []
    for err in report["verification"]["global_issues"]:
        scopes = set()
        if err.get("scope_id"):
            scopes.add(str(err["scope_id"]))
        scopes |= _rev_scope_ids_for_voucher(err, root, bindings)
        if scopes:
            for scope in sorted(scopes):
                scope_conflicts[scope].append({
                    "code": "input_issue_for_scope",
                    "issue": err,
                })
        else:
            global_conflicts.append({
                "code": "scope_not_identifiable",
                "issue": err,
                "message": (
                    "this issue cannot be attributed to one settlement scope because the input "
                    "does not name it; every reported scope on this side is treated as not_comparable"
                ),
            })

    return {
        "label": label,
        "path": str(input_path) if input_path else None,
        "sha256": hashlib.sha256(raw_bytes).hexdigest() if raw_bytes else None,
        "root_rule_version": root.get("rule_version", "unspecified"),
        "report": report,
        "overall_settlement_status": report["overall_status"],
        "segments": records,
        "controls": cells,
        "scope_conflicts": dict(scope_conflicts),
        "range_evidence": range_evidence,
        "global_conflicts": global_conflicts,
        "scope_ids": sorted(
            {k[0] for k in cells}
            | {scope for rec in records.values() for scope in rec["scope_ids"]}
            | set(scope_conflicts)
        ),
        "unscoped_segments": sorted(
            (
                {
                    "side": label,
                    "identity": rec["identity"],
                    "segment_ids": rec["segment_ids"],
                    "segment_status": rec["status"],
                    "raw_difference_wh": rec["delta_wh"],
                    "issues": [_rev_stable_issue(i) for i in rec["issues"]],
                    "reading_evidence": _rev_reading_evidence_view(rec["reading_evidence"]),
                    "binding_evidence": [
                        {**b, "raw": _rev_strip_path(b.get("raw"))} for b in rec["binding_evidence"]
                    ],
                    "candidate_controls": rec["candidate_controls"],
                    "reason": "no_resolvable_scope",
                    "message": "segment cannot be attributed to any settlement scope; retained without pairing",
                }
                for rec in records.values()
                if not rec["scope_ids"]
            ),
            key=lambda x: _rev_segment_key(x["identity"]),
        ),
    }


def _rev_strip_path(raw: Any) -> Any:
    """Drop positional ``path`` from retained raw evidence (order is not identity)."""
    if isinstance(raw, dict):
        return {k: _rev_strip_path(v) for k, v in raw.items() if k != "path"}
    if isinstance(raw, list):
        return [_rev_strip_path(v) for v in raw]
    return raw


def _rev_reading_evidence_view(ev: dict) -> dict:
    return {
        "start_anchors": [_rev_anchor_evidence(a) for a in ev.get("start_anchors", [])],
        "end_anchors": [_rev_anchor_evidence(a) for a in ev.get("end_anchors", [])],
        "warnings": [_rev_stable_issue(w) for w in ev.get("warnings", [])],
        "interval_note": ev.get("interval_note"),
    }


def _rev_stable_conflict(item: dict) -> dict:
    out = dict(item)
    if isinstance(out.get("issues"), list):
        out["issues"] = [_rev_stable_issue(i) for i in out["issues"]]
    if isinstance(out.get("issue"), dict):
        out["issue"] = _rev_stable_issue(out["issue"])
    if isinstance(out.get("reading_evidence"), dict):
        out["reading_evidence"] = _rev_reading_evidence_view(out["reading_evidence"])
    if isinstance(out.get("other_side_issues"), list):
        out["other_side_issues"] = [_rev_stable_issue(i) for i in out["other_side_issues"]]
    if isinstance(out.get("other_side_reading_evidence"), dict):
        out["other_side_reading_evidence"] = _rev_reading_evidence_view(out["other_side_reading_evidence"])
    return out


def _rev_side_scope_state(side: dict, scope: str) -> dict:
    """Presence and conflicts of one scope on one side."""
    cells = [c for k, c in side["controls"].items() if k[0] == scope]
    segs = [r for r in side["segments"].values() if scope in r["scope_ids"]]
    extra = side["scope_conflicts"].get(scope, [])
    globals_here = side["global_conflicts"]

    if not cells and not segs and not extra and not globals_here:
        return {"presence": "absent", "conflicts": []}

    conflicts: list[dict] = []
    for cell in sorted(cells, key=lambda c: _rev_key_tuple(c["control"])):
        if not cell["verified"]:
            conflicts.append({
                "code": "control_cell_not_settled",
                "control": cell["control"],
                "status": cell["status"],
                "component_status": cell["component_status"],
                "target_wh": cell["target_wh"],
                "allocated_wh": cell["allocated_wh"],
                "issues": [_rev_stable_issue(i) for i in cell["issues"]],
                "raw_control_rows": [{"raw": r["raw"]} for r in cell["raw_control_rows"]],
            })
    for rec in sorted(segs, key=lambda r: _rev_segment_key(r["identity"])):
        if not rec["complete"]:
            conflicts.append({
                "code": "reading_segment_not_settled",
                "segment": rec["identity"],
                "segment_status": rec["status"],
                "segment_check": rec["segment_check"],
                "raw_difference_wh": rec["delta_wh"],
                "issues": [_rev_stable_issue(i) for i in rec["issues"]],
                "candidate_controls": rec["candidate_controls"],
                "reading_evidence": _rev_reading_evidence_view(rec["reading_evidence"]),
                "binding_evidence": [
                    {**b, "raw": _rev_strip_path(b.get("raw"))} for b in rec["binding_evidence"]
                ],
            })
    for item in extra:
        conflicts.append(_rev_stable_conflict(item))
    for g in globals_here:
        conflicts.append(_rev_stable_conflict(g))

    return {
        "presence": "incomplete" if conflicts else "complete",
        "conflicts": conflicts,
    }


def _rev_norm_instant(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return abs_to_utc_iso(parse_instant(value).sec)
        except InputError:
            return value
    return value


def _rev_binding_signature(raw: Optional[dict]) -> Optional[str]:
    if not isinstance(raw, dict):
        return None
    payload = {
        "meter_id": raw.get("meter_id"),
        "scope_id": raw.get("scope_id", raw.get("settlement_scope_id", raw.get("account_id"))),
        "point_id": raw.get("point_id"),
        "valid_from_utc": _rev_norm_instant(
            raw.get("valid_from", raw.get("start", raw.get("from")))
        ),
        "valid_to_utc": _rev_norm_instant(
            raw.get("valid_to", raw.get("end", raw.get("to")))
        ),
        "calendar_id_explicit": raw.get("calendar_id"),
        "valid": raw.get("valid", True),
    }
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def _rev_tariff_signature(atom: dict) -> Optional[str]:
    tb = atom.get("tariff_basis")
    if not isinstance(tb, dict):
        return None
    payload = {k: tb.get(k) for k in sorted(tb) if k != "tariff_version"}
    payload["_rate_id"] = atom.get("rate_id")
    payload["_unit_price"] = atom.get("unit_price")
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)


def _rev_calendar_signature(atom: dict) -> Optional[str]:
    payload = {
        "timezone": atom.get("timezone"),
        "billing_day_start_utc": atom.get("billing_day_start_utc"),
        "billing_day_end_utc": atom.get("billing_day_end_utc"),
    }
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def _rev_reading_signature(rec: dict) -> str:
    anchors = []
    for a in rec["reading_evidence"].get("start_anchors", []):
        anchors.append(("start", a.get("kind"), a.get("utc"), a.get("value_wh")))
    for a in rec["reading_evidence"].get("end_anchors", []):
        anchors.append(("end", a.get("kind"), a.get("utc"), a.get("value_wh")))
    return json.dumps(sorted(anchors), sort_keys=True, ensure_ascii=False)


def _rev_stable_issue(issue: dict) -> dict:
    """Copy an issue with list fields sorted so input order cannot reorder evidence."""
    out = dict(issue)
    for key in ("binding_ids", "control_ids", "refs", "voucher_ids", "reading_refs", "segment_ids"):
        val = out.get(key)
        if isinstance(val, list):
            out[key] = sorted(val, key=str)
    if isinstance(out.get("actives_tariff_versions"), list):
        out["actives_tariff_versions"] = sorted(
            out["actives_tariff_versions"], key=lambda x: json.dumps(x, sort_keys=True)
        )
    if isinstance(out.get("anchors"), list):
        out["anchors"] = sorted(out["anchors"], key=lambda x: json.dumps(x, sort_keys=True))
    return out


def _rev_anchor_evidence(anchor: dict) -> dict:
    """Retain anchor evidence without positional input indices.

    ``evidence.path`` is derived from JSON list position.  Reporting it would
    make a pure re-ordering look like a difference, so the stable reading id
    and the raw record are kept instead.
    """
    ev = dict(anchor.get("evidence") or {})
    ev.pop("path", None)
    if isinstance(ev.get("raw"), dict):
        ev["raw"] = _rev_strip_path(ev["raw"])
    return {
        "ref_id": anchor.get("ref_id"),
        "kind": anchor.get("kind"),
        "declared_ts": anchor.get("declared_ts"),
        "utc": anchor.get("utc"),
        "value_wh": anchor.get("value_wh"),
        "evidence": ev,
    }


def _rev_cell_view(cell: dict) -> dict:
    if not cell["present"]:
        state = "absent"
    elif cell["verified"]:
        state = "settled_verified"
    else:
        state = "not_settled"
    slices = cell["slices"]
    return {
        "control": cell["control"],
        "settlement_state": state,
        "target_wh": cell["target_wh"],
        "allocated_wh": cell["allocated_wh"],
        "amount_fraction": _rev_money(cell["amount"]),
        "unit_prices": cell["unit_prices"],
        "tariff_versions": cell["tariff_versions"],
        "binding_ids": cell["binding_ids"],
        "control_ids": cell["control_ids"],
        "component": {
            "component_id": cell["component_id"],
            "component_status": cell["component_status"],
        },
        "control_check_status": cell["status"],
        "verified": cell["verified"],
        "issues": [_rev_stable_issue(i) for i in cell["issues"]],
        "raw_control_rows": [
            {"raw": r["raw"]} for r in cell["raw_control_rows"]
        ],
        "segment_refs": [
            {"meter_id": k[0], "start_utc": k[1], "end_utc": k[2]}
            for k in sorted(cell["segments"])
        ],
        "settlement_basis": {"atoms": slices},
        "signatures": {
            "unit_prices": cell["unit_prices"],
            "tariff": sorted({
                _rev_tariff_signature(s) for s in slices
                if _rev_tariff_signature(s) is not None
            }),
            "binding": sorted({
                _rev_binding_signature(s.get("binding_raw")) for s in slices
                if _rev_binding_signature(s.get("binding_raw")) is not None
            }),
            "calendar": sorted({
                _rev_calendar_signature(s) for s in slices
                if _rev_calendar_signature(s) is not None
            }),
        },
    }


def _rev_segment_view(rec: dict, scope: str) -> dict:
    atoms = [a for a in rec["atoms"] if a["control"]["scope_id"] == scope]
    per_control: dict[tuple[str, str, str], dict] = {}
    complete = rec["complete"]
    for atom in atoms:
        key = _rev_key_tuple(atom["control"])
        e = per_control.setdefault(key, {
            "control": atom["control"],
            "allocated_wh": 0,
            "amount": Fraction(0),
            "complete": True,
            "atom_ids": [],
        })
        e["atom_ids"].append(atom["atom_id"])
        if atom["allocated_wh"] is None or atom["amount_fraction"] is None:
            e["complete"] = False
        else:
            e["allocated_wh"] += atom["allocated_wh"]
            e["amount"] += Fraction(atom["amount_fraction"])
    if not complete or any(not e["complete"] for e in per_control.values()):
        total_wh = None
        total_amount = None
    else:
        total_wh = sum(e["allocated_wh"] for e in per_control.values())
        total_amount = sum((e["amount"] for e in per_control.values()), Fraction(0))
    state = "settled_verified" if rec["complete"] else "not_settled"
    return {
        "segment": rec["identity"],
        "settlement_state": state,
        "segment_status": rec["status"],
        "raw_difference_wh": rec["delta_wh"],
        "duration_seconds": rec["duration_seconds"],
        "allocated_wh_in_scope": total_wh,
        "amount_fraction_in_scope": _rev_money(total_amount),
        "per_control": [
            {
                "control": e["control"],
                "allocated_wh": e["allocated_wh"] if e["complete"] else None,
                "amount_fraction": _rev_money(e["amount"]) if e["complete"] else None,
                "atom_ids": sorted(e["atom_ids"]),
            }
            for _, e in sorted(per_control.items())
        ],
        "segment_check": rec["segment_check"],
        "issues": [_rev_stable_issue(i) for i in rec["issues"]],
        "candidate_controls": rec["candidate_controls"],
        "diagnostic_intervals": rec["diagnostic_intervals"],
        "reading_evidence": _rev_reading_evidence_view(rec["reading_evidence"]),
        "binding_evidence": rec["binding_evidence"],
        "tariff_evidence": rec["tariff_evidence"],
        "settlement_basis": {"atoms": atoms},
        "signatures": {
            "reading": _rev_reading_signature(rec),
            "binding": sorted({_rev_binding_signature(b.get("raw")) for b in rec["binding_evidence"]
                               if _rev_binding_signature(b.get("raw")) is not None}),
            "tariff": sorted({_rev_tariff_signature(a) for a in atoms
                              if _rev_tariff_signature(a) is not None}),
            "calendar": sorted({_rev_calendar_signature(a) for a in atoms
                                if _rev_calendar_signature(a) is not None}),
        },
    }


def _rev_change(field: str, kind: str, baseline: Any, candidate: Any) -> dict:
    return {
        "field": field,
        "kind": kind,
        "baseline": baseline,
        "candidate": candidate,
    }


def _rev_amount_delta(base_value: Any, cand_value: Any) -> Optional[str]:
    if base_value is None or cand_value is None:
        return None
    return frac_str(Fraction(cand_value) - Fraction(base_value))


def _rev_compare_cell(base_view: dict, cand_view: dict) -> tuple[str, list[dict], Optional[int], Optional[str]]:
    changes: list[dict] = []
    b_sig = base_view["signatures"]
    c_sig = cand_view["signatures"]
    for field in ("target_wh", "allocated_wh"):
        b, c = base_view[field], cand_view[field]
        if b != c:
            entry = _rev_change(field, "numeric", b, c)
            if isinstance(b, int) and isinstance(c, int):
                entry["delta"] = c - b
            changes.append(entry)
    if base_view["amount_fraction"] != cand_view["amount_fraction"]:
        entry = _rev_change(
            "amount_fraction", "numeric",
            base_view["amount_fraction"], cand_view["amount_fraction"],
        )
        entry["delta"] = _rev_amount_delta(
            base_view["amount_fraction"], cand_view["amount_fraction"]
        )
        changes.append(entry)
    for field, label in (
        ("unit_prices", "unit_price"),
        ("tariff", "tariff"),
        ("binding", "binding"),
        ("calendar", "calendar"),
    ):
        if b_sig[field] != c_sig[field]:
            changes.append(_rev_change(label, "evidence", b_sig[field], c_sig[field]))
    delta_wh = None
    if isinstance(base_view["allocated_wh"], int) and isinstance(cand_view["allocated_wh"], int):
        delta_wh = cand_view["allocated_wh"] - base_view["allocated_wh"]
    delta_amount = _rev_amount_delta(
        base_view["amount_fraction"], cand_view["amount_fraction"]
    )
    return ("changed" if changes else "unchanged"), changes, delta_wh, delta_amount


def _rev_compare_segment(base_view: dict, cand_view: dict) -> tuple[str, list[dict], Optional[int], Optional[str]]:
    changes: list[dict] = []
    if base_view["raw_difference_wh"] != cand_view["raw_difference_wh"]:
        b, c = base_view["raw_difference_wh"], cand_view["raw_difference_wh"]
        entry = _rev_change("raw_difference_wh", "numeric", b, c)
        if isinstance(b, int) and isinstance(c, int):
            entry["delta"] = c - b
        changes.append(entry)
    if base_view["segment_status"] != cand_view["segment_status"]:
        changes.append(_rev_change(
            "segment_status", "evidence",
            base_view["segment_status"], cand_view["segment_status"],
        ))
    b_pc = {_rev_key_tuple(x["control"]): x for x in base_view["per_control"]}
    c_pc = {_rev_key_tuple(x["control"]): x for x in cand_view["per_control"]}
    allocations_changed = False
    rows = []
    for key in sorted(set(b_pc) | set(c_pc)):
        b = b_pc.get(key)
        c = c_pc.get(key)
        b_val = b["allocated_wh"] if b else None
        c_val = c["allocated_wh"] if c else None
        row = {
            "control": _rev_key_obj(key),
            "baseline_allocated_wh": b_val,
            "candidate_allocated_wh": c_val,
            "delta_allocated_wh": (
                c_val - b_val if isinstance(b_val, int) and isinstance(c_val, int) else None
            ),
            "baseline_amount_fraction": b["amount_fraction"] if b else None,
            "candidate_amount_fraction": c["amount_fraction"] if c else None,
            "delta_amount_fraction": _rev_amount_delta(
                b["amount_fraction"] if b else None,
                c["amount_fraction"] if c else None,
            ),
        }
        rows.append(row)
        if (b is None) != (c is None) or row["delta_allocated_wh"] not in (0, None) \
                or row["delta_amount_fraction"] not in ("0", None):
            allocations_changed = True
    if allocations_changed:
        changes.append({
            "field": "allocated_by_control",
            "kind": "numeric",
            "baseline": [
                {"control": r["control"], "allocated_wh": r["baseline_allocated_wh"],
                 "amount_fraction": r["baseline_amount_fraction"]}
                for r in rows
            ],
            "candidate": [
                {"control": r["control"], "allocated_wh": r["candidate_allocated_wh"],
                 "amount_fraction": r["candidate_amount_fraction"]}
                for r in rows
            ],
            "delta": [
                {"control": r["control"], "allocated_wh": r["delta_allocated_wh"],
                 "amount_fraction": r["delta_amount_fraction"]}
                for r in rows
            ],
        })
    for field, label in (
        ("reading", "reading"),
        ("binding", "binding"),
        ("tariff", "tariff"),
        ("calendar", "calendar"),
    ):
        if base_view["signatures"][field] != cand_view["signatures"][field]:
            changes.append(_rev_change(
                label, "evidence",
                base_view["signatures"][field], cand_view["signatures"][field],
            ))
    delta_wh = None
    if isinstance(base_view["allocated_wh_in_scope"], int) \
            and isinstance(cand_view["allocated_wh_in_scope"], int):
        delta_wh = cand_view["allocated_wh_in_scope"] - base_view["allocated_wh_in_scope"]
    delta_amount = _rev_amount_delta(
        base_view["amount_fraction_in_scope"],
        cand_view["amount_fraction_in_scope"],
    )
    return ("changed" if changes else "unchanged"), changes, delta_wh, delta_amount


def _rev_cross_attribution_conflicts(
    side_self: dict,
    side_other: dict,
    scope: str,
) -> list[dict]:
    """Segments attributed to this scope here but unresolved/unattributed there."""
    out: list[dict] = []
    identities = {
        key for key, rec in side_self["segments"].items() if scope in rec["scope_ids"]
    }
    for key in sorted(identities):
        other = side_other["segments"].get(key)
        if other is None:
            continue
        if scope in other["scope_ids"]:
            continue
        if other["complete"]:
            # Cleanly attributed to another scope: a real reassignment, not a failure.
            continue
        out.append({
            "code": "segment_attribution_not_resolved_on_other_side",
            "segment": {
                "meter_id": key[0], "start_utc": key[1], "end_utc": key[2],
            },
            "other_side_status": other["status"],
            "other_side_scope_ids": other["scope_ids"],
            "other_side_candidate_controls": other["candidate_controls"],
            "other_side_issues": other["issues"],
            "other_side_reading_evidence": other["reading_evidence"],
            "other_side_binding_evidence": other["binding_evidence"],
            "message": (
                "this reading segment is attributed to the scope on one side but has unresolved "
                "binding/reading evidence on the other; the scope cannot be compared"
            ),
        })
    return out


def _rev_cell_state_label(cell: Optional[dict]) -> str:
    if cell is None:
        return "absent"
    return cell.get("settlement_state", "absent")


def _rev_not_comparable_days(
    base_cells: dict, cand_cells: dict, base_segs: dict, cand_segs: dict
) -> list[dict]:
    """Structure the not_comparable scope by billing day without any values."""
    days: dict[str, dict] = {}
    for key in sorted(set(base_cells) | set(cand_cells)):
        control = _rev_key_obj(key)
        entry = days.setdefault(key[1], {"billing_day": key[1], "rates": [], "reading_segments": []})
        entry["rates"].append({
            "rate_id": key[2],
            "change": "not_comparable",
            "baseline_allocated_wh": None,
            "candidate_allocated_wh": None,
            "delta_allocated_wh": None,
            "baseline_target_wh": base_cells[key]["target_wh"] if key in base_cells else None,
            "candidate_target_wh": cand_cells[key]["target_wh"] if key in cand_cells else None,
            "baseline_amount_fraction": None,
            "candidate_amount_fraction": None,
            "delta_amount_fraction": None,
        })
    for key in sorted(set(base_segs) | set(cand_segs)):
        seen_days: set[str] = set()
        for side in (base_segs.get(key), cand_segs.get(key)):
            if not side:
                continue
            for atom in side["atoms"]:
                seen_days.add(atom["control"]["billing_day"])
            for obj in side["candidate_controls"]:
                seen_days.add(obj["billing_day"])
        for day in sorted(seen_days):
            entry = days.setdefault(day, {"billing_day": day, "rates": [], "reading_segments": []})
            entry["reading_segments"].append({
                "segment": {"meter_id": key[0], "start_utc": key[1], "end_utc": key[2]},
                "change": "not_comparable",
                "delta_allocated_wh": None,
            })
    return [days[k] for k in sorted(days)]


def _rev_build_scope_result(
    scope: str,
    base: dict,
    cand: dict,
    base_state: dict,
    cand_state: dict,
) -> dict:
    comparable = (
        base_state["presence"] in ("complete", "absent")
        and cand_state["presence"] in ("complete", "absent")
        and (base_state["presence"] == "complete" or cand_state["presence"] == "complete")
    )
    base_cells = {
        key: cell for key, cell in base["controls"].items() if key[0] == scope
    }
    cand_cells = {
        key: cell for key, cell in cand["controls"].items() if key[0] == scope
    }
    base_segs = {
        key: rec for key, rec in base["segments"].items() if scope in rec["scope_ids"]
    }
    cand_segs = {
        key: rec for key, rec in cand["segments"].items() if scope in rec["scope_ids"]
    }

    result: dict = {
        "scope_id": scope,
        "comparability": "comparable" if comparable else "not_comparable",
        "baseline_presence": base_state["presence"],
        "candidate_presence": cand_state["presence"],
        "baseline_settlement_overall_status": base["overall_settlement_status"],
        "candidate_settlement_overall_status": cand["overall_settlement_status"],
    }
    if not comparable:
        conflicts_base = base_state["conflicts"]
        conflicts_cand = cand_state["conflicts"]
        reasons: list[str] = []
        for label, presence, conflicts in (
            ("baseline", base_state["presence"], conflicts_base),
            ("candidate", cand_state["presence"], conflicts_cand),
        ):
            if presence == "incomplete":
                reasons.append(f"{label}_not_settleable_or_evidence_insufficient")
            elif presence == "absent" and conflicts:
                reasons.append(f"{label}_absence_cannot_be_established")
        if not reasons:
            reasons.append("both_sides_absent")
        result.update({
            "reason": reasons,
            "message": (
                "该结算范围至少一侧无法形成完整、已复核的结算结果；"
                "不做数值比较，也不把缺失侧当作零值。"
            ),
            "conflicts": {"baseline": conflicts_base, "candidate": conflicts_cand},
            "control_cells": [
                {
                    "control": _rev_key_obj(key),
                    "change": "not_comparable",
                    "baseline": _rev_cell_view(base_cells[key]) if key in base_cells else None,
                    "candidate": _rev_cell_view(cand_cells[key]) if key in cand_cells else None,
                    "changes": [],
                    "delta_allocated_wh": None,
                    "delta_amount_fraction": None,
                    "note": "no numeric comparison is made for this scope",
                }
                for key in sorted(set(base_cells) | set(cand_cells))
            ],
            "reading_segments": [
                {
                    "segment": {
                        "meter_id": key[0], "start_utc": key[1], "end_utc": key[2],
                    },
                    "change": "not_comparable",
                    "baseline": _rev_segment_view(base_segs[key], scope) if key in base_segs else None,
                    "candidate": _rev_segment_view(cand_segs[key], scope) if key in cand_segs else None,
                    "changes": [],
                    "delta_allocated_wh": None,
                    "delta_amount_fraction": None,
                    "note": "no numeric comparison is made for this scope",
                }
                for key in sorted(set(base_segs) | set(cand_segs))
            ],
            "rollup": {
                "control_cells": {"added": 0, "removed": 0, "changed": 0, "unchanged": 0,
                                  "not_comparable": len(set(base_cells) | set(cand_cells))},
                "reading_segments": {"added": 0, "removed": 0, "changed": 0, "unchanged": 0,
                                     "not_comparable": len(set(base_segs) | set(cand_segs))},
                "delta_over_shared_control_cells": {
                    "allocated_wh": None,
                    "amount_fraction": None,
                    "note": "not comparable; no delta is computed and no side is treated as zero",
                },
            },
            "by_billing_day": _rev_not_comparable_days(base_cells, cand_cells, base_segs, cand_segs),
        })
        return result

    cell_entries: list[dict] = []
    cell_counts = {"added": 0, "removed": 0, "changed": 0, "unchanged": 0}
    shared_delta_wh = 0
    shared_delta_any = False
    shared_delta_amount = Fraction(0)
    shared_delta_amount_any = False
    added_value_wh = 0
    removed_value_wh = 0
    added_amount = Fraction(0)
    removed_amount = Fraction(0)

    for key in sorted(set(base_cells) | set(cand_cells)):
        b = base_cells.get(key)
        c = cand_cells.get(key)
        b_view = _rev_cell_view(b) if b is not None else None
        c_view = _rev_cell_view(c) if c is not None else None
        entry: dict = {
            "control": _rev_key_obj(key),
            "baseline": b_view,
            "candidate": c_view,
            "changes": [],
            "delta_allocated_wh": None,
            "delta_amount_fraction": None,
        }
        if b is None:
            entry["change"] = "added"
            entry["note"] = "scope/cell absent from baseline input; value shown without assuming baseline 0"
            cell_counts["added"] += 1
            if c_view["allocated_wh"] is not None:
                added_value_wh += c_view["allocated_wh"]
            if c_view["amount_fraction"] is not None:
                added_amount += Fraction(c_view["amount_fraction"])
        elif c is None:
            entry["change"] = "removed"
            entry["note"] = "scope/cell absent from candidate input; value shown without assuming candidate 0"
            cell_counts["removed"] += 1
            if b_view["allocated_wh"] is not None:
                removed_value_wh += b_view["allocated_wh"]
            if b_view["amount_fraction"] is not None:
                removed_amount += Fraction(b_view["amount_fraction"])
        else:
            change, changes, delta_wh, delta_amount = _rev_compare_cell(b_view, c_view)
            entry["change"] = change
            entry["changes"] = changes
            entry["delta_allocated_wh"] = delta_wh
            entry["delta_amount_fraction"] = delta_amount
            cell_counts[change] += 1
            if delta_wh is not None:
                shared_delta_wh += delta_wh
                shared_delta_any = True
            if delta_amount is not None:
                shared_delta_amount += Fraction(delta_amount)
                shared_delta_amount_any = True
        cell_entries.append(entry)

    seg_entries: list[dict] = []
    seg_counts = {"added": 0, "removed": 0, "changed": 0, "unchanged": 0}
    seg_delta_wh_total = 0
    seg_delta_any = False
    seg_added_wh = 0
    seg_removed_wh = 0

    for key in sorted(set(base_segs) | set(cand_segs)):
        b = base_segs.get(key)
        c = cand_segs.get(key)
        b_view = _rev_segment_view(b, scope) if b is not None else None
        c_view = _rev_segment_view(c, scope) if c is not None else None
        entry = {
            "segment": {"meter_id": key[0], "start_utc": key[1], "end_utc": key[2]},
            "baseline": b_view,
            "candidate": c_view,
            "changes": [],
            "delta_allocated_wh": None,
            "delta_amount_fraction": None,
        }
        if b is None:
            entry["change"] = "added"
            entry["note"] = "segment absent from baseline input; value shown without assuming baseline 0"
            seg_counts["added"] += 1
            if c_view["allocated_wh_in_scope"] is not None:
                seg_added_wh += c_view["allocated_wh_in_scope"]
        elif c is None:
            entry["change"] = "removed"
            entry["note"] = "segment absent from candidate input; value shown without assuming candidate 0"
            seg_counts["removed"] += 1
            if b_view["allocated_wh_in_scope"] is not None:
                seg_removed_wh += b_view["allocated_wh_in_scope"]
        else:
            change, changes, delta_wh, delta_amount = _rev_compare_segment(b_view, c_view)
            entry["change"] = change
            entry["changes"] = changes
            entry["delta_allocated_wh"] = delta_wh
            entry["delta_amount_fraction"] = delta_amount
            seg_counts[change] += 1
            if delta_wh is not None:
                seg_delta_wh_total += delta_wh
                seg_delta_any = True
        seg_entries.append(entry)

    # Group by billing day x rate, plus the segments that allocate into that day.
    days: dict[str, dict] = {}
    for entry in cell_entries:
        control = entry["control"]
        day = days.setdefault(control["billing_day"], {"billing_day": control["billing_day"], "rates": [], "segment_refs": {}})
        day["rates"].append({
            "rate_id": control["rate_id"],
            "change": entry["change"],
            "baseline_allocated_wh": entry["baseline"]["allocated_wh"] if entry["baseline"] else None,
            "candidate_allocated_wh": entry["candidate"]["allocated_wh"] if entry["candidate"] else None,
            "delta_allocated_wh": entry["delta_allocated_wh"],
            "baseline_target_wh": entry["baseline"]["target_wh"] if entry["baseline"] else None,
            "candidate_target_wh": entry["candidate"]["target_wh"] if entry["candidate"] else None,
            "baseline_amount_fraction": entry["baseline"]["amount_fraction"] if entry["baseline"] else None,
            "candidate_amount_fraction": entry["candidate"]["amount_fraction"] if entry["candidate"] else None,
            "delta_amount_fraction": entry["delta_amount_fraction"],
        })
    for entry in seg_entries:
        seen_days: set[str] = set()
        for side_view in (entry["baseline"], entry["candidate"]):
            if not side_view:
                continue
            for pc in side_view["per_control"]:
                seen_days.add(pc["control"]["billing_day"])
            for pc in side_view.get("candidate_controls", []):
                seen_days.add(pc["billing_day"])
        for day in sorted(seen_days):
            bucket = days.setdefault(day, {"billing_day": day, "rates": [], "segment_refs": {}})
            bucket["segment_refs"][tuple(sorted(entry["segment"].items()))] = {
                "segment": entry["segment"],
                "change": entry["change"],
                "delta_allocated_wh": entry["delta_allocated_wh"],
            }
    by_day = []
    for day in sorted(days):
        d = days[day]
        by_day.append({
            "billing_day": d["billing_day"],
            "rates": sorted(d["rates"], key=lambda r: r["rate_id"]),
            "reading_segments": [
                d["segment_refs"][k] for k in sorted(d["segment_refs"])
            ],
        })

    result.update({
        "control_cells": cell_entries,
        "reading_segments": seg_entries,
        "by_billing_day": by_day,
        "rollup": {
            "control_cells": cell_counts,
            "reading_segments": seg_counts,
            "delta_over_shared_control_cells": {
                "allocated_wh": shared_delta_wh if shared_delta_any else None,
                "amount_fraction": frac_str(shared_delta_amount) if shared_delta_amount_any else None,
                "note": "只累计双方都存在且都已复核的控制格；单一侧存在的新增/消失不计入差，也不按 0 处理",
            },
            "delta_over_shared_reading_segments": {
                "allocated_wh": seg_delta_wh_total if seg_delta_any else None,
                "note": "只累计双方都存在且都已复核的读数段",
            },
            "added_values_not_delta": {
                "allocated_wh": added_value_wh,
                "amount_fraction": frac_str(added_amount),
            },
            "removed_values_not_delta": {
                "allocated_wh": removed_value_wh,
                "amount_fraction": frac_str(removed_amount),
            },
            "segment_added_values_not_delta": {"allocated_wh": seg_added_wh},
            "segment_removed_values_not_delta": {"allocated_wh": seg_removed_wh},
        },
    })
    return result


def _rev_scope_union(base: dict, cand: dict, extra_scopes: set[str]) -> list[str]:
    scopes = set(base["scope_ids"]) | set(cand["scope_ids"]) | set(extra_scopes)
    for side in (base, cand):
        for rec in side["segments"].values():
            if not rec["scope_ids"]:
                scopes.add("")
    scopes.discard("")
    return sorted(scopes)


def _rev_fatal(failed_side: str, exc: InputError, path_info: dict) -> dict:
    return {
        "report_type": "revision_impact_analysis",
        "overall_status": "fatal_input_error",
        "overall_status_label": REVISION_IMPACT_STATUS_LABELS["fatal_input_error"],
        "failed_side": failed_side,
        "error": str(exc),
        "error_type": "InputError",
        "input_audit": path_info,
        "rule_version": {
            "impact_rule_version": REVISION_IMPACT_RULE_VERSION,
            "engine_rule_version": ENGINE_RULE_VERSION,
        },
    }


def revision_impact(
    baseline_root: dict,
    candidate_root: dict,
    baseline_path: Optional[Path] = None,
    candidate_path: Optional[Path] = None,
    baseline_raw: Optional[bytes] = None,
    candidate_raw: Optional[bytes] = None,
) -> dict:
    def audit(path: Optional[Path], raw: Optional[bytes], root: dict) -> dict:
        return {
            "path": str(path) if path else None,
            "sha256": hashlib.sha256(raw).hexdigest() if raw else None,
            "input_rule_version": root.get("rule_version", "unspecified"),
        }

    input_audit = {
        "baseline": audit(baseline_path, baseline_raw, baseline_root),
        "candidate": audit(candidate_path, candidate_raw, candidate_root),
    }
    try:
        base = _rev_build_side("baseline", baseline_root, baseline_path, baseline_raw)
    except InputError as exc:
        return _rev_fatal("baseline", exc, input_audit)
    try:
        cand = _rev_build_side("candidate", candidate_root, candidate_path, candidate_raw)
    except InputError as exc:
        return _rev_fatal("candidate", exc, input_audit)

    scopes = _rev_scope_union(base, cand, set())
    scope_results: list[dict] = []
    for scope in scopes:
        base_state = _rev_side_scope_state(base, scope)
        cand_state = _rev_side_scope_state(cand, scope)
        cross_base = _rev_cross_attribution_conflicts(base, cand, scope)
        cross_cand = _rev_cross_attribution_conflicts(cand, base, scope)
        if cross_base:
            # A baseline segment claims this scope but the candidate side cannot
            # resolve its attribution: the candidate is the incomplete side.
            cand_state = {
                "presence": "incomplete",
                "conflicts": cand_state["conflicts"] + cross_base,
            }
        if cross_cand:
            base_state = {
                "presence": "incomplete",
                "conflicts": base_state["conflicts"] + cross_cand,
            }
        if base_state["presence"] == "absent" and cand_state["presence"] == "absent":
            continue
        scope_results.append(
            _rev_build_scope_result(scope, base, cand, base_state, cand_state)
        )

    comparable = [s for s in scope_results if s["comparability"] == "comparable"]
    not_comparable = [s for s in scope_results if s["comparability"] == "not_comparable"]

    cell_totals = {"added": 0, "removed": 0, "changed": 0, "unchanged": 0}
    seg_totals = {"added": 0, "removed": 0, "changed": 0, "unchanged": 0}
    delta_wh = 0
    delta_wh_any = False
    delta_amount = Fraction(0)
    delta_amount_any = False
    for s in comparable:
        for k in cell_totals:
            cell_totals[k] += s["rollup"]["control_cells"].get(k, 0)
        for k in seg_totals:
            seg_totals[k] += s["rollup"]["reading_segments"].get(k, 0)
        ds = s["rollup"]["delta_over_shared_control_cells"]
        if ds["allocated_wh"] is not None:
            delta_wh += ds["allocated_wh"]
            delta_wh_any = True
        if ds["amount_fraction"] is not None:
            delta_amount += Fraction(ds["amount_fraction"])
            delta_amount_any = True

    if not scope_results:
        overall = "no_scopes"
    elif not not_comparable:
        overall = "all_scopes_comparable"
    elif comparable:
        overall = "partial_comparability"
    else:
        overall = "not_comparable"

    rule_version_differs = (
        baseline_root.get("rule_version", "unspecified")
        != candidate_root.get("rule_version", "unspecified")
    )
    total_flags = sum(cell_totals.values()) + sum(seg_totals.values())
    changes_present = any(cell_totals[k] for k in ("added", "removed", "changed")) or any(
        seg_totals[k] for k in ("added", "removed", "changed")
    )

    return {
        "report_type": "revision_impact_analysis",
        "overall_status": overall,
        "overall_status_label": REVISION_IMPACT_STATUS_LABELS[overall],
        "has_changes": changes_present,
        "rule_version": {
            "impact_rule_version": REVISION_IMPACT_RULE_VERSION,
            "engine_rule_version": ENGINE_RULE_VERSION,
            "baseline_input_rule_version": baseline_root.get("rule_version", "unspecified"),
            "candidate_input_rule_version": candidate_root.get("rule_version", "unspecified"),
            "input_rule_version_changed": rule_version_differs,
        },
        "input_audit": input_audit,
        "rules": {
            "comparability": REVISION_COMPARABILITY_RULE,
            "identity": REVISION_IDENTITY_RULE,
            "change_categories": REVISION_SETTLEMENT_BASIS_RULES["change_categories"],
            "settlement_basis": {
                k: v for k, v in REVISION_SETTLEMENT_BASIS_RULES.items()
                if k != "change_categories"
            },
        },
        "summary": {
            "scope_count": len(scope_results),
            "comparable_scope_count": len(comparable),
            "not_comparable_scope_count": len(not_comparable),
            "control_cells": cell_totals,
            "reading_segments": seg_totals,
            "comparable_scope_delta_over_shared_control_cells": {
                "allocated_wh": delta_wh if delta_wh_any else None,
                "amount_fraction": frac_str(delta_amount) if delta_amount_any else None,
                "note": "只对可比范围内双方都存在且已复核的控制格求和；新增/消失不按 0 参与差值",
            },
            "not_comparable_scopes": [s["scope_id"] for s in not_comparable],
            "unscoped_segment_count": {
                "baseline": len(base["unscoped_segments"]),
                "candidate": len(cand["unscoped_segments"]),
            },
            "compared_entry_count": total_flags,
        },
        "scopes": scope_results,
        "evidence_catalog": {
            "note": (
                "未结算连通范围的完整证据按 range_id 仅存一份；"
                "scope.conflicts[].range_id 可在对应侧的 catalog 中解析，避免同一范围跨多个 scope 时重复。"
            ),
            "baseline": base["range_evidence"],
            "candidate": cand["range_evidence"],
        },
        "unscoped_segments": {
            "baseline": base["unscoped_segments"],
            "candidate": cand["unscoped_segments"],
        },
    }


def write_revision_impact_csv(report: dict, path: Path) -> None:
    import csv
    fields = [
        "scope_id", "comparability", "billing_day", "rate_id", "cell_change",
        "baseline_target_wh", "candidate_target_wh",
        "baseline_allocated_wh", "candidate_allocated_wh", "delta_allocated_wh",
        "baseline_amount_fraction", "candidate_amount_fraction", "delta_amount_fraction",
        "baseline_settlement_state", "candidate_settlement_state", "reason",
    ]
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for scope in report.get("scopes", []):
            if scope["comparability"] != "comparable":
                for cell in scope["control_cells"]:
                    control = cell["control"]
                    writer.writerow({
                        "scope_id": scope["scope_id"],
                        "comparability": scope["comparability"],
                        "billing_day": control["billing_day"],
                        "rate_id": control["rate_id"],
                        "cell_change": cell["change"],
                        "baseline_settlement_state": _rev_cell_state_label(cell["baseline"]),
                        "candidate_settlement_state": _rev_cell_state_label(cell["candidate"]),
                        "reason": ";".join(scope["reason"]),
                    })
                continue
            for cell in scope["control_cells"]:
                control = cell["control"]
                writer.writerow({
                    "scope_id": scope["scope_id"],
                    "comparability": scope["comparability"],
                    "billing_day": control["billing_day"],
                    "rate_id": control["rate_id"],
                    "cell_change": cell["change"],
                    "baseline_target_wh": "" if not cell["baseline"] or cell["baseline"]["target_wh"] is None else cell["baseline"]["target_wh"],
                    "candidate_target_wh": "" if not cell["candidate"] or cell["candidate"]["target_wh"] is None else cell["candidate"]["target_wh"],
                    "baseline_allocated_wh": "" if not cell["baseline"] or cell["baseline"]["allocated_wh"] is None else cell["baseline"]["allocated_wh"],
                    "candidate_allocated_wh": "" if not cell["candidate"] or cell["candidate"]["allocated_wh"] is None else cell["candidate"]["allocated_wh"],
                    "delta_allocated_wh": "" if cell["delta_allocated_wh"] is None else cell["delta_allocated_wh"],
                    "baseline_amount_fraction": "" if not cell["baseline"] else cell["baseline"]["amount_fraction"] or "",
                    "candidate_amount_fraction": "" if not cell["candidate"] else cell["candidate"]["amount_fraction"] or "",
                    "delta_amount_fraction": cell["delta_amount_fraction"] or "",
                    "baseline_settlement_state": cell["baseline"]["settlement_state"] if cell["baseline"] else "absent",
                    "candidate_settlement_state": cell["candidate"]["settlement_state"] if cell["candidate"] else "absent",
                    "reason": "",
                })


def settle(root: dict, input_path: Optional[Path] = None, raw_bytes: Optional[bytes] = None) -> dict:
    calendars = load_calendars(root)
    segments, excluded, reading_issues = build_segments(root)
    bindings = load_bindings(root)
    tariffs, tariff_errors = expand_tariffs(root, calendars)
    controls = load_controls(root)
    build = build_atoms(segments, bindings, tariffs, calendars, controls)
    all_global_issues = reading_issues
    components, key_to_component, segment_to_component = solve_all(
        root, segments, build, controls, tariff_errors
    )
    report = build_report(
        input_path, raw_bytes, root, segments, excluded, all_global_issues,
        build, controls, components, key_to_component, tariff_errors,
    )
    report["segment_to_component_hint"] = segment_to_component
    return report


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Auditable meter time-of-use integer allocator, with optional "
            "planned collection-window reconciliation and revision impact analysis"
        )
    )
    parser.add_argument("--input", "-i", type=Path, help="Input JSON path")
    parser.add_argument("--output", "-o", type=Path, help="Output audit JSON path (default: stdout)")
    parser.add_argument("--csv", type=Path, help="Optional flat slice CSV path")
    parser.add_argument(
        "--reconcile", action="store_true",
        help="Also run planned collection-window reconciliation and attach it to the report",
    )
    parser.add_argument(
        "--reconcile-output", type=Path,
        help="Write the reconciliation report to this separate JSON path (implies --reconcile)",
    )
    parser.add_argument(
        "--reconcile-csv", type=Path,
        help="Write one row per planned window to this CSV path (implies --reconcile)",
    )
    parser.add_argument(
        "--reconcile-only", action="store_true",
        help=(
            "Run only planned-window reconciliation; no settlement is computed. "
            "JSON goes to --reconcile-output (or --output / stdout)."
        ),
    )
    parser.add_argument("--pretty", action="store_true", default=True, help="Indent JSON output")
    parser.add_argument(
        "--baseline", type=Path,
        help="Baseline settlement input for revision impact analysis (required with --impact)",
    )
    parser.add_argument(
        "--candidate", type=Path,
        help="Candidate revision input for revision impact analysis (required with --impact)",
    )
    parser.add_argument(
        "--impact", action="store_true",
        help=(
            "Compare a baseline settlement input with a candidate revision input and write "
            "the revision impact report. Does not require an existing settlement output. "
            "JSON goes to --impact-output (or --output / stdout)."
        ),
    )
    parser.add_argument(
        "--impact-output", type=Path,
        help="Write the revision impact report to this JSON path (implies --impact)",
    )
    parser.add_argument(
        "--impact-csv", type=Path,
        help="Write one row per compared control cell to this CSV path (implies --impact)",
    )
    args = parser.parse_args(argv)

    wants_impact = bool(args.impact or args.impact_output or args.impact_csv)
    if wants_impact:
        if not args.baseline or not args.candidate:
            parser.error("--impact requires --baseline and --candidate")
        if args.reconcile_only or args.reconcile or args.reconcile_output or args.reconcile_csv:
            parser.error("--impact cannot be combined with planned-window reconciliation options")
        if args.input or args.csv:
            parser.error("--impact reads --baseline and --candidate directly; --input/--csv are not applicable")
        try:
            baseline_raw = args.baseline.read_bytes()
            candidate_raw = args.candidate.read_bytes()
            try:
                baseline_root = json.loads(baseline_raw.decode("utf-8"))
                candidate_root = json.loads(candidate_raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise InputError(f"invalid JSON input: {exc}") from exc
            for label, obj in (("baseline", baseline_root), ("candidate", candidate_root)):
                if not isinstance(obj, dict):
                    raise InputError(f"{label}: top-level JSON value must be an object")
            impact = revision_impact(
                baseline_root, candidate_root,
                args.baseline.resolve(), args.candidate.resolve(),
                baseline_raw, candidate_raw,
            )
            text = json.dumps(impact, ensure_ascii=False, indent=2 if args.pretty else None)
            target = args.impact_output or args.output
            if target:
                _write_text_atomic(target, text)
            else:
                sys.stdout.write(text + "\n")
            if args.impact_csv:
                args.impact_csv.parent.mkdir(parents=True, exist_ok=True)
                write_revision_impact_csv(impact, args.impact_csv)
            return 2 if impact["overall_status"] == "fatal_input_error" else 0
        except InputError as exc:
            err = {
                "report_type": "revision_impact_analysis",
                "overall_status": "fatal_input_error",
                "error": str(exc),
                "error_type": "InputError",
            }
            text = json.dumps(err, ensure_ascii=False, indent=2)
            target = args.impact_output or args.output
            if target:
                _write_text_atomic(target, text)
            else:
                sys.stderr.write(text + "\n")
            return 2

    if not args.input:
        parser.print_help()
        return 2
    if args.reconcile_only and args.csv:
        parser.error("--reconcile-only does not produce settlement slices; --csv is not applicable")
    try:
        raw = args.input.read_bytes()
        root = json.loads(raw.decode("utf-8"))
        if not isinstance(root, dict):
            raise InputError("top-level JSON value must be an object")
        resolved_input = args.input.resolve()

        if args.reconcile_only:
            reconciliation = reconcile_windows(root, resolved_input, raw)
            text = json.dumps(reconciliation, ensure_ascii=False, indent=2 if args.pretty else None)
            target = args.reconcile_output or args.output
            if target:
                _write_text_atomic(target, text)
            else:
                sys.stdout.write(text + "\n")
            if args.reconcile_csv:
                args.reconcile_csv.parent.mkdir(parents=True, exist_ok=True)
                write_reconciliation_csv(reconciliation, args.reconcile_csv)
            return 0

        wants_reconcile = bool(
            args.reconcile or args.reconcile_output or args.reconcile_csv
        )
        report = settle(root, resolved_input, raw)
        if wants_reconcile:
            # Additive, clearly namespaced key: every settlement field above was
            # produced exactly as before and is never recomputed from this data.
            # A malformed planned-window section is reported inside this key so
            # that it cannot suppress an otherwise valid settlement report.
            try:
                report["planned_window_reconciliation"] = reconcile_windows(
                    root, resolved_input, raw
                )
            except InputError as exc:
                report["planned_window_reconciliation"] = {
                    "report_type": "planned_window_reconciliation",
                    "overall_status": "fatal_input_error",
                    "error": str(exc),
                    "error_type": "InputError",
                }
        text = json.dumps(report, ensure_ascii=False, indent=2 if args.pretty else None)
        if args.output:
            _write_text_atomic(args.output, text)
        else:
            sys.stdout.write(text + "\n")
        if args.csv:
            write_slices_csv(report, args.csv)
        if args.reconcile_output:
            _write_text_atomic(
                args.reconcile_output,
                json.dumps(report["planned_window_reconciliation"], ensure_ascii=False, indent=2),
            )
        if args.reconcile_csv:
            args.reconcile_csv.parent.mkdir(parents=True, exist_ok=True)
            write_reconciliation_csv(report["planned_window_reconciliation"], args.reconcile_csv)
    except InputError as exc:
        err = {"overall_status": "fatal_input_error", "error": str(exc), "error_type": "InputError"}
        text = json.dumps(err, ensure_ascii=False, indent=2)
        if args.reconcile_only:
            target = args.reconcile_output
        else:
            target = args.output if args.output else args.reconcile_output
        if target:
            _write_text_atomic(target, text)
        else:
            sys.stderr.write(text + "\n")
        return 2
    return 0


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
