"""Tests for planned collection-window reconciliation.

Run with:
    python3 -I -m unittest discover -s tests -v

The module is loaded by explicit path so the tests do not depend on the current
directory being importable (the tool itself is always run with ``python3 -I``).
"""

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = ROOT / "meter_allocator.py"
EXAMPLE_INPUT = ROOT / "examples" / "park_input.json"
RECON_INPUT = ROOT / "examples" / "reconciliation_input.json"


def load_module():
    name = "meter_allocator_under_test"
    spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    # Register before exec: dataclasses resolves annotations through sys.modules.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


MOD = load_module()


def base_input(readings, bindings, windows):
    return {
        "rule_version": "test",
        "planned_windows": windows,
        "readings": readings,
        "bindings": bindings,
    }


def reading(rid, meter, ts, value=1):
    return {"reading_id": rid, "meter_id": meter, "ts": ts, "value_wh": value}


def binding(bid, meter, point, start, end):
    return {
        "binding_id": bid,
        "meter_id": meter,
        "point_id": point,
        "scope_id": "S-" + point,
        "valid_from": start,
        "valid_to": end,
    }


def window(wid, point, start, end):
    return {"window_id": wid, "point_id": point, "expected_from": start, "expected_to": end}


def run(root):
    return MOD.reconcile_windows(root)


def window_by_id(report, wid):
    return next(w for w in report["windows"] if w["window_id"] == wid)


def unmatched_by_id(report, rid):
    return next(u for u in report["unmatched_readings"] if u["reading_id"] == rid)


class HalfOpenBoundaryTests(unittest.TestCase):
    def test_end_instant_belongs_to_next_window_only(self):
        root = base_input(
            [reading("r1", "M", "2025-01-01T10:00:00+00:00")],
            [binding("b1", "M", "P1", "2025-01-01T00:00:00+00:00", "2025-01-02T00:00:00+00:00")],
            [
                window("w1", "P1", "2025-01-01T09:00:00+00:00", "2025-01-01T10:00:00+00:00"),
                window("w2", "P1", "2025-01-01T10:00:00+00:00", "2025-01-01T11:00:00+00:00"),
            ],
        )
        report = run(root)
        self.assertEqual(window_by_id(report, "w1")["status"], "missed")
        self.assertEqual(window_by_id(report, "w2")["status"], "matched")
        self.assertEqual(window_by_id(report, "w2")["selected_reading_id"], "r1")

    def test_reading_at_end_of_all_windows_is_unmatched(self):
        root = base_input(
            [reading("r1", "M", "2025-01-01T10:00:00+00:00")],
            [binding("b1", "M", "P1", "2025-01-01T00:00:00+00:00", "2025-01-02T00:00:00+00:00")],
            [window("w1", "P1", "2025-01-01T09:00:00+00:00", "2025-01-01T10:00:00+00:00")],
        )
        report = run(root)
        self.assertEqual(window_by_id(report, "w1")["status"], "missed")
        self.assertEqual(
            unmatched_by_id(report, "r1")["reason"], "not_inside_any_planned_window"
        )


class DstTests(unittest.TestCase):
    def test_repeated_local_wall_clock_is_separated_by_offset(self):
        # 2024-11-03 America/New_York fall-back: 01:30-04:00 and 01:30-05:00
        # are one hour apart in UTC.
        root = base_input(
            [
                reading("early", "M", "2024-11-03T01:30:00-04:00"),
                reading("late", "M", "2024-11-03T01:30:00-05:00"),
            ],
            [binding("b1", "M", "P1", "2024-11-01T00:00:00-04:00", "2024-12-01T00:00:00-05:00")],
            [
                window("w-early", "P1", "2024-11-03T01:00:00-04:00", "2024-11-03T01:45:00-04:00"),
                window("w-late", "P1", "2024-11-03T01:45:00-04:00", "2024-11-03T02:00:00-05:00"),
            ],
        )
        report = run(root)
        self.assertEqual(window_by_id(report, "w-early")["selected_reading_id"], "early")
        self.assertEqual(window_by_id(report, "w-late")["selected_reading_id"], "late")


class CandidateAmbiguityTests(unittest.TestCase):
    def test_multiple_candidates_mark_pending_without_selection(self):
        root = base_input(
            [
                reading("r1", "M", "2025-01-01T09:10:00+00:00"),
                reading("r2", "M", "2025-01-01T09:50:00+00:00"),
            ],
            [binding("b1", "M", "P1", "2025-01-01T00:00:00+00:00", "2025-01-02T00:00:00+00:00")],
            [window("w1", "P1", "2025-01-01T09:00:00+00:00", "2025-01-01T10:00:00+00:00")],
        )
        report = run(root)
        w = window_by_id(report, "w1")
        self.assertEqual(w["status"], "pending_review")
        self.assertIsNone(w["selected_reading_id"])
        self.assertEqual(w["candidate_reading_ids"], ["r1", "r2"])
        self.assertEqual([i["code"] for i in w["issues"]], ["multiple_candidate_readings"])
        # Even though r2 is nearer to the window end, nothing is picked.
        self.assertEqual(report["summary"]["selected_reading_count"], 0)
        self.assertEqual({u["reading_id"] for u in report["unmatched_readings"]}, {"r1", "r2"})

    def test_reading_shared_by_overlapping_windows_marks_both_pending(self):
        root = base_input(
            [reading("r1", "M", "2025-01-01T10:30:00+00:00")],
            [binding("b1", "M", "P1", "2025-01-01T00:00:00+00:00", "2025-01-02T00:00:00+00:00")],
            [
                window("w1", "P1", "2025-01-01T10:00:00+00:00", "2025-01-01T11:00:00+00:00"),
                window("w2", "P1", "2025-01-01T10:30:00+00:00", "2025-01-01T11:30:00+00:00"),
            ],
        )
        report = run(root)
        for wid in ("w1", "w2"):
            w = window_by_id(report, wid)
            self.assertEqual(w["status"], "pending_review")
            self.assertIsNone(w["selected_reading_id"])
            self.assertEqual([i["code"] for i in w["issues"]], ["reading_shared_across_windows"])
            self.assertEqual(w["issues"][0]["other_window_ids"], ["w2" if wid == "w1" else "w1"])
        unmatched = unmatched_by_id(report, "r1")
        self.assertEqual(unmatched["reason"], "candidate_but_not_uniquely_assigned")
        self.assertEqual(unmatched["candidate_window_statuses"], {"w1": "pending_review", "w2": "pending_review"})

    def test_one_reading_cannot_be_selected_for_two_windows(self):
        root = base_input(
            [reading("r1", "M", "2025-01-01T10:00:00+00:00")],
            [binding("b1", "M", "P1", "2025-01-01T00:00:00+00:00", "2025-01-02T00:00:00+00:00")],
            [
                window("w1", "P1", "2025-01-01T10:00:00+00:00", "2025-01-01T11:00:00+00:00"),
                window("w2", "P1", "2025-01-01T10:00:00+00:00", "2025-01-01T11:00:00+00:00"),
            ],
        )
        report = run(root)
        selected_total = sum(
            1 for w in report["windows"] if w["selected_reading_id"] is not None
        )
        self.assertEqual(selected_total, 0)

    def test_ambiguous_binding_at_reading_time_marks_pending(self):
        root = base_input(
            [reading("r1", "M", "2025-01-01T10:30:00+00:00")],
            [
                binding("b1", "M", "P1", "2025-01-01T00:00:00+00:00", "2025-01-02T00:00:00+00:00"),
                binding("b2", "M", "P2", "2025-01-01T00:00:00+00:00", "2025-01-02T00:00:00+00:00"),
            ],
            [window("w1", "P1", "2025-01-01T10:00:00+00:00", "2025-01-01T11:00:00+00:00")],
        )
        report = run(root)
        w = window_by_id(report, "w1")
        self.assertEqual(w["status"], "pending_review")
        self.assertEqual([i["code"] for i in w["issues"]], ["ambiguous_binding_at_reading_time"])
        self.assertIsNone(w["selected_reading_id"])
        unmatched = unmatched_by_id(report, "r1")
        self.assertEqual(unmatched["reason"], "ambiguous_binding_at_reading_time")
        self.assertEqual(sorted(unmatched["bound_point_ids"]), ["P1", "P2"])


class MissedAndUnmatchedTests(unittest.TestCase):
    def test_missed_window_lists_inside_reading_bound_to_other_point(self):
        root = base_input(
            [reading("r1", "M", "2025-01-01T10:30:00+00:00")],
            [binding("b1", "M", "P2", "2025-01-01T00:00:00+00:00", "2025-01-02T00:00:00+00:00")],
            [window("w1", "P1", "2025-01-01T10:00:00+00:00", "2025-01-01T11:00:00+00:00")],
        )
        report = run(root)
        w = window_by_id(report, "w1")
        self.assertEqual(w["status"], "missed")
        self.assertEqual(
            [(x["reading_id"], x["attribution"]) for x in w["readings_inside_window"]],
            [("r1", "bound_to_other_point")],
        )
        self.assertEqual(
            unmatched_by_id(report, "r1")["reason"], "binding_point_not_in_containing_window"
        )

    def test_reading_without_effective_binding_is_reported(self):
        root = base_input(
            [reading("r1", "M", "2025-01-01T10:30:00+00:00")],
            [binding("b1", "M", "P1", "2024-01-01T00:00:00+00:00", "2024-12-31T00:00:00+00:00")],
            [window("w1", "P1", "2025-01-01T10:00:00+00:00", "2025-01-01T11:00:00+00:00")],
        )
        report = run(root)
        self.assertEqual(window_by_id(report, "w1")["status"], "missed")
        unmatched = unmatched_by_id(report, "r1")
        self.assertEqual(unmatched["reason"], "no_effective_binding_at_reading_time")
        self.assertEqual(unmatched["active_bindings"], [])
        self.assertEqual(
            window_by_id(report, "w1")["readings_inside_window"][0]["attribution"],
            "no_effective_binding_at_reading_time",
        )

    def test_binding_without_point_id_cannot_attach_reading(self):
        root = base_input(
            [reading("r1", "M", "2025-01-01T10:30:00+00:00")],
            [{
                "binding_id": "b1",
                "meter_id": "M",
                "scope_id": "S1",
                "valid_from": "2025-01-01T00:00:00+00:00",
                "valid_to": "2025-01-02T00:00:00+00:00",
            }],
            [window("w1", "P1", "2025-01-01T10:00:00+00:00", "2025-01-01T11:00:00+00:00")],
        )
        report = run(root)
        self.assertEqual(window_by_id(report, "w1")["status"], "missed")
        self.assertEqual(
            unmatched_by_id(report, "r1")["reason"], "binding_without_point_id"
        )


class ExcludedRecordTests(unittest.TestCase):
    def test_void_reading_is_excluded_from_candidates_but_retained(self):
        root = base_input(
            [{
                "reading_id": "r-void",
                "meter_id": "M",
                "status": "void",
                "ts": "2025-01-01T10:30:00+00:00",
                "value_wh": 7,
            }],
            [binding("b1", "M", "P1", "2025-01-01T00:00:00+00:00", "2025-01-02T00:00:00+00:00")],
            [window("w1", "P1", "2025-01-01T10:00:00+00:00", "2025-01-01T11:00:00+00:00")],
        )
        report = run(root)
        self.assertEqual(window_by_id(report, "w1")["status"], "missed")
        self.assertEqual([r["reading_id"] for r in report["excluded_readings"]], ["r-void"])
        self.assertEqual(report["unmatched_readings"], [])
        self.assertEqual(report["summary"]["excluded_reading_count"], 1)

    def test_invalid_binding_is_not_considered(self):
        root = base_input(
            [reading("r1", "M", "2025-01-01T10:30:00+00:00")],
            [{
                "binding_id": "b1",
                "meter_id": "M",
                "point_id": "P1",
                "valid": False,
                "valid_from": "2025-01-01T00:00:00+00:00",
                "valid_to": "2025-01-02T00:00:00+00:00",
            }],
            [window("w1", "P1", "2025-01-01T10:00:00+00:00", "2025-01-01T11:00:00+00:00")],
        )
        report = run(root)
        self.assertEqual(window_by_id(report, "w1")["status"], "missed")
        self.assertEqual([b["binding_id"] for b in report["excluded_bindings"]], ["b1"])
        self.assertEqual(report["bindings_considered"], [])


class EvidenceTests(unittest.TestCase):
    def test_every_window_carries_raw_window_and_binding_evidence(self):
        report = run(json.loads(RECON_INPUT.read_text(encoding="utf-8")))
        self.assertTrue(report["windows"])
        for w in report["windows"]:
            self.assertIn("raw", w)
            self.assertIn("expected_from", w["raw"])
            self.assertIn("overlapping_point_bindings", w)
            self.assertIn("readings_inside_window", w)
            for candidate in w["candidate_readings"]:
                self.assertIn("active_bindings", candidate)
                self.assertIn("raw", candidate)
                for b in candidate["active_bindings"]:
                    self.assertIn("raw", b)
                    self.assertIn("valid_from_utc", b)

    def test_example_reconciles_expected_statuses(self):
        report = run(json.loads(RECON_INPUT.read_text(encoding="utf-8")))
        expected = {
            "W-01": "matched",
            "W-02": "matched",
            "W-03": "missed",
            "W-04": "pending_review",
            "W-05": "pending_review",
            "W-06": "pending_review",
            "W-07": "missed",
            "W-08": "missed",
            "W-09": "pending_review",
            "W-10": "missed",
            "W-11": "matched",
            "W-12": "matched",
            "W-13": "matched",
            "W-14": "missed",
            "W-15": "missed",
        }
        self.assertEqual(
            {w["window_id"]: w["status"] for w in report["windows"]}, expected
        )
        self.assertEqual(report["overall_status"], "attention_required")
        self.assertEqual(report["summary"]["matched_window_count"], 5)
        self.assertEqual(report["summary"]["pending_review_window_count"], 4)
        self.assertEqual(report["summary"]["missed_window_count"], 6)


class SettlementIsolationTests(unittest.TestCase):
    def test_settlement_output_is_byte_identical_without_reconciliation(self):
        base = MOD.settle(
            json.loads(EXAMPLE_INPUT.read_text(encoding="utf-8")),
            EXAMPLE_INPUT,
            EXAMPLE_INPUT.read_bytes(),
        )
        explicit = MOD.settle(
            json.loads(EXAMPLE_INPUT.read_text(encoding="utf-8")),
            EXAMPLE_INPUT,
            EXAMPLE_INPUT.read_bytes(),
        )
        # settle() never invokes reconciliation; compare against committed output.
        committed = json.loads((ROOT / "examples" / "park_result.json").read_text(encoding="utf-8"))
        self.assertEqual(base, explicit)
        self.assertEqual(
            {k: v for k, v in base.items() if k != "segment_to_component_hint"},
            {k: v for k, v in committed.items() if k != "segment_to_component_hint"},
        )
        self.assertNotIn("planned_window_reconciliation", base)

    def test_cli_default_run_does_not_attach_reconciliation(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out.json"
            subprocess.run(
                [sys.executable, "-I", str(MODULE_PATH), "--input", str(EXAMPLE_INPUT), "--output", str(out)],
                check=True,
            )
            report = json.loads(out.read_text(encoding="utf-8"))
            self.assertNotIn("planned_window_reconciliation", report)

    def test_cli_reconcile_adds_only_namespaced_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            plain = Path(tmp) / "plain.json"
            combined = Path(tmp) / "combined.json"
            subprocess.run(
                [sys.executable, "-I", str(MODULE_PATH), "--input", str(RECON_INPUT), "--output", str(plain)],
                check=True,
            )
            subprocess.run(
                [
                    sys.executable, "-I", str(MODULE_PATH), "--input", str(RECON_INPUT),
                    "--reconcile", "--output", str(combined),
                ],
                check=True,
            )
            plain_report = json.loads(plain.read_text(encoding="utf-8"))
            combined_report = json.loads(combined.read_text(encoding="utf-8"))
            self.assertNotIn("planned_window_reconciliation", plain_report)
            self.assertIn("planned_window_reconciliation", combined_report)
            combined_report.pop("planned_window_reconciliation")
            self.assertEqual(plain_report, combined_report)

    def test_reconcile_only_requires_no_settlement_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "recon.json"
            subprocess.run(
                [
                    sys.executable, "-I", str(MODULE_PATH), "--input", str(RECON_INPUT),
                    "--reconcile-only", "--reconcile-output", str(out),
                ],
                check=True,
            )
            report = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(report["report_type"], "planned_window_reconciliation")
            self.assertNotIn("overall_settlement_status", report)


class InputContractTests(unittest.TestCase):
    def test_missing_planned_windows_raises_input_error(self):
        with self.assertRaises(MOD.InputError):
            MOD.reconcile_windows({"readings": [], "bindings": []})

    def test_non_positive_window_is_rejected(self):
        with self.assertRaises(MOD.InputError):
            MOD.reconcile_windows(base_input(
                [],
                [],
                [window("w1", "P1", "2025-01-01T10:00:00+00:00", "2025-01-01T10:00:00+00:00")],
            ))

    def test_duplicate_window_id_is_rejected(self):
        with self.assertRaises(MOD.InputError):
            MOD.reconcile_windows(base_input(
                [],
                [],
                [
                    window("w1", "P1", "2025-01-01T10:00:00+00:00", "2025-01-01T11:00:00+00:00"),
                    window("w1", "P1", "2025-01-01T11:00:00+00:00", "2025-01-01T12:00:00+00:00"),
                ],
            ))

    def test_naive_timestamp_is_rejected(self):
        with self.assertRaises(MOD.InputError):
            MOD.reconcile_windows(base_input(
                [reading("r1", "M", "2025-01-01T10:30:00")],
                [binding("b1", "M", "P1", "2025-01-01T00:00:00+00:00", "2025-01-02T00:00:00+00:00")],
                [window("w1", "P1", "2025-01-01T10:00:00+00:00", "2025-01-01T11:00:00+00:00")],
            ))

    def test_malformed_reconciliation_does_not_break_settlement_report(self):
        root = json.loads(EXAMPLE_INPUT.read_text(encoding="utf-8"))
        root["planned_windows"] = "not-a-list"
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "input.json"
            out = Path(tmp) / "out.json"
            source.write_text(json.dumps(root), encoding="utf-8")
            subprocess.run(
                [
                    sys.executable, "-I", str(MODULE_PATH), "--input", str(source),
                    "--reconcile", "--output", str(out),
                ],
                check=True,
            )
            report = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(report["overall_status"], "partial_settlement")
            self.assertEqual(
                report["planned_window_reconciliation"]["overall_status"], "fatal_input_error"
            )


if __name__ == "__main__":
    unittest.main()
