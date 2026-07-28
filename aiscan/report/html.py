"""``report.html`` generator (SPEC-3 §5, restructured by SPEC-6 §4).

The report is **the inventory record + a technical annex** — it embeds the
identical record body from :mod:`aiscan.report.inventory` (single renderer, so
the two artifacts cannot drift apart) and appends the drill-down material:
governance panel, full findings, per-call-site usages, declared env defaults,
the full AI-BOM, and scan health/provenance.

Security model (§5.2): every string that originated in scanned code or an LLM
response passes through ``esc`` (attributes included); a CSP meta tag forbids
any external resource. Deterministic: a pure function of the record.
"""

from __future__ import annotations

from aiscan.context import ScanHealth
from aiscan.inventory.schema import GovernanceSlot, Record
from aiscan.report.inventory import (
    enriched_slot,
    esc,
    evidence_links,
    inventory_body,
    page,
    val,
)

_SEV_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3, None: 4}


def _gov(slot: GovernanceSlot) -> str:
    if slot.value is not None:
        return esc(slot.value)
    if slot.candidate is not None:
        return f"{esc(slot.candidate)} <span class=meta>(candidate — unconfirmed)</span>"
    return "<span class=meta>awaiting governance</span>"


def _governance_panel(record: Record) -> str:
    slots = (
        ("Owner", _gov(record.owner)),
        ("Risk tier", _gov(record.risk_tier)),
        ("Purpose", _gov(record.purpose)),
        ("Data classification", _gov(record.data_classification)),
        ("Regulatory scope", _gov(record.regulatory_scope)),
        ("Lifecycle status", _gov(record.lifecycle_status)),
        ("Approval status", _gov(record.approval_status)),
        ("Approver", _gov(record.approver)),
        ("Approval date", _gov(record.approval_date)),
        ("Last review", _gov(record.last_review)),
        ("Next review", _gov(record.next_review)),
    )
    rows = "".join(f"<dt>{esc(k)}</dt><dd>{v}</dd>" for k, v in slots)
    cmdb = (
        f"<code>{esc(record.cmdb_app_id.ref)}</code>"
        if record.cmdb_app_id.ref
        else "<span class=meta>no CMDB link</span>"
    )
    return (
        "<h2>Governance</h2>"
        "<p class=meta>Fields the review process owns. The scanner never fills their "
        "values — at most it supplies clearly-labelled candidates for a human to "
        "confirm.</p><dl>"
        + rows
        + f"<dt>CMDB application id</dt><dd>{cmdb}</dd></dl>"
    )


def _findings(record: Record) -> str:
    if not record.findings:
        return "<h2>Findings</h2><p class=meta>None.</p>"
    ordered = sorted(
        record.findings, key=lambda f: (_SEV_ORDER.get(f.severity, 4), f.kind, f.evidence)
    )
    rows = "".join(
        "<tr><td>"
        + (
            f"<span class='sev {f.severity}'>{esc(f.severity.upper())}</span>"
            if f.severity
            else ""
        )
        + f"</td><td><code>{esc(f.kind)}</code></td>"
        + f"<td>{esc(f.subject_ref or '—')}</td>"
        + f"<td>{evidence_links(f.evidence)}</td>"
        + f"<td>{esc(f.detail or '')}</td></tr>"
        for f in ordered
    )
    return (
        "<h2>Findings</h2>"
        "<p class=meta>The scanner's review queue: things a reviewer should look at "
        "before certifying the record above — policy risks (high/medium, e.g. an "
        "unapproved endpoint or a model that could not be determined) and analysis "
        "notes (info). Findings qualify the record; they are not part of it.</p>"
        "<table><tr><th>Severity</th><th>Kind</th><th>Subject</th>"
        f"<th>Evidence</th><th>Detail</th></tr>{rows}</table>"
    )


def _usages_annex(record: Record) -> str:
    if not record.model_usages:
        return ""
    rows = "".join(
        "<tr><td>"
        + val(u.model.value)
        + f"</td><td>{esc(u.task)}</td><td>"
        + val(u.endpoint)
        + "</td><td>"
        + (val(u.provider_class.value) if u.provider_class else "&mdash;")
        + f"</td><td>{'yes' if u.in_agent else 'no'}</td><td>{evidence_links(u.evidence)}</td></tr>"
        for u in record.model_usages
    )
    return (
        "<h2>Model call sites (full)</h2>"
        "<p class=meta>Every individual LLM call location detected in code — the "
        "evidence trail behind the record's deduplicated “Models used” table. "
        "One row per call site, machine-grade values.</p>"
        "<table><tr><th>Model</th><th>Task</th>"
        "<th>Endpoint</th><th>Provider class</th><th>In agent</th><th>Evidence</th></tr>"
        f"{rows}</table>"
    )


def _env_defaults_annex(record: Record) -> str:
    if not record.env_defaults:
        return ""
    rows = "".join(
        f"<tr><td><code>{esc(key)}</code></td><td>"
        + "<br>".join(
            f"<code>{esc(d.value)}</code> "
            f"<span class=meta>({esc(d.form)} — {esc(d.source)})</span>"
            for d in entries
        )
        + "</td></tr>"
        for key, entries in sorted(record.env_defaults.items())
    )
    return (
        "<h2>Declared env defaults</h2>"
        "<p class=meta>Values the repo's own config declares for env keys referenced "
        "by detected facts — candidates, not detections.</p>"
        "<table><tr><th>Env key</th><th>Declared value(s)</th></tr>"
        f"{rows}</table>"
    )


def _bom_annex(record: Record) -> str:
    if not record.ai_dependencies:
        return ""
    rows = "".join(
        f"<tr><td><code>{esc(d.package)}</code></td><td>{esc(d.version or '—')}</td>"
        f"<td>{evidence_links(d.evidence) if d.evidence else esc(d.source or '—')}</td>"
        f"<td>{'yes' if d.used else '<b>no — dormant</b>'}</td></tr>"
        for d in record.ai_dependencies
    )
    return (
        "<h2>AI dependencies (full BOM)</h2>"
        "<p class=meta>AI packages declared in dependency manifests and whether the "
        "analysed code actually imports them (“no — dormant” is a "
        "declared-but-unused dependency).</p>"
        "<table><tr><th>Package</th><th>Version</th>"
        f"<th>Declared in</th><th>Imported?</th></tr>{rows}</table>"
    )


def _tool_annex(record: Record) -> str:
    if not record.tools:
        return ""
    parts = [
        "<h2>Tools (full detail)</h2>",
        "<p class=meta>Machine-grade tool records: raw side-effect labels, "
        "signatures, credential references and evidence spans behind the record's "
        "Tools section.</p>",
    ]
    for tool in record.tools:
        chips = "".join(f"<span class='chip on'>{esc(s)}</span>" for s in tool.side_effects)
        sensitive = (
            "<span class='chip on'>sensitive</span>"
            if tool.is_sensitive is not None and tool.is_sensitive.value
            else ""
        )
        body = ["<dl>"]
        body.append(f"<dt>Kind</dt><dd>{esc(tool.kind)}</dd>")
        body.append(f"<dt>Location</dt><dd>{esc(tool.location)}</dd>")
        if tool.signature is not None:
            body.append(
                "<dt>Signature</dt><dd><code>("
                + ", ".join(esc(p) for p in tool.signature.params)
                + ")</code></dd>"
            )
        body.append(f"<dt>Side effects</dt><dd>{chips or '&mdash;'}</dd>")
        body.append(f"<dt>External target</dt><dd>{val(tool.external_target)}</dd>")
        body.append(f"<dt>Credential ref</dt><dd>{val(tool.credential_ref)}</dd>")
        if tool.declared_authorisation.value:
            body.append(
                f"<dt>Declared authorisation</dt><dd>{esc(tool.declared_authorisation.value)}</dd>"
            )
        body.append(f"<dt>Evidence</dt><dd>{evidence_links(tool.evidence)}</dd>")
        for label, slot in (
            ("Summary", tool.tool_summary),
            ("Capability class", tool.capability_class),
            ("Data domain", tool.data_domain),
        ):
            body.append(f"<dt>{label}</dt><dd>{enriched_slot(slot)}</dd>")
        body.append("</dl>")
        parts.append(
            f"<details><summary>{esc(tool.tool_id)} {sensitive}</summary>"
            f"<div>{''.join(body)}</div></details>"
        )
    return "".join(parts)


def _enrichment_banner(health: ScanHealth) -> str:
    note = health.enrichment
    if note is None:
        return (
            "<p class=note>Enrichment: not requested — run with <code>--enrich</code>"
            " to draft the summary fields.</p>"
        )
    status = note.get("status")
    if status == "ok":
        return (
            f"<p class=note>Enrichment: ok — {esc(note.get('drafted', 0))} drafted, "
            f"{esc(note.get('grounded_false', 0))} insufficient-evidence, "
            f"{esc(note.get('failed', 0))} failed.</p>"
        )
    reason = note.get("reason", "unknown")
    return f"<p class=note>Enrichment {esc(status)}: {esc(reason)}.</p>"


def _footer(record: Record) -> str:
    health = record.scan_health
    prov = record.inventory_provenance
    rulepacks = ", ".join(f"{esc(k)}@{esc(v)}" for k, v in sorted(prov.rulepacks.items()))
    sinks = (
        ", ".join(f"{esc(k)}: {v}" for k, v in sorted(health.sinks.items()))
        if health.sinks
        else "none"
    )
    return (
        "<h2>Scan health &amp; provenance</h2>"
        + _enrichment_banner(health)
        + "<dl>"
        + f"<dt>Files / LOC</dt><dd>{health.files} / {health.loc}</dd>"
        + f"<dt>Parse errors</dt><dd>{len(health.parse_errors)}</dd>"
        + f"<dt>Sinks</dt><dd>{sinks}</dd>"
        + (
            "<dt>Resolver give-ups</dt><dd>"
            + ", ".join(
                f"{esc(k)}: {v}" for k, v in sorted(health.resolver.top.items())
            )
            + "</dd>"
            if health.resolver.top
            else ""
        )
        + f"<dt>Findings</dt><dd>{health.findings}</dd>"
        + f"<dt>Scanner</dt><dd>{esc(prov.scanner)}</dd>"
        + f"<dt>Rule packs</dt><dd>{rulepacks or '&mdash;'}</dd>"
        + (
            "<dt>Org pack</dt><dd>"
            + (
                esc(prov.org_pack.replace("\\", "/").rsplit("/", 1)[-1])
                if prov.org_pack
                else "&mdash;"
            )
            + "</dd>"
        )
        + (
            f"<dt>Enrichment model</dt><dd>{esc(prov.enrichment_model)}</dd>"
            if prov.enrichment_model
            else ""
        )
        + f"<dt>Detection basis</dt><dd>{esc(prov.detection_basis)}</dd>"
        + "</dl>"
        + "<footer>Generated by aiscan — machine-readable source of truth: record.json.</footer>"
    )


def render_report(record: Record, graph_svg: str | None = None) -> str:
    """The record body (shared with inventory.html) + the technical annex."""
    sections: list[str] = [inventory_body(record, graph_svg, annex_href="#annex")]
    sections.append(
        "<h2 id=annex>— Technical annex —</h2>"
        "<p class=meta>Everything below is the scan's working detail: the evidence, "
        "review queue and diagnostics behind the inventory record above. Reviewers "
        "certify from the record; engineers and auditors drill down here.</p>"
    )
    if record.ai_verdict != "no_ai":
        sections.append(_governance_panel(record))
        sections.append(_findings(record))
        sections.append(_tool_annex(record))
        sections.append(_usages_annex(record))
        sections.append(_env_defaults_annex(record))
        sections.append(_bom_annex(record))
    sections.append(_footer(record))
    return page(f"{record.name} — AI inventory", "".join(sections))
