"""HTML rendering for the operational UI.

Server-rendered, no build step, no framework, no CDN. The pipeline's only
runtime dependency stays PyYAML, and the dashboard runs on a machine with no
network access - which is the normal state of a claims-processing environment.

Visual decisions worth recording
--------------------------------
*   **Magnitude is one hue; status is a dot plus a word.** The six analytics
    classifications are *states*, and the four status hues placed side by side
    as bar fills fail the adjacent-pair separation gate (``warning`` and
    ``serious`` measure only 13.6 normal-vision Delta E apart). So every bar is
    drawn in a single sequential blue, and each row carries a status dot
    *beside its text label*. Identity never rests on colour alone, which is
    also what makes the dashboard legible to a colourblind analyst and in
    print.
*   **No dual axes anywhere.** Where two measures matter, they get two charts.
*   **Tabular figures in tables, proportional in stat tiles**, so columns align
    vertically without making the headline numbers look mechanical.
*   **Dark mode is selected, not inverted**: its own steps, declared under both
    the OS media query and the explicit theme attribute, so a viewer's toggle
    wins either way.
"""

from __future__ import annotations

import html
import json
from collections.abc import Iterable, Sequence
from typing import Any

# Palette roles (reference instance of the data-viz system).
_CSS = """
:root {
  color-scheme: light;
  --plane:        #f9f9f7;
  --surface:      #fcfcfb;
  --surface-2:    #f3f3f0;
  --ink:          #0b0b0b;
  --ink-2:        #52514e;
  --ink-muted:    #898781;
  --grid:         #e1e0d9;
  --axis:         #c3c2b7;
  --border:       rgba(11,11,11,0.10);
  --bar:          #2a78d6;
  --bar-soft:     #9ec5f4;
  --good:         #0ca30c;
  --warning:      #fab219;
  --serious:      #ec835a;
  --critical:     #d03b3b;
  --neutral:      #898781;
  --shadow:       0 1px 2px rgba(11,11,11,0.04), 0 1px 1px rgba(11,11,11,0.03);
}
@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) {
    color-scheme: dark;
    --plane:     #0d0d0d;
    --surface:   #1a1a19;
    --surface-2: #232321;
    --ink:       #f0efec;
    --ink-2:     #c3c2b7;
    --ink-muted: #898781;
    --grid:      #2c2c2a;
    --axis:      #383835;
    --border:    rgba(255,255,255,0.10);
    --bar:       #3987e5;
    --bar-soft:  #184f95;
    --shadow:    none;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --plane:     #0d0d0d;
  --surface:   #1a1a19;
  --surface-2: #232321;
  --ink:       #f0efec;
  --ink-2:     #c3c2b7;
  --ink-muted: #898781;
  --grid:      #2c2c2a;
  --axis:      #383835;
  --border:    rgba(255,255,255,0.10);
  --bar:       #3987e5;
  --bar-soft:  #184f95;
  --shadow:    none;
}

* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body {
  background: var(--plane);
  color: var(--ink);
  font: 14px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
  -webkit-font-smoothing: antialiased;
}
a { color: var(--bar); text-decoration: none; }
a:hover { text-decoration: underline; }

.shell { display: flex; min-height: 100vh; }
.side {
  width: 232px; flex: 0 0 232px; background: var(--surface);
  border-right: 1px solid var(--border); padding: 20px 0; position: sticky;
  top: 0; height: 100vh; overflow-y: auto;
}
.brand { padding: 0 20px 18px; }
.brand b { display: block; font-size: 15px; letter-spacing: -0.01em; }
.brand span { display: block; color: var(--ink-muted); font-size: 11.5px;
  text-transform: uppercase; letter-spacing: 0.07em; margin-top: 3px; }
.nav { display: flex; flex-direction: column; gap: 1px; padding: 0 8px; }
.nav a {
  display: flex; justify-content: space-between; align-items: center;
  gap: 8px; padding: 7px 12px; border-radius: 7px; color: var(--ink-2);
  font-size: 13.5px;
}
.nav a:hover { background: var(--surface-2); text-decoration: none; }
.nav a[aria-current="page"] { background: var(--surface-2); color: var(--ink); font-weight: 600; }
.nav .pill {
  font-size: 11px; font-variant-numeric: tabular-nums; color: var(--ink-muted);
  background: var(--plane); border: 1px solid var(--border);
  border-radius: 999px; padding: 1px 7px;
}
.nav-group { padding: 16px 20px 6px; font-size: 10.5px; text-transform: uppercase;
  letter-spacing: 0.08em; color: var(--ink-muted); }

.main { flex: 1; min-width: 0; padding: 26px 32px 72px; max-width: 1480px; }
header.page { display: flex; align-items: baseline; justify-content: space-between;
  gap: 20px; flex-wrap: wrap; margin-bottom: 22px; }
h1 { font-size: 21px; margin: 0 0 4px; letter-spacing: -0.015em; }
.sub { color: var(--ink-2); font-size: 13px; margin: 0; max-width: 78ch; }
h2 { font-size: 14px; margin: 0 0 14px; letter-spacing: -0.005em; }
h3 { font-size: 12.5px; margin: 18px 0 8px; color: var(--ink-2);
  text-transform: uppercase; letter-spacing: 0.06em; }

.card {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 11px; padding: 17px 19px; box-shadow: var(--shadow);
}
.grid { display: grid; gap: 14px; }
.g-tiles { grid-template-columns: repeat(auto-fit, minmax(158px, 1fr)); }
.g-2 { grid-template-columns: repeat(auto-fit, minmax(340px, 1fr)); }

.tile .k { color: var(--ink-muted); font-size: 11.5px; text-transform: uppercase;
  letter-spacing: 0.06em; }
.tile .v { font-size: 25px; font-weight: 600; letter-spacing: -0.02em; margin-top: 5px; }
.tile .n { color: var(--ink-2); font-size: 12px; margin-top: 3px; }

table { width: 100%; border-collapse: collapse; font-size: 13px;
  font-variant-numeric: tabular-nums; }
th { text-align: left; font-weight: 600; font-size: 11px; color: var(--ink-muted);
  text-transform: uppercase; letter-spacing: 0.06em; padding: 0 10px 7px 0;
  border-bottom: 1px solid var(--grid); white-space: nowrap; }
td { padding: 7px 10px 7px 0; border-bottom: 1px solid var(--grid);
  vertical-align: top; }
tbody tr:hover { background: var(--surface-2); }
td.num, th.num { text-align: right; padding-right: 14px; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
.wrap { white-space: normal; max-width: 46ch; }
.muted { color: var(--ink-muted); }
.dim { color: var(--ink-2); }

.dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%;
  margin-right: 7px; vertical-align: 0.5px; flex: 0 0 8px; }
.s-good { background: var(--good); }
.s-warning { background: var(--warning); }
.s-serious { background: var(--serious); }
.s-critical { background: var(--critical); }
.s-neutral { background: var(--neutral); }

.tag { display: inline-block; font-size: 11px; line-height: 1.5;
  padding: 1px 7px; border-radius: 5px; background: var(--surface-2);
  border: 1px solid var(--border); color: var(--ink-2); margin: 1px 3px 1px 0;
  white-space: nowrap; }
.tag.bad { border-color: var(--critical); color: var(--critical); }
.tag.warn { border-color: var(--serious); color: var(--ink-2); }

.bars { display: flex; flex-direction: column; gap: 9px; }
.bar-row { display: grid; grid-template-columns: minmax(186px, 38%) 1fr auto;
  align-items: center; gap: 12px; }
.bar-label { display: flex; align-items: center; font-size: 12.5px; color: var(--ink-2);
  min-width: 0; }
.bar-label span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.bar-track { height: 9px; background: var(--surface-2); border-radius: 4px; }
.bar-fill { height: 9px; background: var(--bar); border-radius: 0 4px 4px 0; }
.bar-val { font-size: 12.5px; font-variant-numeric: tabular-nums;
  color: var(--ink); min-width: 52px; text-align: right; }

.mini { display: inline-block; width: 70px; height: 7px; background: var(--surface-2);
  border-radius: 3px; overflow: hidden; vertical-align: middle; margin-right: 8px; }
.mini > i { display: block; height: 7px; background: var(--bar); border-radius: 0 3px 3px 0; }

.callout { border: 1px solid var(--critical); border-left-width: 3px;
  border-radius: 9px; padding: 15px 17px; background: var(--surface); }
.callout h2 { color: var(--critical); display: flex; align-items: center; gap: 8px; }
.callout .ev { font-size: 12.5px; color: var(--ink-2); margin: 5px 0 0;
  padding-left: 14px; border-left: 2px solid var(--grid); }

.split { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1.25fr);
  gap: 14px; align-items: start; }
pre.json { margin: 0; padding: 13px; background: var(--surface-2);
  border-radius: 8px; font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 11.5px; line-height: 1.55; overflow: auto; max-height: 560px;
  color: var(--ink-2); white-space: pre-wrap; word-break: break-word; }

form.filters { display: flex; flex-wrap: wrap; gap: 9px; align-items: center;
  margin-bottom: 16px; }
select, input[type=search], button {
  font: inherit; font-size: 13px; padding: 5px 9px; border-radius: 7px;
  border: 1px solid var(--border); background: var(--surface); color: var(--ink);
}
button { cursor: pointer; }
button:hover { background: var(--surface-2); }
.theme { background: none; border: 1px solid var(--border); border-radius: 7px;
  padding: 4px 9px; font-size: 12px; color: var(--ink-2); cursor: pointer; }
.empty { color: var(--ink-muted); font-size: 13px; padding: 22px 0; text-align: center; }
.kv { display: grid; grid-template-columns: auto 1fr; gap: 5px 14px; font-size: 13px; }
.kv dt { color: var(--ink-muted); font-size: 11.5px; text-transform: uppercase;
  letter-spacing: 0.05em; white-space: nowrap; }
.kv dd { margin: 0; word-break: break-word; }
footer.foot { margin-top: 34px; padding-top: 14px; border-top: 1px solid var(--border);
  color: var(--ink-muted); font-size: 11.5px; }
@media (max-width: 960px) {
  .shell { flex-direction: column; }
  .side { width: auto; flex: none; height: auto; position: static; }
  .main { padding: 20px 16px 48px; }
  .split { grid-template-columns: 1fr; }
}
"""

_THEME_JS = """
(function () {
  var KEY = 'veritas-theme';
  try {
    var saved = localStorage.getItem(KEY);
    if (saved) document.documentElement.setAttribute('data-theme', saved);
  } catch (e) { /* private mode: fall back to the OS setting */ }
  window.toggleTheme = function () {
    var root = document.documentElement;
    var now = root.getAttribute('data-theme');
    var next = now === 'dark' ? 'light' : (now === 'light' ? 'dark' :
      (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'light' : 'dark'));
    root.setAttribute('data-theme', next);
    try { localStorage.setItem(KEY, next); } catch (e) {}
  };
})();
"""

NAV = [
    ("Operations", [("/", "Dashboard"), ("/flagged", "Flagged rows"),
                    ("/reviews", "Review queue")]),
    ("Data quality", [("/clinics", "Per-source quality"), ("/duplicates", "Duplicates"),
                      ("/errors", "Dead letters"), ("/conversions", "Unit conversions")]),
    ("Records", [("/records", "Record inspector"), ("/aliases", "Learned aliases"),
                 ("/runs", "Run history")]),
]


def e(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def num(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "-" if value in (None, "") else e(value)


def pct(part: Any, whole: Any, digits: int = 1) -> str:
    try:
        part, whole = float(part or 0), float(whole or 0)
    except (TypeError, ValueError):
        return "-"
    return "-" if not whole else f"{part / whole * 100:.{digits}f}%"


def page(title: str, body: str, *, current: str = "/",
         counts: dict[str, int] | None = None, subtitle: str = "") -> str:
    counts = counts or {}
    nav_html: list[str] = []
    for group, links in NAV:
        nav_html.append(f'<div class="nav-group">{e(group)}</div><div class="nav">')
        for href, label in links:
            badge = counts.get(href)
            pill = f'<span class="pill">{num(badge)}</span>' if badge else ""
            aria = ' aria-current="page"' if href == current else ""
            nav_html.append(f'<a href="{e(href)}"{aria}>{e(label)}{pill}</a>')
        nav_html.append("</div>")

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(title)} - Veritas Claims</title>
<style>{_CSS}</style>
<script>{_THEME_JS}</script>
</head>
<body>
<div class="shell">
<aside class="side">
  <div class="brand"><b>Veritas Claims</b><span>Standardisation Ops</span></div>
  {''.join(nav_html)}
  <div style="padding:18px 20px 0">
    <button class="theme" onclick="toggleTheme()">Light / dark</button>
  </div>
</aside>
<main class="main">
<header class="page">
  <div><h1>{e(title)}</h1>{f'<p class="sub">{subtitle}</p>' if subtitle else ''}</div>
  <button class="theme" onclick="toggleTheme()">Toggle theme</button>
</header>
{body}
<footer class="foot">
  Veritas Claims standardisation pipeline &middot; every figure on this page is
  derived from the warehouse views in <span class="mono">load/schema.py</span>,
  not computed in the UI.
</footer>
</main>
</div>
</body>
</html>"""


def tiles(items: Sequence[tuple[str, Any, str]]) -> str:
    cells = "".join(
        f'<div class="card tile"><div class="k">{e(k)}</div>'
        f'<div class="v">{num(v) if isinstance(v, (int, float)) else e(v)}</div>'
        f'<div class="n">{note}</div></div>'
        for k, v, note in items
    )
    return f'<div class="grid g-tiles">{cells}</div>'


def bar_chart(rows: Iterable[dict[str, Any]], *, total: int | None = None,
              show_status: bool = False, show_share: bool = True,
              href_key: str | None = None) -> str:
    """Horizontal bars: one measure across categories, one sequential hue.

    Deliberately not four status colours side by side - see the module
    docstring. Status, where it applies, is a dot beside the label and the
    label itself, so identity survives greyscale, colourblindness and print.
    Each bar carries a native tooltip; the row text is the table view.
    """
    rows = list(rows)
    if not rows:
        return '<p class="empty">Nothing to show.</p>'
    peak = max((r.get("value") or 0) for r in rows) or 1
    denominator = total if total else sum((r.get("value") or 0) for r in rows)

    out: list[str] = ['<div class="bars">']
    for row in rows:
        value = row.get("value") or 0
        label = str(row.get("label", ""))
        width = max(1.0, value / peak * 100.0)
        dot = (f'<i class="dot s-{e(row.get("status", "neutral"))}" aria-hidden="true"></i>'
               if show_status else "")
        text = (f'<a href="{e(row[href_key])}">{e(label)}</a>'
                if href_key and row.get(href_key) else e(label))
        share = (f' &middot; {pct(value, denominator)}' if show_share and denominator else "")
        out.append(
            f'<div class="bar-row">'
            f'<div class="bar-label">{dot}<span>{text}</span></div>'
            f'<div class="bar-track" title="{e(label)}: {num(value)}{share}">'
            f'<div class="bar-fill" style="width:{width:.2f}%"></div></div>'
            f'<div class="bar-val">{num(value)}</div></div>'
        )
    out.append("</div>")
    if show_status:
        # A legend is mandatory for multi-category status, and it is text plus a
        # dot so colour is never the only channel.
        legend = " ".join(
            f'<span class="tag"><i class="dot s-{status}"></i>{caption}</span>'
            for status, caption in (
                ("good", "in range"), ("warning", "out of range"),
                ("serious", "implausible"), ("critical", "unreadable"),
                ("neutral", "not applicable"))
        )
        out.append(f'<div style="margin-top:12px">{legend}</div>')
    return "".join(out)


def table(columns: Sequence[tuple[str, str]], rows: Sequence[dict[str, Any]],
          *, renderers: dict[str, Any] | None = None,
          empty: str = "No rows.") -> str:
    if not rows:
        return f'<p class="empty">{e(empty)}</p>'
    renderers = renderers or {}
    head = "".join(
        f'<th class="{"num" if kind == "num" else ""}">{e(label)}</th>'
        for label, kind in columns
    )
    body: list[str] = []
    for row in rows:
        cells: list[str] = []
        for label, kind in columns:
            key = kind if kind not in ("num", "text", "mono", "wrap") else label
            value = row.get(key, row.get(label))
            if label in renderers:
                cells.append(renderers[label](row))
                continue
            css = ""
            if kind == "num":
                css, value = "num", num(value)
            elif kind == "mono":
                css, value = "mono", e(value)
            elif kind == "wrap":
                css, value = "wrap dim", e(value)
            else:
                value = e(value)
            cells.append(f'<td class="{css}">{value}</td>')
        body.append(f"<tr>{''.join(cells)}</tr>")
    return (f'<div style="overflow-x:auto"><table><thead><tr>{head}</tr></thead>'
            f'<tbody>{"".join(body)}</tbody></table></div>')


def flag_tags(flags: Any) -> str:
    if not flags:
        return '<span class="muted">-</span>'
    severe = {"unit_dimension_mismatch", "range_implausible_for_test",
              "result_equals_range", "report_misalignment_suspected",
              "outlier_implausible", "ocr_garbage_token", "multi_value_packed"}
    warn = {"test_unmapped", "test_low_confidence",
            "source_interpretation_disagrees", "value_embedded_in_name"}
    out: list[str] = []
    for flag in str(flags).split(","):
        if not flag:
            continue
        css = "bad" if flag in severe else ("warn" if flag in warn else "")
        out.append(f'<span class="tag {css}">{e(flag)}</span>')
    return "".join(out)


def mini_bar(value: Any, peak: Any) -> str:
    try:
        ratio = float(value or 0) / float(peak or 1)
    except (TypeError, ValueError, ZeroDivisionError):
        ratio = 0.0
    width = max(0.0, min(1.0, ratio)) * 100
    return (f'<span class="mini" aria-hidden="true"><i style="width:{width:.1f}%">'
            f'</i></span>{num(value)}')


def card(title: str, inner: str, *, note: str = "") -> str:
    head = f"<h2>{e(title)}</h2>"
    tail = f'<p class="sub" style="margin-top:12px">{note}</p>' if note else ""
    return f'<section class="card">{head}{inner}{tail}</section>'


def pretty_json(value: Any) -> str:
    if value is None:
        return '<p class="empty">No raw payload retained for this document.</p>'
    try:
        parsed = value if isinstance(value, (dict, list)) else json.loads(str(value))
        text = json.dumps(parsed, indent=2, ensure_ascii=False)
    except (ValueError, TypeError):
        text = str(value)
    return f'<pre class="json">{e(text)}</pre>'
