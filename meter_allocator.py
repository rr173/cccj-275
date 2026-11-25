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
from typing import Any, Iterable, Optional, Sequence

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


# ===========================================================================
# Makeup difference ledger (补差账本)
# ---------------------------------------------------------------------------
# Out-of-order electricity-fee exchange orders (电费换单) arrive in actual
# reception batches.  Adjudication follows the supplier effective sequence;
# money is integer cents with an exact Fraction basis, and every reception
# batch freezes the finance view seen at that moment.
#
# The incremental re-adjudication only touches the newly received orders and
# later orders reachable through same-scope half-open day-interval overlap.
# A full from-opening re-fold is provided as an audit/self-check.
# ===========================================================================

MAKEUP_LEDGER_RULE_VERSION = "makeup-difference-ledger-1.0.0"
_LEDGER_OPENING_BATCH = "OPENING"


class LedgerError(InputError):
    """Base class for makeup-ledger contract / accounting failures."""


class LedgerStructureError(LedgerError):
    """A ledger document is structurally illegal and must not be accepted."""


class LedgerOpeningRejected(LedgerError):
    """Fixed baseline reports unattributable blocking evidence / bad days."""

    def __init__(self, report: dict):
        self.report = report
        super().__init__(report.get("error") or "makeup ledger opening rejected")


class LedgerBatchRejected(LedgerError):
    """A reception batch violates uniqueness/idempotency/structure rules."""

    def __init__(self, report: dict):
        self.report = report
        super().__init__(report.get("error") or "makeup ledger batch rejected")


_LED_MISSING = object()


def _led_deepcopy(obj: Any) -> Any:
    """State is JSON primitives only; JSON round-trip is a safe deep copy."""
    return json.loads(json.dumps(obj, ensure_ascii=False))


def _led_pick(raw: dict, keys: tuple[str, ...], default: Any = _LED_MISSING) -> Any:
    for key in keys:
        if key in raw and raw[key] is not None:
            return raw[key]
    if default is _LED_MISSING:
        return None
    return default


def _led_require(raw: dict, keys: tuple[str, ...], path: str) -> Any:
    value = _led_pick(raw, keys)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise LedgerStructureError(f"{path}: missing required field (one of {', '.join(keys)})")
    return value


def _led_day(value: Any, path: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise LedgerStructureError(f"{path}: empty date")
        try:
            if "T" in text or " " in text:
                return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
            return date.fromisoformat(text[:10])
        except ValueError as exc:
            raise LedgerStructureError(f"{path}: {value!r} is not a YYYY-MM-DD billing day") from exc
    raise LedgerStructureError(f"{path}: expected YYYY-MM-DD string, got {type(value).__name__}")


_LED_INTERVAL_PAIRS: tuple[tuple[str, str], ...] = (
    ("day_from", "day_to"),
    ("start_day", "end_day"),
    ("billing_day_from", "billing_day_to"),
    ("billing_day_start", "billing_day_end"),
    ("from_day", "to_day"),
    ("start_date", "end_date"),
    ("date_from", "date_to"),
    ("period_start_day", "period_end_day"),
    ("period_start", "period_end"),
    ("valid_from_day", "valid_to_day"),
    ("from_date", "to_date"),
    ("from", "to"),
    ("start", "end"),
    ("begin", "end"),
    ("开始日期", "结束日期"),
    ("开始日", "结束日"),
    ("起", "止"),
)

_LED_INTERVAL_ARRAY_KEYS = (
    "billing_day_interval", "day_interval", "date_interval", "interval",
    "period", "日期区间", "计费日区间", "区间",
)
_LED_DAY_LIST_KEYS = (
    "billing_days", "billing_day_list", "day_list", "days", "dates", "计费日列表",
)


def _led_interval(raw: dict, path: str) -> tuple[date, date]:
    """Parse a half-open ``[start, end)`` billing-day interval."""
    # 1) Explicit start/end paired fields take precedence.
    start_v = end_v = None
    for sk, ek in _LED_INTERVAL_PAIRS:
        if sk in raw and raw[sk] is not None and ek in raw and raw[ek] is not None:
            start_v, end_v = raw[sk], raw[ek]
            break
    # 2) Two-element endpoint arrays such as interval:[start,end).
    if start_v is None:
        for key in _LED_INTERVAL_ARRAY_KEYS:
            value = raw.get(key)
            if isinstance(value, (list, tuple)) and len(value) == 2:
                start_v, end_v = value[0], value[1]
                break
            if isinstance(value, dict):
                s = _led_pick(value, ("from", "start", "start_day", "day_from", "0"))
                e = _led_pick(value, ("to", "end", "end_day", "day_to", "1"))
                if s is not None and e is not None:
                    start_v, end_v = s, e
                    break
    if start_v is not None and end_v is not None:
        start = _led_day(start_v, path + ".start")
        end = _led_day(end_v, path + ".end")
        inclusive = bool(_led_pick(raw, ("end_inclusive", "inclusive_end", "inclusive", "含结束日"), False))
        if inclusive:
            end = end + timedelta(days=1)
        if end <= start:
            raise LedgerStructureError(
                f"{path}: interval must be a non-empty half-open range, got [{start}, {end})"
            )
        return start, end

    # 3) Explicit contiguous list of included billing days.
    for list_key in _LED_DAY_LIST_KEYS:
        value = raw.get(list_key)
        if isinstance(value, list) and value:
            days = sorted(_led_day(v, f"{path}.{list_key}[{i}]") for i, v in enumerate(value))
            for i in range(1, len(days)):
                if (days[i] - days[i - 1]).days != 1:
                    raise LedgerStructureError(f"{path}.{list_key}: billing days must be contiguous")
            return days[0], days[-1] + timedelta(days=1)

    # 4) A single billing day -> [day, day+1).
    single = _led_pick(raw, ("billing_day", "single_day", "date", "计费日"), None)
    if single is not None:
        d = _led_day(single, path + ".billing_day")
        return d, d + timedelta(days=1)

    raise LedgerStructureError(f"{path}: half-open interval requires start/end billing days")


def _led_days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days)]


def _led_day_key(d: date) -> str:
    return d.isoformat()


def _led_round_half_up_cents(exact_cents: Fraction) -> int:
    """四舍五入到分 (half rounds up); amounts here are non-negative."""
    return int((exact_cents + Fraction(1, 2)).__floor__())


def _led_amount_cents(value: Any, path: str, *, unit: str = "cents") -> int:
    """Parse a non-negative integer-cent amount.

    ``unit='yuan'`` accepts decimal/rational yuan and requires an exact whole
    number of cents.  ``unit='smart'`` treats JSON integers and all-digit
    strings as cents, decimal strings / rationals as yuan.
    """
    if isinstance(value, bool):
        raise LedgerStructureError(f"{path}: boolean is not an amount")
    if unit == "cents":
        if isinstance(value, int):
            cents = value
        elif isinstance(value, str) and value.strip().lstrip("-").isdigit():
            cents = int(value.strip())
        else:
            raise LedgerStructureError(f"{path}: expected non-negative integer cents, got {value!r}")
    elif unit == "yuan":
        try:
            f = parse_rational(value, path) if not isinstance(value, Fraction) else value
        except InputError as exc:
            raise LedgerStructureError(str(exc)) from exc
        scaled = f * 100
        if scaled.denominator != 1:
            raise LedgerStructureError(f"{path}: amount {value!r} is not an exact number of cents")
        cents = scaled.numerator
    else:  # smart
        if isinstance(value, int):
            cents = value
        elif isinstance(value, str):
            text = value.strip()
            if text.lstrip("-").isdigit():
                cents = int(text)
            else:
                return _led_amount_cents(text, path, unit="yuan")
        elif isinstance(value, float):
            return _led_amount_cents(repr(value), path, unit="yuan")
        else:
            return _led_amount_cents(value, path, unit="yuan")
    if cents < 0:
        raise LedgerStructureError(f"{path}: amount must be non-negative, got {cents}")
    return cents


_LED_NON_ENERGY_MARKERS = (
    "basic_fee", "basic_electricity", "demand_fee", "capacity_fee",
    "power_factor", "adjustment_fee", "surcharge", "government_fund",
    "基本电费", "需量电费", "容量电费", "力调", "功率因数调整", "代征", "政府性基金",
)
_LED_ENERGY_MARKERS = (
    "energy", "active_energy", "electricity_charge", "energy_fee",
    "电能", "电度", "电量电费",
)


def _led_is_energy_line(raw: dict) -> bool:
    kind = _led_pick(raw, (
        "fee_type", "charge_type", "fee_category", "category",
        "tariff_kind", "charge_category", "fee_kind",
        "费用类型", "费别", "费用类别",
    ))
    if kind is None:
        return True
    text = str(kind).strip().lower()
    for marker in _LED_NON_ENERGY_MARKERS:
        if marker.lower() in text:
            return False
    return True


def ledger_apportion_by_weights(
    total_cents: int,
    days: Sequence[date] | list[date],
    weights: dict[str, Fraction] | dict[date, Fraction] | None = None,
) -> dict[str, int]:
    """Largest-remainder apportionment to integer cents.

    Floor every exact quota, then hand the remaining cents to the largest
    decimal remainders; equal remainders go to the earlier billing day.
    All-zero weights fall back to equal weights.
    """
    days = list(days)
    if total_cents < 0:
        raise LedgerStructureError("apportionment total must be non-negative")
    n = len(days)
    if n == 0:
        return {}
    weights = weights or {}

    def weight(d: date) -> Fraction:
        if d in weights:  # type: ignore[operator]
            return Fraction(weights[d])  # type: ignore[index]
        key = d.isoformat()
        if key in weights:
            return Fraction(weights[key])  # type: ignore[index]
        return Fraction(0)

    ws = [weight(d) for d in days]
    if any(w < 0 for w in ws):
        raise LedgerStructureError("system weights must be non-negative for proportional apportionment")
    sw = sum(ws, Fraction(0))
    if sw == 0:
        ws = [Fraction(1) for _ in days]
        sw = Fraction(n)
    total_f = Fraction(total_cents)
    quotas = [total_f * w / sw for w in ws]
    floors = [q.numerator // q.denominator for q in quotas]
    remainder = total_cents - sum(floors)
    if not 0 <= remainder <= n:
        # Defensive: mathematically impossible, but never silently corrupt money.
        raise LedgerError(f"apportionment remainder {remainder} out of range 0..{n}")
    fracs = [q - f for q, f in zip(quotas, floors)]
    order = sorted(range(n), key=lambda i: (-fracs[i], days[i].toordinal()))
    for i in order[:remainder]:
        floors[i] += 1
    return {days[i].isoformat(): int(floors[i]) for i in range(n)}


# Common public alias requested by the spec vocabulary.
apportion_amount_to_days = ledger_apportion_by_weights
apportion_amount = ledger_apportion_by_weights
apportion_bill_amount = ledger_apportion_by_weights
largest_remainder_apportionment = ledger_apportion_by_weights
allocate_amount_to_days = ledger_apportion_by_weights


def _led_parse_bill_line(raw: Any, path: str) -> dict:
    if not isinstance(raw, dict):
        raise LedgerStructureError(f"{path}: bill line must be an object")
    line_id = str(_led_require(raw, (
        "line_id", "line_no", "line_number", "bill_line_id", "id",
        "行号", "行编号", "账单行号",
    ), path))
    scope = _led_pick(raw, (
        "scope_id", "scope", "settlement_scope_id", "scope_code",
        "account_scope_id", "结算范围", "范围",
    ))
    scope = str(scope) if scope is not None else None
    start, end = _led_interval(raw, path)
    cents_value = _led_pick(raw, (
        "amount_cents", "amount_fen", "amount_fen_int", "total_amount_cents",
        "cents", "金额分",
    ))
    if cents_value is not None:
        amount_cents = _led_amount_cents(cents_value, path + ".amount_cents", unit="cents")
    else:
        yuan_value = _led_pick(raw, ("amount_yuan", "amount_yuan_fraction", "金额元"))
        if yuan_value is not None:
            amount_cents = _led_amount_cents(yuan_value, path + ".amount_yuan", unit="yuan")
        else:
            smart_value = _led_require(raw, ("amount", "total_amount", "bill_amount", "金额"), path + ".amount")
            amount_cents = _led_amount_cents(smart_value, path + ".amount", unit="smart")
    energy = _led_is_energy_line(raw)
    return {
        "line_id": line_id,
        "scope_id": scope,
        "start_day": _led_day_key(start),
        "end_day": _led_day_key(end),
        "amount_cents": amount_cents,
        "is_energy": energy,
        "fee_type": _led_pick(raw, ("fee_type", "charge_type", "category", "费用类型"), None),
    }


def _led_flatten_bill_lines(raw_bills: Any, path: str) -> tuple[list[dict], list[dict]]:
    """Accept bare line lists or bill documents with nested ``lines``."""
    if raw_bills is None:
        return [], []
    if isinstance(raw_bills, dict):
        raw_bills = [raw_bills]
    if not isinstance(raw_bills, list):
        raise LedgerStructureError(f"{path}: must be a list of bill lines / bills")
    lines: list[dict] = []
    skipped: list[dict] = []
    for i, item in enumerate(raw_bills):
        if isinstance(item, dict) and isinstance(item.get("lines"), list) and not _led_looks_like_line(item):
            bill_id = item.get("bill_id") or item.get("bill_no") or item.get("id")
            for j, sub in enumerate(item["lines"]):
                parsed = _led_parse_bill_line(sub, f"{path}[{i}].lines[{j}]")
                if parsed["scope_id"] is None:
                    raise LedgerStructureError(f"{path}[{i}].lines[{j}]: scope_id is required for original bill lines")
                parsed["bill_id"] = str(bill_id) if bill_id is not None else None
                if parsed["is_energy"]:
                    lines.append(parsed)
                else:
                    skipped.append(parsed)
        else:
            parsed = _led_parse_bill_line(item, f"{path}[{i}]")
            if parsed["scope_id"] is None:
                raise LedgerStructureError(f"{path}[{i}]: scope_id is required for original bill lines")
            if parsed["is_energy"]:
                lines.append(parsed)
            else:
                skipped.append(parsed)
    return lines, skipped


def _led_looks_like_line(raw: dict) -> bool:
    return any(k in raw for k in ("line_id", "line_no", "line_number", "行号", "amount_cents"))


def _led_parse_old_ids(raw: Any, path: str) -> list[str]:
    if raw is None:
        raise LedgerStructureError(f"{path}: old red-flush line ids are required")
    if not isinstance(raw, list) or len(raw) == 0:
        # Also accept a single scalar id.
        if isinstance(raw, (str, int)) and not isinstance(raw, bool):
            return [str(raw)]
        raise LedgerStructureError(f"{path}: expected a non-empty list of old line ids")
    out: list[str] = []
    for i, item in enumerate(raw):
        if isinstance(item, dict):
            lid = _led_require(item, ("line_id", "line_no", "line_number", "id", "行号"), f"{path}[{i}]")
            out.append(str(lid))
        elif isinstance(item, (str, int)) and not isinstance(item, bool):
            out.append(str(item))
        else:
            raise LedgerStructureError(f"{path}[{i}]: old line id must be string/int or object")
    # De-duplicate while preserving the listed order.
    seen: set[str] = set()
    uniq: list[str] = []
    for lid in out:
        if lid not in seen:
            seen.add(lid)
            uniq.append(lid)
    return uniq


def ledger_parse_exchange_order(raw: Any, path: str = "exchange") -> dict:
    if not isinstance(raw, dict):
        raise LedgerStructureError(f"{path}: exchange order must be an object")
    exchange_id = str(_led_require(raw, (
        "exchange_id", "exchange_no", "exchange_number", "order_id", "order_no",
        "voucher_id", "change_id", "note_id", "bill_change_id", "id",
        "换单编号", "换单号", "编号",
    ), path))
    seq_value = _led_require(raw, (
        "effective_seq", "effective_sequence", "effective_sequence_no",
        "effect_seq", "effective_order", "sequence_no", "sequence", "seq",
        "生效序号", "生效次序",
    ), path)
    if isinstance(seq_value, bool):
        raise LedgerStructureError(f"{path}.effective_seq: boolean is not a sequence")
    try:
        if isinstance(seq_value, int):
            seq = seq_value
        elif isinstance(seq_value, float) and float(seq_value).is_integer():
            seq = int(seq_value)
        elif isinstance(seq_value, str) and seq_value.strip().lstrip("-").isdigit():
            seq = int(seq_value.strip())
        else:
            raise ValueError
    except (ValueError, AttributeError) as exc:
        raise LedgerStructureError(f"{path}.effective_seq: {seq_value!r} is not an integer") from exc
    scope = str(_led_require(raw, (
        "scope_id", "scope", "settlement_scope_id", "scope_code", "结算范围", "范围",
    ), path))
    start, end = _led_interval(raw, path)
    old_raw = _led_pick(raw, (
        "red_flush_line_ids", "old_line_ids", "red_line_ids", "flushed_line_ids",
        "flush_line_ids", "replaces_line_ids", "red_flush", "old_lines",
        "red_flush_line_id", "old_line_id", "red_line_id", "flush_line_id",
        "replaces_line_id", "冲红行号", "旧行号", "冲红行",
    ))
    old_line_ids = _led_parse_old_ids(old_raw, path + ".old_line_ids")
    new_raw = _led_pick(raw, (
        "new_lines", "replacement_lines", "new_bill_lines",
        "new_bill_rows", "lines", "新账单行", "新行",
    ))
    if not isinstance(new_raw, list) or not new_raw:
        raise LedgerStructureError(f"{path}.new_lines: expected a non-empty list of new bill lines")
    new_lines: list[dict] = []
    skipped_non_energy: list[dict] = []
    for j, sub in enumerate(new_raw):
        line = _led_parse_bill_line(sub, f"{path}.new_lines[{j}]")
        if line["scope_id"] is None:
            line["scope_id"] = scope
        if line["scope_id"] != scope:
            raise LedgerStructureError(
                f"{path}.new_lines[{j}]: scope {line['scope_id']!r} differs from order scope {scope!r}"
            )
        if line["is_energy"]:
            new_lines.append(line)
        else:
            skipped_non_energy.append(line)
    if not new_lines:
        raise LedgerStructureError(f"{path}.new_lines: no electric-energy lines to post")
    # Structural coverage: new lines must partition the order interval by day.
    order_days = set(_led_days(start, end))
    covered: set[date] = set()
    for line in new_lines:
        ls = date.fromisoformat(line["start_day"])
        le = date.fromisoformat(line["end_day"])
        line_days = set(_led_days(ls, le))
        if not line_days:
            raise LedgerStructureError(f"{path}: new line {line['line_id']} has empty interval")
        if not line_days <= order_days:
            raise LedgerStructureError(
                f"{path}: new line {line['line_id']} [{line['start_day']},{line['end_day']}) "
                f"extends outside order interval [{_led_day_key(start)},{_led_day_key(end)})"
            )
        overlap = covered & line_days
        if overlap:
            days_txt = ",".join(sorted(d.isoformat() for d in overlap))
            raise LedgerStructureError(
                f"{path}: new lines overlap on days {days_txt}; new bill lines must not overlap"
            )
        covered |= line_days
    missing = order_days - covered
    if missing:
        days_txt = ",".join(sorted(d.isoformat() for d in missing))
        raise LedgerStructureError(
            f"{path}: new lines do not cover order interval; missing days {days_txt}"
        )
    new_ids = [ln["line_id"] for ln in new_lines]
    dup = {x for x in new_ids if new_ids.count(x) > 1}
    if dup:
        raise LedgerStructureError(f"{path}: duplicate new line ids {sorted(dup)}")
    content = {
        "exchange_id": exchange_id,
        "effective_seq": seq,
        "scope_id": scope,
        "start_day": _led_day_key(start),
        "end_day": _led_day_key(end),
        "old_line_ids": old_line_ids,
        "new_lines": new_lines,
    }
    content["content_hash"] = _led_content_hash(content)
    return content


def _led_content_hash(content: dict) -> str:
    payload = {k: v for k, v in content.items() if k != "content_hash"}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _led_looks_like_report(obj: Any) -> bool:
    return isinstance(obj, dict) and "verification" in obj and isinstance(obj.get("slices"), list)


def ledger_build_basis(
    settlement: Any = None,
    settlement_report: Any = None,
    system_amounts: Any = None,
    money_scale: int = 100,
) -> dict:
    """Build the per scope/day verified settlement basis.

    Accepts a settlement input root (settle() is run), a ready settlement
    report, or an explicit ``system_amounts`` mapping for lightweight use.
    """
    report = settlement_report
    root = settlement if isinstance(settlement, dict) and not _led_looks_like_report(settlement) else None
    if report is None and settlement is not None:
        if _led_looks_like_report(settlement):
            report = settlement
        else:
            report = settle(settlement)
    if settlement_report is not None and _led_looks_like_report(settlement_report):
        report = settlement_report

    basis: dict[str, dict[str, dict]] = defaultdict(dict)
    global_blockers: list[dict] = []
    scope_blockers: dict[str, list[dict]] = defaultdict(list)
    settlement_info: dict[str, Any] = {"money_scale": money_scale}

    if system_amounts is not None:
        _led_ingest_explicit_amounts(system_amounts, basis, money_scale)

    if report is not None:
        if not _led_looks_like_report(report):
            raise LedgerStructureError("settlement_report does not look like a settle() report")
        settlement_info.update({
            "overall_status": report.get("overall_status"),
            "rule_version": report.get("rule_version"),
            "input_audit": report.get("input_audit"),
        })
        comp_by_id = {c.get("component_id"): c for c in report.get("components", []) if isinstance(c, dict)}
        checks = report.get("verification", {}).get("control_checks", [])
        seg_checks = {
            c.get("segment_id"): c
            for c in report.get("verification", {}).get("segment_checks", [])
            if isinstance(c, dict)
        }
        ccs_by_key: dict[tuple[str, str, str], dict] = {}
        ccs_by_day: dict[tuple[str, str], list[dict]] = defaultdict(list)
        for c in checks:
            ctrl = c.get("control") or {}
            sid, day, rid = ctrl.get("scope_id"), ctrl.get("billing_day"), ctrl.get("rate_id")
            if sid is None or day is None:
                continue
            key = (str(sid), str(day), str(rid))
            ccs_by_key[key] = c
            ccs_by_day[(str(sid), str(day))].append(c)

        amount_by_day: dict[tuple[str, str], Fraction] = defaultdict(Fraction)
        evidence_by_day: dict[tuple[str, str], list[dict]] = defaultdict(list)
        meter_scopes: dict[str, set[str]] = defaultdict(set)
        for sl in report.get("slices", []):
            ctrl = sl.get("control") or {}
            sid, day = ctrl.get("scope_id"), ctrl.get("billing_day")
            if sid is None or day is None:
                continue
            amt_text = sl.get("amount_fraction")
            if amt_text is not None:
                try:
                    amount_by_day[(str(sid), str(day))] += Fraction(str(amt_text))
                except (ValueError, ZeroDivisionError):
                    pass
            b = sl.get("basis") or {}
            if b.get("meter_id"):
                meter_scopes[str(b["meter_id"])].add(str(sid))
            evidence_by_day[(str(sid), str(day))].append({
                "atom_id": sl.get("atom_id"),
                "component_id": sl.get("component_id"),
                "segment_id": sl.get("segment_id"),
                "rate_id": ctrl.get("rate_id"),
                "allocated_wh": sl.get("allocated_wh"),
                "unit_price": sl.get("unit_price"),
                "amount_fraction": sl.get("amount_fraction"),
                "rounding": sl.get("rounding"),
                "meter_id": b.get("meter_id"),
                "binding_id": b.get("binding_id"),
                "tariff_version": b.get("tariff_version"),
            })

        # Ensure every reported control day gets a basis record.
        day_keys = set(ccs_by_day) | set(amount_by_day)
        for sid, day in sorted(day_keys):
            day_checks = ccs_by_day.get((sid, day), [])
            reasons: list[str] = []
            if not day_checks:
                reasons.append("no_verified_settlement_cell")
            component_ids: set[str] = set()
            segment_ids: set[str] = set()
            control_keys: list[dict] = []
            for c in day_checks:
                ctrl = c.get("control") or {}
                control_keys.append(ctrl)
                cid = c.get("component_id")
                if cid is not None:
                    component_ids.add(str(cid))
                if c.get("status") != "verified":
                    reasons.append("control_cell_not_verified")
                if c.get("sum_matches_control") is False:
                    reasons.append("control_sum_mismatch")
                if c.get("component_status") != "settled":
                    reasons.append("component_not_settled")
            for cid in component_ids:
                comp = comp_by_id.get(cid)
                if comp is None:
                    reasons.append("settlement_component_missing")
                    continue
                if comp.get("status") != "settled":
                    reasons.append("connected_range_not_settled")
                for ck in comp.get("control_keys", []):
                    check = ccs_by_key.get((
                        str(ck.get("scope_id")), str(ck.get("billing_day")), str(ck.get("rate_id"))
                    ))
                    if check is None or check.get("status") != "verified":
                        reasons.append("related_control_cell_not_verified")
                for seg_id in comp.get("segment_ids", []):
                    segment_ids.add(str(seg_id))
                    sc = seg_checks.get(seg_id)
                    if sc is None or sc.get("status") != "verified":
                        reasons.append("reading_segment_not_verified")
            amount_yuan = amount_by_day.get((sid, day), Fraction(0))
            exact_cents = amount_yuan * money_scale
            if exact_cents < 0:
                reasons.append("negative_system_amount")
            rounded = _led_round_half_up_cents(exact_cents) if exact_cents >= 0 else None
            # De-duplicate reason codes preserving order.
            reasons = list(dict.fromkeys(reasons))
            basis[sid][day] = {
                "scope_id": sid,
                "billing_day": day,
                "openable": not reasons,
                "block_reasons": reasons,
                "system_amount_yuan_fraction": frac_str(amount_yuan),
                "system_amount_exact_cents_fraction": frac_str(exact_cents),
                "system_amount_cents_rounded": rounded,
                "control_keys": control_keys,
                "component_ids": sorted(component_ids),
                "segment_ids": sorted(segment_ids),
                "settlement_evidence": evidence_by_day.get((sid, day), []),
            }

        # Global issue attribution (same scope attribution idea as revision impact).
        bindings: list[Binding] = []
        issue_root = root
        if issue_root is None:
            audit = report.get("input_audit") or {}
            issue_root = {"exchange_vouchers": audit.get("exchange_vouchers", [])}
        try:
            bindings = load_bindings(issue_root if root is not None else (settlement if isinstance(settlement, dict) else {}))
        except Exception:
            bindings = []
        for issue in report.get("verification", {}).get("global_issues", []):
            if not isinstance(issue, dict):
                continue
            scopes = _led_issue_scopes(issue, issue_root, bindings, meter_scopes)
            record = {"code": issue.get("code"), "issue": issue, "scope_ids": sorted(scopes)}
            if scopes:
                for sid in scopes:
                    scope_blockers[sid].append(record)
            else:
                global_blockers.append(record)

    return {
        "basis": {sid: dict(days) for sid, days in basis.items()},
        "global_blockers": global_blockers,
        "scope_blockers": {sid: items for sid, items in scope_blockers.items()},
        "settlement_info": settlement_info,
    }


def _led_ingest_explicit_amounts(system_amounts: Any, basis: dict, money_scale: int) -> None:
    items: list[tuple[str, str, Any]] = []
    if isinstance(system_amounts, dict):
        for key, value in system_amounts.items():
            if isinstance(value, dict):
                # nested {scope: {day: amount}}
                for day, amt in value.items():
                    items.append((str(key), str(day), amt))
            elif isinstance(key, tuple) and len(key) == 2:
                items.append((str(key[0]), _led_day_key(key[1] if isinstance(key[1], date) else date.fromisoformat(str(key[1]))), value))  # noqa: E501
            elif "|" in str(key):
                sid, day = str(key).split("|", 1)
                items.append((sid, day, value))
            else:
                raise LedgerStructureError(
                    f"system_amounts key {key!r} must be (scope, day) or 'scope|YYYY-MM-DD'"
                )
    else:
        raise LedgerStructureError("system_amounts must be a mapping")
    for sid, day, value in items:
        if isinstance(value, dict) and ("exact_cents" in value or "rounded_cents" in value):
            exact = Fraction(value.get("exact_cents", value.get("rounded_cents", 0)))
            rounded = value.get("rounded_cents")
            rec_extra = value
        elif isinstance(value, dict) and any(
            k in value for k in ("amount_fraction", "yuan_fraction", "system_amount_yuan", "amount_yuan")
        ):
            yuan = value.get("amount_fraction") or value.get("yuan_fraction") \
                or value.get("system_amount_yuan") or value.get("amount_yuan")
            exact = parse_rational(yuan, f"system_amounts[{sid}][{day}]") * money_scale
            rounded = value.get("rounded_cents")
            rec_extra = value
        else:
            exact = Fraction(value) if isinstance(value, Fraction) else Fraction(str(value))
            rounded = None
            rec_extra = {}
        if rounded is None:
            rounded = _led_round_half_up_cents(exact)
        basis[sid][day] = {
            "scope_id": sid,
            "billing_day": day,
            "openable": True,
            "block_reasons": [],
            "system_amount_yuan_fraction": frac_str(exact / money_scale),
            "system_amount_exact_cents_fraction": frac_str(exact),
            "system_amount_cents_rounded": int(rounded),
            "control_keys": rec_extra.get("control_keys", []),
            "component_ids": rec_extra.get("component_ids", []),
            "segment_ids": rec_extra.get("segment_ids", []),
            "settlement_evidence": rec_extra.get("settlement_evidence", []),
            "explicit_basis": True,
        }


def _led_issue_scopes(
    issue: dict,
    root: Any,
    bindings: list,
    meter_scopes: dict[str, set[str]],
) -> set[str]:
    scopes: set[str] = set()
    for key in ("scope_id", "scope", "settlement_scope_id"):
        if issue.get(key):
            scopes.add(str(issue[key]))
    vid = issue.get("voucher_id")
    if vid:
        try:
            scopes |= _rev_scope_ids_for_voucher(issue, root if isinstance(root, dict) else {}, bindings)
        except Exception:
            pass
        # Fallback: voucher meters -> scopes observed on settlement slices.
        vouchers = []
        if isinstance(root, dict):
            vouchers = root.get("exchange_vouchers", root.get("meter_exchanges", [])) or []
        for v in vouchers if isinstance(vouchers, list) else []:
            if not isinstance(v, dict):
                continue
            if str(v.get("voucher_id") or v.get("id") or "") != str(vid):
                continue
            for mkey in ("old_meter_id", "new_meter_id", "meter_id"):
                mid = v.get(mkey)
                if mid and str(mid) in meter_scopes:
                    scopes |= meter_scopes[str(mid)]
    return scopes


_LED_STATUS_EFFECTIVE = "effective"
_LED_STATUS_PENDING = "pending_verification"
_LED_STATUS_LABELS = {
    _LED_STATUS_EFFECTIVE: "已生效",
    _LED_STATUS_PENDING: "待核",
}


class MakeupLedger:
    """Auditable makeup-difference (补差) ledger.

    Use :meth:`open` to create from the fixed settlement input plus the
    non-overlapping original bills, then :meth:`receive_batch` for each actual
    reception batch.  Instances are plain-JSON serializable via :meth:`state`
    and can be closed/reopened with :meth:`save` / :meth:`load`.
    """

    def __init__(self, state: dict, store_path: Optional[Path | str] = None):
        if not isinstance(state, dict) or state.get("report_type") != "makeup_ledger_state":
            raise LedgerError("invalid ledger state object")
        self._state = state
        self.store_path = Path(store_path) if store_path is not None else None

    # ------------------------------------------------------------------ state
    @property
    def state(self) -> dict:
        return self._state

    def to_state(self) -> dict:
        return _led_deepcopy(self._state)

    # ------------------------------------------------------------- opening ---
    @classmethod
    def open(
        cls,
        settlement: Any = None,
        original_bills: Any = None,
        *,
        bills: Any = None,
        settlement_report: Any = None,
        system_amounts: Any = None,
        ledger_id: Optional[str] = None,
        money_scale: int = 100,
        out_of_opening_policy: str = "reject",
        store_path: Optional[Path | str] = None,
        persist: bool = False,
        opening_batch_id: str = _LEDGER_OPENING_BATCH,
        created_at: Optional[str] = None,
        **_ignored: Any,
    ) -> "MakeupLedger":
        if out_of_opening_policy not in ("reject", "pending"):
            raise LedgerStructureError("out_of_opening_policy must be 'reject' or 'pending'")
        basis_pack = ledger_build_basis(
            settlement=settlement,
            settlement_report=settlement_report,
            system_amounts=system_amounts,
            money_scale=money_scale,
        )
        raw_lines, skipped = _led_flatten_bill_lines(
            original_bills if original_bills is not None else bills, "original_bills"
        )
        # Unique original line numbers.
        seen: set[str] = set()
        dup: list[str] = []
        for ln in raw_lines:
            if ln["line_id"] in seen and ln["line_id"] not in dup:
                dup.append(ln["line_id"])
            seen.add(ln["line_id"])
        if dup:
            raise LedgerStructureError(f"original bill line ids must be unique; duplicates: {dup}")

        opening_rejections: list[dict] = []
        if basis_pack["global_blockers"]:
            opening_rejections.append({
                "code": "unattributable_settlement_blocking_evidence",
                "blocking_evidence": basis_pack["global_blockers"],
                "message": "baseline reports settlement blocking evidence that cannot be attributed to any scope",
            })

        basis = basis_pack["basis"]
        # Validate every covered day is openable and build domain/attribution.
        domain_days: dict[str, list[str]] = defaultdict(list)
        line_records: dict[str, dict] = {}
        attribution: dict[str, dict[str, str]] = defaultdict(dict)
        daily: dict[str, dict[str, dict]] = defaultdict(dict)
        for ln in raw_lines:
            sid = ln["scope_id"]
            s = date.fromisoformat(ln["start_day"])
            e = date.fromisoformat(ln["end_day"])
            days = _led_days(s, e)
            weights = {d.isoformat(): cls._basis_weight(basis, sid, d.isoformat()) for d in days}
            bad_days: list[dict] = []
            for d in days:
                rec = basis.get(sid, {}).get(d.isoformat())
                if rec is None:
                    bad_days.append({"billing_day": d.isoformat(), "reasons": ["no_settlement_basis"]})
                elif not rec.get("openable"):
                    bad_days.append({"billing_day": d.isoformat(), "reasons": rec.get("block_reasons", [])})
            if bad_days:
                opening_rejections.append({
                    "code": "bill_line_covers_non_openable_days",
                    "line_id": ln["line_id"],
                    "scope_id": sid,
                    "bad_days": bad_days,
                })
                continue
            app = ledger_apportion_by_weights(ln["amount_cents"], days, weights)
            record = {
                "line_id": ln["line_id"],
                "kind": "original",
                "scope_id": sid,
                "start_day": ln["start_day"],
                "end_day": ln["end_day"],
                "amount_cents": ln["amount_cents"],
                "fee_type": ln.get("fee_type"),
                "source_exchange_id": None,
                "bill_id": ln.get("bill_id"),
                "apportionment": app,
            }
            line_records[ln["line_id"]] = record
            domain_days[sid].extend(d.isoformat() for d in days)

        # Overlap check across original lines of the same scope.
        covered: dict[str, dict[str, str]] = defaultdict(dict)
        for ln in raw_lines:
            sid = ln["scope_id"]
            for day in _led_days(date.fromisoformat(ln["start_day"]), date.fromisoformat(ln["end_day"])):
                key = day.isoformat()
                if key in covered[sid]:
                    opening_rejections.append({
                        "code": "original_bill_lines_overlap",
                        "scope_id": sid,
                        "billing_day": key,
                        "line_ids": [covered[sid][key], ln["line_id"]],
                    })
                else:
                    covered[sid][key] = ln["line_id"]

        if opening_rejections:
            report = {
                "report_type": "makeup_ledger_opening",
                "status": "rejected",
                "ledger_id": ledger_id,
                "rule_version": MAKEUP_LEDGER_RULE_VERSION,
                "rejections": opening_rejections,
                "global_blocking_evidence": basis_pack["global_blockers"],
                "settlement_overall_status": basis_pack["settlement_info"].get("overall_status"),
                "message": "makeup ledger opening rejected; no ledger was created",
            }
            raise LedgerOpeningRejected(report)

        # Build attribution + daily from the validated original lines.
        for lid, rec in line_records.items():
            sid = rec["scope_id"]
            for day_iso, cents in rec["apportionment"].items():
                attribution[sid][day_iso] = lid
                basis_rec = basis[sid][day_iso]
                sys_cents = int(basis_rec["system_amount_cents_rounded"])
                daily[sid][day_iso] = {
                    "scope_id": sid,
                    "billing_day": day_iso,
                    "line_id": lid,
                    "line_kind": "original",
                    "apportioned_amount_cents": cents,
                    "system_amount_cents": sys_cents,
                    "daily_difference_cents": cents - sys_cents,
                }
        for sid in list(domain_days):
            domain_days[sid] = sorted(set(domain_days[sid]))

        ts = created_at or datetime.now(timezone.utc).isoformat()
        state: dict[str, Any] = {
            "report_type": "makeup_ledger_state",
            "rule_version": MAKEUP_LEDGER_RULE_VERSION,
            "ledger_id": ledger_id or f"ML-{hashlib.sha1(json.dumps([l['line_id'] for l in raw_lines]).encode()).hexdigest()[:10]}",  # noqa: E501
            "created_at": ts,
            "current_batch_id": opening_batch_id,
            "opening_batch_id": opening_batch_id,
            "out_of_opening_policy": out_of_opening_policy,
            "money_scale": money_scale,
            "settlement_info": basis_pack["settlement_info"],
            "basis": basis,
            "scope_blockers": basis_pack["scope_blockers"],
            "domain_days": dict(domain_days),
            "lines": line_records,
            "original_line_ids": [ln["line_id"] for ln in raw_lines],
            "attribution": {s: dict(d) for s, d in attribution.items()},
            "daily": {s: dict(d) for s, d in daily.items()},
            "orders": {},
            "seq_index": {},
            "batch_ids": {},
            "batches": [],
            "skipped_non_energy_original_lines": skipped,
        }
        ledger = cls(state, store_path=store_path)
        opening_view = ledger._build_view(
            batch_id=opening_batch_id,
            batch_index=0,
            received_at=ts,
            postings=[],
            daily_changes=[],
            changed_adjudications=[],
            new_order_rulings=[],
            considered_order_ids=[],
            trigger_order_ids=[],
            batch_note="opening",
        )
        opening_record = {
            "batch_id": opening_batch_id,
            "batch_index": 0,
            "received_at": ts,
            "kind": "opening",
            "exchange_ids": [],
            "new_order_ids": [],
            "idempotent_order_ids": [],
            "status": "opened",
            "totals": opening_view["totals"],
            "view": opening_view,
        }
        state["batches"].append(opening_record)
        state["batch_ids"][opening_batch_id] = 0
        ledger._opening_report = {
            "report_type": "makeup_ledger_opening",
            "status": "opened",
            "ledger_id": state["ledger_id"],
            "batch_id": opening_batch_id,
            "original_line_count": len(raw_lines),
            "opened_scope_days": {s: len(ds) for s, ds in domain_days.items()},
            "skipped_non_energy_lines": skipped,
            "totals": opening_view["totals"],
        }
        if persist or store_path is not None:
            ledger.save(store_path)
        return ledger

    @staticmethod
    def _basis_weight(basis: dict, scope: str, day_iso: str) -> Fraction:
        rec = basis.get(scope, {}).get(day_iso)
        if rec is None:
            return Fraction(0)
        return Fraction(rec["system_amount_exact_cents_fraction"])

    @property
    def opening_report(self) -> dict:
        return getattr(self, "_opening_report", {"status": "opened"})

    # ------------------------------------------------------------ persist ---
    def save(self, path: Optional[Path | str] = None) -> Path:
        target = Path(path) if path is not None else self.store_path
        if target is None:
            raise LedgerError("save path is required (no store_path associated with this ledger)")
        if target.is_dir() or target.suffix == "":
            target = target / "makeup_ledger_state.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(
            json.dumps(self._state, ensure_ascii=False, indent=2, sort_keys=False),
            encoding="utf-8",
        )
        tmp.replace(target)
        self.store_path = target
        return target

    @classmethod
    def load(cls, path: Path | str) -> "MakeupLedger":
        p = Path(path)
        if p.is_dir():
            p = p / "makeup_ledger_state.json"
        state = json.loads(p.read_text(encoding="utf-8"))
        return cls(state, store_path=p)

    # ----------------------------------------------------------- queries ----
    def list_batches(self) -> list[dict]:
        return [
            {k: v for k, v in b.items() if k != "view"}
            for b in self._state["batches"]
        ]

    history = list_batches

    def get_batch_record(self, batch_id: Any = None) -> dict:
        batches = self._state["batches"]
        if batch_id is None or batch_id in ("latest", "current", -1):
            return batches[-1]
        if batch_id in ("opening", self._state.get("opening_batch_id")):
            return batches[0]
        if isinstance(batch_id, int):
            return batches[batch_id]
        if isinstance(batch_id, str) and batch_id.lstrip("-").isdigit():
            return batches[int(batch_id)]
        idx = self._state["batch_ids"].get(str(batch_id))
        if idx is None:
            raise LedgerError(f"unknown batch_id {batch_id!r}; known: {list(self._state['batch_ids'])}")
        return batches[idx]

    def get_batch(self, batch_id: Any = None) -> dict:
        """Full retained batch record including the joined finance view."""
        record = _led_deepcopy(self.get_batch_record(batch_id))
        record["view"] = self.view(record.get("batch_id"))
        return record

    def view(self, batch_id: Any = None, *, scope_id: Optional[str] = None) -> dict:
        """Finance view retained after a reception (default: current/latest)."""
        record = self.get_batch_record(batch_id)
        out = _led_deepcopy(record["view"])
        self._enrich_view(out, record.get("batch_index"))
        if scope_id is not None:
            out["effective_attribution"] = [
                r for r in out["effective_attribution"] if r["scope_id"] == scope_id
            ]
            out["bill_attribution"] = out["effective_attribution"]
            out["daily_ledger"] = [r for r in out["daily_ledger"] if r["scope_id"] == scope_id]
            out["daily_apportionments"] = out["daily_ledger"]
            out["daily_differences"] = [
                r for r in out["daily_differences"] if r["scope_id"] == scope_id
            ]
        return out

    snapshot = view
    get_view = view

    def attribution(self, batch_id: Any = None, *, scope_id: Optional[str] = None) -> list[dict]:
        return self.view(batch_id, scope_id=scope_id)["effective_attribution"]

    def apportionments(self, batch_id: Any = None, *, scope_id: Optional[str] = None) -> list[dict]:
        return self.view(batch_id, scope_id=scope_id)["daily_ledger"]

    def postings_history(self) -> list[dict]:
        out: list[dict] = []
        for b in self._state["batches"]:
            for p in b.get("view", {}).get("postings", []):
                row = _led_deepcopy(p)
                row["batch_id"] = b["batch_id"]
                row["batch_index"] = b["batch_index"]
                out.append(row)
        return out

    def get_order(self, exchange_id: str) -> dict:
        if exchange_id not in self._state["orders"]:
            raise LedgerError(f"unknown exchange order {exchange_id!r}")
        return _led_deepcopy(self._order_view(self._state["orders"][exchange_id], include_history=True))

    # ------------------------------------------------------- reception -----
    def receive_batch(
        self,
        batch: Any = None,
        *,
        orders: Any = None,
        exchanges: Any = None,
        items: Any = None,
        batch_id: Optional[str] = None,
        received_at: Optional[str] = None,
        persist: bool = True,
        **kwargs: Any,
    ) -> dict:
        raw_orders, meta = _led_coerce_reception(
            batch if batch is not None else (orders if orders is not None else (exchanges if exchanges is not None else items)),  # noqa: E501
            batch_id=batch_id,
            received_at=received_at,
            extra=kwargs,
        )
        # All parsing and uniqueness validation finishes (and raises) before
        # any state mutation, so a rejected batch leaves this object untouched.
        # Re-adjudication itself only walks overlap-affected later orders.
        result = _led_apply_reception(self._state, raw_orders, meta)
        if persist and self.store_path is not None:
            self.save()
        return result

    # Common verb aliases (assigned additional aliases after the class too).
    def receive(self, *a: Any, **kw: Any) -> dict:
        return self.receive_batch(*a, **kw)

    def accept_batch(self, *a: Any, **kw: Any) -> dict:
        return self.receive_batch(*a, **kw)

    # ------------------------------------------------------- audit refold --
    def full_refold(self) -> dict:
        """Re-adjudicate every received order by effective seq from opening.

        Returns ``{"attribution":..., "daily":...}``.  This is the spec's
        reference semantics and a test/audit tool; normal reception does not
        call it.
        """
        attr: dict[str, dict[str, Optional[str]]] = defaultdict(dict)
        daily: dict[str, dict[str, dict]] = defaultdict(dict)
        for lid in self._state["original_line_ids"]:
            rec = self._state["lines"][lid]
            sid = rec["scope_id"]
            for day_iso, cents in rec["apportionment"].items():
                attr[sid][day_iso] = lid
                sys_cents = int(self._state["basis"][sid][day_iso]["system_amount_cents_rounded"])
                daily[sid][day_iso] = {
                    "scope_id": sid, "billing_day": day_iso, "line_id": lid,
                    "apportioned_amount_cents": cents, "system_amount_cents": sys_cents,
                    "daily_difference_cents": cents - sys_cents,
                }
        orders = sorted(self._state["orders"].values(), key=lambda o: (o["effective_seq"], o["exchange_id"]))
        rulings = {}
        for o in orders:
            status, reasons, prior = _led_adjudicate(draft_like_basis(self._state), o, attr)
            rulings[o["exchange_id"]] = status
            if status == _LED_STATUS_EFFECTIVE:
                _led_apply_order_to_maps(self._state, o, attr, daily)
        return {
            "rulings": rulings,
            "attribution": {s: {d: l for d, l in days.items()} for s, days in attr.items()},
            "daily": {s: dict(days) for s, days in daily.items()},
        }

    def verify_incremental_matches_full_refold(self) -> dict:
        ref = self.full_refold()
        problems: list[str] = []
        cur_attr = self._state["attribution"]
        for sid, days in ref["attribution"].items():
            for day, lid in days.items():
                if cur_attr.get(sid, {}).get(day) != lid:
                    problems.append(f"attribution mismatch {sid} {day}: incremental={cur_attr.get(sid, {}).get(day)} full={lid}")  # noqa: E501
        for sid, days in ref["daily"].items():
            for day, rec in days.items():
                cur = self._state["daily"].get(sid, {}).get(day)
                if cur is None or cur.get("apportioned_amount_cents") != rec["apportioned_amount_cents"]:
                    problems.append(f"daily mismatch {sid} {day}")
        ref_rulings = ref["rulings"]
        for oid, status in ref_rulings.items():
            if self._state["orders"][oid]["status"] != status:
                problems.append(f"ruling mismatch {oid}: incremental={self._state['orders'][oid]['status']} full={status}")  # noqa: E501
        return {"matches": not problems, "problems": problems}

    # ------------------------------------------------------- view builder --
    def _order_view(self, o: dict, *, include_history: bool = False) -> dict:
        out = {
            "exchange_id": o["exchange_id"],
            "effective_seq": o["effective_seq"],
            "scope_id": o["scope_id"],
            "start_day": o["start_day"],
            "end_day": o["end_day"],
            "interval": [o["start_day"], o["end_day"]],
            "old_line_ids": list(o["old_line_ids"]),
            "new_line_ids": [ln["line_id"] for ln in o["new_lines"]],
            "new_lines": _led_deepcopy(o["new_lines"]),
            "old_line_records": _led_line_refs(self._state, list(o["old_line_ids"])),
            "new_line_records": [
                _led_deepcopy(self._state["lines"][ln["line_id"]])
                for ln in o["new_lines"]
                if ln["line_id"] in self._state.get("lines", {})
            ],
            "content_hash": o["content_hash"],
            "status": o["status"],
            "adjudication_status": o["status"],
            "effective": o["status"] == _LED_STATUS_EFFECTIVE,
            "pending": o["status"] == _LED_STATUS_PENDING,
            "status_label": _LED_STATUS_LABELS[o["status"]],
            "adjudication": _led_deepcopy(o.get("adjudication")),
            "received_batch_id": o.get("received_batch_id"),
            "received_batch_index": o.get("received_batch_index"),
            "received_at": o.get("received_at"),
        }
        if include_history:
            out["ruling_history"] = _led_deepcopy(o.get("ruling_history", []))
        return out

    def _build_view(
        self,
        *,
        batch_id: str,
        batch_index: int,
        received_at: str,
        postings: list[dict],
        daily_changes: list[dict],
        changed_adjudications: list[dict],
        new_order_rulings: list[dict],
        considered_order_ids: list[str],
        trigger_order_ids: list[str],
        reaffirmed_adjudications: Optional[list[dict]] = None,
        propagation_edges: Optional[list[dict]] = None,
        batch_note: Optional[str] = None,
    ) -> dict:
        st = self._state
        attribution_rows: list[dict] = []
        daily_rows: list[dict] = []
        for sid in sorted(st["attribution"]):
            for day in sorted(st["attribution"][sid]):
                lid = st["attribution"][sid][day]
                line = st["lines"].get(lid, {})
                attribution_rows.append({
                    "scope_id": sid,
                    "billing_day": day,
                    "line_id": lid,
                    "line_kind": line.get("kind"),
                    "source_exchange_id": line.get("source_exchange_id"),
                })
                rec = st["daily"][sid][day]
                basis_rec = st.get("basis", {}).get(sid, {}).get(day, {})
                daily_rows.append({
                    **_led_deepcopy(rec),
                    "basis": {
                        "openable": basis_rec.get("openable"),
                        "system_amount_yuan_fraction": basis_rec.get("system_amount_yuan_fraction"),
                        "system_amount_exact_cents_fraction": basis_rec.get("system_amount_exact_cents_fraction"),
                        "system_amount_cents_rounded": basis_rec.get("system_amount_cents_rounded"),
                        "control_keys": basis_rec.get("control_keys", []),
                        "component_ids": basis_rec.get("component_ids", []),
                        "segment_ids": basis_rec.get("segment_ids", []),
                        "settlement_evidence": basis_rec.get("settlement_evidence", []),
                    },
                })
        total_app = sum(r["apportioned_amount_cents"] for r in daily_rows)
        total_sys = sum(r["system_amount_cents"] for r in daily_rows)
        opening_total_diff = sum(
            ln["amount_cents"] for ln in st["lines"].values() if ln["kind"] == "original"
        ) - total_sys  # system basis is fixed; opening diff total uses original billed amounts
        posting_delta = sum(p["delta_cents"] for p in postings)
        effective_orders = [oid for oid, o in st["orders"].items() if o["status"] == _LED_STATUS_EFFECTIVE]
        pending_orders = [oid for oid, o in st["orders"].items() if o["status"] == _LED_STATUS_PENDING]
        view = {
            "report_type": "makeup_ledger_view",
            "ledger_id": st["ledger_id"],
            "rule_version": MAKEUP_LEDGER_RULE_VERSION,
            "batch_id": batch_id,
            "batch_index": batch_index,
            "received_at": received_at,
            "note": batch_note,
            "effective_attribution": attribution_rows,
            "daily_ledger": daily_rows,
            # Vocabulary aliases and the compact daily_differences projection are
            # attached by _enrich_view() on query so snapshots store each list once.
            "line_catalog": {},  # joined from immutable history tables on query
            "bill_lines": {},
            "exchange_orders": {},  # reconstructed as-of batch on query
            "exchange_order_ids": [
                oid for oid, _ in sorted(st["orders"].items(), key=lambda kv: kv[1]["effective_seq"])
            ],
            "normalized_snapshot": True,
            "changed_adjudications": changed_adjudications,
            "readjudications": changed_adjudications,  # vocabulary alias
            "reaffirmed_adjudications": reaffirmed_adjudications or [],
            "propagation_edges": propagation_edges or [],
            "new_order_rulings": new_order_rulings,
            "considered_order_ids": considered_order_ids,
            "trigger_order_ids": trigger_order_ids,
            "postings": postings,
            "daily_difference_postings": postings,  # vocabulary alias
            "makeup_entries": postings,  # vocabulary alias
            "daily_changes": daily_changes,
            "totals": {
                "day_count": len(daily_rows),
                "apportioned_amount_cents": total_app,
                "system_amount_cents": total_sys,
                "current_daily_difference_cents": total_app - total_sys,
                "opening_billed_cents": sum(
                    ln["amount_cents"] for ln in st["lines"].values() if ln["kind"] == "original"
                ),
                "opening_daily_difference_cents": opening_total_diff,
                "batch_posting_delta_cents": posting_delta,
                "supplementary_cents": sum(p["delta_cents"] for p in postings if p["delta_cents"] > 0),
                "reversal_cents": sum(-p["delta_cents"] for p in postings if p["delta_cents"] < 0),
                "posting_count": sum(1 for p in postings if p["delta_cents"] != 0),
                "effective_order_count": len(effective_orders),
                "pending_order_count": len(pending_orders),
            },
            # Immutable fixed settlement basis; joined back by _enrich_view().
            "settlement_basis": {},
            "settlement_audit": _led_deepcopy(st["settlement_info"]),
        }
        return view

    def _order_view_at(self, o: dict, batch_index: int) -> dict:
        """Order view with status/adjudication projected as of ``batch_index``."""
        view = self._order_view(o)
        history = [
            h for h in o.get("ruling_history", [])
            if int(h.get("batch_index", o.get("received_batch_index", 1))) <= batch_index
        ]
        if history:
            h = history[-1]
            status = h["status"]
            view["status"] = status
            view["adjudication_status"] = status
            view["effective"] = status == _LED_STATUS_EFFECTIVE
            view["pending"] = status == _LED_STATUS_PENDING
            view["status_label"] = _LED_STATUS_LABELS[status]
            view["adjudication"] = {
                "status": status,
                "reasons": _led_deepcopy(h.get("reasons", [])),
                "adjudicated_in_batch": h.get("batch_id"),
                "adjudicated_at": h.get("received_at"),
            }
        return view

    def _enrich_view(self, view: dict, batch_index: Optional[int] = None) -> dict:
        """Join a normalized stored snapshot with immutable basis/lines/history."""
        if batch_index is None:
            batch_index = int(view.get("batch_index", len(self._state["batches"]) - 1))
        st = self._state
        catalog = {
            lid: _led_deepcopy(rec)
            for lid, rec in st["lines"].items()
            if int(rec.get("created_batch_index", 0)) <= batch_index
        }
        view["line_catalog"] = catalog
        view["bill_lines"] = catalog
        view["settlement_basis"] = _led_deepcopy(st["basis"])
        orders: dict[str, dict] = {}
        for oid, o in sorted(st["orders"].items(), key=lambda kv: kv[1]["effective_seq"]):
            if int(o.get("received_batch_index", 1)) <= batch_index:
                orders[oid] = self._order_view_at(o, batch_index)
        view["exchange_orders"] = orders
        # In-memory vocabulary aliases (same objects; not duplicated in stored JSON).
        view["bill_attribution"] = view["effective_attribution"]
        view["daily_apportionments"] = view["daily_ledger"]
        view["daily_differences"] = [
            {
                "scope_id": r["scope_id"],
                "billing_day": r["billing_day"],
                "daily_difference_cents": r["daily_difference_cents"],
                "line_id": r["line_id"],
            }
            for r in view["daily_ledger"]
        ]
        view["normalized_snapshot"] = False
        return view


# ---------------------------------------------------------------------------
# Reception coercion
# ---------------------------------------------------------------------------

def _led_coerce_reception(
    payload: Any,
    *,
    batch_id: Optional[str],
    received_at: Optional[str],
    extra: dict,
) -> tuple[list[dict], dict]:
    meta = {
        "batch_id": batch_id or extra.get("reception_id") or extra.get("batch_no"),
        "received_at": received_at or extra.get("received_date") or extra.get("batch_received_at"),
    }
    if payload is None:
        # Allow keyword-only style callers to pass via extra containers.
        for key in ("exchange_orders", "batch", "reception", "data"):
            if key in extra and extra[key] is not None:
                payload = extra[key]
                break
    if isinstance(payload, list):
        return payload, meta
    if isinstance(payload, dict):
        # Nested envelope convenience: {"reception": {...}} / {"batch": {...}}.
        for wrap_key in ("reception", "batch", "data", "payload"):
            inner = payload.get(wrap_key)
            if isinstance(inner, dict) and any(
                k in inner for k in ("exchanges", "exchange_orders", "orders", "items", "effective_seq")
            ):
                meta["batch_id"] = meta["batch_id"] or payload.get("batch_id") or payload.get("reception_id")
                return _led_coerce_reception(inner, batch_id=meta["batch_id"],
                                             received_at=meta["received_at"], extra={})
        for key in ("exchanges", "exchange_orders", "orders", "items", "exchange_list", "换单"):
            if isinstance(payload.get(key), list):
                meta["batch_id"] = meta["batch_id"] or payload.get("batch_id") or payload.get("reception_id") or payload.get("batch_no")  # noqa: E501
                meta["received_at"] = meta["received_at"] or payload.get("received_at") or payload.get("received_date")  # noqa: E501
                return payload[key], meta
        # A single exchange order?
        if any(k in payload for k in ("effective_seq", "effective_sequence", "seq", "生效序号")):
            return [payload], meta
        raise LedgerStructureError(
            "receive payload must be a list of exchange orders or an object with an 'exchanges' list"
        )
    raise LedgerStructureError("receive payload must be a list, object, or single exchange order")


# ---------------------------------------------------------------------------
# Adjudication / incremental re-processing
# ---------------------------------------------------------------------------

def draft_like_basis(state: dict) -> dict:
    return state


def _led_enrich_state_view(state: dict, view: dict, batch_index: Optional[int] = None) -> dict:
    """Enrich a normalized snapshot without constructing a public ledger."""
    wrapper = MakeupLedger.__new__(MakeupLedger)
    wrapper._state = state
    wrapper.store_path = None
    return wrapper._enrich_view(view, batch_index)


def _led_adjudicate(state: dict, order: dict, attr: dict) -> tuple[str, list[dict], dict[str, Optional[str]]]:
    """Apply the effective-sequence precondition against a fold state.

    Returns ``(status, reasons, prior_line_by_day)``.  Does not mutate.
    """
    sid = order["scope_id"]
    days = _led_days(date.fromisoformat(order["start_day"]), date.fromisoformat(order["end_day"]))
    scope_attr = attr.get(sid, {})
    old_ids = list(order["old_line_ids"])
    old_set = set(old_ids)
    prior: dict[str, Optional[str]] = {}
    bad_days: list[dict] = []
    hit: dict[str, int] = {oid: 0 for oid in old_ids}
    domain = set(state.get("domain_days", {}).get(sid, []))
    policy = state.get("out_of_opening_policy", "reject")
    outside_days: list[str] = []
    for d in days:
        iso = d.isoformat()
        current = scope_attr.get(iso)
        prior[iso] = current
        if iso not in domain:
            outside_days.append(iso)
        if current in old_set:
            hit[current] += 1
        else:
            bad_days.append({
                "billing_day": iso,
                "current_line_id": current,
                "listed_old_line_ids": old_ids,
            })
    reasons: list[dict] = []
    if outside_days and policy == "pending":
        reasons.append({
            "code": "interval_day_outside_opening",
            "message": "some interval days are outside the opened ledger domain",
            "days": outside_days,
        })
    if bad_days:
        reasons.append({
            "code": "day_not_attributed_to_listed_old_line",
            "message": "not every interval day is currently attributed to one of the listed old lines",
            "bad_days": bad_days,
        })
    for oid in old_ids:
        if hit[oid] == 0:
            exists = oid in state.get("lines", {})
            reasons.append({
                "code": "old_line_has_no_hit_day",
                "old_line_id": oid,
                "line_known_to_ledger": exists,
                "message": (
                    "listed old line does not hit a single interval day under current attribution"
                    + ("" if exists else " (line has not been received yet)")
                ),
            })
    effective = not reasons
    status = _LED_STATUS_EFFECTIVE if effective else _LED_STATUS_PENDING
    return status, reasons, prior


def _led_sync_daily(state: dict, scope: str, day_iso: str, attr: dict, daily: dict) -> None:
    lid = attr.get(scope, {}).get(day_iso)
    if lid is None:
        daily.get(scope, {}).pop(day_iso, None)
        return
    line = state["lines"][lid]
    cents = line["apportionment"][day_iso]
    sys_cents = int(state["basis"][scope][day_iso]["system_amount_cents_rounded"])
    daily.setdefault(scope, {})[day_iso] = {
        "scope_id": scope,
        "billing_day": day_iso,
        "line_id": lid,
        "line_kind": line.get("kind"),
        "apportioned_amount_cents": cents,
        "system_amount_cents": sys_cents,
        "daily_difference_cents": cents - sys_cents,
    }


def _led_apply_order_to_maps(state: dict, order: dict, attr: dict, daily: dict) -> None:
    sid = order["scope_id"]
    attr.setdefault(sid, {})
    for line in order["new_lines"]:
        for day_iso, cents in line["apportionment"].items():
            attr[sid][day_iso] = line["line_id"]
    for d in _led_days(date.fromisoformat(order["start_day"]), date.fromisoformat(order["end_day"])):
        _led_sync_daily(state, sid, d.isoformat(), attr, daily)


def _led_rollback_order(state: dict, order: dict) -> None:
    """Restore the pre-order attribution image stored at adjudication time."""
    sid = order["scope_id"]
    prior = (order.get("adjudication") or {}).get("prior_line_by_day") or {}
    for day_iso, prior_lid in prior.items():
        if prior_lid is None:
            state["attribution"].get(sid, {}).pop(day_iso, None)
            state["daily"].get(sid, {}).pop(day_iso, None)
        else:
            state["attribution"].setdefault(sid, {})[day_iso] = prior_lid
            _led_sync_daily(state, sid, day_iso, state["attribution"], state["daily"])


def _led_affected_order_ids(state: dict, seed_ids: list[str]) -> tuple[set[str], list[dict]]:
    """New orders plus later same-scope orders reachable by interval overlap.

    Edge ``r -> q`` exists when ``seq(q) > seq(r)`` and their half-open
    intervals overlap in the same scope.  This is exactly the transitive
    date-overlap influence channel.  Returns the id set and propagation edges.
    """
    orders = list(state["orders"].values())
    by_id = state["orders"]
    result: set[str] = set(seed_ids)
    edges: list[dict] = []
    edge_keys: set[tuple[str, str]] = set()
    changed = True
    while changed:
        changed = False
        for rid in list(result):
            r = by_id[rid]
            rs, re_ = date.fromisoformat(r["start_day"]), date.fromisoformat(r["end_day"])
            for q in orders:
                qid = q["exchange_id"]
                if q["effective_seq"] <= r["effective_seq"]:
                    continue
                if q["scope_id"] != r["scope_id"]:
                    continue
                qs, qe = date.fromisoformat(q["start_day"]), date.fromisoformat(q["end_day"])
                if max(rs, qs) < min(re_, qe):
                    ekey = (rid, qid)
                    if ekey not in edge_keys:
                        edge_keys.add(ekey)
                        edges.append({
                            "from_exchange_id": rid,
                            "to_exchange_id": qid,
                            "scope_id": r["scope_id"],
                            "overlap_days": sorted(
                                d.isoformat()
                                for d in _led_days(max(rs, qs), min(re_, qe))
                            ),
                        })
                    if qid not in result:
                        result.add(qid)
                        changed = True
    return result, edges


def _led_apply_reception(draft: dict, raw_orders: list[dict], meta: dict) -> dict:
    path = "receive_batch"
    # ---- Phase 1: parse every item structurally (all-or-nothing) ----------
    parsed: list[dict] = []
    rejections: list[dict] = []
    for i, raw in enumerate(raw_orders):
        try:
            parsed.append(ledger_parse_exchange_order(raw, f"exchanges[{i}]"))
        except LedgerStructureError as exc:
            rejections.append({"index": i, "code": "invalid_structure", "reason": str(exc)})
    if rejections:
        raise LedgerBatchRejected({
            "report_type": "makeup_ledger_batch_result",
            "status": "rejected",
            "error": "structurally invalid exchange input; batch not accepted",
            "rejections": rejections,
        })

    # ---- Phase 2: in-batch identity / sequence uniqueness -----------------
    ids_seen: dict[str, dict] = {}
    for p in parsed:
        exid = p["exchange_id"]
        if exid in ids_seen and ids_seen[exid]["content_hash"] != p["content_hash"]:
            rejections.append({
                "code": "duplicate_exchange_id_conflicting_content_in_batch",
                "exchange_id": exid,
            })
        ids_seen[exid] = p
    seq_to_id_batch: dict[int, str] = {}
    for p in parsed:
        seq = p["effective_seq"]
        if seq in seq_to_id_batch and seq_to_id_batch[seq] != p["exchange_id"]:
            rejections.append({
                "code": "duplicate_effective_seq_in_batch",
                "effective_seq": seq,
                "exchange_ids": [seq_to_id_batch[seq], p["exchange_id"]],
            })
        seq_to_id_batch[seq] = p["exchange_id"]
    if rejections:
        raise LedgerBatchRejected({
            "report_type": "makeup_ledger_batch_result",
            "status": "rejected",
            "error": "batch identity/sequence rules violated; batch not accepted",
            "rejections": rejections,
        })
    # Identical repetition of the same exchange id inside one batch is one item.
    uniq_parsed: dict[str, dict] = {}
    for p in parsed:
        uniq_parsed.setdefault(p["exchange_id"], p)
    parsed = list(uniq_parsed.values())

    # ---- Phase 3: idempotency / conflicts against the ledger --------------
    duplicate_ids: list[str] = []
    new_contents: list[dict] = []
    for p in parsed:
        exid = p["exchange_id"]
        existing = draft["orders"].get(exid)
        if existing is not None:
            if existing["content_hash"] == p["content_hash"]:
                duplicate_ids.append(exid)
                continue
            rejections.append({
                "code": "exchange_id_reused_with_changed_content",
                "exchange_id": exid,
                "message": "a valid id may only be resent with identical content; changed content is rejected",
            })
            continue
        if str(p["effective_seq"]) in draft["seq_index"]:
            other = draft["seq_index"][str(p["effective_seq"])]
            rejections.append({
                "code": "effective_seq_already_used",
                "effective_seq": p["effective_seq"],
                "exchange_id": exid,
                "existing_exchange_id": other,
            })
        # New line ids must be globally unique across the whole ledger.
        for ln in p["new_lines"]:
            lid = ln["line_id"]
            owner = draft["lines"].get(lid)
            if owner is not None:
                rejections.append({
                    "code": "new_line_id_not_unique",
                    "line_id": lid,
                    "exchange_id": exid,
                    "owned_by_exchange": owner.get("source_exchange_id"),
                })
        new_contents.append(p)

    # New-line collisions inside this batch across distinct orders.
    new_line_owner: dict[str, str] = {}
    for p in new_contents:
        for ln in p["new_lines"]:
            lid = ln["line_id"]
            if lid in new_line_owner and new_line_owner[lid] != p["exchange_id"]:
                rejections.append({
                    "code": "new_line_id_not_unique_in_batch",
                    "line_id": lid,
                    "exchange_ids": [new_line_owner[lid], p["exchange_id"]],
                })
            new_line_owner[lid] = p["exchange_id"]

    # Interval must belong to the opened domain under the strict policy.
    if draft.get("out_of_opening_policy", "reject") == "reject":
        for p in new_contents:
            domain = set(draft.get("domain_days", {}).get(p["scope_id"], []))
            days = {d.isoformat() for d in _led_days(
                date.fromisoformat(p["start_day"]), date.fromisoformat(p["end_day"])
            )}
            outside = sorted(days - domain)
            if outside:
                rejections.append({
                    "code": "exchange_interval_outside_opening",
                    "exchange_id": p["exchange_id"],
                    "scope_id": p["scope_id"],
                    "outside_days": outside,
                    "message": "exchange interval must be a half-open interval inside the opened ledger",
                })

    if rejections:
        raise LedgerBatchRejected({
            "report_type": "makeup_ledger_batch_result",
            "status": "rejected",
            "error": "exchange batch violates ledger identity/uniqueness/opening rules; nothing was posted",
            "rejections": _led_dedupe_rejections(rejections),
            "idempotent_order_ids": duplicate_ids,
        })

    # Whole-batch idempotent replay by batch_id.
    existing_batch_idx = draft["batch_ids"].get(str(meta["batch_id"])) if meta.get("batch_id") else None
    if existing_batch_idx is not None:
        prior_batch = draft["batches"][existing_batch_idx]
        wanted = {p["exchange_id"] for p in parsed}
        if set(prior_batch.get("exchange_ids", [])) == wanted:
            result = _led_deepcopy(prior_batch["view"])
            _led_enrich_state_view(draft, result, prior_batch.get("batch_index"))
            result["status"] = "unchanged"
            result["idempotent_batch_replay"] = True
            return result
        raise LedgerBatchRejected({
            "report_type": "makeup_ledger_batch_result",
            "status": "rejected",
            "error": f"batch_id {meta['batch_id']!r} was already used for a different reception",
            "rejections": [{"code": "batch_id_already_used", "batch_id": meta["batch_id"]}],
        })

    if not new_contents:
        # Pure valid resend(s): no operation, no new batch.
        latest = draft["batches"][-1]
        result = _led_deepcopy(latest["view"])
        _led_enrich_state_view(draft, result, latest.get("batch_index"))
        result["status"] = "unchanged"
        result["idempotent_order_ids"] = duplicate_ids
        result["message"] = "identical content resent for a known exchange id; no operation"
        return result

    # ---- Phase 4: allocate batch identity and insert immutable lines -------
    batch_index = len(draft["batches"])
    batch_id = str(meta["batch_id"] or f"B{batch_index}")
    received_at = meta["received_at"] or datetime.now(timezone.utc).isoformat()

    seed_ids: list[str] = []
    for p in new_contents:
        exid = p["exchange_id"]
        seed_ids.append(exid)
        # Apportion each new line once from the fixed settlement basis.
        for ln in p["new_lines"]:
            s = date.fromisoformat(ln["start_day"])
            e = date.fromisoformat(ln["end_day"])
            days = _led_days(s, e)
            weights = {d.isoformat(): MakeupLedger._basis_weight(draft["basis"], p["scope_id"], d.isoformat()) for d in days}  # noqa: E501
            ln["apportionment"] = ledger_apportion_by_weights(ln["amount_cents"], days, weights)
            draft["lines"][ln["line_id"]] = {
                "line_id": ln["line_id"],
                "kind": "new",
                "scope_id": p["scope_id"],
                "start_day": ln["start_day"],
                "end_day": ln["end_day"],
                "amount_cents": ln["amount_cents"],
                "fee_type": ln.get("fee_type"),
                "source_exchange_id": exid,
                "apportionment": ln["apportionment"],
                "created_batch_id": batch_id,
                "created_batch_index": batch_index,
            }
        draft["orders"][exid] = {
            **{k: v for k, v in p.items()},
            "status": _LED_STATUS_PENDING,  # replaced during re-adjudication
            "received_batch_id": batch_id,
            "received_batch_index": batch_index,
            "received_at": received_at,
            "adjudication": None,
            "ruling_history": [],
        }
        draft["seq_index"][str(p["effective_seq"])] = exid

    # ---- Phase 5: incremental re-adjudication -----------------------------
    affected, propagation_edges = _led_affected_order_ids(draft, seed_ids)
    touched: set[tuple[str, str]] = set()
    for oid in affected:
        o = draft["orders"][oid]
        for d in _led_days(date.fromisoformat(o["start_day"]), date.fromisoformat(o["end_day"])):
            touched.add((o["scope_id"], d.isoformat()))

    before_daily = {
        (sid, day): _led_deepcopy(draft["daily"].get(sid, {}).get(day))
        for sid, day in touched
    }

    # Roll back currently-effective affected orders, descending effective seq.
    existing_affected = [oid for oid in affected if oid not in seed_ids]
    for oid in sorted(existing_affected, key=lambda x: -draft["orders"][x]["effective_seq"]):
        o = draft["orders"][oid]
        if o["status"] == _LED_STATUS_EFFECTIVE:
            _led_rollback_order(draft, o)

    changed_adjudications: list[dict] = []
    reaffirmed_adjudications: list[dict] = []
    new_order_rulings: list[dict] = []
    for oid in sorted(affected, key=lambda x: (draft["orders"][x]["effective_seq"], x)):
        o = draft["orders"][oid]
        old_status = o["status"] if oid not in seed_ids else "not_received"
        old_adj = _led_deepcopy(o.get("adjudication")) if oid not in seed_ids else None
        status, reasons, prior = _led_adjudicate(draft, o, draft["attribution"])
        new_adj = {
            "status": status,
            "reasons": reasons,
            "prior_line_by_day": prior,
            "adjudicated_in_batch": batch_id,
            "adjudicated_at": received_at,
        }
        ruling_changed = old_status != status
        detail_changed = (
            old_adj is not None
            and _led_canonical_ruling(old_adj) != _led_canonical_ruling(new_adj)
        )
        o["status"] = status
        o["adjudication"] = new_adj
        o.setdefault("ruling_history", []).append({
            "batch_id": batch_id,
            "batch_index": batch_index,
            "received_at": received_at,
            "status": status,
            "reasons": _led_deepcopy(reasons),
        })
        if status == _LED_STATUS_EFFECTIVE:
            _led_apply_order_to_maps(draft, o, draft["attribution"], draft["daily"])
        if oid in seed_ids:
            new_order_rulings.append({
                "exchange_id": oid,
                "effective_seq": o["effective_seq"],
                "scope_id": o["scope_id"],
                "status": status,
                "effective": status == _LED_STATUS_EFFECTIVE,
                "status_label": _LED_STATUS_LABELS[status],
                "reasons": reasons,
            })
        elif ruling_changed:
            changed_adjudications.append({
                "exchange_id": oid,
                "effective_seq": o["effective_seq"],
                "scope_id": o["scope_id"],
                "interval": [o["start_day"], o["end_day"]],
                "before_status": old_status,
                "after_status": status,
                "ruling_changed": True,
                "detail_changed": detail_changed,
                "status_flipped": True,
                "before_status_label": _LED_STATUS_LABELS.get(old_status, old_status),
                "after_status_label": _LED_STATUS_LABELS[status],
                "previous_reasons": (old_adj or {}).get("reasons", []),
                "reasons": reasons,
                "reason": _led_primary_reason(reasons, ruling_changed, old_status, status),
                "old_line_ids": list(o["old_line_ids"]),
                "new_line_ids": [ln["line_id"] for ln in o["new_lines"]],
                "old_line_records": _led_line_refs(draft, o["old_line_ids"]),
                "new_line_records": _led_deepcopy(o["new_lines"]),
            })
        elif detail_changed:
            # Re-adjudicated through overlap with unchanged ruling; the
            # matched day/line evidence moved (audit-only, posts no delta).
            reaffirmed_adjudications.append({
                "exchange_id": oid,
                "effective_seq": o["effective_seq"],
                "scope_id": o["scope_id"],
                "interval": [o["start_day"], o["end_day"]],
                "status": status,
                "ruling_changed": False,
                "detail_changed": True,
                "status_label": _LED_STATUS_LABELS[status],
                "reasons": reasons,
            })

    # ---- Phase 6: postings = current diff - pre-reception diff -----------
    postings: list[dict] = []
    daily_changes: list[dict] = []
    for sid, day in sorted(touched):
        before = before_daily.get((sid, day))
        after = draft["daily"].get(sid, {}).get(day)
        before_cents = (before or {}).get("apportioned_amount_cents")
        after_cents = (after or {}).get("apportioned_amount_cents")
        before_line = (before or {}).get("line_id")
        after_line = (after or {}).get("line_id")
        if before_cents == after_cents and before_line == after_line:
            continue
        sys_cents = int(draft["basis"][sid][day]["system_amount_cents_rounded"])
        b_app = before_cents if before_cents is not None else 0
        a_app = after_cents if after_cents is not None else 0
        b_diff = b_app - sys_cents
        a_diff = a_app - sys_cents
        delta = a_diff - b_diff
        entry = {
            "scope_id": sid,
            "billing_day": day,
            "before_line_id": before_line,
            "after_line_id": after_line,
            "old_line_id": before_line,   # vocabulary alias
            "new_line_id": after_line,     # vocabulary alias
            "before_apportioned_amount_cents": b_app,
            "after_apportioned_amount_cents": a_app,
            "system_amount_cents": sys_cents,
            "before_daily_difference_cents": b_diff,
            "after_daily_difference_cents": a_diff,
            "delta_cents": delta,
            "makeup_delta_cents": delta,   # vocabulary alias
            "entry_type": (
                "supplementary" if delta > 0 else ("reversal" if delta < 0 else "unchanged")
            ),
            "entry_type_label": (
                "补记" if delta > 0 else ("冲回" if delta < 0 else "无差额")
            ),
        }
        daily_changes.append(entry)
        if delta != 0:
            postings.append(entry)

    # ---- Phase 7: freeze the finance view as this batch's snapshot --------
    # Build a transient ledger wrapper to reuse the view constructor.
    wrapper = MakeupLedger.__new__(MakeupLedger)
    wrapper._state = draft
    wrapper.store_path = None
    view = wrapper._build_view(
        batch_id=batch_id,
        batch_index=batch_index,
        received_at=received_at,
        postings=postings,
        daily_changes=daily_changes,
        changed_adjudications=changed_adjudications,
        reaffirmed_adjudications=reaffirmed_adjudications,
        new_order_rulings=new_order_rulings,
        considered_order_ids=sorted(affected),
        trigger_order_ids=seed_ids,
        propagation_edges=propagation_edges,
    )
    exchange_ids = [p["exchange_id"] for p in parsed]
    batch_record = {
        "batch_id": batch_id,
        "batch_index": batch_index,
        "received_at": received_at,
        "kind": "reception",
        "exchange_ids": exchange_ids,
        "new_order_ids": seed_ids,
        "idempotent_order_ids": duplicate_ids,
        "status": "accepted",
        "affected_order_ids": sorted(affected),
        "propagation_edges": propagation_edges,
        "changed_adjudication_ids": [c["exchange_id"] for c in changed_adjudications],
        "reaffirmed_adjudication_ids": [c["exchange_id"] for c in reaffirmed_adjudications],
        "totals": view["totals"],
        "view": view,
    }
    draft["batches"].append(batch_record)
    draft["batch_ids"][batch_id] = batch_index
    draft["current_batch_id"] = batch_id

    result = _led_enrich_state_view(draft, _led_deepcopy(view), batch_index)
    result.update({
        "report_type": "makeup_ledger_batch_result",
        "status": "accepted",
        "batch_id": batch_id,
        "batch_index": batch_index,
        "received_order_ids": exchange_ids,
        "new_order_ids": seed_ids,
        "idempotent_order_ids": duplicate_ids,
        "affected_order_ids": sorted(affected),
        "propagation_edges": propagation_edges,
        "changed_adjudication_ids": [c["exchange_id"] for c in changed_adjudications],
        "reaffirmed_adjudication_ids": [c["exchange_id"] for c in reaffirmed_adjudications],
    })
    # The snapshot is the same complete finance view (independent copy).
    result["snapshot"] = _led_deepcopy(result)
    return result


def _led_canonical_ruling(adj: Optional[dict]) -> str:
    if not adj:
        return ""
    return json.dumps(
        {"status": adj.get("status"), "reasons": adj.get("reasons"), "prior": adj.get("prior_line_by_day")},
        sort_keys=True, ensure_ascii=False,
    )


def _led_primary_reason(reasons: list[dict], changed: bool, old: str, new: str) -> dict:
    if changed and old != "not_received":
        head = {
            "code": "ruling_changed_from_" + old + "_to_" + new,
            "message": f"re-adjudication changed ruling {old} -> {new} due to this reception",
        }
        if reasons:
            head["detail"] = reasons[0]
        return head
    return reasons[0] if reasons else {"code": "all_preconditions_satisfied"}


def _led_line_refs(draft: dict, ids: list[str]) -> list[dict]:
    out = []
    for lid in ids:
        rec = draft["lines"].get(lid)
        if rec is None:
            out.append({"line_id": lid, "in_ledger": False, "note": "old line has not been received yet"})
        else:
            out.append({"line_id": lid, "in_ledger": True, **_led_deepcopy(rec)})
    return out


def _led_dedupe_rejections(items: list[dict]) -> list[dict]:
    seen = set()
    out = []
    for it in items:
        key = json.dumps(it, sort_keys=True, ensure_ascii=False, default=str)
        if key not in seen:
            seen.add(key)
            out.append(it)
    return out


# ---------------------------------------------------------------------------
# Module-level functional API and naming aliases
# ---------------------------------------------------------------------------

def open_makeup_ledger(
    settlement: Any = None,
    original_bills: Any = None,
    *,
    store_path: Optional[Path | str] = None,
    **kwargs: Any,
) -> MakeupLedger:
    """Open the makeup ledger from fixed settlement input + original bills.

    Raises :class:`LedgerOpeningRejected` (carries ``.report``) when the
    baseline has unattributable blocking evidence or bill days are not
    openable; raises :class:`LedgerStructureError` for malformed documents.
    """
    # Tolerate common calling-convention aliases for the settlement artifact.
    if settlement is None:
        for key in ("settlement_input", "root", "baseline", "input", "settlement_root"):
            if key in kwargs and kwargs[key] is not None:
                settlement = kwargs.pop(key)
                break
    if kwargs.get("settlement_report") is None:
        for key in ("report", "settlement_result"):
            if key in kwargs and kwargs[key] is not None:
                kwargs["settlement_report"] = kwargs.pop(key)
                break
    if original_bills is None and kwargs.get("bills") is not None:
        original_bills = kwargs["bills"]
    if kwargs.get("system_amounts") is None:
        for key in (
            "system_daily_amounts", "daily_system_amounts", "basis_amounts",
            "amounts_by_day", "system_cents", "daily_amounts",
        ):
            if kwargs.get(key) is not None:
                kwargs["system_amounts"] = kwargs.pop(key)
                break
    return MakeupLedger.open(
        settlement=settlement,
        original_bills=original_bills,
        store_path=store_path,
        persist=store_path is not None,
        **kwargs,
    )


def try_open_makeup_ledger(*args: Any, **kwargs: Any) -> dict:
    """Non-raising opening: ``{"status":"opened","ledger":...}`` or rejected report."""
    try:
        ledger = open_makeup_ledger(*args, **kwargs)
        return {"status": "opened", "ledger": ledger, "opening": ledger.opening_report}
    except LedgerOpeningRejected as exc:
        return _led_deepcopy(exc.report)
    except (LedgerStructureError, InputError) as exc:
        return {
            "report_type": "makeup_ledger_opening",
            "status": "invalid_structure",
            "error": str(exc),
            "error_type": type(exc).__name__,
        }


def receive_exchange_batch(
    ledger: Any,
    batch: Any = None,
    *,
    store_path: Optional[Path | str] = None,
    **kwargs: Any,
) -> dict:
    """Receive one actual batch (one or many exchange orders) atomically.

    ``ledger`` may be a :class:`MakeupLedger`, a state dict, or a store path.
    """
    if isinstance(ledger, (str, Path)):
        ledger = MakeupLedger.load(ledger)
    elif isinstance(ledger, dict) and ledger.get("report_type") == "makeup_ledger_state":
        ledger = MakeupLedger(ledger)
    elif hasattr(ledger, "receive_batch") and hasattr(ledger, "state"):
        # Duck-typed ledger (e.g. loaded through a separately imported module).
        if store_path is not None and hasattr(ledger, "store_path"):
            ledger.store_path = Path(store_path)
        return ledger.receive_batch(batch, **kwargs)
    if not isinstance(ledger, MakeupLedger):
        raise LedgerStructureError("ledger must be a MakeupLedger, state dict, or store path")
    if store_path is not None:
        ledger.store_path = Path(store_path)
    return ledger.receive_batch(batch, **kwargs)


def try_receive_exchange_batch(ledger: Any, batch: Any = None, **kwargs: Any) -> dict:
    """Non-raising reception: rejected batches come back as status='rejected'."""
    try:
        return receive_exchange_batch(ledger, batch, **kwargs)
    except LedgerBatchRejected as exc:
        return _led_deepcopy(exc.report)
    except LedgerStructureError as exc:
        return {
            "report_type": "makeup_ledger_batch_result",
            "status": "invalid_structure",
            "error": str(exc),
        }


def load_makeup_ledger(path: Path | str) -> MakeupLedger:
    return MakeupLedger.load(path)


def reopen_makeup_ledger(path: Path | str) -> MakeupLedger:
    return MakeupLedger.load(path)


def run_makeup_ledger_workflow(
    *,
    settlement: Any = None,
    settlement_report: Any = None,
    system_amounts: Any = None,
    original_bills: Any = None,
    batches: Optional[list[Any]] = None,
    store_path: Optional[Path | str] = None,
    ledger_id: Optional[str] = None,
) -> dict:
    """Convenience end-to-end driver: open then receive every batch in order.

    Returns ``{"ledger": <MakeupLedger>, "opening": ..., "batch_results": [...]}``.
    Each item of ``batches`` may be a list of orders or a reception document.
    """
    ledger = open_makeup_ledger(
        settlement=settlement,
        settlement_report=settlement_report,
        system_amounts=system_amounts,
        original_bills=original_bills,
        ledger_id=ledger_id,
        store_path=store_path,
    )
    results: list[dict] = []
    for i, batch in enumerate(batches or []):
        if isinstance(batch, dict) and "batch" in batch and isinstance(batch["batch"], (dict, list)):
            doc = batch["batch"]
            kw = {k: v for k, v in batch.items() if k != "batch"}
            if isinstance(doc, list):
                results.append(ledger.receive_batch(doc, **kw))
            else:
                merged = {**kw, **doc}
                results.append(ledger.receive_batch(merged))
        else:
            results.append(ledger.receive_batch(batch, batch_id=f"B{i + 1}" if isinstance(batch, list) else None))
    if store_path is not None:
        ledger.save(store_path)
    return {"ledger": ledger, "opening": ledger.opening_report, "batch_results": results}


def run_ledger_scenario(spec: dict) -> dict:
    """One JSON-able call: open from ``spec['opening']`` and run ``spec['batches']``.

    Unlike :func:`run_makeup_ledger_workflow`, the return value contains no
    ledger object and is directly JSON serializable::

        {"status": "opened"|"rejected", "opening": ...,
         "batch_results": [ ... ], "final_view": ...}

    A rejected opening returns the structured rejection report.  A rejected
    batch is captured as its report and processing stops (the ledger keeps the
    state before that batch).
    """
    if not isinstance(spec, dict):
        raise LedgerStructureError("scenario spec must be an object")
    opening = dict(spec.get("opening") or {})
    store_path = spec.get("store_path", opening.pop("store_path", None))
    try:
        ledger = open_makeup_ledger(store_path=store_path, **opening)
    except LedgerOpeningRejected as exc:
        return _led_deepcopy(exc.report)
    except InputError as exc:
        return {
            "report_type": "makeup_ledger_opening",
            "status": "invalid_structure",
            "error": str(exc),
            "error_type": type(exc).__name__,
        }
    results: list[dict] = []
    for i, batch in enumerate(spec.get("batches") or []):
        try:
            results.append(ledger.receive_batch(batch))
        except LedgerBatchRejected as exc:
            results.append(_led_deepcopy(exc.report))
            break
        except InputError as exc:
            results.append({
                "report_type": "makeup_ledger_batch_result",
                "status": "invalid_structure",
                "error": str(exc),
                "error_type": type(exc).__name__,
            })
            break
    if store_path is not None:
        ledger.save(store_path)
    return {
        "report_type": "makeup_ledger_scenario",
        "status": "completed",
        "opening": ledger.opening_report,
        "batch_results": results,
        "final_view": ledger.view(),
        "self_check": ledger.verify_incremental_matches_full_refold(),
    }


def write_ledger_postings_csv(ledger: MakeupLedger | dict, path: Path | str) -> Path:
    """Write one flat row per nonzero 补记/冲回 posting across all batches."""
    import csv
    if isinstance(ledger, dict):
        ledger = MakeupLedger(ledger)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    rows = ledger.postings_history()
    fields = [
        "batch_id", "batch_index", "scope_id", "billing_day", "entry_type",
        "entry_type_label", "delta_cents", "before_line_id", "after_line_id",
        "before_apportioned_amount_cents", "after_apportioned_amount_cents",
        "system_amount_cents", "before_daily_difference_cents",
        "after_daily_difference_cents",
    ]
    with target.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return target


# Naming aliases for the same capability (different vocabularies call 补差
# "makeup", "true-up", "difference" or "adjustment" ledger).
MakeupDifferenceLedger = MakeupLedger
MakeupDiffLedger = MakeupLedger
MakeupBook = MakeupLedger
TrueUpLedger = MakeupLedger
TrueupLedger = MakeupLedger
TrueUpBook = MakeupLedger
DifferenceLedger = MakeupLedger
DifferenceBook = MakeupLedger
AdjustmentLedger = MakeupLedger
AdjustmentBook = MakeupLedger
SupplementaryLedger = MakeupLedger
Ledger = MakeupLedger

create_makeup_ledger = open_makeup_ledger
build_makeup_ledger = open_makeup_ledger
new_makeup_ledger = open_makeup_ledger
makeup_ledger = open_makeup_ledger
makeup_difference_ledger = open_makeup_ledger
true_up_ledger = open_makeup_ledger
trueup_ledger = open_makeup_ledger
difference_ledger = open_makeup_ledger
open_ledger = open_makeup_ledger
create_ledger = open_makeup_ledger
open_true_up_ledger = open_makeup_ledger
open_difference_ledger = open_makeup_ledger
load_ledger = load_makeup_ledger
reopen_ledger = reopen_makeup_ledger
receive_batch = receive_exchange_batch
receive_makeup_batch = receive_exchange_batch
accept_exchange_batch = receive_exchange_batch
intake_exchange_batch = receive_exchange_batch
account_exchange_orders = run_makeup_ledger_workflow
process_exchange_batches = run_makeup_ledger_workflow
settle_makeup_differences = run_makeup_ledger_workflow
simulate_makeup_ledger = run_ledger_scenario
ledger_scenario = run_ledger_scenario

for _alias in (
    "accept_batch", "intake_batch", "post_batch", "receive_exchanges",
    "add_exchange_orders", "process_batch", "receive_exchange_order",
    "receive_orders", "add_batch", "post", "receive_batches",
):
    if not hasattr(MakeupLedger, _alias):
        setattr(MakeupLedger, _alias, MakeupLedger.receive_batch)
for _alias in ("get_snapshot", "snapshot_at", "as_of", "query_view", "state_at", "view_batch_at"):
    if not hasattr(MakeupLedger, _alias):
        setattr(MakeupLedger, _alias, MakeupLedger.view)
for _alias in ("fetch_batch", "batch_view", "get_reception"):
    if not hasattr(MakeupLedger, _alias):
        setattr(MakeupLedger, _alias, MakeupLedger.get_batch)


# ---------------------------------------------------------------------------
# JSON document / CLI support
# ---------------------------------------------------------------------------

def _led_load_json_maybe_file(value: Any, base_dir: Path) -> Any:
    if isinstance(value, str) and value.endswith(".json"):
        p = Path(value)
        if not p.is_absolute():
            p = base_dir / p
        return json.loads(p.read_text(encoding="utf-8"))
    return value


def ledger_opening_kwargs_from_doc(doc: Any, base_dir: Optional[Path] = None) -> dict:
    """Normalize an opening JSON document into :meth:`MakeupLedger.open` kwargs."""
    base_dir = base_dir or Path.cwd()
    if not isinstance(doc, dict):
        raise LedgerStructureError("opening document must be a JSON object")
    kwargs: dict[str, Any] = {
        "ledger_id": doc.get("ledger_id") or doc.get("id"),
        "money_scale": int(doc.get("money_scale", 100)),
        "out_of_opening_policy": doc.get("out_of_opening_policy", "reject"),
        "opening_batch_id": doc.get("opening_batch_id", _LEDGER_OPENING_BATCH),
        "created_at": doc.get("created_at") or doc.get("opened_at"),
    }
    bills = _led_pick(doc, (
        "original_bills", "original_bill_lines", "bill_lines", "bills", "lines", "原账单",
    ))
    kwargs["original_bills"] = bills

    report = doc.get("settlement_report")
    report_path = doc.get("settlement_report_path") or doc.get("report_path")
    if report_path:
        report = json.loads(_led_resolve(base_dir, report_path).read_text(encoding="utf-8"))
    if report is not None:
        kwargs["settlement_report"] = _led_load_json_maybe_file(report, base_dir) if isinstance(report, str) else report

    settlement = doc.get("settlement_input") or doc.get("settlement")
    input_path = (
        doc.get("settlement_input_path") or doc.get("input_path")
        or doc.get("baseline") or doc.get("baseline_input")
    )
    if input_path:
        p = _led_resolve(base_dir, input_path)
        loaded = json.loads(p.read_text(encoding="utf-8"))
        if _led_looks_like_report(loaded):
            kwargs["settlement_report"] = loaded
        else:
            kwargs["settlement"] = loaded
    elif settlement is not None:
        kwargs["settlement"] = json.loads(Path(settlement).read_text(encoding="utf-8")) if isinstance(settlement, str) and _looks_like_path(settlement) else settlement  # noqa: E501
    elif any(k in doc for k in ("readings", "control_totals", "tariff_versions")):
        # The opening document itself may be the settlement input root.
        kwargs["settlement"] = doc

    if doc.get("system_amounts") is not None:
        kwargs["system_amounts"] = doc["system_amounts"]
    return {k: v for k, v in kwargs.items() if v is not None}


def _looks_like_path(text: str) -> bool:
    return text.endswith(".json") or "/" in text or "\\" in text


def _led_resolve(base_dir: Path, maybe: Any) -> Path:
    p = Path(str(maybe))
    return p if p.is_absolute() else (base_dir / p)


def ledger_cli(args: argparse.Namespace) -> int:
    store = args.ledger_store
    out_target = getattr(args, "ledger_output", None) or args.output

    def emit(payload: Any, code: int = 0) -> int:
        text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
        if out_target:
            _write_text_atomic(Path(out_target), text)
        else:
            sys.stdout.write(text + "\n")
        return code

    try:
        if args.ledger_open:
            doc_path = Path(args.ledger_open)
            doc = json.loads(doc_path.read_text(encoding="utf-8"))
            kwargs = ledger_opening_kwargs_from_doc(doc, doc_path.parent.resolve())
            if store:
                kwargs["store_path"] = store
            try:
                ledger = open_makeup_ledger(**kwargs)
            except LedgerOpeningRejected as exc:
                return emit(exc.report, 3)
            result = ledger.opening_report
            result["opening_view"] = ledger.view(_LEDGER_OPENING_BATCH)
            if store:
                result["store_path"] = str(ledger.store_path)
            return emit(result, 0)

        if args.ledger_receive:
            if not store:
                raise LedgerStructureError("--ledger-receive requires --ledger-store")
            ledger = MakeupLedger.load(store)
            doc_path = Path(args.ledger_receive)
            payload = json.loads(doc_path.read_text(encoding="utf-8"))
            result = ledger.receive_batch(payload)
            result["store_path"] = str(ledger.store_path)
            if args.ledger_postings_csv:
                csv_path = write_ledger_postings_csv(ledger, args.ledger_postings_csv)
                result["postings_csv"] = str(csv_path)
            return emit(result, 0)

        if args.ledger_postings_csv and store and not (args.ledger_view is not None or args.ledger_batch is not None):
            ledger = MakeupLedger.load(store)
            csv_path = write_ledger_postings_csv(ledger, args.ledger_postings_csv)
            return emit({"report_type": "makeup_ledger_postings_csv", "csv_path": str(csv_path),
                         "rows": len(ledger.postings_history())}, 0)

        if args.ledger_history:
            if not store:
                raise LedgerStructureError("--ledger-history requires --ledger-store")
            ledger = MakeupLedger.load(store)
            return emit({
                "report_type": "makeup_ledger_history",
                "ledger_id": ledger.state["ledger_id"],
                "batches": ledger.list_batches(),
            }, 0)

        if args.ledger_view is not None or args.ledger_batch is not None:
            if not store:
                raise LedgerStructureError("--ledger-view requires --ledger-store")
            ledger = MakeupLedger.load(store)
            batch = args.ledger_batch
            if args.ledger_view and args.ledger_view != "__latest__":
                batch = args.ledger_view
            view = ledger.view(batch)
            return emit(view, 0)

        raise LedgerStructureError("no ledger command requested")
    except LedgerBatchRejected as exc:
        return emit(exc.report, 2)
    except LedgerOpeningRejected as exc:
        return emit(exc.report, 3)
    except InputError as exc:
        err = {"report_type": "makeup_ledger_cli_error", "status": "fatal_input_error", "error": str(exc),
               "error_type": type(exc).__name__}
        if out_target:
            _write_text_atomic(Path(out_target), json.dumps(err, ensure_ascii=False, indent=2))
        else:
            sys.stderr.write(json.dumps(err, ensure_ascii=False, indent=2) + "\n")
        return 2


LEDGER_CLI_FLAGS_ADDED = True


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
    parser.add_argument(
        "--ledger-open", type=Path,
        help="Open a makeup-difference ledger from this opening JSON document",
    )
    parser.add_argument(
        "--ledger-receive", type=Path,
        help="Receive one exchange-order batch JSON document into --ledger-store",
    )
    parser.add_argument(
        "--ledger-store", type=Path,
        help="Makeup ledger state file (or directory containing makeup_ledger_state.json)",
    )
    parser.add_argument(
        "--ledger-output", type=Path,
        help="Write the ledger command JSON result here (defaults to --output / stdout)",
    )
    parser.add_argument(
        "--ledger-view", nargs="?", const="__latest__", default=None,
        help="Print the frozen finance view of a batch id (optional value; default latest)",
    )
    parser.add_argument(
        "--ledger-batch", default=None,
        help="Batch id/index for --ledger-view",
    )
    parser.add_argument(
        "--ledger-history", action="store_true",
        help="List retained reception batches in --ledger-store",
    )
    parser.add_argument(
        "--ledger-postings-csv", type=Path,
        help="Write one row per 补记/冲回 posting (all retained batches) to this CSV",
    )
    args = parser.parse_args(argv)

    wants_ledger = bool(
        args.ledger_open or args.ledger_receive or args.ledger_view is not None
        or args.ledger_history or args.ledger_batch is not None
        or args.ledger_postings_csv
    )
    if wants_ledger:
        return ledger_cli(args)

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
