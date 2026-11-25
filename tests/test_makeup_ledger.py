"""Tests for the makeup-difference ledger (补差账本).

Covers:
  * exact largest-remainder cent apportionment and 四舍五入 system amounts;
  * opening settlement-basis gates (verified cells/segments, global blockers);
  * effective-sequence adjudication: effective / pending_verification;
  * late orders flipping later rulings both directions;
  * overlap-transitive incremental re-adjudication vs full from-opening refold;
  * idempotent resend / changed-content rejection / unique line & sequence;
  * atomic batches and telescoping 补记/冲回 postings;
  * retained per-batch finance snapshots and reopen queries;
  * end-to-end CLI.

Run with:
    python3 -I -m unittest discover -s tests -v
"""

from __future__ import annotations

import importlib.util
import json
import random
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = ROOT / "meter_allocator.py"


def load_module():
    name = "meter_allocator_ledger_under_test"
    spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


MOD = load_module()

DAYS = [f"2025-02-{i:02d}" for i in range(1, 9)]  # 8 days
SCOPE = "S1"


def amounts(cents=100, days=DAYS):
    return {SCOPE: {d: cents for d in days}}


def line(lid, start, end, amount, scope=SCOPE, **extra):
    rec = {"line_id": lid, "scope_id": scope, "day_from": start, "day_to": end,
           "amount_cents": amount}
    rec.update(extra)
    return rec


def order(oid, seq, start, end, olds, news, scope=SCOPE, **extra):
    rec = {"exchange_id": oid, "effective_seq": seq, "scope_id": scope,
           "day_from": start, "day_to": end, "old_line_ids": olds, "new_lines": news}
    rec.update(extra)
    return rec


def one_new_line(oid, start, end, amount, scope=SCOPE):
    return [line(f"N-{oid}", start, end, amount, scope=scope)]


def open_ledger(bills=None, system=None, **kw):
    if bills is None:
        bills = [line("L1", DAYS[0], _plus(DAYS[-1], 1), 800)]
    return MOD.open_makeup_ledger(system_amounts=system or amounts(),
                                  original_bills=bills, ledger_id="T", **kw)


def _plus(iso_day, n):
    d = date.fromisoformat(iso_day)
    from datetime import timedelta
    return (d + timedelta(days=n)).isoformat()


class ApportionmentTests(unittest.TestCase):
    def app(self, total, weights):
        days = [date(2025, 1, i) for i in range(1, len(weights) + 1)]
        w = {d.isoformat(): x for d, x in zip(days, weights)}
        return MOD.ledger_apportion_by_weights(total, days, w)

    def test_floor_then_largest_remainder(self):
        r = self.app(100, [1, 1, 1])
        self.assertEqual([r[f"2025-01-0{i}"] for i in (1, 2, 3)], [34, 33, 33])
        self.assertEqual(sum(r.values()), 100)

    def test_largest_fraction_wins(self):
        r = self.app(100, [333, 333, 334])
        self.assertEqual([r[f"2025-01-0{i}"] for i in (1, 2, 3)], [33, 33, 34])

    def test_equal_remainder_tie_goes_earlier(self):
        # 2 units over 4 equal-weight days => quota 0.5 each; two extra cents
        # go to the two earliest days.
        r = self.app(2, [5, 5, 5, 5])
        self.assertEqual([r[f"2025-01-0{i}"] for i in (1, 2, 3, 4)], [1, 1, 0, 0])

    def test_all_zero_weights_equal(self):
        r = self.app(10, [0, 0])
        self.assertEqual(r, {"2025-01-01": 5, "2025-01-02": 5})

    def test_conservation_for_many_totals(self):
        rng = random.Random(7)
        for n in range(1, 12):
            weights = [rng.randint(0, 50) for _ in range(n)]
            for total in (0, 1, 7, 99, 1000, 12345):
                r = self.app(total, weights)
                self.assertEqual(sum(r.values()), total)

    def test_round_half_up(self):
        F = MOD.Fraction
        self.assertEqual(MOD._led_round_half_up_cents(F("66.5")), 67)
        self.assertEqual(MOD._led_round_half_up_cents(F("66.499")), 66)
        self.assertEqual(MOD._led_round_half_up_cents(F("33.5")), 34)
        self.assertEqual(MOD._led_round_half_up_cents(F("100")), 100)


class OpeningTests(unittest.TestCase):
    def test_opening_apportions_original_line(self):
        led = open_ledger()
        app = led.state["lines"]["L1"]["apportionment"]
        self.assertEqual(sum(app.values()), 800)
        self.assertTrue(all(v == 100 for v in app.values()))
        view = led.view()
        self.assertEqual(len(view["daily_ledger"]), 8)
        self.assertTrue(all(r["daily_difference_cents"] == 0 for r in view["daily_ledger"]))

    def test_duplicate_original_line_ids_rejected(self):
        with self.assertRaises(MOD.LedgerStructureError):
            open_ledger(bills=[
                line("L1", DAYS[0], DAYS[2], 200),
                line("L1", DAYS[2], DAYS[4], 200),
            ])

    def test_overlapping_original_lines_rejected(self):
        out = MOD.try_open_makeup_ledger(
            system_amounts=amounts(),
            original_bills=[
                line("L1", DAYS[0], DAYS[3], 300),
                line("L2", DAYS[2], DAYS[4], 200),
            ],
        )
        self.assertEqual(out["status"], "rejected")
        self.assertIn("original_bill_lines_overlap",
                      [r["code"] for r in out["rejections"]])

    def test_negative_amount_rejected(self):
        with self.assertRaises(MOD.LedgerStructureError):
            open_ledger(bills=[line("L1", DAYS[0], DAYS[1], -5)])

    def test_unattributable_global_blocker_rejects_opening(self):
        report = {
            "overall_status": "input_error", "slices": [], "components": [],
            "verification": {
                "control_checks": [], "segment_checks": [],
                "global_issues": [{"code": "mystery_blocking_evidence"}],
            },
        }
        out = MOD.try_open_makeup_ledger(
            settlement_report=report,
            original_bills=[line("L1", DAYS[0], DAYS[1], 10)],
        )
        self.assertEqual(out["status"], "rejected")
        self.assertEqual(out["rejections"][0]["code"],
                         "unattributable_settlement_blocking_evidence")

    def test_non_openable_day_rejects_opening(self):
        report = {
            "overall_status": "settled", "slices": [],
            "components": [{
                "component_id": "C1", "status": "settled",
                "control_keys": [{"scope_id": SCOPE, "billing_day": DAYS[0], "rate_id": "r"}],
                "segment_ids": ["SEG1"],
            }],
            "verification": {
                "control_checks": [{
                    "control": {"scope_id": SCOPE, "billing_day": DAYS[0], "rate_id": "r"},
                    "component_id": "C1", "component_status": "settled",
                    "status": "verified", "sum_matches_control": True,
                }],
                "segment_checks": [{
                    "segment_id": "SEG1", "status": "verified",
                }],
                "global_issues": [],
            },
        }
        # Only DAYS[0] has a cell; bill covers DAYS[0],DAYS[1].
        out = MOD.try_open_makeup_ledger(
            settlement_report=report,
            system_amounts={SCOPE: {DAYS[0]: 100}},
            original_bills=[line("L1", DAYS[0], DAYS[2], 200)],
        )
        self.assertEqual(out["status"], "rejected")
        bad_line = next(r for r in out["rejections"]
                        if r["code"] == "bill_line_covers_non_openable_days")
        bad_days = {b["billing_day"] for b in bad_line["bad_days"]}
        self.assertIn(DAYS[1], bad_days)
        self.assertNotIn(DAYS[0], bad_days)

    def test_feasible_crossday_rounds_system_amount_to_cents(self):
        root = json.loads((ROOT / "examples" / "feasible_crossday_input.json").read_text())
        led = MOD.open_makeup_ledger(
            settlement=root,
            original_bills=[line("L1", "2025-01-01", "2025-01-03", 100, scope="S1")],
        )
        b = led.state["basis"]["S1"]
        self.assertEqual(b["2025-01-01"]["system_amount_exact_cents_fraction"], "200/3")
        self.assertEqual(b["2025-01-01"]["system_amount_cents_rounded"], 67)
        self.assertEqual(b["2025-01-02"]["system_amount_cents_rounded"], 33)
        self.assertEqual(led.state["lines"]["L1"]["apportionment"],
                         {"2025-01-01": 67, "2025-01-02": 33})

    def test_park_report_real_basis_opens_on_verified_scope_day(self):
        report = json.loads((ROOT / "examples" / "park_result.json").read_text())
        led = MOD.open_makeup_ledger(
            settlement_report=report,
            original_bills=[line("PX", "2024-11-04", "2024-11-05", 123, scope="S-XDAY")],
        )
        row = next(r for r in led.view()["daily_ledger"] if r["billing_day"] == "2024-11-04")
        self.assertTrue(row["basis"]["control_keys"])
        self.assertTrue(row["basis"]["segment_ids"])
        self.assertEqual(sum(led.state["lines"]["PX"]["apportionment"].values()), 123)


class AdjudicationTests(unittest.TestCase):
    def test_effective_order_replaces_interval_only(self):
        led = open_ledger()
        o = order("E1", 1, DAYS[2], DAYS[5], ["L1"],
                  [line("N1", DAYS[2], DAYS[5], 300)])
        res = led.receive_batch([o], batch_id="B1")
        self.assertEqual(led.get_order("E1")["status"], "effective")
        posts = {p["billing_day"]: p["delta_cents"] for p in res["postings"]}
        # 3 equal-weight days, 300 -> 100 each, same as before => no delta.
        self.assertEqual(posts, {})
        attr = led.state["attribution"][SCOPE]
        self.assertEqual(attr[DAYS[1]], "L1")
        self.assertEqual(attr[DAYS[2]], "N1")
        self.assertEqual(attr[DAYS[4]], "N1")
        self.assertEqual(attr[DAYS[5]], "L1")

    def test_wrong_old_line_makes_whole_order_pending(self):
        led = open_ledger()
        o = order("E1", 1, DAYS[0], DAYS[2], ["GHOST"],
                  one_new_line("E1", DAYS[0], DAYS[2], 200))
        res = led.receive_batch([o], batch_id="B1")
        self.assertEqual(led.get_order("E1")["status"], "pending_verification")
        self.assertEqual(res["postings"], [])
        # No daily change at all.
        self.assertTrue(all(r["line_id"] == "L1" for r in led.view()["daily_ledger"]))
        ruling = led.get_order("E1")["adjudication"]
        codes = {r["code"] for r in ruling["reasons"]}
        self.assertIn("day_not_attributed_to_listed_old_line", codes)
        self.assertIn("old_line_has_no_hit_day", codes)

    def test_each_listed_old_line_must_hit_a_day(self):
        led = open_ledger()
        # First replace days 0..1 with A.
        led.receive_batch([order("A", 1, DAYS[0], DAYS[2], ["L1"],
                                 one_new_line("A", DAYS[0], DAYS[2], 200))])
        # E2 lists L1 and A over days 0..2: day0,1 are A; day2 is L1 -> both hit => effective.
        e2 = order("E2", 2, DAYS[0], DAYS[3], ["L1", "N-A"],
                   one_new_line("E2", DAYS[0], DAYS[3], 300))
        led.receive_batch([e2])
        self.assertEqual(led.get_order("E2")["status"], "effective")
        # E3 lists L1 only over days 0..2 but those are now N-E2 => pending.
        e3 = order("E3", 3, DAYS[0], DAYS[2], ["L1"],
                   one_new_line("E3", DAYS[0], DAYS[2], 200))
        led.receive_batch([e3])
        self.assertEqual(led.get_order("E3")["status"], "pending_verification")

    def test_unknown_old_line_can_arrive_later_and_activate(self):
        led = open_ledger()
        high = order("H", 10, DAYS[1], DAYS[3], ["N-LOW"],
                     one_new_line("H", DAYS[1], DAYS[3], 260))
        r1 = led.receive_batch([high], batch_id="R1")
        self.assertEqual(led.get_order("H")["status"], "pending_verification")
        self.assertEqual(r1["postings"], [])
        low = order("LOW", 5, DAYS[1], DAYS[3], ["L1"],
                    one_new_line("LOW", DAYS[1], DAYS[3], 220))
        r2 = led.receive_batch([low], batch_id="R2")
        self.assertEqual(led.get_order("LOW")["status"], "effective")
        self.assertEqual(led.get_order("H")["status"], "effective")
        flips = {c["exchange_id"]: c for c in r2["changed_adjudications"]}
        self.assertEqual(flips["H"]["before_status"], "pending_verification")
        self.assertEqual(flips["H"]["after_status"], "effective")
        posts = {p["billing_day"]: p["delta_cents"] for p in r2["postings"]}
        # Final attribution is H's line at 130/day vs opening L1 100/day.
        self.assertEqual(posts, {DAYS[1]: 30, DAYS[2]: 30})

    def test_late_order_flips_effective_to_pending(self):
        led = open_ledger()
        high = order("H", 8, DAYS[2], DAYS[4], ["L1"],
                     one_new_line("H", DAYS[2], DAYS[4], 240))  # 120/day
        led.receive_batch([high], batch_id="B1")
        self.assertEqual(led.get_order("H")["status"], "effective")
        low = order("LOW", 5, DAYS[1], DAYS[3], ["L1"],
                    one_new_line("LOW", DAYS[1], DAYS[3], 220))  # 110/day
        res = led.receive_batch([low], batch_id="B2")
        self.assertEqual(led.get_order("H")["status"], "pending_verification")
        flip = next(c for c in res["changed_adjudications"] if c["exchange_id"] == "H")
        self.assertTrue(flip["ruling_changed"])
        posts = {p["billing_day"]: p["delta_cents"] for p in res["postings"]}
        self.assertEqual(posts, {DAYS[1]: 10, DAYS[2]: -10, DAYS[3]: -20})
        types = {p["billing_day"]: p["entry_type"] for p in res["postings"]}
        self.assertEqual(types[DAYS[1]], "supplementary")
        self.assertEqual(types[DAYS[3]], "reversal")

    def test_old_line_remaining_days_keep_apportioned_cents(self):
        # Unequal system weights [100,200,300]; line amount 600 apportions 100/200/300.
        sysmap = {SCOPE: {DAYS[0]: 100, DAYS[1]: 200, DAYS[2]: 300,
                          DAYS[3]: 400, DAYS[4]: 500}}
        led = MOD.open_makeup_ledger(
            system_amounts=sysmap,
            original_bills=[line("L1", DAYS[0], DAYS[3], 600)],
        )
        self.assertEqual(led.state["lines"]["L1"]["apportionment"],
                         {DAYS[0]: 100, DAYS[1]: 200, DAYS[2]: 300})
        # Replace only the middle day DAYS[1] with a 250-cent line.
        led.receive_batch([order("X", 1, DAYS[1], DAYS[2], ["L1"],
                                 one_new_line("X", DAYS[1], DAYS[2], 250))])
        app = led.state["lines"]["L1"]["apportionment"]
        # The old line's own apportionment table is untouched ...
        self.assertEqual(app, {DAYS[0]: 100, DAYS[1]: 200, DAYS[2]: 300})
        daily = {r["billing_day"]: r for r in led.view()["daily_ledger"]}
        # ... and its remaining days stay at 100 / 300 while middle becomes 250.
        self.assertEqual(daily[DAYS[0]]["apportioned_amount_cents"], 100)
        self.assertEqual(daily[DAYS[1]]["apportioned_amount_cents"], 250)
        self.assertEqual(daily[DAYS[2]]["apportioned_amount_cents"], 300)

    def test_transitive_overlap_chain_reassessment(self):
        led = open_ledger()
        # Nested coverage: O2 days1..3, O3 days2..3 (old N-O2), O4 day3 (old N-O3).
        o2 = order("O2", 2, DAYS[1], DAYS[4], ["L1"], one_new_line("O2", DAYS[1], DAYS[4], 300))
        o3 = order("O3", 3, DAYS[2], DAYS[4], ["N-O2"], one_new_line("O3", DAYS[2], DAYS[4], 200))
        o4 = order("O4", 4, DAYS[3], DAYS[4], ["N-O3"], one_new_line("O4", DAYS[3], DAYS[4], 100))
        # Receive high end first: O4 pending (N-O3 unknown).
        led.receive_batch([o4], batch_id="B1")
        self.assertEqual(led.get_order("O4")["status"], "pending_verification")
        # O3 next: pending (N-O2 unknown); overlap O3-O4 means O4 reconsidered, still pending.
        r2 = led.receive_batch([o3], batch_id="B2")
        self.assertEqual(led.get_order("O3")["status"], "pending_verification")
        considered = set(r2["considered_order_ids"])
        self.assertIn("O4", considered)
        # O2 finally activates O2 -> O3 (overlap day2) -> O4 (overlap day3) transitively.
        r3 = led.receive_batch([o2], batch_id="B3")
        self.assertEqual(led.get_order("O2")["status"], "effective")
        self.assertEqual(led.get_order("O3")["status"], "effective")
        self.assertEqual(led.get_order("O4")["status"], "effective")
        considered3 = set(r3["considered_order_ids"])
        self.assertEqual(considered3, {"O2", "O3", "O4"})
        edges = {(e["from_exchange_id"], e["to_exchange_id"]) for e in r3["propagation_edges"]}
        self.assertIn(("O2", "O3"), edges)
        self.assertIn(("O3", "O4"), edges)
        chk = led.verify_incremental_matches_full_refold()
        self.assertTrue(chk["matches"], chk["problems"])

    def test_unrelated_and_cross_scope_orders_not_affected(self):
        led = open_ledger()
        # A far-away later order on days 6,7.
        far_end = _plus(DAYS[7], 1)
        far = order("FAR", 9, DAYS[6], far_end, ["L1"], one_new_line("FAR", DAYS[6], far_end, 200))
        led.receive_batch([far])
        # New early order on days 0,1 must not drag FAR into re-adjudication.
        r = led.receive_batch([order("EARLY", 1, DAYS[0], DAYS[2], ["L1"],
                                     one_new_line("EARLY", DAYS[0], DAYS[2], 200))])
        self.assertNotIn("FAR", r["considered_order_ids"])
        # Cross-scope order sharing dates is isolated: open two scopes.
        led2 = MOD.open_makeup_ledger(
            system_amounts={"S1": {d: 100 for d in DAYS}, "S2": {d: 100 for d in DAYS}},
            original_bills=[
                line("L1", DAYS[0], DAYS[7], 700, scope="S1"),
                line("L9", DAYS[0], DAYS[7], 700, scope="S2"),
            ],
        )
        rr = led2.receive_batch([
            order("E1", 1, DAYS[0], DAYS[2], ["L1"],
                  one_new_line("E1", DAYS[0], DAYS[2], 200, scope="S1"), scope="S1"),
            order("SC", 5, DAYS[0], DAYS[2], ["L9"],
                  one_new_line("SC", DAYS[0], DAYS[2], 200, scope="S2"), scope="S2"),
        ])
        self.assertEqual(led2.get_order("SC")["status"], "effective")
        # No propagation edge may cross scopes despite identical dates.
        targets = {e["to_exchange_id"] for e in rr.get("propagation_edges", [])}
        self.assertNotIn("SC", targets)
        self.assertNotIn("E1", targets)


class IncrementalEquivalenceTests(unittest.TestCase):
    def test_random_arrival_permutations_match_full_refold(self):
        rng = random.Random(1234)
        n_days = 8
        days = DAYS[:n_days]
        # Build 14 random structurally-valid orders with unique seq/new ids.
        orders = []
        day_end = _plus(days[-1], 1)
        for k in range(14):
            a = rng.randrange(0, n_days - 1)
            b = rng.randrange(a + 1, n_days + 1)
            end_iso = day_end if b == n_days else days[b]
            seq = k + 1  # unique effective sequence
            # Old ids drawn from originals and all future new ids (may be unknown).
            pool = ["L1"] + [f"N-O{j}" for j in range(14)]
            olds = rng.sample(pool, rng.randint(1, 3))
            oid = f"O{k}"
            orders.append(order(oid, seq, days[a], end_iso, olds,
                                one_new_line(oid, days[a], end_iso,
                                             rng.choice([0, 50, 137, 300, 999]))))
        for trial, perm_seed in enumerate(range(6)):
            led = open_ledger()
            perm = orders[:]
            rng2 = random.Random(100 + perm_seed)
            rng2.shuffle(perm)
            batch_no = 0
            for o in perm:
                batch_no += 1
                res = led.receive_batch([o], batch_id=f"P{trial}-{batch_no}")
                chk = led.verify_incremental_matches_full_refold()
                self.assertTrue(chk["matches"], f"trial {trial} order {o['exchange_id']}: {chk['problems']}")
                # Telescoping postings: batch diff total moves current balance.
                self.assertEqual(
                    res["totals"]["batch_posting_delta_cents"],
                    sum(p["delta_cents"] for p in res["postings"]),
                )
            # Whole posting history telescopes to current - opening.
            by_day: dict[tuple[str, str], int] = {}
            for p in led.postings_history():
                by_day[(p["scope_id"], p["billing_day"])] = (
                    by_day.get((p["scope_id"], p["billing_day"]), 0) + p["delta_cents"]
                )
            opening = led.view("OPENING")
            current = led.view()
            open_map = {r["billing_day"]: r["apportioned_amount_cents"] for r in opening["daily_ledger"]}
            cur_map = {r["billing_day"]: r["apportioned_amount_cents"] for r in current["daily_ledger"]}
            for d in days:
                self.assertEqual(by_day.get((SCOPE, d), 0), cur_map[d] - open_map[d])


class IdempotencyAndUniquenessTests(unittest.TestCase):
    def test_identical_resend_is_noop(self):
        led = open_ledger()
        o = order("E1", 1, DAYS[0], DAYS[2], ["L1"], one_new_line("E1", DAYS[0], DAYS[2], 200))
        led.receive_batch([o], batch_id="B1")
        n = len(led.list_batches())
        res = MOD.try_receive_exchange_batch(led, [json.loads(json.dumps(o))], batch_id="B2")
        self.assertEqual(res["status"], "unchanged")
        self.assertEqual(len(led.list_batches()), n)

    def test_changed_content_same_id_rejected_atomically(self):
        led = open_ledger()
        o = order("E1", 1, DAYS[0], DAYS[2], ["L1"], one_new_line("E1", DAYS[0], DAYS[2], 200))
        led.receive_batch([o], batch_id="B1")
        changed = json.loads(json.dumps(o))
        changed["new_lines"][0]["amount_cents"] = 201
        out = MOD.try_receive_exchange_batch(led, [
            order("E2", 2, DAYS[3], DAYS[5], ["L1"], one_new_line("E2", DAYS[3], DAYS[5], 200)),
            changed,
        ], batch_id="BX")
        self.assertEqual(out["status"], "rejected")
        self.assertNotIn("E2", led.state["orders"])  # whole batch refused

    def test_duplicate_effective_seq_different_order_rejected(self):
        led = open_ledger()
        led.receive_batch([order("E1", 7, DAYS[0], DAYS[1], ["L1"],
                                 one_new_line("E1", DAYS[0], DAYS[1], 100))])
        out = MOD.try_receive_exchange_batch(led, [
            order("E2", 7, DAYS[2], DAYS[3], ["L1"], one_new_line("E2", DAYS[2], DAYS[3], 100))
        ])
        self.assertEqual(out["status"], "rejected")
        self.assertTrue(any(r["code"] == "effective_seq_already_used" for r in out["rejections"]))

    def test_new_line_id_globally_unique_even_for_pending_order(self):
        led = open_ledger()
        # Pending order still introduces its new line id into the ledger.
        led.receive_batch([order("E1", 1, DAYS[0], DAYS[2], ["NOPE"],
                                 [line("DUP", DAYS[0], DAYS[2], 200)])])
        out = MOD.try_receive_exchange_batch(led, [
            order("E2", 2, DAYS[2], DAYS[4], ["L1"], [line("DUP", DAYS[2], DAYS[4], 200)])
        ])
        self.assertEqual(out["status"], "rejected")
        self.assertTrue(any(r["code"] == "new_line_id_not_unique" for r in out["rejections"]))

    def test_overlapping_new_lines_are_structurally_rejected(self):
        led = open_ledger()
        bad = order("E1", 1, DAYS[0], DAYS[3], ["L1"], [
            line("A", DAYS[0], DAYS[2], 100),
            line("B", DAYS[1], DAYS[3], 200),
        ])
        out = MOD.try_receive_exchange_batch(led, [bad])
        self.assertEqual(out["status"], "rejected")
        self.assertNotIn("E1", led.state["orders"])

    def test_new_lines_must_cover_full_interval(self):
        led = open_ledger()
        bad = order("E1", 1, DAYS[0], DAYS[3], ["L1"], [
            line("A", DAYS[0], DAYS[2], 200),  # misses DAYS[2]
        ])
        out = MOD.try_receive_exchange_batch(led, [bad])
        self.assertEqual(out["status"], "rejected")

    def test_batch_id_identical_replay(self):
        led = open_ledger()
        o = order("E1", 1, DAYS[0], DAYS[1], ["L1"], one_new_line("E1", DAYS[0], DAYS[1], 100))
        led.receive_batch([o], batch_id="SAME")
        again = led.receive_batch([o], batch_id="SAME")
        self.assertEqual(again["status"], "unchanged")
        self.assertTrue(again.get("idempotent_batch_replay"))

    def test_multi_order_single_batch_adjudicates_by_seq(self):
        led = open_ledger()
        # Two new orders arrive together; the higher-seq one flushes the lower's
        # new line, so fold-by-seq inside the batch must activate both.
        low = order("LOW", 2, DAYS[0], DAYS[2], ["L1"], one_new_line("LOW", DAYS[0], DAYS[2], 200))
        high = order("HIGH", 5, DAYS[0], DAYS[1], ["N-LOW"], one_new_line("HIGH", DAYS[0], DAYS[1], 130))
        res = led.receive_batch({"batch_id": "M1", "exchanges": [high, low]})  # deliberately reversed
        self.assertEqual(res["status"], "accepted")
        self.assertEqual(led.get_order("LOW")["status"], "effective")
        self.assertEqual(led.get_order("HIGH")["status"], "effective")
        daily = {r["billing_day"]: r for r in led.view()["daily_ledger"]}
        self.assertEqual(daily[DAYS[0]]["line_id"], "N-HIGH")
        self.assertEqual(daily[DAYS[0]]["apportioned_amount_cents"], 130)
        self.assertEqual(daily[DAYS[1]]["line_id"], "N-LOW")
        chk = led.verify_incremental_matches_full_refold()
        self.assertTrue(chk["matches"], chk["problems"])

    def test_structurally_invalid_item_in_batch_changes_nothing(self):
        led = open_ledger()
        good = order("G", 1, DAYS[0], DAYS[1], ["L1"], one_new_line("G", DAYS[0], DAYS[1], 100))
        bad = {"exchange_id": "B", "effective_seq": 2}  # missing everything
        out = MOD.try_receive_exchange_batch(led, [good, bad])
        self.assertEqual(out["status"], "rejected")
        self.assertEqual(led.state["orders"], {})


class SnapshotPersistenceTests(unittest.TestCase):
    def test_frozen_batch_views_survive_reopen(self):
        with tempfile.TemporaryDirectory() as td:
            store = Path(td) / "ledger.json"
            led = open_ledger(store_path=store)
            led.receive_batch([order("H", 8, DAYS[2], DAYS[4], ["L1"],
                                     one_new_line("H", DAYS[2], DAYS[4], 240))],
                              batch_id="B1")
            led.receive_batch([order("L", 5, DAYS[1], DAYS[3], ["L1"],
                                     one_new_line("L", DAYS[1], DAYS[3], 220))],
                              batch_id="B2")
            led.save()
            reopened = MOD.MakeupLedger.load(store)
            # B1 frozen finance view still shows H effective at 120/day.
            b1 = reopened.view("B1")
            self.assertEqual(b1["exchange_orders"]["H"]["status"], "effective")
            b1_day2 = next(r for r in b1["daily_ledger"] if r["billing_day"] == DAYS[2])
            self.assertEqual(b1_day2["line_id"], "N-H")
            self.assertEqual(b1_day2["apportioned_amount_cents"], 120)
            # Latest shows H pending.
            latest = reopened.view()
            self.assertEqual(latest["exchange_orders"]["H"]["status"], "pending_verification")
            # Query surfaces old/new lines and settlement basis.
            self.assertIn("N-L", latest["line_catalog"])
            h_change = next(c for c in latest["changed_adjudications"] if c["exchange_id"] == "H")
            self.assertTrue(h_change["old_line_records"])
            self.assertTrue(h_change["new_line_records"])
            self.assertTrue(latest["settlement_basis"][SCOPE][DAYS[0]]["control_keys"] is not None)
            # Attribution/apportionment query helpers.
            attr = reopened.attribution("B1", scope_id=SCOPE)
            self.assertTrue(any(r["line_id"] == "N-H" for r in attr))
            app = reopened.apportionments("B2")
            self.assertEqual(len(app), 8)
            # History lists opening + two receptions.
            self.assertEqual([b["batch_id"] for b in reopened.list_batches()],
                             ["OPENING", "B1", "B2"])

    def test_receive_after_reopen(self):
        with tempfile.TemporaryDirectory() as td:
            store = Path(td) / "ledger.json"
            led = open_ledger(store_path=store)
            led.receive_batch([order("H", 10, DAYS[1], DAYS[3], ["N-L"],
                                     one_new_line("H", DAYS[1], DAYS[3], 260))],
                              batch_id="B1")
            led.save()
            led2 = MOD.MakeupLedger.load(store)
            led2.receive_batch([order("L", 5, DAYS[1], DAYS[3], ["L1"],
                                      one_new_line("L", DAYS[1], DAYS[3], 220))],
                               batch_id="B2")
            self.assertEqual(led2.get_order("H")["status"], "effective")
            chk = led2.verify_incremental_matches_full_refold()
            self.assertTrue(chk["matches"], chk["problems"])
            led2.save()
            led3 = MOD.load_makeup_ledger(store)
            self.assertEqual(led3.get_order("H")["status"], "effective")

    def test_totals_chain_between_batch_views(self):
        led = open_ledger()
        last_end = _plus(DAYS[7], 1)
        spans = [(0, 2, DAYS[2], 220), (2, 5, DAYS[5], 300), (5, None, last_end, 240)]
        prev = led.view("OPENING")["totals"]["current_daily_difference_cents"]
        for i, (a, b, end, amt) in enumerate(spans):
            res = led.receive_batch([
                order(f"O{i}", i + 1, DAYS[a], end, ["L1"],
                      one_new_line(f"O{i}", DAYS[a], end, amt))
            ], batch_id=f"B{i}")
            cur = res["totals"]["current_daily_difference_cents"]
            self.assertEqual(cur - prev, res["totals"]["batch_posting_delta_cents"])
            prev = cur


class CLITests(unittest.TestCase):
    def test_end_to_end_cli(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            store = td / "store.json"
            opening_doc = td / "opening.json"
            opening_doc.write_text(json.dumps({
                "ledger_id": "CLI",
                "system_amounts": {SCOPE: {d: 100 for d in DAYS[:3]}},
                "original_bills": [line("L1", DAYS[0], DAYS[3], 300)],
            }), encoding="utf-8")
            batch_doc = td / "batch.json"
            batch_doc.write_text(json.dumps({
                "batch_id": "R1",
                "exchanges": [order("E1", 1, DAYS[0], DAYS[2], ["L1"],
                                    one_new_line("E1", DAYS[0], DAYS[2], 240))],
            }), encoding="utf-8")

            def run(*extra):
                p = subprocess.run(
                    [sys.executable, "-I", str(MODULE_PATH), *extra],
                    capture_output=True, text=True,
                )
                return p

            p = run("--ledger-open", str(opening_doc), "--ledger-store", str(store))
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertTrue(store.exists())
            p = run("--ledger-receive", str(batch_doc), "--ledger-store", str(store))
            self.assertEqual(p.returncode, 0, p.stderr)
            out = json.loads(p.stdout)
            self.assertEqual(out["status"], "accepted")
            self.assertEqual({x["billing_day"]: x["delta_cents"] for x in out["postings"]},
                             {DAYS[0]: 20, DAYS[1]: 20})
            p = run("--ledger-history", "--ledger-store", str(store))
            self.assertEqual(p.returncode, 0)
            hist = json.loads(p.stdout)
            self.assertEqual(len(hist["batches"]), 2)
            p = run("--ledger-view", "R1", "--ledger-store", str(store))
            self.assertEqual(p.returncode, 0)
            view = json.loads(p.stdout)
            self.assertEqual(view["batch_id"], "R1")

    def test_cli_rejection_exit_code(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            store = td / "store.json"
            opening_doc = td / "opening.json"
            opening_doc.write_text(json.dumps({
                "ledger_id": "CLI2",
                "system_amounts": {SCOPE: {DAYS[0]: 100}},
                "original_bills": [line("L1", DAYS[0], DAYS[2], 200)],  # day1 no basis
            }), encoding="utf-8")
            p = subprocess.run(
                [sys.executable, "-I", str(MODULE_PATH),
                 "--ledger-open", str(opening_doc), "--ledger-store", str(store)],
                capture_output=True, text=True,
            )
            self.assertEqual(p.returncode, 3)
            self.assertFalse(store.exists())


if __name__ == "__main__":
    unittest.main()
