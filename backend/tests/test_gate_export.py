"""Tests for scripts/gate_export.py -- the Validation Gate results exporter.

Offline only: reads the committed evidence files under docs/evidence/ and
docs/gate/sample-results.json. No network, no model, no live database (the
autouse fixture in conftest.py already guarantees that for the whole suite).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import gate_export

REPO = Path(__file__).resolve().parents[2]
SAMPLE = REPO / "docs" / "gate" / "sample-results.json"

GATE_IDS = {
    "bundle_b_energy_mean_below_55",
    "bundle_b_paired_invariant",
    "bundle_b_phantom_zero_legitimate_exclusions",
    "bundle_a_piece2_bench_equals_clean",
}


def test_evidence_files_exist():
    assert gate_export.SCORE_TXT.exists(), gate_export.SCORE_TXT
    assert gate_export.PIECE2_JSON.exists(), gate_export.PIECE2_JSON


def test_parse_cal_energy_mean_reads_the_recorded_value():
    text = gate_export.SCORE_TXT.read_text(encoding="utf-8")
    got = gate_export.parse_cal_energy_mean(text)
    # docs/evidence/2026-09-13-meal-replay/score.txt line 56, ARM CAL section.
    assert got == {"n": 25, "mean_pct": 74.7, "median_pct": 40.9, "signed_pct": 31.9}


def test_parse_cal_energy_mean_raises_if_the_section_is_gone():
    with pytest.raises(ValueError):
        gate_export.parse_cal_energy_mean("nothing relevant in here")
    with pytest.raises(ValueError):
        gate_export.parse_cal_energy_mean("\nARM CAL\nnothing here\nARM UNCAL\n")


def test_parse_piece2_counts_identical_rows():
    obj = json.loads(gate_export.PIECE2_JSON.read_text(encoding="utf-8"))
    got = gate_export.parse_piece2(obj)
    # docs/evidence: 'BENCH == CLEAN on 16/16'.
    assert got == {"identical": 16, "total": 16}


def test_parse_piece2_counts_a_mismatch():
    obj = {"rows": [{"identical": True}, {"identical": False}, {"identical": True}]}
    assert gate_export.parse_piece2(obj) == {"identical": 2, "total": 3}


class TestBuildResults:
    @classmethod
    @pytest.fixture(scope="class")
    def payload(cls):
        return gate_export.build_results()

    def test_top_level_shape(self, payload):
        assert payload["schema_version"] == gate_export.SCHEMA_VERSION
        assert payload["generator"] == "backend/scripts/gate_export.py"
        assert isinstance(payload["generated_at"], str) and payload["generated_at"].endswith("Z")
        assert isinstance(payload["gates"], list) and len(payload["gates"]) == 4

    def test_repo_commit_and_branch_are_populated(self, payload):
        # Not asserting exact values -- this repo's HEAD moves -- only that the
        # exporter actually found git and did not silently fabricate a value.
        assert payload["repo"]["commit"] and len(payload["repo"]["commit"]) == 40
        assert payload["repo"]["branch"]

    def test_gate_ids_match_the_four_handoff_gates(self, payload):
        ids = {g["id"] for g in payload["gates"]}
        assert ids == GATE_IDS

    def test_measurable_gates_carry_a_value_and_no_excuse(self, payload):
        for g in payload["gates"]:
            if g["measurable"]:
                assert g["value"] is not None, g["id"]
                assert g["reason_not_measurable"] is None, g["id"]
                assert g["evidence"], f"{g['id']} claims measurable with no evidence citation"

    def test_not_measurable_gates_carry_no_fabricated_value(self, payload):
        for g in payload["gates"]:
            if not g["measurable"]:
                assert g["value"] is None, g["id"]
                assert g["reason_not_measurable"], f"{g['id']} is NOT_MEASURABLE with no reason given"

    def test_bundle_b_mean_gate_value(self, payload):
        g = next(x for x in payload["gates"] if x["id"] == "bundle_b_energy_mean_below_55")
        assert g["value"] == 74.7
        assert g["threshold"] == 55.0
        assert g["comparison"] == "lt"
        assert g["n"] == 25

    def test_bundle_a_piece2_gate_value(self, payload):
        g = next(x for x in payload["gates"] if x["id"] == "bundle_a_piece2_bench_equals_clean")
        assert g["value"] == 16
        assert g["threshold"] == 16
        assert g["n"] == 16

    def test_evidence_sha256_matches_the_file_on_disk(self, payload):
        for g in payload["gates"]:
            for ev in g["evidence"]:
                path = REPO / ev["path"]
                assert gate_export._sha256(path) == ev["sha256"], ev["path"]


def test_main_writes_valid_json_to_a_file(tmp_path):
    out = tmp_path / "results.json"
    rc = gate_export.main(["--out", str(out)])
    assert rc == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert {g["id"] for g in data["gates"]} == GATE_IDS


def test_main_prints_valid_json_to_stdout(capsys):
    rc = gate_export.main([])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert {g["id"] for g in data["gates"]} == GATE_IDS


def test_committed_sample_matches_the_schema_the_exporter_writes():
    """The sample file in docs/gate/ is a real export, not hand-written, so it
    cannot drift from what the script actually produces."""
    sample = json.loads(SAMPLE.read_text(encoding="utf-8"))
    fresh = gate_export.build_results()
    assert {g["id"] for g in sample["gates"]} == {g["id"] for g in fresh["gates"]} == GATE_IDS
    assert sample["schema_version"] == fresh["schema_version"]
    for s, f in zip(
        sorted(sample["gates"], key=lambda g: g["id"]),
        sorted(fresh["gates"], key=lambda g: g["id"]),
    ):
        assert s["value"] == f["value"], s["id"]
        assert s["measurable"] == f["measurable"], s["id"]
        assert s["threshold"] == f["threshold"], s["id"]
