"""The campaign file — Y-019.

The roadmap, as machine data. The human roadmap
(``docs/YGGDRASIL_BUILD_ROADMAP_2026-10-30.md``) is the law of this
build — 244 slices in 11 parts, written to be read. Keepers,
boards, and tooling cannot run a markdown essay, so this slice
gives them the same road in a shape they can run (the Mythic Coder
M-201 pattern: the campaign as data, not as prose):

- ``data/campaign.json`` carries every part (number, name, goal in
  the roadmap's own words, the slice range it spans) and every
  slice (id, number, part, short title, and the slice's full text
  carried over with its wording intact — whitespace reflowed where
  the markdown wraps a line, nothing else touched). It is
  schema-shaped (``campaign_file`` / ``campaign_part`` /
  ``campaign_slice``) and registered in the Y-016 data manifest
  like every other standing data file.
- The file is *generated*, never hand-edited:
  :func:`parse_roadmap` reads the roadmap markdown and
  :func:`build_campaign` produces the data. :func:`verify_campaign`
  regenerates it and refuses any drift — if the markdown and the
  machine data ever disagree, one of them was edited without the
  other, and the build stops until they agree again. The markdown
  stays the law; this file is its mirror, and the mirror is
  checked.
- :func:`first_unbanked` is the keeper's question in one call:
  given the ids already banked, which slice is next in roadmap
  order? Banking itself stays where it has always lived, in
  ``CAMPAIGN_STATE.md`` — this module names the road, it does not
  keep the ledger, and it claims no slice's state.

Honesty: this is build-time paperwork, like Y-016/Y-018. No
running mesh reads the campaign file; it serves the people and
keepers building the mesh.
"""

import json
import re
from pathlib import Path

from jsonschema import validators
from referencing import Registry, Resource

_ROOT = Path(__file__).resolve().parents[2]
_CAMPAIGN_PATH = _ROOT / "data" / "campaign.json"
_ROADMAP_PATH = _ROOT / "docs" / "YGGDRASIL_BUILD_ROADMAP_2026-10-30.md"

CAMPAIGN_VERSION = "yggdrasil-campaign-v1"
_ROADMAP_RELATIVE = "docs/YGGDRASIL_BUILD_ROADMAP_2026-10-30.md"

_ROMAN = {
    "I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6,
    "VII": 7, "VIII": 8, "IX": 9, "X": 10, "XI": 11,
}
_PART_RE = re.compile(
    r"^## Part (?P<roman>I|II|III|IV|V|VI|VII|VIII|IX|X|XI) — "
    r"(?P<name>.+) \(Y-(?P<first>[0-9]{3}) – Y-(?P<last>[0-9]{3})\)$"
)
_SLICE_RE = re.compile(r"^- \*\*Y-(?P<number>[0-9]{3})\*\* (?P<text>.*)$")
_TITLE_RE = re.compile(r"^# (?P<title>.+)$")


class CampaignError(Exception):
    """Any campaign-file failure: the roadmap cannot be parsed into
    a complete road, the machine data fails its schema, or the two
    have drifted apart."""


def _validator(root: Path, name: str):
    """A validator for one schema, with every contract schema
    registered so relative $refs resolve — the registry pattern
    the earlier slices build."""
    schema_dir = root / "contracts" / "schemas"
    schema = json.loads((schema_dir / name).read_text())
    resources = []
    for path in sorted(schema_dir.glob("*.json")):
        other = json.loads(path.read_text())
        resource = Resource.from_contents(other)
        resources.append((path.name, resource))
        resources.append((other["$id"], resource))
        resources.append((other["$id"].rsplit("/", 1)[0] + "/" + path.name, resource))
    registry = Registry().with_resources(resources)
    return validators.validator_for(schema)(schema, registry=registry)


def _normalise(lines: list[str]) -> str:
    """Reflow wrapped markdown lines into one string: wording and
    punctuation untouched, line breaks become single spaces."""
    return " ".join(part.strip() for part in lines if part.strip())


def _slice_title(text: str) -> str:
    """A slice's short title: its heading phrase, up to the first
    ': ' (or the first ' — ') — the way the roadmap itself heads
    each slice. A slice whose text has neither keeps its first
    sentence as its title; the full text always rides in `text`."""
    for separator in (": ", " — ", "; "):
        index = text.find(separator)
        if 0 < index <= 100:
            return text[:index]
    sentence = text.split(". ", 1)[0]
    if len(sentence) <= 140:
        return sentence.removesuffix(".")
    return text[:120].rsplit(" ", 1)[0]


def parse_roadmap(roadmap_text: str) -> tuple[str, list[dict], list[dict]]:
    """Parse the roadmap markdown into (title, parts, slices).

    Strict on purpose: a part header that does not match the named
    shape, a goal that never closes, a slice outside any part, or
    a slice whose number falls outside its part's declared range
    are all refused. A roadmap that changed shape must fail loudly
    here, not quietly produce a different campaign file.
    """
    title = None
    parts: list[dict] = []
    slices: list[dict] = []
    current_part = None
    pending: list[str] | None = None  # lines of the slice being read
    goal_lines: list[str] | None = None

    def flush_slice() -> None:
        nonlocal pending
        if pending is None:
            return
        match = _SLICE_RE.match(pending[0])
        number = int(match.group("number"))
        text = _normalise([match.group("text"), *pending[1:]])
        first = int(current_part["first_slice"][2:])
        last = int(current_part["last_slice"][2:])
        if not first <= number <= last:
            raise CampaignError(
                f"slice Y-{number:03d} sits in Part {current_part['roman']} "
                f"but outside its declared range "
                f"{current_part['first_slice']} – {current_part['last_slice']}"
            )
        slices.append(
            {
                "id": f"Y-{number:03d}",
                "number": number,
                "part": current_part["number"],
                "title": _slice_title(text),
                "text": text,
            }
        )
        pending = None

    for line in roadmap_text.splitlines():
        if title is None:
            title_match = _TITLE_RE.match(line)
            if title_match:
                title = title_match.group("title")
            continue
        part_match = _PART_RE.match(line)
        if part_match:
            flush_slice()
            if goal_lines is not None:
                raise CampaignError("a part opened while a goal was still open")
            current_part = {
                "number": _ROMAN[part_match.group("roman")],
                "roman": part_match.group("roman"),
                "name": part_match.group("name"),
                "goal": "",
                "first_slice": f"Y-{part_match.group('first')}",
                "last_slice": f"Y-{part_match.group('last')}",
                "slice_count": 0,
            }
            parts.append(current_part)
            continue
        if line.startswith("## "):
            flush_slice()
            current_part = None  # the parts are over; the rest is prose
            continue
        if current_part is None:
            continue
        if goal_lines is not None:
            goal_lines.append(line)
            if line.rstrip().endswith("*"):
                goal = _normalise(goal_lines)
                body = goal.removeprefix("*").removesuffix("*").strip()
                if not body.startswith("Goal:"):
                    raise CampaignError(f"part goal does not open with 'Goal:': {goal!r}")
                current_part["goal"] = body[len("Goal:"):].strip()
                goal_lines = None
            continue
        if line.startswith("*Goal:"):
            goal_lines = [line]
            if line.rstrip().endswith("*") and len(line.strip()) > 1:
                goal = _normalise(goal_lines)
                body = goal.removeprefix("*").removesuffix("*").strip()
                current_part["goal"] = body[len("Goal:"):].strip()
                goal_lines = None
            continue
        if _SLICE_RE.match(line):
            flush_slice()
            pending = [line]
            continue
        if line.strip() == "---":
            flush_slice()
            continue
        if pending is not None:
            if line.strip() == "":
                flush_slice()
            else:
                pending.append(line)
    flush_slice()
    if goal_lines is not None:
        raise CampaignError("the roadmap ends inside a part goal")
    if title is None:
        raise CampaignError("the roadmap carries no '# ' title")
    for part in parts:
        part["slice_count"] = sum(1 for s in slices if s["part"] == part["number"])
    return title, parts, slices


def build_campaign(roadmap_text: str) -> dict:
    """The campaign file's contents, built from the roadmap text
    and checked for a complete road: 11 parts, 244 slices, ids
    running Y-001 – Y-244 with no gap, no duplicate, no slice
    outside its part's span, and each part's count matching the
    slices actually parsed for it."""
    title, parts, slices = parse_roadmap(roadmap_text)
    campaign = {
        "campaign": CAMPAIGN_VERSION,
        "roadmap_slice": "Y-019",
        "title": title,
        "source_roadmap": _ROADMAP_RELATIVE,
        "note": (
            "The build roadmap as machine data (Y-019), GENERATED from "
            "docs/YGGDRASIL_BUILD_ROADMAP_2026-10-30.md by "
            "yggdrasil.campaign.build_campaign and drift-checked against it "
            "by verify_campaign: the markdown is the law, this file is its "
            "mirror. Slice text is the roadmap's own wording, reflowed "
            "where the markdown wraps a line. Banking state is NOT kept "
            "here — it lives in CAMPAIGN_STATE.md."
        ),
        "total_parts": len(parts),
        "total_slices": len(slices),
        "parts": parts,
        "slices": slices,
    }
    check_structure(campaign)
    return campaign


def check_structure(campaign: dict) -> None:
    """The road's completeness rules, in code: totals agree with
    the lists, part numbers run 1 – 11, slice numbers run
    1 – 244 in order with ids to match, every slice sits inside
    its part's declared range, and every part's slice_count is
    the count of slices that name it."""
    parts = campaign["parts"]
    slices = campaign["slices"]
    if campaign["total_parts"] != len(parts) or len(parts) != 11:
        raise CampaignError(
            f"total_parts {campaign['total_parts']} != {len(parts)} parts (want 11)"
        )
    if campaign["total_slices"] != len(slices) or len(slices) != 244:
        raise CampaignError(
            f"total_slices {campaign['total_slices']} != {len(slices)} slices (want 244)"
        )
    if [p["number"] for p in parts] != list(range(1, 12)):
        raise CampaignError("part numbers do not run 1 – 11 in order")
    if [s["number"] for s in slices] != list(range(1, 245)):
        raise CampaignError("slice numbers do not run 1 – 244 in order")
    for slice_ in slices:
        if slice_["id"] != f"Y-{slice_['number']:03d}":
            raise CampaignError(
                f"slice id {slice_['id']!r} does not match its number {slice_['number']}"
            )
        part = parts[slice_["part"] - 1]
        first = int(part["first_slice"][2:])
        last = int(part["last_slice"][2:])
        if not first <= slice_["number"] <= last:
            raise CampaignError(
                f"{slice_['id']} names part {part['number']} but sits outside "
                f"its range {part['first_slice']} – {part['last_slice']}"
            )
    for part in parts:
        counted = sum(1 for s in slices if s["part"] == part["number"])
        if part["slice_count"] != counted:
            raise CampaignError(
                f"Part {part['roman']} declares slice_count {part['slice_count']} "
                f"but {counted} slices name it"
            )
        expected = int(part["last_slice"][2:]) - int(part["first_slice"][2:]) + 1
        if counted != expected:
            raise CampaignError(
                f"Part {part['roman']} spans {expected} slices but holds {counted}"
            )


def load_campaign(root: Path | None = None) -> dict:
    """Load the campaign file, validate it against its schemas,
    and check the road is complete. Raises CampaignError on any
    disagreement."""
    root = Path(root) if root is not None else _ROOT
    path = root / "data" / "campaign.json"
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise CampaignError(f"cannot load campaign file {path}: {exc}") from exc
    validator = _validator(root, "campaign_file.schema.json")
    errors = sorted(validator.iter_errors(raw), key=str)
    if errors:
        raise CampaignError(f"campaign file fails its schema: {errors[0].message}")
    if raw["campaign"] != CAMPAIGN_VERSION:
        raise CampaignError(f"unexpected campaign version: {raw['campaign']!r}")
    if raw["roadmap_slice"] != "Y-019":
        raise CampaignError(f"unexpected owning slice: {raw['roadmap_slice']!r}")
    check_structure(raw)
    return raw


def render_campaign(campaign: dict) -> str:
    """The campaign file's canonical on-disk form — the house data
    style: one line, keys sorted, the repo's own characters kept."""
    return json.dumps(campaign, sort_keys=True, ensure_ascii=False)


def verify_campaign(root: Path | None = None) -> dict:
    """The drift check: regenerate the campaign from the roadmap
    markdown on disk and compare it, part by part and slice by
    slice, with the campaign file on disk. Any difference — a
    slice edited in one and not the other, a part renamed, a goal
    reworded — is refused with the first difference named."""
    root = Path(root) if root is not None else _ROOT
    on_disk = load_campaign(root)
    try:
        roadmap_text = (root / on_disk["source_roadmap"]).read_text()
    except OSError as exc:
        raise CampaignError(f"cannot load source roadmap: {exc}") from exc
    rebuilt = build_campaign(roadmap_text)
    if rebuilt["title"] != on_disk["title"]:
        raise CampaignError(
            f"campaign drift: title {on_disk['title']!r} != roadmap {rebuilt['title']!r}"
        )
    for built_part, disk_part in zip(rebuilt["parts"], on_disk["parts"], strict=True):
        if built_part != disk_part:
            raise CampaignError(
                f"campaign drift in Part {disk_part['roman']}: "
                f"file {disk_part!r} != roadmap {built_part!r}"
            )
    for built_slice, disk_slice in zip(rebuilt["slices"], on_disk["slices"], strict=True):
        if built_slice != disk_slice:
            raise CampaignError(
                f"campaign drift in {disk_slice['id']}: "
                f"file {disk_slice!r} != roadmap {built_slice!r}"
            )
    if rebuilt != on_disk:
        raise CampaignError("campaign drift outside the parts and slices")
    return on_disk


def slice_by_id(campaign: dict, slice_id: str) -> dict:
    """One slice by its id; an unknown id is refused, never
    silently missed."""
    for slice_ in campaign["slices"]:
        if slice_["id"] == slice_id:
            return slice_
    raise CampaignError(f"no such slice in the campaign file: {slice_id!r}")


def first_unbanked(campaign: dict, banked_ids) -> dict | None:
    """The keeper's question: the first slice in roadmap order
    whose id is not in `banked_ids`, or None when the road is
    fully banked. A banked id the campaign does not know is
    refused — a ledger naming slices that do not exist is a bug,
    not a detail."""
    known = {s["id"] for s in campaign["slices"]}
    unknown = sorted(set(banked_ids) - known)
    if unknown:
        raise CampaignError(f"banked ids the campaign does not know: {unknown}")
    banked = set(banked_ids)
    for slice_ in campaign["slices"]:
        if slice_["id"] not in banked:
            return slice_
    return None
