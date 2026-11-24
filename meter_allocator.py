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
from collections import defaultdict, deque
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
    parser = argparse.ArgumentParser(description="Auditable meter time-of-use integer allocator")
    parser.add_argument("--input", "-i", type=Path, help="Input JSON path")
    parser.add_argument("--output", "-o", type=Path, help="Output audit JSON path (default: stdout)")
    parser.add_argument("--csv", type=Path, help="Optional flat slice CSV path")
    parser.add_argument("--pretty", action="store_true", default=True, help="Indent JSON output")
    args = parser.parse_args(argv)

    if not args.input:
        parser.print_help()
        return 2
    try:
        raw = args.input.read_bytes()
        root = json.loads(raw.decode("utf-8"))
        if not isinstance(root, dict):
            raise InputError("top-level JSON value must be an object")
        report = settle(root, args.input.resolve(), raw)
        text = json.dumps(report, ensure_ascii=False, indent=2 if args.pretty else None)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(text + "\n", encoding="utf-8")
        else:
            sys.stdout.write(text + "\n")
        if args.csv:
            write_slices_csv(report, args.csv)
    except InputError as exc:
        err = {"overall_status": "fatal_input_error", "error": str(exc), "error_type": "InputError"}
        text = json.dumps(err, ensure_ascii=False, indent=2)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(text + "\n", encoding="utf-8")
        else:
            sys.stderr.write(text + "\n")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
