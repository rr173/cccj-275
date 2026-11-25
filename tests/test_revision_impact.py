"""Tests for revision impact analysis (baseline settlement vs. candidate revision).

Run with:
    python3 -I -m unittest discover -s tests -v

Run any Python that reads input files with -I as the harness does; the module
itself is always invoked with ``python3 -I``.
"""

import copy
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = ROOT / "meter_allocator.py"
PARK_INPUT = ROOT / "examples" / "park_input.json"
CANDIDATE_INPUT = ROOT / "examples" / "revision_candidate_input.json"
IMPACT_RESULT = ROOT / "examples" / "revision_impact_result.json"


def load_module():
    name = "meter_allocator_impact_under_test"
    spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


MOD = load_module()


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def scope_by_id(report, scope_id):
    return next(s for s in report["scopes"] if s["scope_id"] == scope_id)


def strip_audit(report):
    """Audit bytes change when records are re-ordered; identities must not."""
    out = copy.deepcopy(report)
    for side in ("baseline", "candidate"):
        out["input_audit"][side].pop("sha256", None)
        out["input_audit"][side].pop("path", None)
    return out


def shuffled(root, seed=0):
    import random
    out = copy.deepcopy(root)
    rng = random.Random(seed)
    for key in ("readings", "bindings", "tariff_versions", "control_totals", "exchange_vouchers"):
        if isinstance(out.get(key), list):
            rng.shuffle(out[key])
    return out


def settleable_reading_inputs():
    """A minimal pair where both sides settle cleanly and one value changed."""
    base = load(ROOT / "examples" / "feasible_crossday_input.json")
    cand = copy.deepcopy(base)
    for r in cand["readings"]:
        if r["reading_id"] == "r2":
            r["value_wh"] = 5
    for c in cand["control_totals"]:
        if c["billing_day"] == "2025-01-02":
            c["total_wh"] = 2
    for c in cand["control_totals"]:
        if c["billing_day"] == "2025-01-01":
            c["total_wh"] = 3
    return base, cand


class IdentityAndOrderTests(unittest.TestCase):
    def test_same_input_reports_no_changes(self):
        root = load(PARK_INPUT)
        report = MOD.revision_impact(
            root, root, PARK_INPUT, PARK_INPUT, PARK_INPUT.read_bytes(), PARK_INPUT.read_bytes()
        )
        self.assertEqual(report["report_type"], "revision_impact_analysis")
        self.assertFalse(report["has_changes"])
        self.assertEqual(report["summary"]["control_cells"]["changed"], 0)
        self.assertEqual(report["summary"]["control_cells"]["added"], 0)
        self.assertEqual(report["summary"]["control_cells"]["removed"], 0)
        self.assertEqual(report["summary"]["reading_segments"]["changed"], 0)
        self.assertEqual(report["summary"]["comparable_scope_delta_over_shared_control_cells"]["allocated_wh"], 0)

    def test_record_order_does_not_create_changes(self):
        root = load(PARK_INPUT)
        reordered = shuffled(root, seed=17)
        a = MOD.revision_impact(
            root, root, PARK_INPUT, PARK_INPUT, PARK_INPUT.read_bytes(), PARK_INPUT.read_bytes()
        )
        b = MOD.revision_impact(
            reordered, root, PARK_INPUT, PARK_INPUT,
            json.dumps(reordered).encode(), PARK_INPUT.read_bytes(),
        )
        self.assertEqual(strip_audit(a), strip_audit(b))
        self.assertFalse(b["has_changes"])

    def test_candidate_record_order_does_not_create_changes(self):
        base = load(PARK_INPUT)
        cand = load(CANDIDATE_INPUT)
        cand_shuffled = shuffled(cand, seed=5)
        a = MOD.revision_impact(
            base, cand, PARK_INPUT, CANDIDATE_INPUT,
            PARK_INPUT.read_bytes(), CANDIDATE_INPUT.read_bytes(),
        )
        b = MOD.revision_impact(
            base, cand_shuffled, PARK_INPUT, CANDIDATE_INPUT,
            PARK_INPUT.read_bytes(), json.dumps(cand_shuffled).encode(),
        )
        self.assertEqual(strip_audit(a), strip_audit(b))
        self.assertTrue(b["has_changes"])

    def test_segment_identity_is_meter_and_utc_not_atom_id(self):
        base, cand = settleable_reading_inputs()
        report = MOD.revision_impact(base, cand, None, None, None, None)
        scope = scope_by_id(report, "S1")
        entries = scope["reading_segments"]
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["segment"]["meter_id"], "M1")
        self.assertEqual(entry["segment"]["start_utc"], "2025-01-01T23:30:00Z")
        self.assertEqual(entry["segment"]["end_utc"], "2025-01-02T00:30:00Z")
        self.assertEqual(entry["change"], "changed")
        self.assertEqual(entry["delta_allocated_wh"], 2)


class ChangeCategoryTests(unittest.TestCase):
    def test_scope_day_rate_rollup_and_raw_segment_delta(self):
        report = MOD.revision_impact(
            load(PARK_INPUT), load(CANDIDATE_INPUT),
            PARK_INPUT, CANDIDATE_INPUT,
            PARK_INPUT.read_bytes(), CANDIDATE_INPUT.read_bytes(),
        )
        scope = scope_by_id(report, "S-XDAY")
        self.assertEqual(scope["comparability"], "comparable")
        self.assertEqual(scope["rollup"]["control_cells"]["changed"], 2)
        self.assertEqual(scope["rollup"]["control_cells"]["unchanged"], 0)
        self.assertEqual(scope["rollup"]["reading_segments"]["changed"], 1)
        self.assertEqual(scope["rollup"]["delta_over_shared_control_cells"]["allocated_wh"], 1)
        self.assertEqual(scope["rollup"]["delta_over_shared_control_cells"]["amount_fraction"], "13/4")

        # billing day breakdown: 2024-11-04 holds same 2 Wh, 2024-11-05 grows 1 -> 2 Wh
        days = {d["billing_day"]: d for d in scope["by_billing_day"]}
        day04 = days["2024-11-04"]["rates"][0]
        day05 = days["2024-11-05"]["rates"][0]
        self.assertEqual(day04["delta_allocated_wh"], 0)
        self.assertEqual(day04["delta_amount_fraction"], "1")
        self.assertEqual(day05["delta_allocated_wh"], 1)
        self.assertEqual(day05["delta_amount_fraction"], "9/4")

        # raw reading segment change: 3 -> 4 Wh across the same half-open interval
        seg = scope["reading_segments"][0]
        fields = {c["field"] for c in seg["changes"]}
        self.assertIn("raw_difference_wh", fields)
        self.assertIn("reading", fields)
        self.assertEqual(seg["baseline"]["raw_difference_wh"], 3)
        self.assertEqual(seg["candidate"]["raw_difference_wh"], 4)

    def test_unaffected_scope_is_unchanged_with_zero_delta(self):
        report = MOD.revision_impact(
            load(PARK_INPUT), load(CANDIDATE_INPUT),
            PARK_INPUT, CANDIDATE_INPUT,
            PARK_INPUT.read_bytes(), CANDIDATE_INPUT.read_bytes(),
        )
        for scope_id in ("S-DST", "S-EX"):
            scope = scope_by_id(report, scope_id)
            self.assertEqual(scope["comparability"], "comparable")
            self.assertEqual(scope["rollup"]["control_cells"]["unchanged"], 1)
            self.assertEqual(scope["rollup"]["control_cells"]["changed"], 0)
            self.assertEqual(scope["rollup"]["delta_over_shared_control_cells"]["allocated_wh"], 0)

    def test_committed_example_output_matches_live_run(self):
        live = MOD.revision_impact(
            load(PARK_INPUT), load(CANDIDATE_INPUT),
            PARK_INPUT, CANDIDATE_INPUT,
            PARK_INPUT.read_bytes(), CANDIDATE_INPUT.read_bytes(),
        )
        committed = load(IMPACT_RESULT)
        self.assertEqual(live, committed)

    def test_added_scope_is_not_a_zero_baseline_delta(self):
        base = load(ROOT / "examples" / "feasible_crossday_input.json")
        cand = copy.deepcopy(base)
        cand["readings"] += [
            {"reading_id": "n1", "meter_id": "M9", "ts": "2025-01-05T00:00:00+00:00", "value_wh": 0},
            {"reading_id": "n2", "meter_id": "M9", "ts": "2025-01-05T02:00:00+00:00", "value_wh": 4},
        ]
        cand["bindings"].append({
            "binding_id": "b9", "meter_id": "M9", "scope_id": "S9",
            "valid_from": "2025-01-04T00:00:00+00:00", "valid_to": "2025-01-06T00:00:00+00:00",
        })
        cand["tariff_versions"].append({
            "tariff_version": "tv9", "scope_id": "S9", "rate_id": "flat", "unit_price": "3",
            "effective_from": "2025-01-04T00:00:00+00:00", "effective_to": "2025-01-06T00:00:00+00:00",
        })
        cand["control_totals"].append({
            "scope_id": "S9", "billing_day": "2025-01-05", "rate_id": "flat", "total_wh": 4,
        })
        report = MOD.revision_impact(base, cand, None, None, None, None)
        scope = scope_by_id(report, "S9")
        self.assertEqual(scope["comparability"], "comparable")
        self.assertEqual(scope["baseline_presence"], "absent")
        self.assertEqual(scope["candidate_presence"], "complete")
        cell = scope["control_cells"][0]
        self.assertEqual(cell["change"], "added")
        self.assertIsNone(cell["baseline"])
        self.assertIsNone(cell["delta_allocated_wh"])
        self.assertIsNone(cell["delta_amount_fraction"])
        self.assertEqual(scope["rollup"]["added_values_not_delta"]["allocated_wh"], 4)
        self.assertEqual(scope["rollup"]["added_values_not_delta"]["amount_fraction"], "12")
        self.assertIsNone(scope["rollup"]["delta_over_shared_control_cells"]["allocated_wh"])

    def test_removed_scope_is_not_a_zero_candidate_delta(self):
        base = load(ROOT / "examples" / "feasible_crossday_input.json")
        cand = copy.deepcopy(base)
        cand["readings"] = [r for r in cand["readings"] if r["reading_id"] not in ("r1", "r2")]
        cand["bindings"] = []
        cand["tariff_versions"] = []
        cand["control_totals"] = []
        report = MOD.revision_impact(base, cand, None, None, None, None)
        scope = scope_by_id(report, "S1")
        self.assertEqual(scope["candidate_presence"], "absent")
        cell = scope["control_cells"][0]
        self.assertEqual(cell["change"], "removed")
        self.assertIsNone(cell["delta_allocated_wh"])
        self.assertEqual(scope["rollup"]["removed_values_not_delta"]["allocated_wh"] > 0, True)


class ComparabilityFailureTests(unittest.TestCase):
    def test_blocked_scope_is_not_comparable_with_conflict_evidence(self):
        report = MOD.revision_impact(
            load(PARK_INPUT), load(PARK_INPUT),
            PARK_INPUT, PARK_INPUT, PARK_INPUT.read_bytes(), PARK_INPUT.read_bytes(),
        )
        for scope_id in ("S-NEG", "S-PEND", "S-AMB1", "S-AMB2"):
            scope = scope_by_id(report, scope_id)
            self.assertEqual(scope["comparability"], "not_comparable")
            self.assertTrue(scope["conflicts"]["baseline"])
            self.assertTrue(scope["conflicts"]["candidate"])
            for cell in scope["control_cells"]:
                self.assertEqual(cell["change"], "not_comparable")
                self.assertIsNone(cell["delta_allocated_wh"])
            self.assertIsNone(
                scope["rollup"]["delta_over_shared_control_cells"]["allocated_wh"]
            )

    def test_infeasible_range_is_not_comparable(self):
        report = MOD.revision_impact(
            load(PARK_INPUT), load(PARK_INPUT),
            PARK_INPUT, PARK_INPUT, PARK_INPUT.read_bytes(), PARK_INPUT.read_bytes(),
        )
        for scope_id in ("S-HALL", "S-HQ"):
            scope = scope_by_id(report, scope_id)
            self.assertEqual(scope["comparability"], "not_comparable")
            codes = {c["code"] for c in scope["conflicts"]["baseline"]}
            self.assertIn("connected_range_not_settled", codes)

    def test_failure_on_one_side_never_reports_a_numeric_delta(self):
        base = load(PARK_INPUT)
        cand = copy.deepcopy(base)
        # Remove a binding so the candidate can no longer attribute this segment.
        cand["bindings"] = [b for b in cand["bindings"] if b["binding_id"] != "B-XD"]
        report = MOD.revision_impact(base, cand, PARK_INPUT, PARK_INPUT, None, None)
        scope = scope_by_id(report, "S-XDAY")
        self.assertEqual(scope["comparability"], "not_comparable")
        self.assertEqual(scope["baseline_presence"], "complete")
        self.assertEqual(scope["candidate_presence"], "incomplete")
        self.assertIn("candidate_not_settleable_or_evidence_insufficient", scope["reason"])
        for cell in scope["control_cells"]:
            self.assertEqual(cell["change"], "not_comparable")
            self.assertIsNone(cell["delta_allocated_wh"])
            self.assertIsNone(cell["delta_amount_fraction"])
        # The unattributed candidate segment is retained unscoped rather than
        # silently paired with the baseline segment.
        unscoped = report["unscoped_segments"]["candidate"]
        self.assertEqual(len(unscoped), 1)
        self.assertEqual(unscoped[0]["identity"]["meter_id"], "M-XDAY")
        self.assertEqual(unscoped[0]["reason"], "no_resolvable_scope")

    def test_missing_control_total_makes_scope_not_comparable(self):
        base = load(ROOT / "examples" / "feasible_crossday_input.json")
        cand = copy.deepcopy(base)
        cand["control_totals"] = [c for c in cand["control_totals"] if c["billing_day"] != "2025-01-02"]
        report = MOD.revision_impact(base, cand, None, None, None, None)
        scope = scope_by_id(report, "S1")
        self.assertEqual(scope["comparability"], "not_comparable")
        codes = {c["code"] for c in scope["conflicts"]["candidate"]}
        self.assertIn("control_cell_not_settled", codes)
        self.assertTrue(
            any(i.get("code") == "missing_control_total"
                for c in scope["conflicts"]["candidate"]
                for i in c.get("issues", []))
        )

    def test_duplicate_control_total_evidence_is_retained(self):
        base = load(ROOT / "examples" / "feasible_crossday_input.json")
        cand = copy.deepcopy(base)
        cand["control_totals"].append({
            "control_id": "dup", "scope_id": "S1", "billing_day": "2025-01-01",
            "rate_id": "flat", "total_wh": 2,
        })
        report = MOD.revision_impact(base, cand, None, None, None, None)
        scope = scope_by_id(report, "S1")
        self.assertEqual(scope["comparability"], "not_comparable")
        dumped = json.dumps(scope["conflicts"]["candidate"], ensure_ascii=False)
        self.assertIn("duplicate_control_total", dumped)

    def test_global_issue_without_scope_blocks_every_scope(self):
        base = load(ROOT / "examples" / "feasible_crossday_input.json")
        cand = copy.deepcopy(base)
        # A voucher referencing a missing reading cannot be attributed to a scope.
        cand["exchange_vouchers"] = [{
            "voucher_id": "V-ORPHAN", "exchange_at": "2025-01-01T12:00:00+00:00",
            "old_meter_id": "M-ORPHAN", "new_meter_id": "M-ORPHAN2",
            "old_end_reading_id": "missing-id",
        }]
        report = MOD.revision_impact(base, cand, None, None, None, None)
        self.assertEqual(report["summary"]["comparable_scope_count"], 0)
        scope = scope_by_id(report, "S1")
        self.assertEqual(scope["comparability"], "not_comparable")
        self.assertTrue(
            any(c["code"] == "scope_not_identifiable" for c in scope["conflicts"]["candidate"])
        )

    def test_overall_status_reflects_mixture(self):
        mixed = MOD.revision_impact(
            load(PARK_INPUT), load(CANDIDATE_INPUT),
            PARK_INPUT, CANDIDATE_INPUT,
            PARK_INPUT.read_bytes(), CANDIDATE_INPUT.read_bytes(),
        )
        self.assertEqual(mixed["overall_status"], "partial_comparability")
        self.assertEqual(mixed["summary"]["comparable_scope_count"], 3)
        self.assertEqual(mixed["summary"]["not_comparable_scope_count"], 6)

        both_bad = MOD.revision_impact(
            load(PARK_INPUT), load(PARK_INPUT), None, None, None, None
        )
        self.assertEqual(both_bad["overall_status"], "partial_comparability")

        only_bad = load(ROOT / "examples" / "feasible_crossday_input.json")
        only_bad_bad = copy.deepcopy(only_bad)
        only_bad_bad["bindings"] = []
        report = MOD.revision_impact(only_bad, only_bad_bad, None, None, None, None)
        self.assertEqual(report["overall_status"], "not_comparable")


class EvidenceRetentionTests(unittest.TestCase):
    def test_comparable_cell_carries_rule_binding_reading_and_settlement_basis(self):
        report = MOD.revision_impact(
            load(PARK_INPUT), load(CANDIDATE_INPUT),
            PARK_INPUT, CANDIDATE_INPUT,
            PARK_INPUT.read_bytes(), CANDIDATE_INPUT.read_bytes(),
        )
        scope = scope_by_id(report, "S-XDAY")
        cell = scope["control_cells"][0]
        for side in ("baseline", "candidate"):
            view = cell[side]
            self.assertTrue(view["raw_control_rows"])
            self.assertTrue(view["settlement_basis"]["atoms"])
            self.assertTrue(view["binding_ids"])
            self.assertTrue(view["tariff_versions"])
            atom = view["settlement_basis"]["atoms"][0]
            for field in ("binding_raw", "tariff_basis", "unit_price", "rate_id", "calendar_id", "timezone"):
                self.assertIn(field, atom)
        seg = scope["reading_segments"][0]
        for side in ("baseline", "candidate"):
            view = seg[side]
            self.assertTrue(view["reading_evidence"]["start_anchors"])
            self.assertTrue(view["reading_evidence"]["end_anchors"])
            self.assertTrue(view["binding_evidence"])
            self.assertTrue(view["tariff_evidence"])

    def test_not_comparable_scope_carries_both_sides_raw_evidence(self):
        report = MOD.revision_impact(
            load(PARK_INPUT), load(PARK_INPUT),
            PARK_INPUT, PARK_INPUT, PARK_INPUT.read_bytes(), PARK_INPUT.read_bytes(),
        )
        scope = scope_by_id(report, "S-NEG")
        segment_conflicts = [
            c for c in scope["conflicts"]["baseline"] if c["code"] == "reading_segment_not_settled"
        ]
        self.assertTrue(segment_conflicts)
        conflict = segment_conflicts[0]
        self.assertTrue(conflict["reading_evidence"]["start_anchors"])
        self.assertTrue(conflict["binding_evidence"])
        self.assertEqual(conflict["raw_difference_wh"], -50)
        self.assertIn("unvouchered_negative_drop", {i["code"] for i in conflict["issues"]})

    def test_rule_text_is_retained(self):
        report = MOD.revision_impact(
            load(PARK_INPUT), load(PARK_INPUT), None, None, None, None
        )
        self.assertIn("rules", report)
        self.assertIn("comparability", report["rules"])
        self.assertIn("identity", report["rules"])
        self.assertIn("settlement_basis", report["rules"])
        self.assertIn("change_categories", report["rules"])


class IsolationTests(unittest.TestCase):
    def test_settle_is_unchanged_by_impact_module(self):
        root = load(PARK_INPUT)
        report = MOD.settle(root, PARK_INPUT, PARK_INPUT.read_bytes())
        committed = load(ROOT / "examples" / "park_result.json")
        self.assertEqual(
            {k: v for k, v in report.items() if k != "segment_to_component_hint"},
            {k: v for k, v in committed.items() if k != "segment_to_component_hint"},
        )
        self.assertNotIn("revision_impact", report)

    def test_reconciliation_is_unchanged(self):
        recon_input = ROOT / "examples" / "reconciliation_input.json"
        report = MOD.reconcile_windows(
            load(recon_input), recon_input, recon_input.read_bytes()
        )
        committed = load(ROOT / "examples" / "reconciliation_result.json")
        self.assertEqual(report, committed)

    def test_cli_impact_mode_writes_report_and_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "impact.json"
            csv_out = Path(tmp) / "impact.csv"
            result = subprocess.run(
                [
                    sys.executable, "-I", str(MODULE_PATH),
                    "--baseline", str(PARK_INPUT),
                    "--candidate", str(CANDIDATE_INPUT),
                    "--impact-output", str(out),
                    "--impact-csv", str(csv_out),
                ],
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(report["report_type"], "revision_impact_analysis")
            self.assertEqual(report["overall_status"], "partial_comparability")
            rows = csv_out.read_text(encoding="utf-8").strip().splitlines()
            self.assertGreater(len(rows), 1)
            self.assertIn("S-XDAY", "\n".join(rows))

    def test_cli_rejects_impact_without_baseline(self):
        result = subprocess.run(
            [
                sys.executable, "-I", str(MODULE_PATH),
                "--candidate", str(CANDIDATE_INPUT), "--impact",
            ],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 2)

    def test_cli_impact_cannot_be_combined_with_reconciliation(self):
        result = subprocess.run(
            [
                sys.executable, "-I", str(MODULE_PATH),
                "--baseline", str(PARK_INPUT), "--candidate", str(CANDIDATE_INPUT),
                "--impact", "--reconcile",
            ],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 2)

    def test_cli_invalid_json_is_fatal_input_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.json"
            bad.write_text("{not json", encoding="utf-8")
            out = Path(tmp) / "impact.json"
            result = subprocess.run(
                [
                    sys.executable, "-I", str(MODULE_PATH),
                    "--baseline", str(bad), "--candidate", str(CANDIDATE_INPUT),
                    "--impact-output", str(out),
                ],
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 2)
            report = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(report["overall_status"], "fatal_input_error")


if __name__ == "__main__":
    unittest.main()
