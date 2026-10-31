# The Registry Contract v1 — Y-008

*Spec §4.2's capability-aware model registry, as data.*

A `ModelEntry` (`model_entry.schema.json`) names a model family and
name, its quants (name, format, size), its capabilities, the device
classes it is runnable on, and — once Part VIII's hardware work lands
— **measured** speeds per device class, each carrying a grade
(`proven | partial | unproven`). An entry without measurements is a
claim; an entry with them is a fact, and Verðandi's placement math
(Y-087) may only spend facts.

The registry store itself is CRDT-replicated (ADR-003); entries are
data files under version control in this repo until the live registry
service (Y-053) takes over custody.
