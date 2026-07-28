"""LLM enrichment layer (SPEC-2 §4).

Drafts the [E] summary fields from the record's detected facts + one bounded
code slice per node. It summarises a verified inventory record; it never
re-analyses the code, writes [G]/[X], or overrides detected [D]. Off the
critical path: the record is complete and valid without it, and with
``grounded=false`` it writes a null summary with a reason, never a fabrication.
"""
