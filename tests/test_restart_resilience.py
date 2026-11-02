"""Restart resilience tests — Y-066's acceptance.

The slice must prove: (a) the rejoin law is data —
a policy naming a full reload or a non-delta sync
mode fails validation, as does a zero delta bound;
(b) a digest carries stamps and census hashes
only, never census content, and names the
replica's whole element count; (c) a node that
checkpointed, crashed, and missed a handful of
changes rejoins through one peer by delta alone —
the elements it receives are exactly the changes
it missed, strictly fewer than the peer's whole
state, nothing it already held travels, and the
pair converges; (d) the mesh keeps working while
a node is down, and acts aimed at the down node
are refused by name; (e) durability is honest —
a write neither checkpointed nor gossiped before
the crash is gone, a checkpointed one survives
and propagates to the peer on rejoin; (f) a node
with no checkpoint rejoins from nothing — its
incoming delta is the peer's whole element count,
counted element by element, and the report's
full_reloads still stands at zero; (g) a
tombstone struck while a node was down arrives
by delta and the rejoined node's own record is
gone in its own replica; (h) rejoins are judged —
self_rejoin, unknown_peer, not_restarted,
node_down, peer_down, delta_too_large, and
malformed_digest refusals are named, counted
apart, and apply nothing; (i) a raw delta at the
apply_delta seam is judged the same way
(malformed_delta, delta_not_for_me); (j) API
misuse raises by name — double crash, restart of
an up node, duplicate membership; (k) the report
and every record validate, counts are honest,
events validate, and the founding README is
untouched.

Everything here is SIMULATED — a crash is the
in-process loss of one replica object; no process
is killed, no disk is written, no socket is
opened, and no LAN is crossed (real carriage was
Y-062's own proof).
"""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from yggdrasil.crdt_stores import validate_state as validate_crdt_state
from yggdrasil.observability import validate_event
from yggdrasil.restart_resilience import (
    RejoinCoordinator,
    RestartError,
    compute_delta,
    default_policy,
    elements_of,
    validate_delta,
    validate_digest,
    validate_policy,
    validate_record,
    validate_report,
)
from yggdrasil.simulator import make_fake_census

ROOT = Path(__file__).resolve().parents[1]
README_MD5 = "89a37a4e18cab9a998e1751c32e7bfd5"
EPOCH = datetime(2026, 11, 2, 12, 0, 0, tzinfo=UTC)


class Clock:
    def __init__(self):
        self.moment = EPOCH

    def __call__(self):
        return self.moment

    def advance(self, seconds):
        self.moment += timedelta(seconds=seconds)


def census(node_id, *, ram_gb=8.0):
    found = make_fake_census(node_id)
    found["ram_gb"] = ram_gb
    return found


def mesh(policy=None, clock=None):
    found = RejoinCoordinator(policy=policy, now=clock or Clock())
    for node_id in ("alfheim", "brage", "solberg"):
        found.add_node(node_id)
    return found


def settled_mesh(policy=None, clock=None):
    """Three nodes, each holding its own census and
    alfheim holding one model offer, all synced."""
    found = mesh(policy=policy, clock=clock)
    for node_id in ("alfheim", "brage", "solberg"):
        found.store(node_id).put_node(census(node_id))
    found.store("alfheim").add_model("alfheim", "ygg-base-7b")
    found.sync_pair("alfheim", "brage")
    found.sync_pair("alfheim", "solberg")
    found.sync_pair("brage", "solberg")
    return found


def fingerprint(store):
    state = store.state()
    mergeable = {
        key: state[key]
        for key in ("writes", "tombstones", "model_adds", "model_removes")
    }
    return hashlib.sha256(json.dumps(mergeable, sort_keys=True).encode()).hexdigest()


# -- the law is data ----------------------------------------------


def test_policy_law_is_schema_constants():
    policy = default_policy()
    assert policy["sync_mode"] == "delta"
    assert policy["full_reload_allowed"] is False
    validate_policy(policy)
    with pytest.raises(RestartError):
        validate_policy({**policy, "full_reload_allowed": True})
    with pytest.raises(RestartError):
        validate_policy({**policy, "sync_mode": "full_state"})
    with pytest.raises(RestartError):
        validate_policy({**policy, "max_delta_elements": 0})


def test_digest_carries_stamps_and_hashes_never_census():
    found = settled_mesh()
    digest = found.digest("brage")
    validate_digest(digest)
    assert digest["node_id"] == "brage"
    assert digest["elements"] == 4  # 3 writes + 1 model add
    assert len(digest["writes"]) == 3
    assert digest["lamport_clock"] >= 1
    blob = json.dumps(digest)
    assert "ram_gb" not in blob and "capability census" not in blob
    held = found.store("brage").state()
    for entry in digest["writes"]:
        source = next(w for w in held["writes"] if w["node_id"] == entry["node_id"])
        assert entry["stamp"] == source["stamp"]
        assert entry["census_hash"] == hashlib.sha256(
            json.dumps(source["census"], sort_keys=True).encode()
        ).hexdigest()


# -- the headline: rejoin by delta, converging ---------------------


def test_rejoin_by_delta_only_missed_changes_travel_and_the_pair_converges():
    found = settled_mesh()
    assert found.checkpoint("brage") == 4
    found.crash("brage")
    assert found.status("brage") == "down"
    # The mesh keeps working without brage: two census
    # rewrites, one model add, one model remove, and a
    # fourth citizen joins and registers.
    found.store("alfheim").put_node(census("alfheim", ram_gb=16.0))
    found.store("solberg").put_node(census("solberg", ram_gb=32.0))
    found.store("solberg").add_model("solberg", "ygg-embed-1b")
    assert found.store("alfheim").remove_model("alfheim", "ygg-base-7b")
    found.add_node("dain")
    found.store("dain").put_node(census("dain"))
    found.sync_pair("alfheim", "solberg")
    found.sync_pair("alfheim", "dain")
    peer_elements = elements_of(found.store("alfheim").state())
    assert peer_elements == 7  # 4 writes, 2 model adds, 1 model remove

    found.restart("brage")
    assert found.status("brage") == "restarted"
    record = found.rejoin("brage", via="alfheim")
    validate_record(record)
    assert record["verdict"] == "rejoined"
    assert record["reason"] is None
    assert record["checkpoint_used"] is True
    # Exactly the five missed elements travel in; brage
    # holds nothing alfheim lacks, so nothing travels out.
    assert record["elements_received"] == 5
    assert record["elements_sent"] == 0
    assert record["elements_received"] < peer_elements
    assert record["converged"] is True
    assert found.status("brage") == "up"
    assert fingerprint(found.store("brage")) == fingerprint(found.store("alfheim"))
    rejoined = found.store("brage")
    assert rejoined.node("dain") is not None
    assert rejoined.node("alfheim")["census"]["ram_gb"] == 16.0
    assert rejoined.nodes_with_model("ygg-embed-1b") == ["solberg"]
    assert rejoined.nodes_with_model("ygg-base-7b") == []
    report = found.report()
    assert report["full_reloads"] == 0
    assert report["elements_transferred"] == 5
    assert report["digests_exchanged"] == 2


def test_the_mesh_refuses_acts_aimed_at_a_down_node_by_name():
    found = settled_mesh()
    found.crash("brage")
    with pytest.raises(RestartError, match="node_down"):
        found.store("brage")
    with pytest.raises(RestartError, match="node_down"):
        found.sync_pair("alfheim", "brage")
    with pytest.raises(RestartError, match="node_down"):
        found.checkpoint("brage")
    with pytest.raises(RestartError, match="node_down"):
        found.digest("brage")
    # …while the survivors carry on regardless.
    found.store("alfheim").put_node(census("alfheim", ram_gb=12.0))
    found.sync_pair("alfheim", "solberg")
    assert found.store("solberg").node("alfheim")["census"]["ram_gb"] == 12.0


# -- durability, stated honestly ------------------------------------


def test_a_write_neither_checkpointed_nor_gossiped_is_gone():
    found = settled_mesh()
    found.checkpoint("brage")
    found.store("brage").put_node(census("brage", ram_gb=64.0))  # never leaves brage
    found.crash("brage")
    found.restart("brage")
    # The checkpoint's older census is what survived.
    assert found.store("brage").node("brage")["census"]["ram_gb"] == 8.0
    record = found.rejoin("brage", via="alfheim")
    assert record["converged"] is True
    assert found.store("alfheim").node("brage")["census"]["ram_gb"] == 8.0


def test_a_checkpointed_write_survives_and_propagates_on_rejoin():
    found = settled_mesh()
    found.store("brage").put_node(census("brage", ram_gb=64.0))
    found.checkpoint("brage")  # saved after the write, never gossiped
    found.crash("brage")
    assert found.store("alfheim").node("brage")["census"]["ram_gb"] == 8.0
    found.restart("brage")
    record = found.rejoin("brage", via="alfheim")
    assert record["elements_sent"] == 1  # brage's own newer write, out by delta
    assert record["elements_received"] == 0
    assert record["converged"] is True
    assert found.store("alfheim").node("brage")["census"]["ram_gb"] == 64.0


def test_no_checkpoint_rejoin_is_a_total_delta_and_still_never_a_reload():
    found = settled_mesh()
    found.crash("brage")  # brage never checkpointed
    found.restart("brage")
    assert found.store("brage").nodes() == []
    record = found.rejoin("brage", via="solberg")
    assert record["checkpoint_used"] is False
    assert record["elements_received"] == elements_of(found.store("solberg").state())
    assert record["elements_received"] == 4
    assert record["converged"] is True
    assert found.report()["full_reloads"] == 0


def test_a_tombstone_struck_while_down_arrives_by_delta():
    found = settled_mesh()
    found.checkpoint("brage")
    found.crash("brage")
    assert found.store("alfheim").remove_node("brage")
    found.sync_pair("alfheim", "solberg")
    found.restart("brage")
    assert found.store("brage").node("brage") is not None  # checkpoint still holds it
    record = found.rejoin("brage", via="alfheim")
    assert record["converged"] is True
    assert found.store("brage").node("brage") is None  # the delete reached it too
    assert found.store("brage").report()["nodes_tombstoned"] == 1


# -- judged rejoins ---------------------------------------------------


def test_rejoin_refusals_are_named_counted_and_apply_nothing():
    found = settled_mesh()
    up = found.rejoin("alfheim", via="solberg")
    assert (up["verdict"], up["reason"]) == ("refused", "not_restarted")
    own = found.rejoin("alfheim", via="alfheim")
    assert own["reason"] == "self_rejoin"
    ghost = found.rejoin("alfheim", via="never-joined")
    assert ghost["reason"] == "unknown_peer"
    found.checkpoint("brage")
    found.crash("brage")
    early = found.rejoin("brage", via="alfheim")
    assert early["reason"] == "node_down"  # crashed, not yet restarted
    found.restart("brage")
    found.crash("solberg")
    before = fingerprint(found.store("brage"))
    dead_peer = found.rejoin("brage", via="solberg")
    assert dead_peer["reason"] == "peer_down"
    assert fingerprint(found.store("brage")) == before  # nothing applied
    assert found.status("brage") == "restarted"  # still owed its rejoin
    report = found.report()
    validate_report(report)
    assert report["rejoins_refused"] == 5
    assert report["refusals_by_reason"] == {
        "node_down": 1,
        "not_restarted": 1,
        "peer_down": 1,
        "self_rejoin": 1,
        "unknown_peer": 1,
    }
    assert report["rejoins"] == 0


def test_a_delta_past_the_policy_bound_is_refused_atomically():
    tight = default_policy()
    tight["max_delta_elements"] = 1
    found = settled_mesh(policy=tight)
    found.checkpoint("brage")
    found.crash("brage")
    found.store("alfheim").put_node(census("alfheim", ram_gb=16.0))
    found.store("solberg").put_node(census("solberg", ram_gb=32.0))
    found.sync_pair("alfheim", "solberg")
    peer_before = fingerprint(found.store("alfheim"))
    found.restart("brage")
    own_before = fingerprint(found.store("brage"))
    record = found.rejoin("brage", via="alfheim")
    assert (record["verdict"], record["reason"]) == ("refused", "delta_too_large")
    assert fingerprint(found.store("brage")) == own_before
    assert fingerprint(found.store("alfheim")) == peer_before
    assert found.status("brage") == "restarted"
    assert found.report()["elements_transferred"] == 0


def test_a_malformed_peer_digest_is_refused_and_counted():
    found = settled_mesh()
    found.checkpoint("brage")
    found.crash("brage")
    found.restart("brage")
    junk = found.rejoin("brage", via="alfheim", peer_digest={"bogus": True})
    assert (junk["verdict"], junk["reason"]) == ("refused", "malformed_digest")
    alien = found.digest("solberg")  # a real digest, but not the peer's
    wrong_name = found.rejoin("brage", via="alfheim", peer_digest=alien)
    assert wrong_name["reason"] == "malformed_digest"
    assert found.status("brage") == "restarted"
    honest = found.rejoin("brage", via="alfheim", peer_digest=found.digest("alfheim"))
    assert honest["verdict"] == "rejoined"


def test_apply_delta_judges_raw_deltas_at_the_seam():
    found = settled_mesh()
    found.store("alfheim").put_node(census("alfheim", ram_gb=24.0))  # brage is stale
    delta_state = compute_delta(
        found.store("alfheim").state(), found.digest("brage")
    )
    validate_crdt_state(delta_state)
    delta = {
        "version": 1,
        "from_node": "alfheim",
        "to_node": "brage",
        "at": "2026-11-02T12:00:00Z",
        "elements": elements_of(delta_state),
        "state": delta_state,
        "simulated": True,
    }
    validate_delta(delta)
    applied = found.apply_delta("brage", delta)
    assert applied["verdict"] == "applied"
    assert applied["elements_received"] == 1
    assert applied["converged"] is True
    assert found.store("brage").node("alfheim")["census"]["ram_gb"] == 24.0
    misaddressed = found.apply_delta("solberg", delta)
    assert misaddressed["reason"] == "delta_not_for_me"
    lying = {**delta, "elements": 99}
    with pytest.raises(RestartError, match="elements_mismatch"):
        validate_delta(lying)
    malformed = found.apply_delta("brage", lying)
    assert malformed["reason"] == "malformed_delta"
    shapeless = found.apply_delta("brage", {"version": 1})
    assert shapeless["reason"] == "malformed_delta"
    report = found.report()
    assert report["rejoins_refused"] == 0  # delta refusals are not rejoin refusals
    assert report["refusals_by_reason"] == {
        "delta_not_for_me": 1,
        "malformed_delta": 2,
    }


def test_api_misuse_raises_by_name():
    found = settled_mesh()
    with pytest.raises(RestartError, match="already_in_mesh"):
        found.add_node("alfheim")
    with pytest.raises(RestartError, match="unknown_node"):
        found.store("never-joined")
    with pytest.raises(RestartError, match="not_down"):
        found.restart("alfheim")
    found.crash("brage")
    with pytest.raises(RestartError, match="already_down"):
        found.crash("brage")
    with pytest.raises(RestartError, match="unknown_node"):
        found.rejoin("never-joined", via="alfheim")


def test_report_records_and_events_validate_with_honest_counts():
    found = settled_mesh()
    found.checkpoint("brage")
    found.crash("brage")
    found.store("alfheim").put_node(census("alfheim", ram_gb=16.0))
    found.sync_pair("alfheim", "solberg")
    found.restart("brage")
    found.rejoin("brage", via="alfheim")
    found.rejoin("brage", via="solberg")  # refused: already back up
    report = found.report()
    validate_report(report)
    assert report["crashes"] == 1
    assert report["restarts"] == 1
    assert report["rejoins"] == 1
    assert report["converged_rejoins"] == 1
    assert report["rejoins_refused"] == 1
    assert report["full_reloads"] == 0
    assert len(report["records"]) == 2
    for record in report["records"]:
        validate_record(record)
    standings = {n["node_id"]: n for n in report["nodes"]}
    assert standings["brage"]["status"] == "up"
    assert standings["brage"]["crashes"] == 1
    assert standings["brage"]["rejoins"] == 1
    assert standings["brage"]["checkpoint_elements"] == 4
    assert standings["alfheim"]["checkpoint_elements"] == 0
    for event in found.events:
        validate_event(event)
    kinds = [e["type"] for e in found.events]
    assert "restart.node_crashed" in kinds
    assert "restart.node_rejoined" in kinds
    # No census content rides any record or report.
    blob = json.dumps(report)
    assert "ram_gb" not in blob


def test_the_founding_readme_is_untouched():
    digest = hashlib.md5((ROOT / "README.md").read_bytes()).hexdigest()
    assert digest == README_MD5
