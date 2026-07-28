"""Report layer (SPEC-3, restructured by SPEC-6): the AI inventory record
(``inventory.html``) and the same record + technical annex (``report.html``),
rendered through one shared body so they cannot drift apart."""

from aiscan.report.html import render_report
from aiscan.report.inventory import render_inventory

__all__ = ["render_inventory", "render_report"]
