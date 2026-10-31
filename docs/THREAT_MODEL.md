# Threat Model — Y-010

*The mesh's enemies, named before its doors are built. Machine
register: `security/threat_register.json`, shaped by
`contracts/schemas/threat_record.schema.json`, proven by
`tests/test_threat_model.py`.*

## Scope and stance

The mesh is **local first**: household devices on one LAN, operated
by one household, no cloud, no external API at runtime. The enemies
below are ranked for that scale. WorldNet-scale enemies (Sybil
crowds, eclipse attacks, planetary poisoning) are catalogued later
(Y-215, Y-224) and are **NOT PROVEN** concerns of this local model —
nothing here claims to defeat them.

Stance, inherited from the founding spec (§6) and Part I contracts:

- Every node is its **key** (Y-011); every channel is **mTLS**
  (Y-051); every message is **signed** (Y-059).
- Tasks are **data, never code** (Y-007, enforced Y-043).
- A model is present only when its **hash verifies** (Y-026).
- Every result carries **provenance** (Y-098); every fact in Urðr
  carries its **source** (Y-110).
- **Honesty grade for this whole document:** at Y-010 every control
  is *designed* (mapped to the slice that builds it), none is yet
  *built*. The register's `status` field may only move to
  `control_built` in the commit of the slice that builds and tests
  the control.

## Attackers

| Attacker | Who that is at local scale |
|---|---|
| `outsider_on_lan` | A device or person on the home network (or a future guest network) that is not a mesh member |
| `rogue_or_compromised_node` | A member node whose key or software has been subverted |
| `malicious_caller` | Anyone or anything able to submit task envelopes within (or appearing within) granted scopes |
| `compromised_engine_or_model` | A tampered engine build or model file presenting as the trusted thing |
| `careless_operator` | The household's own mistakes: lost keys, phished credentials, an audit tap left on |

## The register (summary — the JSON is canonical)

| ID | Threat | Severity | First controls built by |
|---|---|---|---|
| T-01 | Node impersonation | critical | Y-011, Y-051, Y-059 |
| T-02 | Poisoned results | critical | Y-098, Y-124, Y-125, Y-132 |
| T-03 | Gossip / heartbeat spam | high | Y-060, Y-059, Y-064 |
| T-04 | Malicious tasks | critical | Y-043, Y-029, Y-117, Y-139, Y-070 |
| T-05 | Tampered or poisoned model files | high | Y-026, Y-037, Y-032 |
| T-06 | Eavesdropping / traffic analysis | high | Y-051, Y-070, Y-235 |
| T-07 | Resource exhaustion by legitimate-shaped load | high | Y-028, Y-084, Y-091 |
| T-08 | Registry / Urðr memory poisoning | high | Y-110, Y-112, Y-056, Y-111 |
| T-09 | Replay / duplicate execution | medium | Y-058, Y-061, Y-059 |
| T-10 | Operator-door abuse / credential theft | high | Y-196, Y-071, Y-234 |

The four enemies the roadmap named for this slice — node
impersonation, poisoned results, gossip spam, malicious tasks — are
T-01…T-04. The other six are the same enemies' close kin, found by
walking the contracts (Y-006…Y-008) and spec §6 while naming the
first four; each carries its residual risk in the register, stated
plainly, because a threat model that hides its leftovers is a lie.

## What this slice does NOT do

- It builds no control. Identity (Y-011) is the next slice and the
  first control.
- It claims no hardware proof: all fixtures and the register itself
  are paperwork — SIMULATED by nature, and labelled so in the
  register's own `note`.
- It does not touch federation: there is none (household meshes stay
  sovereign; Y-097, Y-186 prove separation, not connection).
