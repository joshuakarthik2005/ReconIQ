"""
Dashboard Generator (Part 7b)
===============================

Generates a self-contained HTML dashboard from pipeline outputs.
Reads ``reports/exceptions.json`` and ``reports/audit_trail.jsonl`` —
no pipeline modules imported, pure JSON consumer.

Output: ``reports/dashboard.html`` — one file, no external dependencies.
"""

import json
from collections import Counter
from pathlib import Path
from typing import List


def generate_dashboard(
    reports_dir: Path,
    output_path: Path,
) -> None:
    """Generate a self-contained HTML dashboard from pipeline outputs.

    Reads exceptions.json and audit_trail.jsonl — no pipeline imports.
    All data is embedded as JSON in the HTML for client-side rendering.
    """
    # ── Load data ────────────────────────────────────────────
    with open(reports_dir / "exceptions.json", "r", encoding="utf-8") as f:
        exceptions_data = json.load(f)

    audit_entries: List[dict] = []
    with open(reports_dir / "audit_trail.jsonl", "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                audit_entries.append(json.loads(line))

    summary = exceptions_data.get("summary", {})
    exceptions = exceptions_data.get("exceptions", [])

    # ── Pre-compute server-side aggregations ─────────────────
    # GL category breakdown (from classification entries)
    gl_counts: Counter = Counter()
    for entry in audit_entries:
        if entry["resolution_path"] == "classification":
            cat = entry.get("gl_category", "")
            if cat:
                gl_counts[cat] += 1

    # Rule breakdown (from match entries)
    rule_counts: Counter = Counter()
    for entry in audit_entries:
        if entry["resolution_path"] in ("rule", "llm"):
            rule = entry.get("rule_name", "")
            if rule:
                rule_counts[rule] += 1

    # Resolution path breakdown
    path_counts: Counter = Counter()
    for entry in audit_entries:
        path = entry["resolution_path"]
        if path != "classification":
            path_counts[path] += 1

    # Match entries for the matched records table
    match_entries = [
        e for e in audit_entries
        if e["resolution_path"] in ("rule", "llm")
    ]

    # ── Build HTML ───────────────────────────────────────────
    html = _build_html(
        summary=summary,
        exceptions=exceptions,
        gl_counts=dict(gl_counts.most_common()),
        rule_counts=dict(rule_counts.most_common()),
        path_counts=dict(sorted(path_counts.items())),
        match_entries=match_entries,
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)


def _build_html(
    summary: dict,
    exceptions: list,
    gl_counts: dict,
    rule_counts: dict,
    path_counts: dict,
    match_entries: list,
) -> str:
    """Build the complete self-contained HTML dashboard."""

    internal_count = summary.get("internal_count", 0)
    external_count = summary.get("external_count", 0)
    matched_count = summary.get("matched_count", 0)
    unmatched_int = summary.get("unmatched_internal", 0)
    unmatched_ext = summary.get("unmatched_external", 0)
    parse_errors = summary.get("parse_error_count", 0)
    match_rate = (matched_count / internal_count * 100) if internal_count else 0

    # Embed data as JSON for client-side filtering/sorting
    exceptions_json = json.dumps(exceptions, ensure_ascii=False)
    match_entries_json = json.dumps(match_entries, ensure_ascii=False)
    gl_counts_json = json.dumps(gl_counts, ensure_ascii=False)
    rule_counts_json = json.dumps(rule_counts, ensure_ascii=False)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AI Finance Controller — Reconciliation Dashboard</title>
<style>
:root {{
  --bg: #0f1117;
  --surface: #1a1d27;
  --surface2: #242836;
  --border: #2e3347;
  --text: #e4e7f0;
  --text-dim: #8b8fa3;
  --accent: #6c5ce7;
  --accent-light: #a29bfe;
  --green: #00b894;
  --green-dim: #00b89433;
  --red: #ff6b6b;
  --red-dim: #ff6b6b22;
  --orange: #fdcb6e;
  --orange-dim: #fdcb6e22;
  --blue: #74b9ff;
  --blue-dim: #74b9ff22;
  --font: 'Segoe UI', -apple-system, BlinkMacSystemFont, sans-serif;
  --mono: 'Cascadia Code', 'JetBrains Mono', 'Fira Code', monospace;
}}

* {{ margin: 0; padding: 0; box-sizing: border-box; }}

body {{
  font-family: var(--font);
  background: var(--bg);
  color: var(--text);
  line-height: 1.6;
  padding: 2rem;
  max-width: 1400px;
  margin: 0 auto;
}}

h1 {{
  font-size: 1.8rem;
  font-weight: 700;
  margin-bottom: 0.5rem;
  background: linear-gradient(135deg, var(--accent-light), var(--blue));
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  background-clip: text;
}}

.subtitle {{
  color: var(--text-dim);
  font-size: 0.9rem;
  margin-bottom: 2rem;
}}

/* ── KPI Cards ────────────────────────────────────── */

.kpi-grid {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 1rem;
  margin-bottom: 2rem;
}}

.kpi-card {{
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 1.25rem;
  transition: transform 0.15s ease, box-shadow 0.15s ease;
}}

.kpi-card:hover {{
  transform: translateY(-2px);
  box-shadow: 0 8px 24px rgba(0,0,0,0.3);
}}

.kpi-card .label {{
  font-size: 0.8rem;
  color: var(--text-dim);
  text-transform: uppercase;
  letter-spacing: 0.05em;
  margin-bottom: 0.5rem;
}}

.kpi-card .value {{
  font-size: 2rem;
  font-weight: 700;
}}

.kpi-card.green .value {{ color: var(--green); }}
.kpi-card.red .value {{ color: var(--red); }}
.kpi-card.blue .value {{ color: var(--blue); }}
.kpi-card.orange .value {{ color: var(--orange); }}
.kpi-card.accent .value {{ color: var(--accent-light); }}

/* ── Match rate ring ──────────────────────────────── */

.match-rate-container {{
  display: flex;
  align-items: center;
  gap: 1rem;
}}

.ring-chart {{
  width: 80px;
  height: 80px;
  border-radius: 50%;
  background: conic-gradient(
    var(--green) 0% {match_rate:.1f}%,
    var(--surface2) {match_rate:.1f}% 100%
  );
  display: flex;
  align-items: center;
  justify-content: center;
  position: relative;
}}

.ring-chart::after {{
  content: '';
  width: 56px;
  height: 56px;
  border-radius: 50%;
  background: var(--surface);
  position: absolute;
}}

.ring-chart .ring-value {{
  position: relative;
  z-index: 1;
  font-size: 1rem;
  font-weight: 700;
  color: var(--green);
}}

/* ── Section containers ───────────────────────────── */

.section {{
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 1.5rem;
  margin-bottom: 1.5rem;
}}

.section h2 {{
  font-size: 1.15rem;
  font-weight: 600;
  margin-bottom: 1rem;
  color: var(--accent-light);
}}

/* ── Tables ───────────────────────────────────────── */

table {{
  width: 100%;
  border-collapse: collapse;
  font-size: 0.85rem;
}}

thead th {{
  background: var(--surface2);
  color: var(--text-dim);
  text-transform: uppercase;
  font-size: 0.75rem;
  letter-spacing: 0.05em;
  padding: 0.75rem 1rem;
  text-align: left;
  border-bottom: 1px solid var(--border);
  cursor: pointer;
  user-select: none;
  white-space: nowrap;
}}

thead th:hover {{
  color: var(--accent-light);
}}

thead th .sort-arrow {{
  margin-left: 0.3em;
  opacity: 0.3;
}}

thead th.sorted .sort-arrow {{
  opacity: 1;
  color: var(--accent-light);
}}

tbody td {{
  padding: 0.6rem 1rem;
  border-bottom: 1px solid var(--border);
  vertical-align: top;
}}

tbody tr:hover {{
  background: var(--surface2);
}}

.reason-cell {{
  max-width: 500px;
  word-wrap: break-word;
  overflow-wrap: break-word;
}}

/* ── Filter bar ───────────────────────────────────── */

.filter-bar {{
  display: flex;
  gap: 0.75rem;
  margin-bottom: 1rem;
  flex-wrap: wrap;
  align-items: center;
}}

.filter-bar input,
.filter-bar select {{
  background: var(--surface2);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 0.5rem 0.75rem;
  color: var(--text);
  font-family: var(--font);
  font-size: 0.85rem;
  outline: none;
  transition: border-color 0.15s;
}}

.filter-bar input:focus,
.filter-bar select:focus {{
  border-color: var(--accent);
}}

.filter-bar input {{ flex: 1; min-width: 200px; }}

/* ── Breakdown bars ───────────────────────────────── */

.breakdown-list {{
  list-style: none;
}}

.breakdown-item {{
  display: flex;
  align-items: center;
  gap: 1rem;
  margin-bottom: 0.5rem;
}}

.breakdown-label {{
  width: 200px;
  font-size: 0.85rem;
  color: var(--text);
  text-align: right;
  flex-shrink: 0;
}}

.breakdown-bar-container {{
  flex: 1;
  background: var(--surface2);
  border-radius: 6px;
  height: 28px;
  overflow: hidden;
  position: relative;
}}

.breakdown-bar {{
  height: 100%;
  border-radius: 6px;
  transition: width 0.6s ease;
  display: flex;
  align-items: center;
  padding-left: 0.5rem;
  font-size: 0.75rem;
  font-weight: 600;
  color: white;
  min-width: 2rem;
}}

.breakdown-count {{
  width: 40px;
  font-size: 0.85rem;
  font-weight: 600;
  color: var(--text-dim);
  text-align: right;
  flex-shrink: 0;
}}

/* ── Badge ────────────────────────────────────────── */

.badge {{
  display: inline-block;
  padding: 0.15em 0.6em;
  border-radius: 4px;
  font-size: 0.75rem;
  font-weight: 600;
  font-family: var(--mono);
}}

.badge-rule {{ background: var(--green-dim); color: var(--green); }}
.badge-llm {{ background: var(--blue-dim); color: var(--blue); }}
.badge-exception {{ background: var(--red-dim); color: var(--red); }}
.badge-internal {{ background: var(--orange-dim); color: var(--orange); }}
.badge-external {{ background: var(--accent-light)22; color: var(--accent-light); }}

/* ── Responsive ───────────────────────────────────── */

@media (max-width: 768px) {{
  body {{ padding: 1rem; }}
  .kpi-grid {{ grid-template-columns: repeat(2, 1fr); }}
  .breakdown-label {{ width: 120px; font-size: 0.75rem; }}
  table {{ font-size: 0.75rem; }}
  thead th, tbody td {{ padding: 0.4rem 0.5rem; }}
}}
</style>
</head>
<body>

<h1>AI Finance Controller</h1>
<p class="subtitle">Reconciliation Dashboard &mdash; read-only view of pipeline results</p>

<!-- KPI Cards -->
<div class="kpi-grid">
  <div class="kpi-card green">
    <div class="label">Match Rate</div>
    <div class="match-rate-container">
      <div class="ring-chart"><span class="ring-value">{match_rate:.0f}%</span></div>
      <div><div class="value">{matched_count}/{internal_count}</div><div class="label" style="margin:0">internal matched</div></div>
    </div>
  </div>
  <div class="kpi-card blue">
    <div class="label">Rule Matches</div>
    <div class="value">{path_counts.get('rule', 0)}</div>
  </div>
  <div class="kpi-card accent">
    <div class="label">LLM Matches</div>
    <div class="value">{path_counts.get('llm', 0)}</div>
  </div>
  <div class="kpi-card red">
    <div class="label">Exceptions</div>
    <div class="value">{len(exceptions)}</div>
    <div class="label" style="margin:0">{unmatched_int} internal / {unmatched_ext} external</div>
  </div>
  <div class="kpi-card orange">
    <div class="label">External Records</div>
    <div class="value">{external_count}</div>
  </div>
  <div class="kpi-card" style="border-left: 3px solid var(--text-dim);">
    <div class="label">Parse Errors</div>
    <div class="value">{parse_errors}</div>
  </div>
</div>

<!-- GL Category Breakdown -->
<div class="section">
  <h2>GL Category Breakdown</h2>
  <ul class="breakdown-list" id="gl-breakdown"></ul>
</div>

<!-- Rule Breakdown -->
<div class="section">
  <h2>Matching Rule Distribution</h2>
  <ul class="breakdown-list" id="rule-breakdown"></ul>
</div>

<!-- Exceptions Table -->
<div class="section">
  <h2>What Broke &mdash; Exception Details</h2>
  <div class="filter-bar">
    <input type="text" id="exc-search" placeholder="Search by ID or reason...">
    <select id="exc-type-filter">
      <option value="">All types</option>
      <option value="unmatched_internal">Unmatched Internal</option>
      <option value="unmatched_external">Unmatched External</option>
      <option value="parse_error">Parse Error</option>
    </select>
  </div>
  <table id="exc-table">
    <thead>
      <tr>
        <th data-col="record_id">ID <span class="sort-arrow">&#x25B4;</span></th>
        <th data-col="record_type">Type <span class="sort-arrow">&#x25B4;</span></th>
        <th data-col="reason">Reason <span class="sort-arrow">&#x25B4;</span></th>
      </tr>
    </thead>
    <tbody id="exc-body"></tbody>
  </table>
</div>

<!-- Matched Records Table -->
<div class="section">
  <h2>Matched Records</h2>
  <div class="filter-bar">
    <input type="text" id="match-search" placeholder="Search by ID, rule, or detail...">
    <select id="match-path-filter">
      <option value="">All paths</option>
      <option value="rule">Rule</option>
      <option value="llm">LLM</option>
    </select>
  </div>
  <table id="match-table">
    <thead>
      <tr>
        <th data-col="record_id">Internal ID <span class="sort-arrow">&#x25B4;</span></th>
        <th data-col="matched_to">External ID <span class="sort-arrow">&#x25B4;</span></th>
        <th data-col="resolution_path">Path <span class="sort-arrow">&#x25B4;</span></th>
        <th data-col="rule_name">Rule <span class="sort-arrow">&#x25B4;</span></th>
        <th data-col="confidence">Conf <span class="sort-arrow">&#x25B4;</span></th>
        <th data-col="detail">Detail <span class="sort-arrow">&#x25B4;</span></th>
      </tr>
    </thead>
    <tbody id="match-body"></tbody>
  </table>
</div>

<script>
// ── Embedded Data ───────────────────────────────────
const EXCEPTIONS = {exceptions_json};
const MATCH_ENTRIES = {match_entries_json};
const GL_COUNTS = {gl_counts_json};
const RULE_COUNTS = {rule_counts_json};

// ── Breakdown Bars ──────────────────────────────────
const BAR_COLORS = [
  '#6c5ce7', '#00b894', '#74b9ff', '#fdcb6e', '#ff6b6b',
  '#a29bfe', '#55efc4', '#0984e3', '#e17055', '#d63031'
];

function renderBreakdown(containerId, counts) {{
  const el = document.getElementById(containerId);
  const maxVal = Math.max(...Object.values(counts));
  let i = 0;
  for (const [label, count] of Object.entries(counts)) {{
    const pct = (count / maxVal * 100).toFixed(1);
    const color = BAR_COLORS[i % BAR_COLORS.length];
    el.innerHTML += `
      <li class="breakdown-item">
        <span class="breakdown-label">${{label}}</span>
        <div class="breakdown-bar-container">
          <div class="breakdown-bar" style="width:${{pct}}%;background:${{color}}">${{count}}</div>
        </div>
        <span class="breakdown-count">${{count}}</span>
      </li>`;
    i++;
  }}
}}

renderBreakdown('gl-breakdown', GL_COUNTS);
renderBreakdown('rule-breakdown', RULE_COUNTS);

// ── Table Rendering & Sorting ───────────────────────
function escapeHtml(str) {{
  const d = document.createElement('div');
  d.textContent = str;
  return d.innerHTML;
}}

function badgeClass(type) {{
  if (type === 'rule') return 'badge-rule';
  if (type === 'llm') return 'badge-llm';
  if (type === 'exception') return 'badge-exception';
  if (type.includes('internal')) return 'badge-internal';
  if (type.includes('external')) return 'badge-external';
  return '';
}}

// ── Exception Table ─────────────────────────────────
let excSortCol = 'record_id', excSortAsc = true;
let excFiltered = [...EXCEPTIONS];

function renderExcTable() {{
  const tbody = document.getElementById('exc-body');
  tbody.innerHTML = excFiltered.map(e => `
    <tr>
      <td><code>${{escapeHtml(e.record_id)}}</code></td>
      <td><span class="badge ${{badgeClass(e.record_type)}}">${{escapeHtml(e.record_type)}}</span></td>
      <td class="reason-cell">${{escapeHtml(e.reason)}}</td>
    </tr>
  `).join('');
}}

function filterExceptions() {{
  const search = document.getElementById('exc-search').value.toLowerCase();
  const typeFilter = document.getElementById('exc-type-filter').value;
  excFiltered = EXCEPTIONS.filter(e => {{
    if (typeFilter && e.record_type !== typeFilter) return false;
    if (search) {{
      return e.record_id.toLowerCase().includes(search) ||
             e.reason.toLowerCase().includes(search);
    }}
    return true;
  }});
  sortExcTable(excSortCol, false);
}}

function sortExcTable(col, toggle = true) {{
  if (toggle) {{
    if (excSortCol === col) excSortAsc = !excSortAsc;
    else {{ excSortCol = col; excSortAsc = true; }}
  }}
  excFiltered.sort((a, b) => {{
    const va = (a[col] || '').toString().toLowerCase();
    const vb = (b[col] || '').toString().toLowerCase();
    return excSortAsc ? va.localeCompare(vb) : vb.localeCompare(va);
  }});
  // Update sort arrows
  document.querySelectorAll('#exc-table th').forEach(th => {{
    th.classList.toggle('sorted', th.dataset.col === excSortCol);
    const arrow = th.querySelector('.sort-arrow');
    if (arrow) arrow.textContent = (th.dataset.col === excSortCol && !excSortAsc) ? '\\u25BE' : '\\u25B4';
  }});
  renderExcTable();
}}

document.getElementById('exc-search').addEventListener('input', filterExceptions);
document.getElementById('exc-type-filter').addEventListener('change', filterExceptions);
document.querySelectorAll('#exc-table th').forEach(th => {{
  th.addEventListener('click', () => sortExcTable(th.dataset.col));
}});

sortExcTable('record_id', false);

// ── Match Table ─────────────────────────────────────
let matchSortCol = 'record_id', matchSortAsc = true;
let matchFiltered = [...MATCH_ENTRIES];

function renderMatchTable() {{
  const tbody = document.getElementById('match-body');
  tbody.innerHTML = matchFiltered.map(e => `
    <tr>
      <td><code>${{escapeHtml(e.record_id)}}</code></td>
      <td><code>${{escapeHtml(e.matched_to || '')}}</code></td>
      <td><span class="badge ${{badgeClass(e.resolution_path)}}">${{e.resolution_path}}</span></td>
      <td><code>${{escapeHtml(e.rule_name || '')}}</code></td>
      <td>${{e.confidence.toFixed(2)}}</td>
      <td class="reason-cell">${{escapeHtml(e.detail)}}</td>
    </tr>
  `).join('');
}}

function filterMatches() {{
  const search = document.getElementById('match-search').value.toLowerCase();
  const pathFilter = document.getElementById('match-path-filter').value;
  matchFiltered = MATCH_ENTRIES.filter(e => {{
    if (pathFilter && e.resolution_path !== pathFilter) return false;
    if (search) {{
      return e.record_id.toLowerCase().includes(search) ||
             (e.matched_to || '').toLowerCase().includes(search) ||
             (e.rule_name || '').toLowerCase().includes(search) ||
             e.detail.toLowerCase().includes(search);
    }}
    return true;
  }});
  sortMatchTable(matchSortCol, false);
}}

function sortMatchTable(col, toggle = true) {{
  if (toggle) {{
    if (matchSortCol === col) matchSortAsc = !matchSortAsc;
    else {{ matchSortCol = col; matchSortAsc = true; }}
  }}
  matchFiltered.sort((a, b) => {{
    let va = a[col], vb = b[col];
    if (typeof va === 'number') return matchSortAsc ? va - vb : vb - va;
    va = (va || '').toString().toLowerCase();
    vb = (vb || '').toString().toLowerCase();
    return matchSortAsc ? va.localeCompare(vb) : vb.localeCompare(va);
  }});
  document.querySelectorAll('#match-table th').forEach(th => {{
    th.classList.toggle('sorted', th.dataset.col === matchSortCol);
    const arrow = th.querySelector('.sort-arrow');
    if (arrow) arrow.textContent = (th.dataset.col === matchSortCol && !matchSortAsc) ? '\\u25BE' : '\\u25B4';
  }});
  renderMatchTable();
}}

document.getElementById('match-search').addEventListener('input', filterMatches);
document.getElementById('match-path-filter').addEventListener('change', filterMatches);
document.querySelectorAll('#match-table th').forEach(th => {{
  th.addEventListener('click', () => sortMatchTable(th.dataset.col));
}});

sortMatchTable('record_id', false);
</script>
</body>
</html>"""
