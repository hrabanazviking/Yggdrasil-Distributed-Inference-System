"""CRDT store tests — Y-056's acceptance (ADR-003).

The slice must prove: (a) the registry + membership
store is an LWW element set per node record under the
ADR-003 stamp triple — newest write wins whole, ties
break by node_id, the same stamp with different facts
is a counted conflict settled deterministically, and a
write's stamp must be authored by its own node;
(b) deletes are tombstones — an older write cannot
resurrect a removed node, a newer write is a genuine
rejoin, and tombstones prune only after their epoch
retention, together with the write they superseded;
(c) models are an observed-remove set — an observed
offer removed is gone, a concurrent (unobserved) offer
survives the remove and is present when it arrives;
(d) THE PROPERTIES ADR-003 demands: merge is
idempotent and order-insensitive — divergent replicas
merged in every permutation, and piecemeal state
fragments merged in seeded shuffles, all resolve to
identical resolved records AND identical mergeable
state; merge is associative; (e) the Lamport clock
ticks on local acts and rises on observed stamps;
(f) reports, states, and events validate, counts are
honest, and the founding README is untouched.

Everything here is the in-process model: no socket is
opened and no LAN is crossed, and nothing claims one
was (real carriage is Y-062's).
"""

import copy
import hashlib
import itertools
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from yggdrasil.crdt_stores import (
    CrdtError,
    CrdtStore,
    default_policy,
    make_stamp,
    stamp_key,
    validate_policy,
    validate_record,
    validate_report,
    validate_stamp,
    validate_state,
)
from yggdrasil.observability import validate_event
from yggdrasil.simulator import make_fake_census

ROOT = Path(__file__).resolve().parents[1]
README_MD5 = "89a37a4e18cab9a998e1751c32e7bfd5"
EPOCH = datetime(2026, 10, 31, 12, 0, 0, tzinfo=UTC)


class Clock:
    def __init__(self):
        self.moment = EPOCH

    def __call__(self):
        return self.moment

    def advance(self, seconds):
        self.moment += timedelta(seconds=seconds)


def census(node_id, *, ram_gb=8.0, capabilities=("generate",)):
    found = make_fake_census(node_id, capabilities=capabilities)
    found["ram_gb"] = ram_gb
    return found


def store(node_id="alfheim", clock=None, policy=None):
    return CrdtStore(
        local_node_id=node_id, policy=policy, now=clock or Clock()
    )


def stamp(node_id, lamport, *, seconds=0):
    return make_stamp(
        node_id, lamport=lamport, wall_time=EPOCH + timedelta(seconds=seconds)
    )


# -- stamps and policy -------------------------------------------


def test_stamp_total_order_breaks_ties_by_node_id():
    low = stamp("alfheim", 3, seconds=5)
    high = stamp("jotunheim", 3, seconds=5)
    assert stamp_key(low) < stamp_key(high)
    assert stamp_key(stamp("alfheim", 4)) > stamp_key(high)
    assert stamp_key(stamp("alfheim", 3, seconds=6)) > stamp_key(high)
    validate_stamp(low)
    with pytest.raises(CrdtError):
        validate_stamp({**low, "lamport": -1})


def test_policy_is_data_and_bounded():
    policy = default_policy()
    assert policy["tombstone_retention_epochs"] == 1
    validate_policy(policy)
    with pytest.raises(CrdtError):
        validate_policy({**policy, "tombstone_retention_epochs": 0})
    with pytest.raises(CrdtError):
        validate_policy({**policy, "max_nodes": 0})


# -- LWW element set ----------------------------------------------


def test_newest_write_wins_whole_and_older_is_stale():
    s = store()
    first = s.put_node(census("alfheim", ram_gb=8.0), stamp=stamp("alfheim", 1))
    assert first["census"]["ram_gb"] == 8.0
    newer = s.put_node(census("alfheim", ram_gb=16.0), stamp=stamp("alfheim", 2))
    assert newer["census"]["ram_gb"] == 16.0
    assert (
        s.put_node(census("alfheim", ram_gb=4.0), stamp=stamp("alfheim", 1))
        is not None
    )
    assert s.node("alfheim")["census"]["ram_gb"] == 16.0
    report = s.report()
    assert report["writes_applied"] == 2
    assert report["writes_stale"] == 1


def test_tie_breaks_by_wall_time_then_deterministically():
    a = store("alfheim")
    b = store("jotunheim")
    a.put_node(census("midgard", ram_gb=8.0), stamp=stamp("midgard", 2, seconds=1))
    b.put_node(census("midgard", ram_gb=32.0), stamp=stamp("midgard", 2, seconds=9))
    a.sync(b)
    for replica in (a, b):
        assert replica.node("midgard")["census"]["ram_gb"] == 32.0


def test_same_stamp_different_facts_is_a_counted_deterministic_conflict():
    a = store("alfheim")
    b = store("jotunheim")
    a.put_node(census("midgard", ram_gb=8.0), stamp=stamp("midgard", 2))
    b.put_node(census("midgard", ram_gb=32.0), stamp=stamp("midgard", 2))
    a.sync(b)
    assert a.node("midgard") == b.node("midgard")
    assert a.report()["conflicts"] + b.report()["conflicts"] >= 1
    # the settlement replays identically on fresh replicas
    c = store("vanaheim")
    d = store("niflheim")
    c.put_node(census("midgard", ram_gb=32.0), stamp=stamp("midgard", 2))
    d.put_node(census("midgard", ram_gb=8.0), stamp=stamp("midgard", 2))
    c.sync(d)
    assert c.node("midgard") == a.node("midgard")


def test_a_write_must_be_authored_by_its_own_node():
    s = store()
    with pytest.raises(CrdtError):
        s.put_node(census("alfheim"), stamp=stamp("jotunheim", 1))
    with pytest.raises(CrdtError):
        s.add_model("alfheim", "fixture-7b", stamp=stamp("jotunheim", 1))
    with pytest.raises(CrdtError):
        s.put_node({"node_id": "alfheim"})


def test_store_is_bounded_by_policy():
    s = store(policy={**default_policy(), "max_nodes": 1})
    s.put_node(census("alfheim"), stamp=stamp("alfheim", 1))
    assert s.put_node(census("jotunheim"), stamp=stamp("jotunheim", 1)) is None
    assert s.report()["nodes_present"] == 1


# -- tombstones -----------------------------------------------------


def test_tombstone_blocks_resurrection_and_newer_write_rejoins():
    s = store()
    s.put_node(census("alfheim"), stamp=stamp("alfheim", 1))
    assert s.remove_node("alfheim", stamp=stamp("alfheim", 2)) is True
    assert s.node("alfheim") is None
    # the old write travelling late cannot resurrect it
    assert s.put_node(census("alfheim"), stamp=stamp("alfheim", 1)) is None
    assert s.node("alfheim") is None
    # a genuinely newer write is a rejoin, not a resurrection
    rejoined = s.put_node(census("alfheim", ram_gb=16.0), stamp=stamp("alfheim", 3))
    assert rejoined["census"]["ram_gb"] == 16.0
    assert s.report()["withdrawn_node_ids"] == ["alfheim"]


def test_stale_remove_records_the_tombstone_but_the_node_stands():
    s = store()
    s.put_node(census("alfheim", ram_gb=16.0), stamp=stamp("alfheim", 5))
    assert s.remove_node("alfheim", stamp=stamp("alfheim", 2)) is False
    assert s.node("alfheim")["census"]["ram_gb"] == 16.0


def test_tombstones_prune_after_their_epoch_with_their_write():
    s = store()
    s.put_node(census("alfheim"), stamp=stamp("alfheim", 1))
    s.remove_node("alfheim", stamp=stamp("alfheim", 2))
    assert s.advance_epoch() == 2
    assert s.report()["nodes_tombstoned"] == 1  # retained for its epoch
    assert s.advance_epoch() == 3
    report = s.report()
    assert report["nodes_tombstoned"] == 0
    assert report["tombstones_pruned"] == 1
    assert s.node("alfheim") is None
    assert s.state()["writes"] == []  # the superseded write went with it


# -- observed-remove models ------------------------------------------


def test_observed_remove_kills_only_what_was_observed():
    home = store("alfheim")
    home.put_node(census("alfheim"), stamp=stamp("alfheim", 1))
    tag = home.add_model("alfheim", "fixture-7b", stamp=stamp("alfheim", 2))
    validate_stamp(tag)
    assert home.node("alfheim")["models"] == ["fixture-7b"]

    peer = store("jotunheim")
    peer.merge(home)
    assert peer.node("alfheim")["models"] == ["fixture-7b"]
    assert peer.remove_model("alfheim", "fixture-7b") is True

    # a concurrent offer the remover never observed, made
    # on a third replica under its own stamp, survives
    third = store("midgard")
    third.merge(home)
    third.add_model("alfheim", "fixture-7b", stamp=stamp("alfheim", 3))

    home.merge(peer)
    home.merge(third)
    assert home.node("alfheim")["models"] == ["fixture-7b"]
    assert home.nodes_with_model("fixture-7b") == ["alfheim"]

    # and once that surviving add IS observed and removed,
    # it is gone everywhere the remove reaches
    assert home.remove_model("alfheim", "fixture-7b") is True
    peer.merge(home)
    assert peer.node("alfheim")["models"] == []


def test_remove_of_an_unobserved_model_is_no_op():
    s = store()
    assert s.remove_model("alfheim", "never-offered") is False
    assert s.report()["model_removes"] == 0


# -- the clock --------------------------------------------------------


def test_lamport_clock_ticks_locally_and_rises_on_observation():
    s = store("alfheim")
    record = s.put_node(census("alfheim"))
    assert record["stamp"]["lamport"] == 1
    assert s.clock == 1
    s.merge_state(
        {
            "version": 1,
            "epoch": 1,
            "simulated": True,
            "writes": [
                {
                    "node_id": "jotunheim",
                    "stamp": stamp("jotunheim", 41),
                    "census": census("jotunheim"),
                }
            ],
            "tombstones": [],
            "model_adds": [],
            "model_removes": [],
        }
    )
    assert s.clock == 41
    follow = s.put_node(census("alfheim", ram_gb=16.0))
    assert follow["stamp"]["lamport"] == 42


# -- THE PROPERTIES: idempotent, order-insensitive, associative -------


def divergent_replicas():
    """Three replicas that lived apart: overlapping
    writes at competing stamps, a delete on one road, a
    model offered on one road and withdrawn on another."""
    a = store("alfheim")
    a.put_node(census("alfheim", ram_gb=8.0), stamp=stamp("alfheim", 1))
    a.put_node(census("midgard", ram_gb=8.0), stamp=stamp("midgard", 1))
    a.add_model("midgard", "fixture-7b", stamp=stamp("midgard", 2))

    b = store("jotunheim")
    b.put_node(census("jotunheim"), stamp=stamp("jotunheim", 1))
    b.put_node(census("midgard", ram_gb=32.0), stamp=stamp("midgard", 3))
    b.remove_node("alfheim", stamp=stamp("alfheim", 4))
    b.add_model("jotunheim", "fixture-13b", stamp=stamp("jotunheim", 2))

    c = store("vanaheim")
    c.put_node(census("alfheim", ram_gb=16.0), stamp=stamp("alfheim", 2))
    c.put_node(census("vanaheim"), stamp=stamp("vanaheim", 1))
    c.merge(a)
    c.remove_model("midgard", "fixture-7b")
    return a, b, c


def resolved(store_):
    return [(r["node_id"], r["census"]["ram_gb"], r["models"]) for r in store_.nodes()]


def test_merge_converges_in_every_permutation():
    replicas = divergent_replicas()
    states = [r.state() for r in replicas]
    outcomes = set()
    for order in itertools.permutations(range(3)):
        fresh = store("niflheim")
        for index in order:
            fresh.merge_state(copy.deepcopy(states[index]))
        outcomes.add(
            (
                tuple(
                    (r["node_id"], r["census"]["ram_gb"], tuple(r["models"]))
                    for r in fresh.nodes()
                ),
                repr(sorted(fresh.state()["writes"], key=repr)),
                repr(sorted(fresh.state()["tombstones"], key=repr)),
                repr(sorted(fresh.state()["model_adds"], key=repr)),
                repr(sorted(fresh.state()["model_removes"], key=repr)),
            )
        )
    assert len(outcomes) == 1
    merged = store("niflheim")
    for state in states:
        merged.merge_state(copy.deepcopy(state))
    by_id = {r["node_id"]: r for r in merged.nodes()}
    assert sorted(by_id) == ["jotunheim", "midgard", "vanaheim"]
    assert by_id["midgard"]["census"]["ram_gb"] == 32.0
    assert by_id["midgard"]["models"] == []  # observed on c's road, removed
    assert by_id["jotunheim"]["models"] == ["fixture-13b"]


def test_merge_is_idempotent():
    a, b, _ = divergent_replicas()
    a.merge(b)
    once_state, once_nodes = a.state(), a.nodes()
    a.merge(b)
    a.merge_state(b.state())
    assert a.state() == once_state
    assert a.nodes() == once_nodes


def test_merge_is_associative():
    a, b, c = divergent_replicas()
    left = store("niflheim")
    left.merge(a)
    left.merge(b)
    left.merge(c)
    bc = store("muspelheim")
    bc.merge(b)
    bc.merge(c)
    right = store("niflheim")
    right.merge(a)
    right.merge(bc)
    assert left.nodes() == right.nodes()
    assert left.state() == right.state()


def fragments(state):
    """One state broken into single-element fragments —
    the piecemeal road gossip actually travels."""
    pieces = []
    for key in ("writes", "tombstones", "model_adds", "model_removes"):
        for item in state[key]:
            piece = {
                "version": 1,
                "epoch": state["epoch"],
                "simulated": True,
                "writes": [],
                "tombstones": [],
                "model_adds": [],
                "model_removes": [],
            }
            piece[key] = [copy.deepcopy(item)]
            pieces.append(piece)
    return pieces


def test_piecemeal_merges_converge_under_seeded_shuffles():
    replicas = divergent_replicas()
    whole = store("niflheim")
    for replica in replicas:
        whole.merge(replica)
    pieces = []
    for replica in replicas:
        pieces.extend(fragments(replica.state()))
    for seed in (1, 7, 42, 99, 2026):
        shuffled = list(pieces)
        random.Random(seed).shuffle(shuffled)
        fresh = store("svartalfheim")
        for piece in shuffled:
            fresh.merge_state(piece)
        assert fresh.nodes() == whole.nodes()
        assert fresh.state() == whole.state()


def test_sync_converges_both_replicas_and_self_merge_is_refused():
    a, b, _ = divergent_replicas()
    a.sync(b)
    assert a.nodes() == b.nodes()
    assert a.state() == b.state()
    with pytest.raises(CrdtError):
        a.merge(a)
    with pytest.raises(CrdtError):
        a.sync(a)


# -- reports, state, events --------------------------------------------


def test_report_state_and_events_validate():
    s = store()
    s.put_node(census("alfheim"), stamp=stamp("alfheim", 1))
    s.add_model("alfheim", "fixture-7b", stamp=stamp("alfheim", 2))
    s.remove_node("jotunheim", stamp=stamp("jotunheim", 9))
    report = s.report()
    validate_report(report)
    validate_state(s.state())
    for record in report["records"]:
        validate_record(record)
    assert report["nodes_present"] == 1
    assert report["models_present"] == 1
    assert report["nodes_tombstoned"] == 1
    assert report["withdrawn_node_ids"] == ["jotunheim"]
    assert report["simulated"] is True
    assert s.events
    for event in s.events:
        validate_event(event)
        assert event["source"]["component"] == "registry"


def test_malformed_state_is_refused_by_name():
    s = store()
    with pytest.raises(CrdtError):
        s.merge_state({"version": 1, "epoch": 1, "simulated": True})
    with pytest.raises(CrdtError):
        validate_state({**s.state(), "simulated": False})


def test_readme_untouched():
    digest = hashlib.md5((ROOT / "README.md").read_bytes()).hexdigest()
    assert digest == README_MD5
