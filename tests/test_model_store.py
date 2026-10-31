"""Model store tests — Y-026's acceptance.

The store must prove: (a) registration pins the truth — adding
a file records its real SHA-256 and size (measured independently
here), and the entry validates against its contract schema;
(b) presence is a measurement — a model is present only while
its hash verifies: tampered bytes make it corrupt and remove
it from present_models / the census / registry entries, a
deleted file makes it missing, restoring the bytes restores
presence; (c) a name cannot be re-pointed — registering the
same name with different bytes is refused, the same bytes are
idempotent, an empty file and an escaping path are refused,
and an index naming a path outside the root refuses to load;
(d) the registry shape — model_entries() yields validated
Y-008 entries for verified models only; (e) the node agent
integration — an agent built with a model store lists exactly
its verified models in its census, and a tamper shows up in
the next refresh by field name; (f) the founding README is
untouched.
"""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from yggdrasil.census import ProbeSet
from yggdrasil.identity import (
    LocalKeyStore,
    NodeKeypair,
    create_mesh_root,
    issue_node_certificate,
)
from yggdrasil.model_store import (
    ModelStore,
    ModelStoreError,
    hash_file,
    validate_store_entry,
)
from yggdrasil.node_agent import NodeAgent, NodeConfig

ROOT = Path(__file__).resolve().parents[1]
FIXED = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
README_MD5 = "89a37a4e18cab9a998e1751c32e7bfd5"

WEIGHTS = b"SIMULATED weights - not a real model, just bytes with a hash"
WEIGHTS_V2 = b"SIMULATED weights - different bytes, different hash...."


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_store(tmp_path) -> ModelStore:
    return ModelStore(tmp_path / "store", now=lambda: FIXED)


def add_one(store, tmp_path, data=WEIGHTS, name="fixture-7b", quant="Q4_K_M"):
    source = tmp_path / f"source-{name}-{quant}.gguf"
    source.write_bytes(data)
    return store.add(
        source,
        name=name,
        family="fixture",
        quant=quant,
        format="gguf",
        capabilities=("generate", "stream"),
    )


def test_add_pins_real_hash_and_size(tmp_path):
    store = make_store(tmp_path)
    entry = add_one(store, tmp_path)
    assert validate_store_entry(entry) == entry
    assert entry["sha256"] == sha(WEIGHTS)
    assert entry["size_bytes"] == len(WEIGHTS)
    assert entry["registered_at"] == "2026-06-01T12:00:00Z"
    # The copy lives under the store root, and its bytes hash alike.
    copied = store.root / entry["path"]
    assert copied.is_file()
    assert hash_file(copied) == (sha(WEIGHTS), len(WEIGHTS))
    # The index on disk carries the same pin (a fresh store reads it).
    fresh = make_store(tmp_path)
    assert fresh.entries() == [entry]
    assert fresh.verify("fixture-7b")["status"] == "verified"


def test_present_only_when_hash_verifies(tmp_path):
    store = make_store(tmp_path)
    entry = add_one(store, tmp_path)
    assert store.present_models() == ["fixture-7b"]
    assert store.is_present("fixture-7b") is True
    assert store.is_present("never-registered") is False

    # Tamper: one byte flipped — corrupt, and presence falls away.
    copied = store.root / entry["path"]
    tampered = bytearray(copied.read_bytes())
    tampered[0] ^= 0xFF
    copied.write_bytes(bytes(tampered))
    report = store.verify("fixture-7b")
    assert report["status"] == "corrupt"
    assert report["actual_sha256"] != report["expected_sha256"]
    assert store.present_models() == []
    assert store.is_present("fixture-7b") is False
    # Reported, not silently dropped: verify_all still names it.
    assert [r["name"] for r in store.verify_all()] == ["fixture-7b"]

    # Restoring the exact bytes restores presence.
    copied.write_bytes(WEIGHTS)
    assert store.present_models() == ["fixture-7b"]


def test_missing_file_is_missing_not_present(tmp_path):
    store = make_store(tmp_path)
    entry = add_one(store, tmp_path)
    (store.root / entry["path"]).unlink()
    report = store.verify("fixture-7b")
    assert report["status"] == "missing"
    assert report["actual_sha256"] is None
    assert store.present_models() == []


def test_truncation_is_corrupt(tmp_path):
    store = make_store(tmp_path)
    entry = add_one(store, tmp_path)
    copied = store.root / entry["path"]
    copied.write_bytes(WEIGHTS[:10])
    report = store.verify("fixture-7b")
    assert report["status"] == "corrupt"
    assert report["actual_size_bytes"] == 10


def test_name_cannot_be_repointed(tmp_path):
    store = make_store(tmp_path)
    add_one(store, tmp_path)
    # Same name, same bytes: idempotent.
    again = add_one(store, tmp_path)
    assert again["sha256"] == sha(WEIGHTS)
    assert len(store.entries()) == 1
    # Same name, different bytes: refused by name.
    with pytest.raises(ModelStoreError, match="different bytes"):
        add_one(store, tmp_path, data=WEIGHTS_V2)
    # After unregistering, the name may be registered afresh.
    store.unregister("fixture-7b")
    entry = add_one(store, tmp_path, data=WEIGHTS_V2)
    assert entry["sha256"] == sha(WEIGHTS_V2)


def test_two_quants_of_one_family_do_not_collide(tmp_path):
    store = make_store(tmp_path)
    a = add_one(store, tmp_path, name="fixture-7b-q4", quant="Q4_K_M")
    b = add_one(
        store, tmp_path, data=WEIGHTS_V2, name="fixture-7b-q8", quant="Q8_0"
    )
    assert a["path"] != b["path"]
    assert store.present_models() == ["fixture-7b-q4", "fixture-7b-q8"]


def test_empty_and_missing_sources_refused(tmp_path):
    store = make_store(tmp_path)
    empty = tmp_path / "empty.gguf"
    empty.write_bytes(b"")
    with pytest.raises(ModelStoreError, match="empty|does not exist"):
        store.add(empty, name="empty-1", family="fixture", quant="Q4", format="gguf")
    with pytest.raises(ModelStoreError, match="does not exist"):
        store.add(
            tmp_path / "ghost.gguf",
            name="ghost-1",
            family="fixture",
            quant="Q4",
            format="gguf",
        )
    assert store.entries() == []


def test_register_existing_and_path_escape_refused(tmp_path):
    store = make_store(tmp_path)
    inside = store.root / "loose.gguf"
    inside.parent.mkdir(parents=True, exist_ok=True)
    inside.write_bytes(WEIGHTS)
    entry = store.register_existing(
        "loose.gguf", name="loose-1", family="fixture", quant="Q4", format="gguf"
    )
    assert entry["sha256"] == sha(WEIGHTS)
    assert store.present_models() == ["loose-1"]
    with pytest.raises(ModelStoreError, match="escapes|does not exist"):
        store.register_existing(
            "../outside.gguf",
            name="evil-1",
            family="fixture",
            quant="Q4",
            format="gguf",
        )


def test_index_with_escaping_path_refuses_to_load(tmp_path):
    store = make_store(tmp_path)
    add_one(store, tmp_path)
    index = json.loads(store.index_path.read_text())
    index["models"][0]["path"] = "../escape.gguf"
    store.index_path.write_text(json.dumps(index))
    with pytest.raises(ModelStoreError, match="escapes"):
        make_store(tmp_path)


def test_unregister_keeps_file_and_says_so(tmp_path):
    store = make_store(tmp_path)
    entry = add_one(store, tmp_path)
    store.unregister("fixture-7b")
    assert store.entries() == []
    assert store.present_models() == []
    # The file stays on disk: unregistering is bookkeeping, not deletion.
    assert (store.root / entry["path"]).is_file()
    with pytest.raises(ModelStoreError, match="not registered"):
        store.unregister("fixture-7b")


def test_model_entries_only_for_verified(tmp_path):
    store = make_store(tmp_path)
    entry = add_one(store, tmp_path)
    entries = store.model_entries()
    assert len(entries) == 1
    record = entries[0]
    assert record["name"] == "fixture-7b"
    assert record["family"] == "fixture"
    assert record["quants"][0]["name"] == "Q4_K_M"
    assert record["quants"][0]["format"] == "gguf"
    assert record["capabilities"] == ["generate", "stream"]
    assert record["runnable_on"] == []  # Y-038's word, not the store's
    copied = store.root / entry["path"]
    copied.write_bytes(WEIGHTS_V2)
    assert store.model_entries() == []


def static_probes() -> ProbeSet:
    return ProbeSet(
        cpu=lambda: {"arch": "x86_64", "cores": 4, "model": "Fixture CPU"},
        ram_gb=lambda: 16.0,
        accelerators=lambda: [{"kind": "none"}],
        disk_free_gb=lambda: 100.0,
        thermal_c=lambda: None,
        vram_gb=lambda: None,
    )


def make_agent(tmp_path, store) -> NodeAgent:
    keys = tmp_path / "keys"
    key_store = LocalKeyStore(keys)
    root_keypair, root_cert = create_mesh_root(now=FIXED)
    node_keypair = NodeKeypair.generate()
    cert = issue_node_certificate(
        root_keypair,
        root_cert,
        node_id="store-node-1",
        node_keypair=node_keypair,
        device_class="vm",
        agent_version="0.0.1",
        now=FIXED,
    )
    key_store.save_certificate(root_cert)
    key_store.save_seed("store-node-1", node_keypair.seed)
    key_store.save_certificate(cert)
    config = NodeConfig.from_dict(
        {
            "version": 1,
            "node_id": "store-node-1",
            "device_class": "vm",
            "key_store_dir": str(keys),
        },
        base_dir=tmp_path,
    )
    return NodeAgent(
        config,
        now=lambda: FIXED,
        census_probes=static_probes(),
        model_store=store,
    )


def test_agent_census_lists_only_verified_models(tmp_path):
    store = make_store(tmp_path)
    entry = add_one(store, tmp_path)
    agent = make_agent(tmp_path, store)
    census = agent.census()
    assert census["models_present"] == ["fixture-7b"]
    # Tamper, refresh: the model falls off the census, named by field.
    (store.root / entry["path"]).write_bytes(WEIGHTS_V2)
    refreshed = agent.refresh_census()
    assert refreshed["census"]["models_present"] == []
    assert "models_present" in refreshed["changed_fields"]


def test_readme_untouched():
    digest = hashlib.md5((ROOT / "README.md").read_bytes()).hexdigest()
    assert digest == README_MD5
