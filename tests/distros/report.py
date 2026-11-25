#!/usr/bin/env python3
"""Turns tests/distros/out/results.tsv into docs/TESTING.md and
tests/distros/out/report.html (a self contained page with the matrix)."""
import collections
import datetime
import html
import pathlib
import re
import sys

TSV = pathlib.Path(sys.argv[1])
SRC = pathlib.Path(sys.argv[2])
VERSION = re.search(r'^VERSION = "(.*)"$', (SRC / "discord-game-presence.py").read_text(), re.M).group(1)
KINDS = {"script": "script", "package": "package"}

rows = [line.split("\t") for line in TSV.read_text().splitlines() if line.strip()]
rows = [(r + [""] * 5)[:5] for r in rows]
rows = [r for r in rows if r[2].strip()]
distros = list(dict.fromkeys(r[0] for r in rows))
columns = [(d, k) for d in distros for k in KINDS if any(r[0] == d and r[1] == k for r in rows)]
checks = list(dict.fromkeys(r[2] for r in rows))
cell = {(r[0], r[1], r[2]): (r[3], r[4]) for r in rows}
total = collections.Counter(r[3] for r in rows)
today = datetime.date.today().isoformat()
python = {(r[0]): re.search(r"python ([\d.]+)", r[2]).group(1) for r in rows if "python " in r[2]}


def norm(check):
    return re.sub(r" with this distro's python [\d.]+", " with the distro's python", check)


ordered = list(dict.fromkeys(norm(c) for c in checks))
lookup = {}
for (d, k, c), v in cell.items():
    lookup[(d, k, norm(c))] = v

# ------------------------------------------------------------------ markdown
md = [f"# Tested distributions\n",
      f"Version **{VERSION}**, run on {today} with `tests/distros/run.sh`.\n",
      "Each distro runs in a container with **systemd as PID 1**, a normal user with a real "
      "`systemctl --user` manager and session bus, and a headless X server. The app is installed "
      "the two ways the README offers (the `curl | bash` installer and the native package), then "
      "used the way a person uses it: a fake game is started and must reach a fake Discord socket, "
      "the tray must register with a StatusNotifier host (the protocol Plasma uses) and its menu is "
      "clicked over D-Bus. Nothing touches a real Discord account.\n",
      f"**{total['ok']} checks passed, {total['fail']} failed.**\n"]
head = "| Check | " + " | ".join(f"{d}<br>{KINDS[k]}" for d, k in columns) + " |"
md += [head, "|" + "---|" * (len(columns) + 1)]
for c in ordered:
    marks = []
    for d, k in columns:
        state = lookup.get((d, k, c), ("", ""))[0]
        marks.append({"ok": "✅", "fail": "❌"}.get(state, "·"))
    md.append(f"| {c} | " + " | ".join(marks) + " |")
fails = [(d, k, c, v[1]) for (d, k, c), v in lookup.items() if v[0] == "fail"]
if fails:
    md += ["\n## Failures\n"] + [f"* **{d} {KINDS[k]}**: {c}. `{det}`" for d, k, c, det in fails]
md += ["\n## Run it yourself\n", "```sh", "packaging/build.sh", "tests/distros/run.sh            # all",
       "tests/distros/run.sh fedora     # one", "```",
       "\nNeeds podman (rootless is enough) or docker. Logs land in `tests/distros/out/`.\n"]
(SRC / "docs").mkdir(exist_ok=True)
(SRC / "docs" / "TESTING.md").write_text("\n".join(md) + "\n")

# ---------------------------------------------------------------------- html
def td(d, k, c):
    state, detail = lookup.get((d, k, c), ("", ""))
    sym = {"ok": "✓", "fail": "✗"}.get(state, "·")
    return f'<td class="{state or "na"}" title="{html.escape(detail)}">{sym}</td>'


summary = []
for d in distros:
    states = [v[0] for (dd, _k, _c), v in lookup.items() if dd == d]
    ok = states.count("ok")
    summary.append(f'<div class="card {"bad" if "fail" in states else "good"}"><b>{html.escape(d)}</b>'
                   f'<span>{ok}/{len(states)}</span><small>python {python.get(d, "?")}</small></div>')
page = f"""<!doctype html><meta charset="utf-8"><title>Discord Presence {VERSION} distro matrix</title>
<style>
html{{color-scheme:dark;background:#14161a}}
body{{font-family:system-ui,sans-serif;margin:24px;color:var(--ag-text,#e3e6eb);background:#14161a}}
h1{{font-size:20px;margin:0 0 4px}} p{{color:var(--ag-muted,#999);margin:0 0 16px}}
.cards{{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:18px}}
.card{{background:var(--ag-surface,#1d2026);border:1px solid var(--ag-border,#343943);border-radius:var(--ag-radius,10px);
padding:10px 14px;min-width:120px;display:flex;flex-direction:column}}
.card span{{font-size:22px;font-weight:700}} .card.good span{{color:var(--ag-ok,#4c4)}} .card.bad span{{color:var(--ag-err,#e55)}}
.card small{{color:var(--ag-muted,#999)}}
table{{border-collapse:collapse;font-size:13px}} th,td{{border:1px solid var(--ag-border,#343943);padding:5px 8px}}
th{{background:var(--ag-surface-2,#262a31);font-weight:600}} td:first-child{{text-align:left}}
td{{text-align:center}} td.ok{{color:var(--ag-ok,#4c4)}} td.fail{{color:var(--ag-err,#e55);font-weight:700}} td.na{{color:var(--ag-muted,#777)}}
</style>
<h1>Discord Presence {VERSION}, {total['ok']} of {total['ok'] + total['fail']} checks passed</h1>
<p>{today}, containers with systemd as PID 1, a real user session bus, headless X. Hover a cell for details.</p>
<div class="cards">{''.join(summary)}</div>
<table><tr><th>Check</th>{''.join(f'<th>{html.escape(d)}<br><small>{KINDS[k]}</small></th>' for d, k in columns)}</tr>
{''.join('<tr><td>' + html.escape(c) + '</td>' + ''.join(td(d, k, c) for d, k in columns) + '</tr>' for c in ordered)}
</table>"""
(TSV.parent / "report.html").write_text(page)
print(f"{total['ok']} ok, {total['fail']} failed -> docs/TESTING.md, {TSV.parent / 'report.html'}")
sys.exit(1 if total["fail"] else 0)
