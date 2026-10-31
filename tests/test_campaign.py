"""Campaign-file tests — Y-019's acceptance.

The campaign file must prove: (a) it carries the whole road —
11 parts, 244 slices, in order, each slice inside its part's
declared span; (b) it is a true mirror of the roadmap markdown —
regenerating it from docs/YGGDRASIL_BUILD_ROADMAP_2026-10-30.md
reproduces the file exactly, and any drift, in either direction,
is refused with the first difference named; (c) its contents are
the roadmap's own words — spot-checked against slices a reader
can find in the markdown by eye; (d) the keeper's question works
(first unbanked slice, road's end, unknown banked ids refused);
and (e) it is registered in the Y-016 manifest like every other
standing data file. The campaign file is paperwork for keepers —
it keeps no banking state and claims no slice's state.
"""

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from yggdrasil.campaign import (
    CampaignError,
    build_campaign,
    check_structure,
    first_unbanked,
    load_campaign,
    render_campaign,
    slice_by_id,
    verify_campaign,
)
from yggdrasil.data_discipline import schema_version, validate_repo

ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = ROOT / "contracts" / "schemas"
ROADMAP = (ROOT / "docs" / "YGGDRASIL_BUILD_ROADMAP_2026-10-30.md").read_text()
CAMPAIGN = load_campaign()


def test_schemas_are_valid():
    for name in (
        "campaign_file.schema.json",
        "campaign_part.schema.json",
        "campaign_slice.schema.json",
    ):
        schema = json.loads((SCHEMAS / name).read_text())
        Draft202012Validator.check_schema(schema)
        assert schema_version(schema) == 1


def test_the_whole_road_is_carried():
    assert CAMPAIGN["campaign"] == "yggdrasil-campaign-v1"
    assert CAMPAIGN["roadmap_slice"] == "Y-019"
    assert CAMPAIGN["title"] == "Yggdrasil — The Growing of the Tree"
    assert CAMPAIGN["total_parts"] == 11
    assert CAMPAIGN["total_slices"] == 244
    assert [s["id"] for s in CAMPAIGN["slices"]] == [
        f"Y-{n:03d}" for n in range(1, 245)
    ]
    # The roadmap's own part table (§3), re-measured from the file.
    expected_counts = {
        1: 20, 2: 28, 3: 26, 4: 26, 5: 22, 6: 22,
        7: 22, 8: 22, 9: 20, 10: 22, 11: 14,
    }
    assert {p["number"]: p["slice_count"] for p in CAMPAIGN["parts"]} == expected_counts
    check_structure(CAMPAIGN)  # raises on any gap, span or count fault


def test_parts_carry_the_roadmaps_own_words():
    part_one = CAMPAIGN["parts"][0]
    assert part_one["roman"] == "I"
    assert part_one["name"] == "Roots: decisions, contracts, scaffolding"
    assert part_one["first_slice"] == "Y-001"
    assert part_one["last_slice"] == "Y-020"
    assert part_one["goal"].startswith(
        "turn a specification into a buildable project"
    )
    part_eleven = CAMPAIGN["parts"][10]
    assert part_eleven["roman"] == "XI"
    assert part_eleven["name"] == "The Proof Spine"
    for part in CAMPAIGN["parts"]:
        assert len(part["goal"]) > 40  # a real goal, never a stub


def test_slice_text_is_the_roadmaps_own_wording():
    y019 = slice_by_id(CAMPAIGN, "Y-019")
    assert y019["title"] == "The campaign file"
    assert y019["part"] == 1
    assert y019["text"] == (
        "The campaign file: this roadmap as machine data (the "
        "Mythic Coder M-201 pattern) so keepers and boards can run it."
    )
    y244 = slice_by_id(CAMPAIGN, "Y-244")
    assert y244["part"] == 11
    assert y244["text"].endswith("evidence index attached.")
    y172 = slice_by_id(CAMPAIGN, "Y-172")
    assert "NOT PROVEN" in y172["text"]  # the honesty survives the mirror
    with pytest.raises(CampaignError, match="no such slice"):
        slice_by_id(CAMPAIGN, "Y-245")


def test_the_mirror_is_exact_and_drift_is_refused(tmp_path):
    rebuilt = build_campaign(ROADMAP)
    assert render_campaign(rebuilt) == (ROOT / "data" / "campaign.json").read_text()
    assert verify_campaign() == CAMPAIGN

    def mini_repo(campaign_text, roadmap_text):
        root = tmp_path / "repo"
        (root / "data").mkdir(parents=True, exist_ok=True)
        (root / "docs").mkdir(exist_ok=True)
        (root / "contracts").mkdir(exist_ok=True)
        import shutil

        shutil.copytree(SCHEMAS, root / "contracts" / "schemas", dirs_exist_ok=True)
        (root / "data" / "campaign.json").write_text(campaign_text)
        (root / "docs" / "YGGDRASIL_BUILD_ROADMAP_2026-10-30.md").write_text(roadmap_text)
        return root

    # A hand-edit in the JSON: one slice's title quietly changed.
    tampered = json.loads(render_campaign(rebuilt))
    tampered["slices"][18]["title"] = "A quieter title"
    root = mini_repo(render_campaign(tampered), ROADMAP)
    with pytest.raises(CampaignError, match="drift in Y-019"):
        verify_campaign(root)

    # An edit in the markdown that was never regenerated.
    edited = ROADMAP.replace(
        "so keepers and boards can run it.",
        "so keepers and boards can run it twice.",
    )
    assert edited != ROADMAP
    root = mini_repo(render_campaign(rebuilt), edited)
    with pytest.raises(CampaignError, match="drift in Y-019"):
        verify_campaign(root)


def test_incomplete_roads_are_refused():
    broken = json.loads(render_campaign(CAMPAIGN))
    del broken["slices"][100]  # a slice goes missing
    with pytest.raises(CampaignError):
        check_structure(broken)
    broken = json.loads(render_campaign(CAMPAIGN))
    broken["slices"][0]["part"] = 2  # a slice names the wrong part
    with pytest.raises(CampaignError, match="outside"):
        check_structure(broken)
    broken = json.loads(render_campaign(CAMPAIGN))
    broken["parts"][0]["slice_count"] = 19  # a count that lies
    with pytest.raises(CampaignError, match="slice_count"):
        check_structure(broken)


def test_first_unbanked_is_the_keepers_question():
    assert first_unbanked(CAMPAIGN, set())["id"] == "Y-001"
    banked = {f"Y-{n:03d}" for n in range(1, 19)}  # Y-001 – Y-018
    assert first_unbanked(CAMPAIGN, banked)["id"] == "Y-019"
    assert first_unbanked(CAMPAIGN, {s["id"] for s in CAMPAIGN["slices"]}) is None
    with pytest.raises(CampaignError, match="does not know"):
        first_unbanked(CAMPAIGN, {"Y-999"})


def test_registered_in_the_data_manifest():
    reports = {r.id: r for r in validate_repo()}
    entry = reports["campaign_file"]
    assert entry.version == "yggdrasil-campaign-v1"
    assert entry.items_checked == 244  # every slice shaped by its schema
