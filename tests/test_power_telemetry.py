"""Power and thermal telemetry tests — Y-044's acceptance.

The telemetry and its governor citation must prove:
(a) the system snapshot reads a real machine's shapes from
files only — thermal zones named by their own type files,
power supplies in micro-units converted (watts computed
from voltage × current when no power file is published),
hwmon rails as the fallback power source, and RAPL-class
energy counters; (b) absence is reported as absence — a
machine with no zones and no supplies (this VM's shape)
reads empty and null, never a cool or a zero-watt fiction;
(c) a sample is a validated reading naming the hottest
zone, the first sample's CPU power is null (a rate needs
two readings), the second derives watts from the counter
delta over measured elapsed time, and a counter that goes
backwards yields null, never a negative rate; (d) the
report keeps the window in numbers — peaks, mean power,
thermal slope, energy consumed — and names staleness
plainly once the newest sample ages past the policy;
(e) unshaped snapshots are refused by name; (f) the
governor's readings can come from the telemetry (its
hottest zone drives a real thermal refusal), and a
governor state *and* a governor decision can each be
cited against the reading they rest on — fresh, stale,
and not-yet-sampled citations all told honestly;
(g) the node agent carries the seam — wired agents sample,
report, and cite; unwired agents report None exactly as
before; (h) the Pi and laptop citizen factories wire a
telemetry the built agent carries, and in the Pi factory,
with no readings provider handed in, the governor's own
thermal reading is the telemetry's sampled hottest zone;
(i) the founding README is untouched.

The snapshots here are injected fixtures shaped from the
Pi's and the laptop's measured facts, and the fake root is
a tmp tree — every run in this file is SIMULATED except
the one real system snapshot of this VM, which is asserted
only for its honest emptiness. No live power/thermal run
on Mimir or purplelongship is claimed.
"""

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from yggdrasil.census import ProbeSet
from yggdrasil.governor import ResourceGovernor, default_policy
from yggdrasil.identity import (
    LocalKeyStore,
    NodeKeypair,
    create_mesh_root,
    issue_node_certificate,
)
from yggdrasil.laptop_citizen import build_laptop_citizen_agent
from yggdrasil.node_agent import NodeAgent, NodeConfig
from yggdrasil.observability import validate_event
from yggdrasil.pi_citizen import build_pi_citizen_agent
from yggdrasil.power_telemetry import (
    PowerTelemetry,
    PowerTelemetryError,
    system_power_snapshot,
    validate_citation,
    validate_reading,
    validate_report,
)

ROOT = Path(__file__).resolve().parents[1]
FIXED = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
NODE_ID = "telemetry-node-1"
README_MD5 = "89a37a4e18cab9a998e1751c32e7bfd5"
GIB = 1024**3


class Clock:
    """A hand-advanced clock: ages, slopes, and rates are
    measured against it deterministically."""

    def __init__(self, start: datetime = FIXED):
        self.t = start

    def __call__(self) -> datetime:
        return self.t

    def advance(self, **kwargs) -> None:
        self.t += timedelta(**kwargs)


def _snapshot(temp_c: float = 50.0, power_w: float = 24.0, energy_uj: int = 1_000_000) -> dict:
    return {
        "thermal_zones": [
            {"name": "cpu_thermal", "temp_c": temp_c},
            {"name": "gpu", "temp_c": temp_c - 6.0},
        ],
        "power_supplies": [
            {
                "name": "BAT0",
                "kind": "battery",
                "status": "Discharging",
                "voltage_v": 12.0,
                "current_a": 2.0,
                "power_w": power_w,
                "capacity_percent": 87.0,
            }
        ],
        "power_w": power_w,
        "energy_counters": [{"name": "package-0", "energy_uj": energy_uj}],
    }


# -- the system snapshot, against a fake root -----------------------------------


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _fake_root(tmp_path: Path) -> Path:
    root = tmp_path / "machine"
    thermal = root / "sys" / "class" / "thermal"
    _write(thermal / "thermal_zone0" / "type", "cpu_thermal\n")
    _write(thermal / "thermal_zone0" / "temp", "51300\n")
    _write(thermal / "thermal_zone1" / "type", "gpu\n")
    _write(thermal / "thermal_zone1" / "temp", "48000\n")
    supply = root / "sys" / "class" / "power_supply" / "BAT0"
    _write(supply / "type", "Battery\n")
    _write(supply / "status", "Discharging\n")
    _write(supply / "voltage_now", "12000000\n")
    _write(supply / "current_now", "2000000\n")
    _write(supply / "capacity", "87\n")
    cap = root / "sys" / "class" / "powercap" / "intel-rapl:0"
    _write(cap / "name", "package-0\n")
    _write(cap / "energy_uj", "123456789\n")
    return root


def test_the_system_snapshot_reads_a_machine_from_files(tmp_path: Path):
    snapshot = system_power_snapshot(_fake_root(tmp_path))
    assert snapshot["thermal_zones"] == [
        {"name": "cpu_thermal", "temp_c": 51.3},
        {"name": "gpu", "temp_c": 48.0},
    ]
    (supply,) = snapshot["power_supplies"]
    assert supply["kind"] == "battery"
    assert supply["status"] == "Discharging"
    assert supply["voltage_v"] == 12.0
    assert supply["current_a"] == 2.0
    # No power_now file is published: watts are the product
    # of the two measured values, as the module states.
    assert supply["power_w"] == 24.0
    assert supply["capacity_percent"] == 87.0
    assert snapshot["power_w"] == 24.0
    assert snapshot["energy_counters"] == [
        {"name": "package-0", "energy_uj": 123456789}
    ]


def test_the_system_snapshot_falls_back_to_hwmon_power(tmp_path: Path):
    root = tmp_path / "machine"
    hwmon = root / "sys" / "class" / "hwmon" / "hwmon0"
    _write(hwmon / "name", "coretemp\n")
    _write(hwmon / "power1_input", "45000000\n")
    _write(hwmon / "power2_input", "5000000\n")
    snapshot = system_power_snapshot(root)
    assert snapshot["power_w"] == 50.0


def test_absence_is_reported_as_absence_on_this_vm():
    # The one real reading in this file: this VM exposes no
    # thermal zone, no power supply, no energy counter —
    # and the snapshot says exactly that.
    snapshot = system_power_snapshot("/")
    assert snapshot["thermal_zones"] == []
    assert snapshot["power_supplies"] == []
    assert snapshot["power_w"] is None
    telemetry = PowerTelemetry(NODE_ID, snapshot=lambda: snapshot, now=Clock())
    reading = validate_reading(telemetry.sample())
    assert reading["hottest_c"] is None
    assert reading["hottest_zone"] is None
    assert reading["power_w"] is None


# -- sampling ---------------------------------------------------------------------


def test_a_sample_is_a_validated_reading_naming_the_hottest_zone():
    clock = Clock()
    telemetry = PowerTelemetry(
        NODE_ID, snapshot=lambda: _snapshot(), simulated=True, now=clock
    )
    reading = validate_reading(telemetry.sample())
    assert reading["source"] == "injected"
    assert reading["simulated"] is True
    assert reading["hottest_c"] == 50.0
    assert reading["hottest_zone"] == "cpu_thermal"
    assert reading["power_w"] == 24.0
    # A rate needs two readings: the first sample claims none.
    assert reading["cpu_power_w"] is None
    (event,) = telemetry.events
    assert validate_event(event)["type"] == "power_telemetry.sampled"


def test_cpu_power_is_derived_from_counter_deltas_over_measured_time():
    clock = Clock()
    snaps = iter(
        [
            _snapshot(energy_uj=1_000_000),
            _snapshot(energy_uj=1_000_000 + 120_000_000),
            _snapshot(energy_uj=500),  # a counter gone backwards: wrap/reset
        ]
    )
    telemetry = PowerTelemetry(NODE_ID, snapshot=lambda: next(snaps), now=clock)
    telemetry.sample()
    clock.advance(seconds=60)
    second = telemetry.sample()
    # 120 J over 60 s = 2 W, from the counters alone.
    assert second["cpu_power_w"] == 2.0
    clock.advance(seconds=60)
    third = telemetry.sample()
    assert third["cpu_power_w"] is None


def test_unshaped_snapshots_are_refused_by_name():
    telemetry = PowerTelemetry(NODE_ID, snapshot=lambda: {"thermal_zones": [{"x": 1}]})
    with pytest.raises(PowerTelemetryError):
        telemetry.sample()
    telemetry = PowerTelemetry(NODE_ID, snapshot=lambda: "not a dict")
    with pytest.raises(PowerTelemetryError):
        telemetry.sample()
    with pytest.raises(PowerTelemetryError):
        PowerTelemetry("", snapshot=lambda: _snapshot())
    with pytest.raises(PowerTelemetryError):
        PowerTelemetry(NODE_ID, snapshot=lambda: _snapshot(), stale_after_s=0)


# -- the report -------------------------------------------------------------------


def test_the_report_keeps_the_window_in_numbers_and_names_staleness():
    clock = Clock()
    snaps = iter(
        [
            _snapshot(temp_c=50.0, power_w=20.0, energy_uj=1_000_000),
            _snapshot(temp_c=56.0, power_w=30.0, energy_uj=1_000_000 + 90_000_000),
        ]
    )
    telemetry = PowerTelemetry(NODE_ID, snapshot=lambda: next(snaps), now=clock)
    empty = validate_report(telemetry.report())
    assert empty["samples"] == 0
    assert empty["latest"] is None
    assert empty["stale"] is True
    telemetry.sample()
    clock.advance(seconds=60)
    telemetry.sample()
    report = validate_report(telemetry.report())
    assert report["samples"] == 2
    assert report["peak_thermal_c"] == 56.0
    assert report["peak_power_w"] == 30.0
    assert report["mean_power_w"] == 25.0
    assert report["thermal_slope_c_per_min"] == 6.0
    assert report["energy_consumed_uj"] == 90_000_000
    assert report["stale"] is False
    clock.advance(seconds=121)
    assert telemetry.report()["stale"] is True


# -- the governor's doors -----------------------------------------------------------


def test_telemetry_readings_drive_a_real_governor_refusal():
    clock = Clock()
    telemetry = PowerTelemetry(
        NODE_ID, snapshot=lambda: _snapshot(temp_c=85.0), now=clock
    )
    governor = ResourceGovernor(
        NODE_ID,
        default_policy(),
        readings=lambda: telemetry.readings_for_governor(
            {
                "memory_used_bytes": 1 * GIB,
                "memory_total_bytes": 8 * GIB,
                "cpu_share_used": 0.1,
            }
        ),
        now=clock,
    )
    decision = governor.admit(task_id="task-hot")
    assert decision["admitted"] is False
    assert "thermal" in decision["reasons"]
    # The governor judged the telemetry's own number.
    assert governor.last_state["readings"]["thermal_c"] == 85.0


def test_a_governor_state_and_decision_are_cited_against_their_reading():
    clock = Clock()
    telemetry = PowerTelemetry(
        NODE_ID, snapshot=lambda: _snapshot(temp_c=85.0), simulated=True, now=clock
    )
    governor = ResourceGovernor(
        NODE_ID,
        default_policy(),
        readings=lambda: {
            "thermal_c": 85.0,
            "memory_used_bytes": None,
            "memory_total_bytes": None,
            "cpu_share_used": None,
        },
        now=clock,
    )
    telemetry.sample()
    state_citation = validate_citation(telemetry.cite(governor.evaluate()))
    assert state_citation["subject"] == "state"
    assert state_citation["shedding"] is True
    assert state_citation["reasons"] == ["thermal"]
    cited = state_citation["telemetry"]
    assert cited["thermal_c"] == 85.0
    assert cited["hottest_zone"] == "cpu_thermal"
    assert cited["power_w"] == 24.0
    assert cited["age_s"] == 0.0
    assert cited["stale"] is False
    assert cited["simulated"] is True

    decision_citation = validate_citation(telemetry.cite(governor.admit()))
    assert decision_citation["subject"] == "decision"
    assert decision_citation["shedding"] is True

    clock.advance(seconds=3600)
    stale = telemetry.cite(governor.evaluate())
    assert stale["telemetry"]["stale"] is True
    assert stale["telemetry"]["age_s"] == 3600.0


def test_a_citation_before_any_sample_says_so():
    telemetry = PowerTelemetry(NODE_ID, snapshot=lambda: _snapshot(), now=Clock())
    governor = ResourceGovernor(
        NODE_ID,
        readings=lambda: {
            "thermal_c": None,
            "memory_used_bytes": None,
            "memory_total_bytes": None,
            "cpu_share_used": None,
        },
        now=Clock(),
    )
    citation = validate_citation(telemetry.cite(governor.evaluate()))
    assert citation["telemetry"]["reading_at"] is None
    assert citation["telemetry"]["thermal_c"] is None
    assert citation["telemetry"]["stale"] is True
    with pytest.raises(PowerTelemetryError):
        telemetry.cite({"not": "a governor record"})


# -- the node agent seam ------------------------------------------------------------


def _provisioned_agent(tmp_path: Path, node_id: str = NODE_ID, **agent_kwargs) -> NodeAgent:
    store_dir = tmp_path / "keys"
    store = LocalKeyStore(store_dir)
    root_keypair, root_cert = create_mesh_root(now=FIXED)
    node_keypair = NodeKeypair.generate()
    cert = issue_node_certificate(
        root_keypair,
        root_cert,
        node_id=node_id,
        node_keypair=node_keypair,
        device_class="vm",
        agent_version="0.0.1",
        now=FIXED,
    )
    store.save_certificate(root_cert)
    store.save_seed(node_id, node_keypair.seed)
    store.save_certificate(cert)
    config = NodeConfig.from_dict(
        {
            "version": 1,
            "node_id": node_id,
            "device_class": "vm",
            "key_store_dir": str(store_dir),
        }
    )
    return NodeAgent(config, now=lambda: FIXED, **agent_kwargs)


def test_the_agent_samples_reports_and_cites_when_wired(tmp_path: Path):
    clock = Clock()
    telemetry = PowerTelemetry(
        NODE_ID, snapshot=lambda: _snapshot(temp_c=85.0), simulated=True, now=clock
    )
    governor = ResourceGovernor(
        NODE_ID,
        readings=lambda: {
            "thermal_c": 85.0,
            "memory_used_bytes": None,
            "memory_total_bytes": None,
            "cpu_share_used": None,
        },
        now=clock,
    )
    agent = _provisioned_agent(tmp_path, governor=governor, power_telemetry=telemetry)
    assert agent.power_telemetry is telemetry
    # No verdict yet: the citation is None, never forced.
    assert agent.governor_citation() is None
    reading = agent.sample_power_telemetry()
    assert reading["hottest_c"] == 85.0
    governor.evaluate()
    citation = validate_citation(agent.governor_citation())
    assert citation["telemetry"]["thermal_c"] == 85.0
    assert agent.status()["power_telemetry"]["samples"] == 1
    agent.start()
    agent.join()
    agent.stop()


def test_an_unwired_agent_senses_nothing(tmp_path: Path):
    agent = _provisioned_agent(tmp_path)
    assert agent.power_telemetry is None
    assert agent.sample_power_telemetry() is None
    assert agent.power_telemetry_report() is None
    assert agent.governor_citation() is None
    assert agent.status()["power_telemetry"] is None


# -- the citizen factories ----------------------------------------------------------


def _citizen_config(tmp_path: Path, node_id: str, device_class: str) -> NodeConfig:
    store_dir = tmp_path / f"keys-{device_class}"
    store = LocalKeyStore(store_dir)
    root_keypair, root_cert = create_mesh_root(now=FIXED)
    node_keypair = NodeKeypair.generate()
    cert = issue_node_certificate(
        root_keypair,
        root_cert,
        node_id=node_id,
        node_keypair=node_keypair,
        device_class=device_class,
        agent_version="0.0.1",
        now=FIXED,
    )
    store.save_certificate(root_cert)
    store.save_seed(node_id, node_keypair.seed)
    store.save_certificate(cert)
    return NodeConfig.from_dict(
        {
            "version": 1,
            "node_id": node_id,
            "device_class": device_class,
            "key_store_dir": str(store_dir),
        }
    )


def _pi_probes() -> ProbeSet:
    return ProbeSet(
        cpu=lambda: {
            "arch": "aarch64",
            "cores": 4,
            "model": "Raspberry Pi 5 Model B Rev 1.1",
        },
        ram_gb=lambda: 16598576 / (1024 * 1024),
        accelerators=lambda: [{"kind": "none"}],
        disk_free_gb=lambda: 202.0,
        thermal_c=lambda: 41.9,
        vram_gb=lambda: None,
    )


def _laptop_probes() -> ProbeSet:
    return ProbeSet(
        cpu=lambda: {
            "arch": "x86_64",
            "cores": 16,
            "model": "AMD Ryzen 9 4900HS with Radeon Graphics",
        },
        ram_gb=lambda: 40465868 / (1024 * 1024),
        accelerators=lambda: [
            {"kind": "cuda", "name": "NVIDIA GeForce RTX 2060 with Max-Q Design"}
        ],
        disk_free_gb=lambda: 1100.0,
        thermal_c=lambda: 40.0,
        vram_gb=lambda: 6144 / 1024,
    )


def test_the_pi_factory_wires_telemetry_and_the_governor_reads_it(tmp_path: Path):
    config = _citizen_config(tmp_path, "mimir-1", "pi")
    agent, report = build_pi_citizen_agent(
        config,
        store_root=tmp_path / "store",
        probes=_pi_probes(),
        # No readings provider: the governor's thermal comes
        # from the telemetry (Y-044), sampled on evaluation.
        neighbours={
            "samtal_service": "active",
            "aurelia_port": "listening",
            "kiosk_port": "not_listening",
            "hailo_device": "absent",
        },
        simulated=True,
        now=lambda: FIXED,
        power_snapshot=lambda: _snapshot(temp_c=41.9),
    )
    assert report["ready"] is True
    assert agent.power_telemetry is not None
    assert agent.power_telemetry.source == "injected"
    state = agent.governor.evaluate()
    # The governor judged the telemetry's sampled hottest
    # zone — the same measurement a citation would cite.
    assert state["readings"]["thermal_c"] == 41.9
    assert agent.power_telemetry_report()["samples"] == 1
    citation = validate_citation(agent.governor_citation())
    assert citation["telemetry"]["thermal_c"] == 41.9
    assert agent.status()["power_telemetry"]["samples"] == 1


def test_the_laptop_factory_wires_telemetry(tmp_path: Path):
    config = _citizen_config(tmp_path, "purplelongship-1", "laptop")
    agent, report = build_laptop_citizen_agent(
        config,
        store_root=tmp_path / "store",
        probes=_laptop_probes(),
        readings=lambda: {
            "thermal_c": 40.0,
            "memory_used_bytes": 8 * GIB,
            "memory_total_bytes": 38 * GIB,
            "cpu_share_used": 0.25,
        },
        neighbours={"desktop_session": "active", "cuda_device": "present"},
        simulated=True,
        now=lambda: FIXED,
        power_snapshot=lambda: _snapshot(temp_c=40.0, power_w=65.0),
    )
    assert report["ready"] is True
    assert agent.power_telemetry is not None
    reading = agent.sample_power_telemetry()
    assert reading["power_w"] == 65.0
    assert agent.status()["power_telemetry"]["latest"]["hottest_zone"] == "cpu_thermal"


# -- the founding document ----------------------------------------------------------


def test_the_founding_readme_is_untouched():
    digest = hashlib.md5((ROOT / "README.md").read_bytes()).hexdigest()
    assert digest == README_MD5
