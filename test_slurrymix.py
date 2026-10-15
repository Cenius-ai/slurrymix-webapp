"""Regression suite for Slurrymix.

Runs under the standard library (``python -m unittest -v``) and under pytest.
No network access, no third-party imports, every state file lives in a
temporary directory so the repository root is never touched.
"""

from __future__ import annotations

import contextlib
import csv
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import slurrymix

REPO_ROOT = Path(__file__).resolve().parent
FIELD_CMD = [
    "field",
    "add",
    "--name",
    "North 12",
    "--area",
    "4.5",
    "--crop",
    "wheat",
    "--target-n",
    "170",
]
BATCH_CMD = ["batch", "add", "--label", "Pit A March", "--source", "cattle", "--n", "4.2", "--dm", "8.5"]


class CliTestCase(unittest.TestCase):
    """Base case: every command runs in a throwaway working directory."""

    def setUp(self) -> None:
        self._cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)
        self.addCleanup(self._cleanup)
        self.state = str(Path(self._tmp.name) / "state.json")

    def _cleanup(self) -> None:
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def invoke(self, argv: list[str], *, use_state: bool = True) -> tuple[int, str, str]:
        command = (["--state", self.state] if use_state else []) + list(argv)
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = slurrymix.main(command)
        return code, stdout.getvalue(), stderr.getvalue()

    def ok(self, *argv: str, use_state: bool = True) -> tuple[str, str]:
        code, stdout, stderr = self.invoke(list(argv), use_state=use_state)
        self.assertEqual(code, 0, msg=f"expected success, got {code}: out={stdout!r} err={stderr!r}")
        return stdout, stderr

    def fails(self, expected_code: int, *argv: str, use_state: bool = True) -> tuple[str, str]:
        code, stdout, stderr = self.invoke(list(argv), use_state=use_state)
        self.assertEqual(code, expected_code, msg=f"expected {expected_code}: out={stdout!r} err={stderr!r}")
        return stdout, stderr

    def seed_registers(self) -> None:
        self.ok(*FIELD_CMD)
        self.ok(*BATCH_CMD)
        self.ok("storage", "add", "--name", "Lagoon 1", "--capacity", "1200")

    def read_state(self) -> dict:
        return json.loads(Path(self.state).read_text(encoding="utf-8"))


class StateStoreTests(CliTestCase):
    def test_read_command_without_state_file_exits_clean_and_creates_nothing(self) -> None:
        stdout, _ = self.ok("status", use_state=False)
        self.assertIn("first run checklist", stdout)
        self.assertFalse(Path("slurrymix.json").exists(), "a read command must not create the state file")

    def test_corrupt_state_reports_friendly_message_and_leaves_file_alone(self) -> None:
        Path(self.state).write_text("{not json at all", encoding="utf-8")
        _stdout, stderr = self.fails(1, "status")
        self.assertIn("is not valid JSON", stderr)
        self.assertIn(self.state, stderr)
        self.assertEqual(Path(self.state).read_text(encoding="utf-8"), "{not json at all")

    def test_newer_schema_version_is_refused(self) -> None:
        Path(self.state).write_text(json.dumps({"schema_version": 2, "fields": []}), encoding="utf-8")
        _stdout, stderr = self.fails(1, "status")
        self.assertIn("state file schema version 2 is newer than this tool (1)", stderr)

    def test_hand_edited_state_is_loaded(self) -> None:
        Path(self.state).write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "units": "metric",
                    "fields": [
                        {
                            "id": 7,
                            "name": "Hand Added",
                            "area_ha": 2.0,
                            "crop": "grass",
                            "target_n_kg_per_ha": 140,
                        }
                    ],
                    "batches": [],
                    "storages": [],
                    "plan": [],
                    "applications": [],
                }
            ),
            encoding="utf-8",
        )
        stdout, _ = self.ok("field", "list")
        self.assertIn("Hand Added", stdout)
        self.assertIn("140", stdout)

    def test_hand_edited_state_with_bad_number_is_reported_not_crashed(self) -> None:
        Path(self.state).write_text(
            json.dumps({"schema_version": 1, "batches": [{"id": 1, "label": "X", "source": "cattle", "n_kg_per_m3": 0}]}),
            encoding="utf-8",
        )
        _stdout, stderr = self.fails(1, "batch", "list")
        self.assertIn("n_kg_per_m3", stderr)
        self.assertIn("must be at least", stderr)

    def test_dangling_plan_reference_is_reported(self) -> None:
        Path(self.state).write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "plan": [
                        {
                            "id": 1,
                            "field_id": 99,
                            "batch_id": 1,
                            "storage_id": 1,
                            "planned_date": "2026-03-14",
                            "volume_m3": 10,
                            "n_kg": 42,
                            "status": "planned",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        _stdout, stderr = self.fails(1, "plan")
        self.assertIn("references field #99", stderr)

    def test_state_override_does_not_touch_the_working_directory_file(self) -> None:
        Path("slurrymix.json").write_text(json.dumps({"schema_version": 1, "fields": []}), encoding="utf-8")
        before = Path("slurrymix.json").read_text(encoding="utf-8")
        self.ok(*FIELD_CMD)  # written to --state, not ./slurrymix.json
        self.assertEqual(Path("slurrymix.json").read_text(encoding="utf-8"), before)
        self.assertTrue(Path(self.state).exists())
        self.assertEqual(self.read_state()["fields"][0]["name"], "North 12")

    def test_state_file_is_readable_json_with_schema_version(self) -> None:
        self.ok(*FIELD_CMD)
        payload = self.read_state()
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["units"], "metric")
        self.assertIn("updated_at", payload)
        self.assertEqual(payload["fields"][0]["area_ha"], 4.5)

    def test_failed_publish_keeps_the_previous_complete_file(self) -> None:
        self.ok(*FIELD_CMD)
        before = Path(self.state).read_text(encoding="utf-8")
        with mock.patch("slurrymix.os.replace", side_effect=OSError("disk gone")):
            _stdout, stderr = self.fails(1, "field", "add", "--name", "South 3", "--area", "2", "--crop", "grass", "--target-n", "150")
        self.assertIn("cannot write state file", stderr)
        self.assertEqual(Path(self.state).read_text(encoding="utf-8"), before)
        leftovers = [name for name in os.listdir(self._tmp.name) if name.endswith(".tmp")]
        self.assertEqual(leftovers, [], "the temporary file must be cleaned up on failure")

    def test_state_file_is_written_to_a_subdirectory_when_asked(self) -> None:
        nested = str(Path(self._tmp.name) / "data" / "farm.json")
        stdout, _ = self.ok("--state", nested, "field", "list")
        self.assertIn("no fields yet", stdout)
        code, _stdout, _stderr = self.invoke_with_state(nested, *FIELD_CMD)
        self.assertEqual(code, 0)
        self.assertTrue(Path(nested).exists())

    def invoke_with_state(self, state: str, *argv: str) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = slurrymix.main(["--state", state, *argv])
        return code, stdout.getvalue(), stderr.getvalue()


class CliShellTests(CliTestCase):
    def test_help_lists_every_command(self) -> None:
        stdout, _ = self.ok("--help")
        for command in ("mix", "field", "batch", "plan", "storage", "status", "export", "wizard"):
            self.assertIn(command, stdout)

    def test_bare_invocation_prints_help_and_exits_zero(self) -> None:
        stdout, _ = self.ok(use_state=False)
        self.assertIn("usage: slurrymix", stdout)

    def test_version_flag(self) -> None:
        stdout, _ = self.ok("--version", use_state=False)
        self.assertIn(slurrymix.VERSION, stdout)

    def test_unknown_command_exits_two(self) -> None:
        _stdout, stderr = self.fails(2, "frobnicate", use_state=False)
        self.assertIn("unknown command 'frobnicate'", stderr)

    def test_unknown_flag_exits_two(self) -> None:
        _stdout, stderr = self.fails(2, "status", "--nope")
        self.assertIn("unknown flag", stderr)

    def test_non_numeric_flag_names_the_flag(self) -> None:
        _stdout, stderr = self.fails(2, "mix", "--area", "abc", use_state=False)
        self.assertIn("area must be a number", stderr)

    def test_comma_decimal_separator_is_rejected_with_a_hint(self) -> None:
        _stdout, stderr = self.fails(2, "mix", "--n", "4,2", "--area", "3", "--target", "170", use_state=False)
        self.assertIn("n must be a number - use a dot as the decimal separator", stderr)

    def test_missing_required_flag_names_the_flag(self) -> None:
        _stdout, stderr = self.fails(2, "field", "add", "--name", "X", "--area", "1", "--crop", "grass")
        self.assertIn("target-n", stderr)

    def test_help_of_a_subcommand_exits_zero(self) -> None:
        stdout, _ = self.ok("plan", "add", "--help")
        self.assertIn("--volume", stdout)


class MixTests(CliTestCase):
    def test_inline_mix_prints_volume_rate_and_balance(self) -> None:
        stdout, _ = self.ok("mix", "--n", "4.2", "--area", "3", "--target", "170")
        self.assertIn("121.4 m3", stdout)
        self.assertIn("40.5 m3/ha", stdout)
        self.assertIn("kg N/m3", stdout)
        self.assertIn("kg N/ha", stdout)
        self.assertIn("510 kg N", stdout)

    def test_delivered_nitrogen_matches_volume_times_concentration(self) -> None:
        stdout, _ = self.ok("mix", "--n", "4.2", "--area", "3", "--target", "170")
        volume = 121.4
        self.assertIn(slurrymix.num(round(volume * 4.2, 1)), stdout)

    def test_zero_nitrogen_is_rejected(self) -> None:
        _stdout, stderr = self.fails(2, "mix", "--n", "0", "--area", "3", "--target", "170", use_state=False)
        self.assertIn("n must be greater than 0", stderr)

    def test_negative_area_is_rejected(self) -> None:
        _stdout, stderr = self.fails(2, "mix", "--n", "4.2", "--area", "-3", "--target", "170", use_state=False)
        self.assertIn("area must be greater than 0", stderr)

    def test_missing_target_names_the_flag(self) -> None:
        _stdout, stderr = self.fails(2, "mix", "--n", "4.2", "--area", "3", use_state=False)
        self.assertIn("target is required", stderr)

    def test_bad_date_is_rejected_without_a_traceback(self) -> None:
        _stdout, stderr = self.fails(2, "mix", "--n", "4.2", "--area", "3", "--target", "170", "--date", "yesterday", use_state=False)
        self.assertIn("date must be YYYY-MM-DD", stderr)

    def test_mix_uses_a_stored_field_and_batch(self) -> None:
        self.seed_registers()
        stdout, _ = self.ok("mix", "--field", "North 12", "--batch", "Pit A March")
        self.assertIn("182.1 m3", stdout)
        self.assertIn("40.5 m3/ha", stdout)
        self.assertIn("remaining season budget for North 12", stdout)
        self.assertIn("0 kg N", stdout)

    def test_unknown_field_lists_the_available_names(self) -> None:
        self.seed_registers()
        _stdout, stderr = self.fails(1, "mix", "--field", "South 3", "--batch", "Pit A March")
        self.assertIn("no field named 'South 3'", stderr)
        self.assertIn("North 12", stderr)

    def test_stored_batch_overrides_an_inline_nitrogen_value(self) -> None:
        self.seed_registers()
        stdout, _ = self.ok("mix", "--field", "North 12", "--batch", "Pit A March", "--n", "9.9")
        self.assertIn("overrode --n", stdout)
        self.assertIn("4.2 kg N/m3", stdout)
        self.assertNotIn("9.9", stdout)

    def test_mix_json_is_one_object(self) -> None:
        stdout, _ = self.ok("mix", "--n", "4.2", "--area", "3", "--target", "170", "--json", use_state=False)
        payload = json.loads(stdout)
        self.assertEqual(payload["required_volume_m3"], 121.4)
        self.assertEqual(payload["target_n_kg"], 510.0)
        self.assertEqual(payload["units"], "metric")


class FieldRegisterTests(CliTestCase):
    def test_add_then_list(self) -> None:
        start, _ = self.ok(*FIELD_CMD)
        self.assertIn("North 12", start)
        stdout, _ = self.ok("field", "list")
        self.assertIn("North 12", stdout)
        self.assertIn("4.5", stdout)
        self.assertIn("wheat", stdout)
        self.assertIn("170", stdout)

    def test_bare_field_command_lists(self) -> None:
        self.ok(*FIELD_CMD)
        stdout, _ = self.ok("field")
        self.assertIn("North 12", stdout)

    def test_duplicate_name_is_refused_without_changing_the_file(self) -> None:
        self.ok(*FIELD_CMD)
        before = Path(self.state).read_text(encoding="utf-8")
        _stdout, stderr = self.fails(1, *FIELD_CMD)
        self.assertIn("field 'North 12' already exists", stderr)
        self.assertEqual(Path(self.state).read_text(encoding="utf-8"), before)

    def test_empty_state_invites_field_add(self) -> None:
        stdout, _ = self.ok("field", "list")
        self.assertIn("no fields yet", stdout)
        self.assertIn("slurrymix field add", stdout)

    def test_out_of_range_area_names_the_value(self) -> None:
        _stdout, stderr = self.fails(2, "field", "add", "--name", "X", "--area", "0", "--crop", "grass", "--target-n", "150")
        self.assertIn("area must be greater than 0", stderr)

    def test_field_list_json(self) -> None:
        self.ok(*FIELD_CMD)
        stdout, _ = self.ok("field", "list", "--json")
        payload = json.loads(stdout)
        self.assertEqual(payload["fields"][0]["name"], "North 12")


class BatchRegisterTests(CliTestCase):
    def test_add_then_list(self) -> None:
        self.ok(*BATCH_CMD)
        stdout, _ = self.ok("batch", "list")
        self.assertIn("Pit A March", stdout)
        self.assertIn("cattle", stdout)
        self.assertIn("4.2", stdout)
        self.assertIn("8.5", stdout)

    def test_dry_matter_above_100_is_rejected(self) -> None:
        _stdout, stderr = self.fails(2, "batch", "add", "--label", "X", "--source", "cattle", "--n", "4.2", "--dm", "120")
        self.assertIn("dm must be between 0 and 100", stderr)

    def test_negative_nitrogen_is_rejected_and_not_written(self) -> None:
        _stdout, stderr = self.fails(2, "batch", "add", "--label", "X", "--source", "cattle", "--n", "-1")
        self.assertIn("n must be greater than 0", stderr)
        self.assertFalse(Path(self.state).exists())

    def test_unknown_source_is_rejected(self) -> None:
        _stdout, stderr = self.fails(2, "batch", "add", "--label", "X", "--source", "unicorn", "--n", "4.2")
        self.assertIn("source must be one of cattle, pig, poultry, other", stderr)

    def test_duplicate_label_is_refused(self) -> None:
        self.ok(*BATCH_CMD)
        _stdout, stderr = self.fails(1, *BATCH_CMD)
        self.assertIn("batch 'Pit A March' already exists", stderr)

    def test_empty_state_invites_batch_add(self) -> None:
        stdout, _ = self.ok("batch", "list")
        self.assertIn("no slurry batches yet", stdout)
        self.assertIn("slurrymix batch add", stdout)

    def test_sampling_date_must_be_iso(self) -> None:
        _stdout, stderr = self.fails(
            2, "batch", "add", "--label", "X", "--source", "cattle", "--n", "4.2", "--sampled", "02-03-2026"
        )
        self.assertIn("sampled must be YYYY-MM-DD", stderr)


class StorageTrackerTests(CliTestCase):
    def test_new_unit_starts_empty(self) -> None:
        self.ok("storage", "add", "--name", "Lagoon 1", "--capacity", "1200")
        stdout, _ = self.ok("storage")
        self.assertIn("Lagoon 1", stdout)
        self.assertIn("0 m3", stdout)
        self.assertIn("1200 m3", stdout)
        self.assertIn("0%", stdout)

    def test_level_shows_free_space_and_percent_bar(self) -> None:
        self.ok("storage", "add", "--name", "Lagoon 1", "--capacity", "1200")
        self.ok("storage", "set", "--name", "Lagoon 1", "--level", "950")
        stdout, _ = self.ok("storage")
        self.assertIn("950 m3", stdout)
        self.assertIn("250 m3", stdout)
        self.assertIn("79%", stdout)

    def test_over_capacity_warns_on_stderr_but_still_stores(self) -> None:
        self.ok("storage", "add", "--name", "Lagoon 1", "--capacity", "1200")
        stdout, stderr = self.ok("storage", "set", "--name", "Lagoon 1", "--level", "1300")
        self.assertIn("Lagoon 1 is over capacity by 100 m3 (108%)", stderr)
        self.assertIn("-100 m3 free", stdout)  # free space goes negative, nothing is clamped
        self.assertEqual(self.read_state()["storages"][0]["level_m3"], 1300.0)

    def test_negative_level_is_rejected(self) -> None:
        self.ok("storage", "add", "--name", "Lagoon 1", "--capacity", "1200")
        _stdout, stderr = self.fails(2, "storage", "set", "--name", "Lagoon 1", "--level", "-5")
        self.assertIn("level must be 0 or greater", stderr)

    def test_unknown_unit_lists_available_names(self) -> None:
        self.ok("storage", "add", "--name", "Lagoon 1", "--capacity", "1200")
        _stdout, stderr = self.fails(1, "storage", "set", "--name", "Lagoon 2", "--level", "10")
        self.assertIn("no storage named 'Lagoon 2'", stderr)
        self.assertIn("Lagoon 1", stderr)

    def test_empty_state_invites_storage_add(self) -> None:
        stdout, _ = self.ok("storage")
        self.assertIn("no storage units yet", stdout)
        self.assertIn("slurrymix storage add", stdout)

    def test_storage_json_reports_percent_and_days(self) -> None:
        self.seed_registers()
        self.ok("storage", "set", "--name", "Lagoon 1", "--level", "600")
        stdout, _ = self.ok("storage", "--json")
        payload = json.loads(stdout)
        unit = payload["storages"][0]
        self.assertEqual(unit["percent_full"], 50.0)
        self.assertEqual(unit["free_m3"], 600.0)
        self.assertFalse(unit["at_capacity"])


class PlanLifecycleTests(CliTestCase):
    def test_planned_entry_is_listed_by_date(self) -> None:
        self.seed_registers()
        self.ok("plan", "add", "--field", "North 12", "--batch", "Pit A March", "--storage", "Lagoon 1", "--date", "2026-03-14", "--volume", "180")
        stdout, _ = self.ok("plan")
        self.assertIn("2026-03-14", stdout)
        self.assertIn("North 12", stdout)
        self.assertIn("180 m3", stdout)
        self.assertIn("756 kg N", stdout)
        self.assertIn("planned", stdout)

    def test_entries_are_ordered_by_date_not_insertion(self) -> None:
        self.seed_registers()
        for planned_date, volume in (("2026-05-02", "60"), ("2026-04-01", "40")):
            self.ok(
                "plan", "add", "--field", "North 12", "--batch", "Pit A March",
                "--storage", "Lagoon 1", "--date", planned_date, "--volume", volume,
            )
        stdout, _ = self.ok("plan")
        lines = [line for line in stdout.splitlines() if "2026-" in line]
        self.assertEqual(len(lines), 2)
        self.assertLess(lines[0].index("2026-04-01"), lines[0].index("2026-05-02") if "2026-05-02" in lines[0] else 10**6)

    def test_volume_is_calculated_when_omitted(self) -> None:
        self.seed_registers()
        stdout, _ = self.ok("plan", "add", "--field", "North 12", "--batch", "Pit A March", "--storage", "Lagoon 1", "--date", "2026-03-14")
        self.assertIn("calculated", stdout)
        self.assertIn("182.1 m3", stdout)

    def test_malformed_date_is_rejected(self) -> None:
        self.seed_registers()
        _stdout, stderr = self.fails(
            2, "plan", "add", "--field", "North 12", "--batch", "Pit A March",
            "--storage", "Lagoon 1", "--date", "14/03/2026",
        )
        self.assertIn("date must be YYYY-MM-DD", stderr)

    def test_over_budget_entry_warns_and_is_still_stored(self) -> None:
        self.ok("field", "add", "--name", "North 12", "--area", "2", "--crop", "wheat", "--target-n", "105")
        self.ok(*BATCH_CMD)
        self.ok("storage", "add", "--name", "Lagoon 1", "--capacity", "1200")
        # 170 kg N already applied leaves 40 kg N of the 210 kg N season budget.
        state = self.read_state()
        state["applications"] = [
            {"id": 1, "entry_id": 1, "field_id": 1, "batch_id": 1, "applied_on": "2026-02-01", "volume_m3": 40.5, "n_applied_kg": 170}
        ]
        Path(self.state).write_text(json.dumps(state), encoding="utf-8")
        _stdout, stderr = self.ok(
            "plan", "add", "--field", "North 12", "--batch", "Pit A March",
            "--storage", "Lagoon 1", "--date", "2026-03-14", "--volume", "21.4",
        )
        self.assertIn("North 12 exceeds its remaining 40 kg N this season by 49.9 kg N", stderr)
        self.assertEqual(len(self.read_state()["plan"]), 1)

    def test_unknown_storage_lists_available_names(self) -> None:
        self.seed_registers()
        _stdout, stderr = self.fails(
            1, "plan", "add", "--field", "North 12", "--batch", "Pit A March",
            "--storage", "Lagoon 9", "--date", "2026-03-14",
        )
        self.assertIn("no storage named 'Lagoon 9'", stderr)
        self.assertIn("Lagoon 1", stderr)

    def test_non_positive_volume_is_rejected(self) -> None:
        self.seed_registers()
        _stdout, stderr = self.fails(
            2, "plan", "add", "--field", "North 12", "--batch", "Pit A March",
            "--storage", "Lagoon 1", "--date", "2026-03-14", "--volume", "0",
        )
        self.assertIn("volume must be greater than 0", stderr)

    def test_remove_planned_entry(self) -> None:
        self.seed_registers()
        self.ok("plan", "add", "--field", "North 12", "--batch", "Pit A March", "--storage", "Lagoon 1", "--date", "2026-03-14", "--volume", "180")
        self.ok("plan", "remove", "--id", "1")
        stdout, _ = self.ok("plan")
        self.assertIn("no planned spreads", stdout)
        self.assertEqual(self.read_state()["plan"], [])

    def test_remove_with_unknown_id_is_a_domain_error(self) -> None:
        self.seed_registers()
        _stdout, stderr = self.fails(1, "plan", "remove", "--id", "3")
        self.assertIn("no plan entry with id 3", stderr)

    def test_empty_state_invites_plan_add(self) -> None:
        stdout, _ = self.ok("plan")
        self.assertIn("no planned spreads yet", stdout)
        self.assertIn("slurrymix plan add", stdout)

    def test_plan_json_lists_entries_and_counts(self) -> None:
        self.seed_registers()
        self.ok("plan", "add", "--field", "North 12", "--batch", "Pit A March", "--storage", "Lagoon 1", "--date", "2026-03-14", "--volume", "180")
        stdout, _ = self.ok("plan", "--json")
        payload = json.loads(stdout)
        self.assertEqual(payload["planned"], 1)
        self.assertEqual(payload["completed"], 0)
        self.assertEqual(payload["plan"][0]["volume_m3"], 180.0)


class SpreadCompletionTests(CliTestCase):
    def seed_plan(self, level: float = 950.0, volume: str = "180") -> None:
        self.seed_registers()
        self.ok("storage", "set", "--name", "Lagoon 1", "--level", str(level))
        self.ok("plan", "add", "--field", "North 12", "--batch", "Pit A March", "--storage", "Lagoon 1", "--date", "2026-03-14", "--volume", volume)

    def test_completion_records_the_application_and_draws_storage_down(self) -> None:
        self.seed_plan()
        stdout, _ = self.ok("plan", "done", "--id", "1")
        self.assertIn("entry #1 done", stdout)
        storage_out, _ = self.ok("storage")
        self.assertIn("770 m3", storage_out)
        plan_out, _ = self.ok("plan")
        self.assertIn("no planned spreads", plan_out)
        state = self.read_state()
        self.assertEqual(state["plan"][0]["status"], "done")
        self.assertEqual(state["applications"][0]["n_applied_kg"], 756.0)
        self.assertEqual(state["applications"][0]["applied_on"], "2026-03-14")

    def test_completed_entry_is_visible_with_all(self) -> None:
        self.seed_plan()
        self.ok("plan", "done", "--id", "1")
        stdout, _ = self.ok("plan", "--all")
        self.assertIn("done", stdout)
        self.assertIn("180", stdout)

    def test_status_counts_the_applied_nitrogen(self) -> None:
        self.seed_plan()
        self.ok("plan", "done", "--id", "1")
        stdout, _ = self.ok("status")
        self.assertIn("756 kg N", stdout)
        self.assertIn("applications", stdout)

    def test_completing_twice_is_refused_and_changes_nothing(self) -> None:
        self.seed_plan()
        self.ok("plan", "done", "--id", "1")
        before = Path(self.state).read_text(encoding="utf-8")
        _stdout, stderr = self.fails(1, "plan", "done", "--id", "1")
        self.assertIn("plan entry 1 is already done", stderr)
        self.assertEqual(Path(self.state).read_text(encoding="utf-8"), before)

    def test_completion_short_of_stock_clamps_to_zero_and_warns(self) -> None:
        self.seed_plan(level=100.0, volume="180")
        _stdout, stderr = self.ok("plan", "done", "--id", "1")
        self.assertIn("level set to 0 m3", stderr)
        self.assertEqual(self.read_state()["storages"][0]["level_m3"], 0.0)

    def test_completed_entry_cannot_be_removed(self) -> None:
        self.seed_plan()
        self.ok("plan", "done", "--id", "1")
        _stdout, stderr = self.fails(1, "plan", "remove", "--id", "1")
        self.assertIn("already done and cannot be removed", stderr)


class StatusSummaryTests(CliTestCase):
    def seed_full(self) -> None:
        self.seed_registers()
        self.ok("field", "add", "--name", "South 3", "--area", "2", "--crop", "grass", "--target-n", "120")
        self.ok("storage", "set", "--name", "Lagoon 1", "--level", "600")
        self.ok("plan", "add", "--field", "North 12", "--batch", "Pit A March", "--storage", "Lagoon 1", "--date", "2026-03-14", "--volume", "180")
        self.ok("plan", "add", "--field", "South 3", "--batch", "Pit A March", "--storage", "Lagoon 1", "--date", "2026-03-18", "--volume", "60")
        self.ok("plan", "done", "--id", "1")

    def test_one_screen_shows_everything_asked_for(self) -> None:
        self.seed_full()
        stdout, _ = self.ok("status")
        self.assertIn("fields", stdout)
        self.assertIn("batches", stdout)
        self.assertIn("storages", stdout)
        self.assertIn("plan entries", stdout)
        self.assertIn("planned volume", stdout)
        self.assertIn("North 12", stdout)
        self.assertIn("South 3", stdout)
        self.assertIn("REMAINING (kg N)", stdout)
        self.assertIn("PERCENT FULL", stdout)
        self.assertIn("DAYS OF SPREADING", stdout)

    def test_days_of_spreading_are_estimated_from_the_planned_rate(self) -> None:
        self.seed_full()
        stdout, _ = self.ok("status")
        # 420 m3 left, 60 m3 still planned -> 60 m3/day -> 7 days of spreading
        self.assertIn("7 days at 60 m3/day", stdout)

    def test_status_json_has_the_documented_keys(self) -> None:
        self.seed_full()
        stdout, _ = self.ok("status", "--json")
        payload = json.loads(stdout)
        for key in ("fields", "batches", "storages", "plan", "totals"):
            self.assertIn(key, payload)
        self.assertEqual(payload["totals"]["fields"], 2)
        self.assertEqual(payload["totals"]["planned_entries"], 1)
        self.assertEqual(payload["totals"]["done_entries"], 1)

    def test_empty_state_prints_the_first_run_checklist_in_order(self) -> None:
        stdout, _ = self.ok("status")
        field_at = stdout.index("slurrymix field add")
        batch_at = stdout.index("slurrymix batch add")
        storage_at = stdout.index("slurrymix storage add")
        self.assertLess(field_at, batch_at)
        self.assertLess(batch_at, storage_at)


class ExportTests(CliTestCase):
    def seed_export_data(self) -> None:
        self.seed_registers()
        self.ok("field", "add", "--name", 'East 4, "the wet one"', "--area", "3", "--crop", "barley", "--target-n", "150")
        self.ok("storage", "set", "--name", "Lagoon 1", "--level", "300")
        self.ok("plan", "add", "--field", 'East 4, "the wet one"', "--batch", "Pit A March", "--storage", "Lagoon 1", "--date", "2026-03-20", "--volume", "90")

    def test_export_writes_both_files_with_headers(self) -> None:
        self.seed_export_data()
        stdout, _ = self.ok("export", "--out", ".")
        self.assertIn("plan.csv", stdout)
        self.assertIn("storage.csv", stdout)
        with Path("plan.csv").open(encoding="utf-8", newline="") as handle:
            rows = list(csv.reader(handle))
        self.assertEqual(rows[0], slurrymix.PLAN_HEADER)
        self.assertEqual(len(rows), 2)
        with Path("storage.csv").open(encoding="utf-8", newline="") as handle:
            storage_rows = list(csv.reader(handle))
        self.assertEqual(storage_rows[0], slurrymix.STORAGE_HEADER)
        self.assertEqual(storage_rows[1][1], "Lagoon 1")

    def test_existing_file_is_refused_without_force(self) -> None:
        self.seed_export_data()
        self.ok("export", "--out", ".")
        before = Path("plan.csv").read_text(encoding="utf-8")
        _stdout, stderr = self.fails(1, "export", "--out", ".")
        self.assertIn("plan.csv exists; pass --force to overwrite", stderr)
        self.assertEqual(Path("plan.csv").read_text(encoding="utf-8"), before)

    def test_force_overwrites(self) -> None:
        self.seed_export_data()
        self.ok("export", "--out", ".")
        self.ok("export", "--out", ".", "--force")
        self.assertTrue(Path("plan.csv").exists())

    def test_names_with_commas_and_quotes_survive_the_round_trip(self) -> None:
        self.seed_export_data()
        self.ok("export", "--out", ".")
        with Path("plan.csv").open(encoding="utf-8", newline="") as handle:
            rows = list(csv.reader(handle))
        self.assertEqual(rows[1][2], 'East 4, "the wet one"')

    def test_export_into_a_missing_directory_is_created(self) -> None:
        self.seed_export_data()
        self.ok("export", "--out", "reports")
        self.assertTrue(Path("reports", "plan.csv").exists())


class WizardTests(CliTestCase):
    def type_answers(self, answers: list[str]):
        stream = iter(answers)

        def fake_input(prompt: str = "") -> str:
            try:
                return next(stream)
            except StopIteration:
                raise EOFError from None

        return mock.patch("builtins.input", side_effect=fake_input)

    def test_enter_through_every_prompt_writes_one_batch_and_one_entry(self) -> None:
        self.seed_registers()
        with self.type_answers([""] * 8):
            stdout, _ = self.ok("wizard")
        state = self.read_state()
        self.assertEqual(len(state["batches"]), 2)
        self.assertEqual(len(state["plan"]), 1)
        self.assertEqual(state["plan"][0]["status"], "planned")
        self.assertIn("Storage levels", stdout)

    def test_interrupt_writes_nothing_and_exits_130(self) -> None:
        self.seed_registers()
        calls = {"count": 0}

        def fake_input(prompt: str = "") -> str:
            calls["count"] += 1
            if calls["count"] == 2:
                raise KeyboardInterrupt
            return ""

        with mock.patch("builtins.input", side_effect=fake_input):
            code, _stdout, _stderr = self.invoke(["wizard"])
        self.assertEqual(code, 130)
        self.assertEqual(len(self.read_state()["batches"]), 1, "no partial batch may be written")

    def test_input_ending_early_exits_130_without_writing(self) -> None:
        self.seed_registers()
        with self.type_answers([""]):
            code, _stdout, stderr = self.invoke(["wizard"])
        self.assertEqual(code, 130)
        self.assertIn("nothing was written", stderr)
        self.assertEqual(len(self.read_state()["batches"]), 1)

    def test_out_of_range_dry_matter_reasks_with_the_same_message(self) -> None:
        self.seed_registers()
        answers = ["", "", "", "150", "8.5", "", "", "", ""]
        with self.type_answers(answers):
            _stdout, stderr = self.ok("wizard")
        self.assertIn("dm must be between 0 and 100", stderr)
        self.assertEqual(self.read_state()["batches"][1]["dry_matter_pct"], 8.5)

    def test_wizard_without_fields_skips_the_spread_step(self) -> None:
        with self.type_answers([""] * 4):
            stdout, _ = self.ok("wizard")
        self.assertIn("skipping the spread step", stdout)
        state = self.read_state()
        self.assertEqual(len(state["batches"]), 1)
        self.assertEqual(state["plan"], [])


class ProcessContractTests(CliTestCase):
    """The shipped file has to work as a real process, not only in-process."""

    def run_process(self, args: list[str], stdin: str = "") -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(REPO_ROOT / "slurrymix.py"), *args],
            input=stdin,
            capture_output=True,
            text=True,
            cwd=self._tmp.name,
            check=False,
        )

    def test_help_exits_zero_from_a_clean_checkout(self) -> None:
        result = self.run_process(["--help"])
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("usage: slurrymix", result.stdout)
        self.assertFalse(Path("slurrymix.json").exists())

    def test_full_loop_through_the_process_interface(self) -> None:
        for args in (
            ["field", "add", "--name", "North 12", "--area", "4.5", "--crop", "wheat", "--target-n", "170"],
            ["batch", "add", "--label", "Pit A March", "--source", "cattle", "--n", "4.2"],
            ["storage", "add", "--name", "Lagoon 1", "--capacity", "1200"],
            ["storage", "set", "--name", "Lagoon 1", "--level", "600"],
            ["plan", "add", "--field", "North 12", "--batch", "Pit A March", "--storage", "Lagoon 1", "--date", "2026-03-14"],
            ["plan", "done", "--id", "1"],
            ["status"],
        ):
            result = self.run_process(args)
            self.assertEqual(result.returncode, 0, msg=f"{args}: {result.stderr}")
            self.assertNotIn("Traceback", result.stderr)
        self.assertTrue(Path("slurrymix.json").exists())

    def test_bad_input_never_prints_a_traceback(self) -> None:
        result = self.run_process(["mix", "--n", "abc"])
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn("n must be a number", result.stderr)

    def test_wizard_runs_non_interactively_from_piped_input(self) -> None:
        self.run_process(["field", "add", "--name", "North 12", "--area", "4.5", "--crop", "wheat", "--target-n", "170"])
        self.run_process(["batch", "add", "--label", "Pit A March", "--source", "cattle", "--n", "4.2"])
        self.run_process(["storage", "add", "--name", "Lagoon 1", "--capacity", "1200"])
        result = self.run_process(["wizard"], stdin="\n" * 8)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("Storage levels", result.stdout)
        payload = json.loads(Path("slurrymix.json").read_text(encoding="utf-8"))
        self.assertEqual(len(payload["batches"]), 2)
        self.assertEqual(len(payload["plan"]), 1)


def tearDownModule() -> None:
    """The suite must never leave a state file in the repository."""
    assert not (REPO_ROOT / "slurrymix.json").exists(), "the test suite wrote slurrymix.json in the repository root"


if __name__ == "__main__":
    unittest.main(verbosity=2)
