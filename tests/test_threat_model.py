"""Threat-model tests — Y-010's acceptance.

The register (security/threat_register.json) must (a) validate
against the threat_record schema, (b) cover the four enemies the
roadmap names for Y-010, (c) map every mitigation to a real roadmap
slice, and (d) stay honest: at Y-010 no control may claim to be
built. Register data is paperwork — SIMULATED by nature; these tests
prove the model's shape and coverage, not any running defence.
"""

import json
import re
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
REGISTER = json.loads((ROOT / "security" / "threat_register.json").read_text())
SCHEMA = json.loads((ROOT / "contracts" / "schemas" / "threat_record.schema.json").read_text())
ROADMAP = (ROOT / "docs" / "YGGDRASIL_BUILD_ROADMAP_2026-10-30.md").read_text()

THREATS = REGISTER["threats"]

# The four enemies the roadmap names verbatim for Y-010.
ROADMAP_ENEMIES = {
    "Node impersonation",
    "Poisoned results",
    "Gossip and heartbeat spam",
    "Malicious tasks",
}


def test_schema_is_valid():
    Draft202012Validator.check_schema(SCHEMA)


def test_every_threat_validates():
    v = Draft202012Validator(SCHEMA)
    for threat in THREATS:
        assert list(v.iter_errors(threat)) == [], threat["threat_id"]


def test_ids_unique_and_sequential():
    ids = [t["threat_id"] for t in THREATS]
    assert len(ids) == len(set(ids)) >= 8
    assert ids == sorted(ids)


def test_roadmap_named_enemies_covered():
    assert ROADMAP_ENEMIES <= {t["name"] for t in THREATS}


def test_every_mitigation_slice_exists_in_roadmap():
    known = set(re.findall(r"Y-\d{3}", ROADMAP))
    for threat in THREATS:
        for mitigation in threat["mitigations"]:
            assert mitigation["built_by"] in known, (threat["threat_id"], mitigation)


def test_no_control_claims_built_yet():
    # Honesty discipline: controls are designed at Y-010, built later.
    assert all(t["status"] != "control_built" for t in THREATS)


def test_every_threat_states_residual_risk():
    for threat in THREATS:
        assert len(threat["residual_risk"]) >= 40, threat["threat_id"]
