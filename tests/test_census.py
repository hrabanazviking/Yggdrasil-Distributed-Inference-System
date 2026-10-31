"""Capability census tests — Y-022's acceptance.

The census must prove: (a) measurement is real — the system
probes read this machine into a validated census, and a fake
filesystem root shaped like another machine (GPU + Hailo +
thermal sensor) is read just as exactly, so "measured, not
assumed" is tested, not incanted; (b) honesty of absence — a
machine with no sensor and no accelerator reports no thermal
field and exactly one ``none`` accelerator, and detection
never sets ``proven`` (presence is not a proven run); (c)
unshaped or impossible readings are refused by name (unknown
accelerator kind, 'none' beside real devices, a failing probe,
an unvalidated engine entry); (d) refresh-on-change — a
collector re-measures, a timestamp alone is not a change, and
a changed reading is named by field, in the diff, the
fingerprint, and the node agent's ``node.census_changed``
event; (e) the agent integration — ``census()`` measures on
first asking, ``status()`` carries the census only once
measured, and the founding README is untouched.
"""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from yggdrasil.census import (
    CensusCollector,
    CensusError,
    ProbeSet,
    census_fingerprint,
    diff_censuses,
    measure_census,
    system_probe_set,
    validate_census,
)
from yggdrasil.identity import LocalKeyStore, NodeKeypair, create_mesh_root, issue_node_certificate
from yggdrasil.node_agent import NodeAgent, NodeConfig
from yggdrasil.observability import validate_event

ROOT = Path(__file__).resolve().parents[1]
FIXED = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
NODE_ID = "census-node-1"


def static_probes(**overrides) -> ProbeSet:
    """A fully deterministic probe set: one small machine,
    stated exactly, overridable field by field."""
    values = {
        "cpu": lambda: {"arch": "aarch64", "cores": 4, "model": "Fixture CPU"},
        "ram_gb": lambda: 8.0,
        "accelerators": lambda: [{"kind": "none"}],
        "disk_free_gb": lambda: 42.5,
        "thermal_c": lambda: 51.24,
        "vram_gb": lambda: None,
    }
    values.update(overrides)
    return ProbeSet(**values)


# -- measured, not assumed ------------------------------------------------


def test_the_system_probes_measure_this_machine():
    census = measure_census("this-vm", at=FIXED)
    validate_census(census)
    assert census["node_id"] == "this-vm"
    assert census["measured_at"] == "2026-06-01T12:00:00Z"
    assert census["cpu"]["cores"] >= 1
    assert census["cpu"]["arch"]
    assert census["ram_gb"] > 0
    # No engine adapter exists until Y-023 and no model store
    # until Y-026 — the census claims neither early.
    assert census["engines"] == []
    assert census["models_present"] == []
    kinds = [entry["kind"] for entry in census["accelerators"]]
    assert kinds  # at least the honest 'none' on a bare machine


def test_a_fake_root_is_measured_exactly(tmp_path):
    root = tmp_path / "machine"
    (root / "proc" / "driver" / "nvidia" / "gpus" / "0000").mkdir(parents=True)
    (root / "proc" / "meminfo").write_text("MemTotal:        16384000 kB\n")
    (root / "proc" / "cpuinfo").write_text("model name\t: Fixture Processor 9000\n")
    (root / "proc" / "driver" / "nvidia" / "gpus" / "0000" / "information").write_text(
        "Model: \t\t Fixture GPU 16G\nUUID: \t\t GPU-fixture\n"
    )
    zone = root / "sys" / "class" / "thermal" / "thermal_zone0"
    zone.mkdir(parents=True)
    (zone / "temp").write_text("61300\n")
    (root / "dev").mkdir()
    (root / "dev" / "nvidia0").touch()
    (root / "dev" / "hailo0").touch()

    census = measure_census(NODE_ID, probes=system_probe_set(root), at=FIXED)
    assert census["ram_gb"] == pytest.approx(15.625)
    assert census["cpu"]["model"] == "Fixture Processor 9000"
    assert census["thermal_c"] == 61.3  # millidegrees read as degrees, one decimal
    assert census["disk_free_gb"] > 0
    assert census["accelerators"] == [
        {"kind": "cuda", "name": "Fixture GPU 16G", "proven": False},
        {"kind": "hailo", "name": "Hailo device node", "proven": False},
    ]


def test_an_injected_probe_set_is_measured_exactly():
    census = measure_census(NODE_ID, probes=static_probes(), at=FIXED)
    assert census == {
        "node_id": NODE_ID,
        "measured_at": "2026-06-01T12:00:00Z",
        "cpu": {"arch": "aarch64", "cores": 4, "model": "Fixture CPU"},
        "ram_gb": 8.0,
        "accelerators": [{"kind": "none", "proven": False}],
        "engines": [],
        "disk_free_gb": 42.5,
        "thermal_c": 51.2,
        "models_present": [],
    }


# -- honesty of absence -----------------------------------------------------


def test_absent_readings_are_absent_fields_not_fictions():
    probes = static_probes(thermal_c=lambda: None, disk_free_gb=lambda: None)
    census = measure_census(NODE_ID, probes=probes, at=FIXED)
    assert "thermal_c" not in census
    assert "disk_free_gb" not in census
    assert "vram_gb" not in census
    assert census["accelerators"] == [{"kind": "none", "proven": False}]


def test_a_sensorless_fake_root_reports_no_thermal(tmp_path):
    root = tmp_path / "bare"
    (root / "proc").mkdir(parents=True)
    (root / "proc" / "meminfo").write_text("MemTotal:         4096000 kB\n")
    census = measure_census(NODE_ID, probes=system_probe_set(root), at=FIXED)
    assert "thermal_c" not in census  # no sensor in the tree, no number invented
    assert census["accelerators"] == [{"kind": "none", "proven": False}]


def test_detection_never_claims_proven():
    probes = static_probes(
        accelerators=lambda: [{"kind": "cuda", "name": "Some GPU"}]
    )
    census = measure_census(NODE_ID, probes=probes, at=FIXED)
    # The probe did not even set the flag; presence was detected,
    # nothing was proven — the census says exactly that.
    assert census["accelerators"] == [
        {"kind": "cuda", "name": "Some GPU", "proven": False}
    ]


# -- refusals -----------------------------------------------------------------


def test_unknown_and_contradictory_accelerators_are_refused():
    with pytest.raises(CensusError, match="unknown device"):
        measure_census(
            NODE_ID,
            probes=static_probes(accelerators=lambda: [{"kind": "quantum"}]),
            at=FIXED,
        )
    with pytest.raises(CensusError, match="both 'none' and real"):
        measure_census(
            NODE_ID,
            probes=static_probes(
                accelerators=lambda: [{"kind": "none"}, {"kind": "cuda"}]
            ),
            at=FIXED,
        )


def test_a_failing_required_probe_is_named_and_an_optional_one_is_absent():
    def broken():
        raise OSError("sensor bus gone")

    with pytest.raises(CensusError, match="ram probe failed"):
        measure_census(NODE_ID, probes=static_probes(ram_gb=broken), at=FIXED)
    census = measure_census(NODE_ID, probes=static_probes(thermal_c=broken), at=FIXED)
    assert "thermal_c" not in census


def test_impossible_measurements_fail_the_schema():
    with pytest.raises(CensusError, match="capability census"):
        measure_census(NODE_ID, probes=static_probes(ram_gb=lambda: -1.0), at=FIXED)
    with pytest.raises(CensusError, match="capability census"):
        measure_census(
            NODE_ID,
            probes=static_probes(cpu=lambda: {"arch": "x86_64", "cores": 0}),
            at=FIXED,
        )


def test_engines_are_validated_before_they_enter_the_census():
    engine = {
        "engine": "ollama",
        "reachable": True,
        "capabilities": {"generate": True, "provenance_grade": "unproven"},
    }
    census = measure_census(
        NODE_ID, probes=static_probes(), engines=[engine],
        models_present=["fixture-model"], at=FIXED,
    )
    assert census["engines"] == [engine]
    assert census["models_present"] == ["fixture-model"]
    with pytest.raises(CensusError, match="engine capability"):
        measure_census(
            NODE_ID, probes=static_probes(),
            engines=[{"engine": "magic-box", "reachable": True, "capabilities": {}}],
            at=FIXED,
        )


# -- refresh on change ----------------------------------------------------------


def test_time_alone_is_not_a_change():
    first = measure_census(NODE_ID, probes=static_probes(), at=FIXED)
    later = measure_census(
        NODE_ID, probes=static_probes(), at=FIXED + timedelta(hours=2)
    )
    assert diff_censuses(first, later) == ()
    assert census_fingerprint(first) == census_fingerprint(later)


def test_the_collector_refreshes_and_names_what_changed():
    readings = {"thermal": 40.0, "accelerators": [{"kind": "none"}]}
    probes = static_probes(
        thermal_c=lambda: readings["thermal"],
        accelerators=lambda: readings["accelerators"],
    )
    collector = CensusCollector(NODE_ID, probes=probes, now=lambda: FIXED)
    baseline, changed = collector.refresh()
    assert changed == ("initial",)
    assert collector.current == baseline

    again, changed = collector.refresh()
    assert changed == ()
    assert again["measured_at"] == baseline["measured_at"]

    readings["thermal"] = 73.5
    readings["accelerators"] = [{"kind": "hailo"}]
    refreshed, changed = collector.refresh()
    assert changed == ("accelerators", "thermal_c")
    assert refreshed["thermal_c"] == 73.5
    assert refreshed["accelerators"][0]["kind"] == "hailo"
    assert census_fingerprint(refreshed) != census_fingerprint(baseline)


def test_callable_engines_feed_a_refresh_live():
    live = {"engines": []}
    collector = CensusCollector(
        NODE_ID, probes=static_probes(),
        engines=lambda: live["engines"], now=lambda: FIXED,
    )
    collector.measure()
    assert collector.current["engines"] == []
    live["engines"] = [{
        "engine": "ollama", "reachable": False,
        "capabilities": {"provenance_grade": "unproven"},
    }]
    _, changed = collector.refresh()
    assert changed == ("engines",)


# -- the node agent's census ------------------------------------------------------


def _provisioned_agent(tmp_path: Path, **agent_kwargs) -> NodeAgent:
    store_dir = tmp_path / "keys"
    store = LocalKeyStore(store_dir)
    root_keypair, root_cert = create_mesh_root(now=FIXED)
    node_keypair = NodeKeypair.generate()
    cert = issue_node_certificate(
        root_keypair, root_cert, node_id=NODE_ID, node_keypair=node_keypair,
        device_class="vm", agent_version="0.0.1", now=FIXED,
    )
    store.save_certificate(root_cert)
    store.save_seed(NODE_ID, node_keypair.seed)
    store.save_certificate(cert)
    config = NodeConfig.from_dict({
        "version": 1, "node_id": NODE_ID, "device_class": "vm",
        "key_store_dir": str(store_dir),
    })
    return NodeAgent(config, now=lambda: FIXED, **agent_kwargs)


def test_the_agent_measures_on_first_asking_and_status_follows(tmp_path):
    agent = _provisioned_agent(tmp_path, census_probes=static_probes())
    assert agent.status()["census"] is None  # nothing measured until asked
    census = agent.census()
    assert census["node_id"] == NODE_ID
    assert agent.census() is census  # held, not re-measured per asking
    assert agent.status()["census"] == census


def test_the_agent_records_a_census_change_as_an_event(tmp_path):
    readings = {"thermal": 40.0}
    agent = _provisioned_agent(
        tmp_path,
        census_probes=static_probes(thermal_c=lambda: readings["thermal"]),
    )
    agent.start()
    first = agent.refresh_census()
    assert first["changed_fields"] == ["initial"]
    assert agent.refresh_census()["changed_fields"] == []
    readings["thermal"] = 88.0
    result = agent.refresh_census()
    assert result["changed_fields"] == ["thermal_c"]
    events = [e for e in agent.events if e["type"] == "node.census_changed"]
    assert len(events) == 1
    validate_event(events[0])
    assert events[0]["payload"]["changed_fields"] == ["thermal_c"]
    assert events[0]["payload"]["fabric_connected"] is False
    assert events[0]["source"]["component"] == "node_agent"
    # The lifecycle events kept their own sequence around it.
    assert [e["type"] for e in agent.events] == ["node.started", "node.census_changed"]


def test_the_founding_readme_is_untouched():
    digest = hashlib.md5((ROOT / "README.md").read_bytes()).hexdigest()
    assert digest == "89a37a4e18cab9a998e1751c32e7bfd5"
    assert json.loads((ROOT / "data" / "data_manifest.json").read_text())
