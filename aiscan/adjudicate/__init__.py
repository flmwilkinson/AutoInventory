"""Optional LLM adjudication tier (SPEC §6.10) — off by default.

Bounded, cached, never load-bearing: the scanner is fully useful without it.
Facts created here carry ``method: llm_adjudicated``, are capped at medium
confidence, and may only *add* where deterministic layers abstained.
"""
