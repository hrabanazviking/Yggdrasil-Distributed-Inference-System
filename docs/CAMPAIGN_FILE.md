# The Campaign File — Y-019

*The roadmap, as machine data — so keepers and boards can run the
road the humans read. The Mythic Coder M-201 pattern, applied to
the Growing of the Tree.*

## What stands

`data/campaign.json` is the whole build roadmap in one versioned
data file (`yggdrasil-campaign-v1`), registered in the Y-016 data
manifest and shaped by three schemas in `contracts/schemas/`:

- **`campaign_file.schema.json`** — the whole file: version,
  owning slice, the roadmap's title, the source it mirrors, the
  totals (11 parts, 244 slices, held as constants so a truncated
  file fails its own shape), the parts and the slices.
- **`campaign_part.schema.json`** — one part: number, roman
  numeral, name, its goal *in the roadmap's own words*, the slice
  range it spans, and how many slices it holds.
- **`campaign_slice.schema.json`** — one slice: id (`Y-NNN`),
  number, the part it belongs to, a short title (the slice's own
  heading phrase), and its **full text carried over with its
  wording intact** — whitespace reflowed where the markdown wraps
  a line, nothing else touched.

## The mirror rule

The markdown roadmap (`docs/YGGDRASIL_BUILD_ROADMAP_2026-10-30.md`)
is the law; the JSON is its mirror, and the mirror is *generated,
never hand-edited*:

- `yggdrasil.campaign.build_campaign(roadmap_text)` parses the
  markdown — part headers, goals, and slice bullets — under strict
  rules: a slice outside its part's declared range, a goal that
  never closes, a broken sequence, all refused loudly.
- `yggdrasil.campaign.verify_campaign()` regenerates the file
  from the roadmap on disk and compares it part by part and slice
  by slice. **Any drift fails** — a slice edited in the markdown
  but not regenerated, or a hand-edit in the JSON, stops the
  build until the two agree again. To change the road: edit the
  roadmap, regenerate the file, commit both together.
- `check_structure` holds the completeness rules in code: part
  numbers run 1–11, slice numbers run 1–244 in order with ids to
  match, every slice sits inside its part's span, and every
  part's `slice_count` agrees with the slices that name it.

## What keepers and boards get

- `load_campaign()` — the road, schema-validated and complete.
- `slice_by_id(campaign, "Y-019")` — one slice, or a refusal.
- `first_unbanked(campaign, banked_ids)` — the next slice in
  roadmap order given the ids already banked, or `None` at the
  road's end. A banked id the campaign does not know is refused:
  a ledger naming slices that do not exist is a bug, not a
  detail.

## Honest limits

- **Banking state is not kept here.** Which slices are banked,
  and the evidence for each, lives in `CAMPAIGN_STATE.md` — the
  ledger a human reads. The campaign file names the road; it does
  not keep score, and it claims no slice's state.
- This is build-time paperwork, like Y-016's manifest and Y-018's
  register: no running mesh reads the campaign file. It serves
  the keepers and boards building the mesh.
