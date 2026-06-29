"""HTML report generator for benchmark run comparisons."""
from __future__ import annotations

import json
import uuid
from datetime import datetime

# Palette cycling by harness name
_HARNESS_COLORS = [
    "#42A5F5", "#FF7043", "#A3BE8C", "#CBA6F7",
    "#F9E2AF", "#89DCEB", "#FAB387", "#94E2D5",
]


def _harness_color_map(runs: list[dict]) -> dict[str, str]:
    seen: dict[str, str] = {}
    idx = 0
    for s in runs:
        h = s.get("harness", "") or "unknown"
        if h not in seen:
            seen[h] = _HARNESS_COLORS[idx % len(_HARNESS_COLORS)]
            idx += 1
    return seen


GRADE_ORDER = ["CORRECT", "PASS", "PARTIAL", "NOT_ATTEMPTED", "INCORRECT", "FAIL", "ERROR"]

GRADE_COLOR = {
    "CORRECT": "#3cb94d",
    "PASS": "#3fb950",
    "PARTIAL": "#d4a017",
    "NOT_ATTEMPTED": "#8b949e",
    "INCORRECT": "#f85149",
    "FAIL": "#f85149",
    "ERROR": "#ff7b72",
}

def _get_display(name: str) -> str:
    from framework.benchmarks.base import _BENCHMARK_REGISTRY
    cls = _BENCHMARK_REGISTRY.get(name)
    return cls.display_name if cls and cls.display_name else name


def _acc_color(acc: float) -> str:
    if acc >= 0.7:
        return "#3fb950"
    if acc >= 0.4:
        return "#d4a017"
    return "#f85149"


def _bar(acc: float, width: int = 120) -> str:
    filled = int(acc * width)
    pct = f"{acc * 100:.1f}%"
    color = _acc_color(acc)
    return (
        f'<div style="display:flex;align-items:center;gap:8px">'
        f'<div style="width:{width}px;height:10px;background:#21262d;border-radius:5px;overflow:hidden">'
        f'<div style="width:{filled}px;height:100%;background:{color};border-radius:5px"></div></div>'
        f'<span style="color:{color};font-weight:600;min-width:44px">{pct}</span>'
        f'</div>'
    )


def _run_label(s: dict) -> str:
    model = s.get("model", "unknown")
    harness = s.get("harness", "") or "unknown"
    return f"{model} [{harness}]"


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}K"
    return str(n)


def _grades_pill_html(grades: dict) -> str:
    parts = []
    for g in GRADE_ORDER:
        if g in grades and grades[g] > 0:
            color = GRADE_COLOR.get(g, "#8b949e")
            parts.append(
                f'<span style="color:{color};margin-right:8px">'
                f'<span style="opacity:.6;font-size:10px">{g} </span>{grades[g]}</span>'
            )
    return "".join(parts)


# ──────────────────────────────────────────────────────────────────────────────
# Summary table section
# ──────────────────────────────────────────────────────────────────────────────

def _summary_table(runs: list[dict]) -> str:
    all_benches: list[str] = []
    seen = set()
    for s in runs:
        for b in s.get("benchmarks", {}):
            if b not in seen:
                all_benches.append(b)
                seen.add(b)

    run_ids = [s.get("run_id", "") for s in runs]

    # Group runs by harness, preserving order
    harness_groups: list[tuple[str, list[int]]] = []
    seen_h: dict[str, int] = {}  # harness -> index in harness_groups
    for i, s in enumerate(runs):
        h = s.get("harness", "") or "unknown"
        if h not in seen_h:
            seen_h[h] = len(harness_groups)
            harness_groups.append((h, []))
        harness_groups[seen_h[h]][1].append(i)

    # Row 1: harness headers with colspan
    th_harness = ""
    for harness, idxs in harness_groups:
        span = len(idxs)
        th_harness += (
            f'<th colspan="{span}" style="padding:6px 14px;text-align:center;'
            f'border-left:1px solid #30363d;border-bottom:1px solid #30363d;'
            f'color:#e6edf3;font-weight:700;font-size:12px;'
            f'background:#1c2128">'
            f'{_esc(harness)}</th>'
        )

    # Row 2: model + run_id per column
    th_models = ""
    for harness, idxs in harness_groups:
        for i in idxs:
            model = runs[i].get("model", "unknown")
            th_models += (
                f'<th style="padding:6px 14px;text-align:center;border-left:1px solid #30363d">'
                f'<div style="color:#c9d1d9;font-weight:500;font-size:12px">{_esc(model)}</div>'
                f'<div style="color:#484f58;font-size:10px;font-weight:400;margin-top:2px">{_esc(run_ids[i])}</div>'
                f'</th>'
            )

    # Reorder runs to match harness_groups order
    col_order = [i for _, idxs in harness_groups for i in idxs]
    runs_ordered = [runs[i] for i in col_order]

    html = (
        '<table style="width:100%;border-collapse:collapse;font-size:13px">'
        '<thead>'
        '<tr>'
        '<th rowspan="2" style="padding:8px 14px;text-align:left;color:#8b949e;font-weight:400;'
        'vertical-align:bottom;border-bottom:1px solid #30363d">Benchmark</th>'
        + th_harness +
        '</tr>'
        '<tr>' + th_models + '</tr>'
        '</thead><tbody>'
    )

    for bench in all_benches:
        display = _get_display(bench)
        cells = []
        accs = []
        for s in runs_ordered:
            bdata = s.get("benchmarks", {}).get(bench)
            if bdata:
                accs.append(bdata["accuracy"])
            else:
                accs.append(None)

        # total tasks count (use first run that has this bench)
        total_n = next(
            (s["benchmarks"][bench]["total"] for s in runs_ordered if bench in s.get("benchmarks", {})),
            None,
        )
        n_label = f'<span style="color:#484f58;font-size:11px;font-weight:400"> /{total_n}</span>' if total_n else ""

        valid = [(i, a) for i, a in enumerate(accs) if a is not None]
        best_acc = max(a for _, a in valid) if valid else None
        winner_idxs = {i for i, a in valid if a == best_acc} if best_acc is not None and len(valid) > 1 else set()

        for i, acc in enumerate(accs):
            if acc is None:
                cells.append('<td style="padding:8px 14px;border-left:1px solid #30363d;color:#484f58;text-align:center">—</td>')
                continue
            bg = "background:#0d2818" if i in winner_idxs else ""
            cells.append(
                f'<td style="padding:8px 14px;border-left:1px solid #30363d;{bg}">'
                f'{_bar(acc)}'
                f'</td>'
            )

        bg_row = "background:#161b22" if all_benches.index(bench) % 2 == 0 else ""
        html += (
            f'<tr style="{bg_row}">'
            f'<td style="padding:8px 14px;color:#e6edf3;font-weight:500">{_esc(display)}{n_label}</td>'
            + "".join(cells) +
            '</tr>'
        )

    html += '</tbody></table>'
    return html


# ──────────────────────────────────────────────────────────────────────────────
# Per-benchmark detail cards
# ──────────────────────────────────────────────────────────────────────────────

def _row(label: str, value: str) -> str:
    return (
        f'<div style="margin-bottom:8px;font-size:12px;line-height:1.6">'
        f'<span style="color:#8b949e">{_esc(label)}:&nbsp;</span>'
        f'<span style="color:#c9d1d9">{_esc(value)}</span>'
        f'</div>'
    )


def _section(title: str, color: str = "#388bfd") -> str:
    return (
        f'<div style="font-size:12px;font-weight:600;color:{color};'
        f'margin:14px 0 6px;padding-top:10px;border-top:1px solid #21262d">'
        f'{_esc(title)}</div>'
    )


def _render_analysis(obj) -> str:
    """Render BenchmarkAnalysis object to HTML."""
    from framework.analyzer import BenchmarkAnalysis
    if not isinstance(obj, BenchmarkAnalysis):
        return ""
    parts = []
    for run in obj.runs:
        parts.append(_section(run.label))
        parts.append(_row("Сильные стороны", run.strengths))
        parts.append(_row("Слабые стороны", run.weaknesses))
        parts.append(_row("Характерная ошибка", run.characteristic_error))

    if obj.comparison_a_advantage or obj.comparison_b_advantage:
        parts.append(_section("Сравнение", "#d4a017"))
        if obj.comparison_a_advantage:
            label = obj.runs[0].label if obj.runs else "Модель A"
            parts.append(_row(f"Преимущество {label}", obj.comparison_a_advantage))
        if obj.comparison_b_advantage:
            label = obj.runs[1].label if len(obj.runs) > 1 else "Модель B"
            parts.append(_row(f"Преимущество {label}", obj.comparison_b_advantage))

    parts.append(_section("Итог", "#d4a017"))
    parts.append(_row("Вывод", obj.conclusion))
    return "".join(parts)


def _analysis_block(analysis) -> str:
    """Render analysis as a collapsible HTML block. Accepts BenchmarkAnalysis or error str."""
    from framework.analyzer import BenchmarkAnalysis

    # Error / empty string
    if not analysis or (isinstance(analysis, str) and analysis.startswith("[")):
        color = "#8b949e" if not analysis else "#d4a017"
        return (
            f'<div style="margin-top:14px;padding:10px 14px;background:#0d1117;'
            f'border-left:3px solid #30363d;border-radius:0 4px 4px 0;'
            f'color:{color};font-size:12px;font-style:italic">{_esc(str(analysis or ""))}</div>'
        )

    inner = _render_analysis(analysis) if isinstance(analysis, BenchmarkAnalysis) else _esc(str(analysis))
    return (
        '<details style="margin-top:14px">'
        '<summary style="cursor:pointer;font-size:11px;color:#388bfd;user-select:none;'
        'list-style:none;display:flex;align-items:center;gap:6px;padding:4px 0">'
        '<span class="det-arrow" style="font-size:9px;transition:transform .15s">▶</span>'
        'AI Анализ'
        '</summary>'
        f'<div style="margin-top:10px;padding:12px 16px;background:#0d1117;'
        f'border-left:3px solid #388bfd;border-radius:0 4px 4px 0">'
        f'{inner}</div>'
        '</details>'
    )


def _bench_chart(bench: str, runs: list[dict], color_map: dict[str, str]) -> str:
    """Return a Plotly bar chart div for the given benchmark."""
    traces = []
    for s in runs:
        bdata = s.get("benchmarks", {}).get(bench)
        if not bdata:
            continue
        model = s.get("model", "unknown")
        harness = s.get("harness", "") or "unknown"
        acc_pct = round(bdata["accuracy"] * 100, 1)
        color = color_map.get(harness, "#8b949e")
        # Short model label: last component after /
        short = model.split("/")[-1] if "/" in model else model
        label = f"{short}<br><span style='font-size:10px;color:#8b949e'>{harness}</span>"
        traces.append({
            "type": "bar",
            "name": f"{model} [{harness}]",
            "x": [label],
            "y": [acc_pct],
            "text": [f"{acc_pct}%"],
            "textposition": "outside",
            "marker": {"color": color},
            "showlegend": False,
        })

    if not traces:
        return ""

    div_id = f"chart_{bench}_{uuid.uuid4().hex[:8]}"
    layout = {
        "paper_bgcolor": "#161b22",
        "plot_bgcolor": "#161b22",
        "font": {"color": "#c9d1d9", "family": "-apple-system, BlinkMacSystemFont, 'Segoe UI', monospace"},
        "margin": {"t": 30, "b": 60, "l": 50, "r": 20},
        "yaxis": {
            "range": [0, 110],
            "ticksuffix": "%",
            "gridcolor": "#21262d",
            "zerolinecolor": "#30363d",
        },
        "xaxis": {
            "tickfont": {"size": 11},
        },
        "bargap": 0.35,
        "height": 260,
    }

    traces_json = json.dumps(traces)
    layout_json = json.dumps(layout)
    return (
        f'<div id="{div_id}" style="width:100%;margin-top:14px"></div>'
        f'<script>'
        f'Plotly.newPlot("{div_id}", {traces_json}, {layout_json}, '
        f'{{displayModeBar: false, responsive: true}});'
        f'</script>'
    )


def _bench_card(bench: str, runs: list[dict], analysis: dict[str, str] | None = None,
                color_map: dict[str, str] | None = None,
                swe_stats: list[dict | None] | None = None) -> str:
    display = _get_display(bench)

    # collect all categories across all runs
    all_cats: list[str] = []
    seen_cats: set[str] = set()
    for s in runs:
        bdata = s.get("benchmarks", {}).get(bench, {})
        for c in bdata.get("categories", {}):
            if c not in seen_cats:
                all_cats.append(c)
                seen_cats.add(c)

    labels = [_run_label(s) for s in runs]

    is_swe = bench in ("swe_bench", "swe_bench_multilingual")

    # overall accuracy rows per run
    rows_html = ""
    for i, s in enumerate(runs):
        bdata = s.get("benchmarks", {}).get(bench)
        if not bdata:
            continue
        acc = bdata["accuracy"]

        swe_extra = ""
        if is_swe and swe_stats and i < len(swe_stats) and swe_stats[i]:
            st = swe_stats[i]
            f2p_pct = f"{st['f2p_pass']/st['f2p_total']*100:.1f}%" if st["f2p_total"] else "—"
            p2p_pct = f"{st['p2p_pass']/st['p2p_total']*100:.1f}%" if st["p2p_total"] else "—"
            swe_extra = (
                f'<div style="font-size:11px;color:#8b949e;margin-top:3px">'
                f'<span style="color:#3fb950">F2P</span> '
                f'<span style="color:#e6edf3">{st["f2p_pass"]}/{st["f2p_total"]}</span>'
                f'<span style="color:#484f58"> ({f2p_pct})</span>'
                f'&nbsp;&nbsp;'
                f'<span style="color:#388bfd">P2P</span> '
                f'<span style="color:#e6edf3">{st["p2p_pass"]}/{st["p2p_total"]}</span>'
                f'<span style="color:#484f58"> ({p2p_pct})</span>'
                f'</div>'
            )

        rows_html += (
            f'<div style="display:flex;align-items:center;gap:16px;padding:10px 0;'
            f'border-bottom:1px solid #21262d">'
            f'<div style="min-width:260px;color:#e6edf3;font-size:12px">{_esc(labels[i])}</div>'
            f'<div>'
            f'{_bar(acc, 160)}'
            f'{swe_extra}'
            f'</div>'
            f'<div style="color:#8b949e;font-size:11px;flex:1">'
            f'{_grades_pill_html(bdata.get("grades", {}))}</div>'
            f'</div>'
        )

    # categories table (collapsed by default)
    cats_html = ""
    if all_cats:
        th = "".join(
            f'<th style="padding:5px 10px;color:#8b949e;font-weight:400;text-align:right;'
            f'border-left:1px solid #21262d;font-size:11px">{_esc(labels[i])}</th>'
            for i in range(len(runs))
        )
        rows_cats = ""
        for cat in all_cats:
            cat_accs = []
            for s in runs:
                bdata = s.get("benchmarks", {}).get(bench, {})
                cdata = bdata.get("categories", {}).get(cat)
                cat_accs.append(cdata["accuracy"] if cdata else None)

            valid_cat = [(i, a) for i, a in enumerate(cat_accs) if a is not None]
            best_cat = max(a for _, a in valid_cat) if valid_cat else None
            winner_cats = {i for i, a in valid_cat if a == best_cat} if best_cat is not None and len(valid_cat) > 1 else set()

            tds = ""
            for i, ca in enumerate(cat_accs):
                if ca is None:
                    tds += '<td style="padding:5px 10px;border-left:1px solid #21262d;text-align:right;color:#484f58">—</td>'
                else:
                    color = _acc_color(ca)
                    bold = "font-weight:600" if i in winner_cats else ""
                    n = runs[i].get("benchmarks", {}).get(bench, {}).get("categories", {}).get(cat, {}).get("n", "")
                    tds += (
                        f'<td style="padding:5px 10px;border-left:1px solid #21262d;'
                        f'text-align:right;color:{color};{bold}">'
                        f'{ca*100:.0f}%'
                        f'<span style="color:#484f58;font-weight:400"> /{n}</span>'
                        f'</td>'
                    )

            bg = "background:#0d1117" if all_cats.index(cat) % 2 == 0 else ""
            rows_cats += f'<tr style="{bg}"><td style="padding:5px 10px;color:#8b949e">{_esc(cat)}</td>{tds}</tr>'

        cats_html = (
            '<details style="margin-top:14px">'
            '<summary style="cursor:pointer;font-size:11px;color:#8b949e;user-select:none;'
            'list-style:none;display:flex;align-items:center;gap:6px;padding:4px 0">'
            '<span class="det-arrow" style="font-size:9px;transition:transform .15s">▶</span>'
            f'Categories ({len(all_cats)})'
            '</summary>'
            '<div style="margin-top:10px;overflow-x:auto">'
            '<table style="width:100%;border-collapse:collapse;font-size:11px">'
            f'<thead><tr><th style="padding:5px 10px;text-align:left;color:#8b949e;font-weight:400">Category</th>{th}</tr></thead>'
            f'<tbody>{rows_cats}</tbody>'
            '</table></div>'
            '</details>'
        )

    analysis_html = ""
    if analysis and bench in analysis:
        analysis_html = _analysis_block(analysis[bench])

    chart_html = _bench_chart(bench, runs, color_map or {})

    return (
        f'<div style="background:#161b22;border:1px solid #30363d;border-radius:8px;'
        f'padding:16px 20px;margin-bottom:16px">'
        f'<div style="font-size:14px;font-weight:600;color:#e6edf3;margin-bottom:12px">'
        f'{_esc(display)}</div>'
        f'{chart_html}'
        f'{rows_html}'
        f'{cats_html}'
        f'{analysis_html}'
        f'</div>'
    )


# ──────────────────────────────────────────────────────────────────────────────
# Task categories section
# ──────────────────────────────────────────────────────────────────────────────

def _get_task_categories() -> list[tuple[str, list[str]]]:
    from framework.benchmarks.base import _BENCHMARK_REGISTRY
    cats: dict[str, list[str]] = {}
    for name, cls in _BENCHMARK_REGISTRY.items():
        cat = cls.category or "Прочее"
        cats.setdefault(cat, []).append(name)
    return [(cat, names) for cat, names in cats.items()]


def _task_categories_section(runs: list[dict]) -> str:
    available: set[str] = set()
    for s in runs:
        available.update(s.get("benchmarks", {}).keys())

    cards_html = ""
    for cat_name, bench_keys in _get_task_categories():
        present = [b for b in bench_keys if b in available]
        if not present:
            continue

        # For each run: compute per-bench accuracy + mean over present benches
        run_accs: list[dict] = []
        for s in runs:
            per_bench = {}
            for b in present:
                bdata = s.get("benchmarks", {}).get(b)
                per_bench[b] = bdata["accuracy"] if bdata else None
            valid_accs = [a for a in per_bench.values() if a is not None]
            avg = sum(valid_accs) / len(valid_accs) if valid_accs else None
            run_accs.append({"run": s, "per_bench": per_bench, "avg": avg})

        # Find winner (highest avg)
        valid_runs = [(i, r["avg"]) for i, r in enumerate(run_accs) if r["avg"] is not None]
        best_avg = max(a for _, a in valid_runs) if valid_runs else None
        winner_idxs = {i for i, a in valid_runs if a == best_avg} if best_avg is not None and len(valid_runs) > 1 else set()

        # Header row: benchmark names
        bench_ths = "".join(
            f'<th style="padding:6px 12px;text-align:right;color:#8b949e;font-weight:400;'
            f'border-left:1px solid #21262d;font-size:11px">{_esc(_get_display(b))}</th>'
            for b in present
        )
        avg_th = (
            '<th style="padding:6px 12px;text-align:right;color:#8b949e;font-weight:600;'
            'border-left:2px solid #388bfd;font-size:11px">Среднее</th>'
        )

        rows = ""
        for i, ra in enumerate(run_accs):
            s = ra["run"]
            model = s.get("model", "unknown")
            harness = s.get("harness", "") or "unknown"
            short = model.split("/")[-1] if "/" in model else model
            is_winner = i in winner_idxs
            row_bg = "background:#0d2818" if is_winner else ""

            bench_tds = ""
            for b in present:
                acc = ra["per_bench"][b]
                if acc is None:
                    bench_tds += '<td style="padding:6px 12px;text-align:right;border-left:1px solid #21262d;color:#484f58;font-size:12px">—</td>'
                else:
                    color = _acc_color(acc)
                    bench_tds += (
                        f'<td style="padding:6px 12px;text-align:right;border-left:1px solid #21262d;'
                        f'color:{color};font-size:12px">{acc*100:.1f}%</td>'
                    )

            avg = ra["avg"]
            if avg is None:
                avg_td = '<td style="padding:6px 12px;text-align:right;border-left:2px solid #388bfd;color:#484f58;font-size:12px">—</td>'
            else:
                color = _acc_color(avg)
                bold = "font-weight:700" if is_winner else ""
                avg_td = (
                    f'<td style="padding:6px 12px;text-align:right;border-left:2px solid #388bfd;'
                    f'color:{color};font-size:12px;{bold}">{avg*100:.1f}%</td>'
                )

            rows += (
                f'<tr style="{row_bg}">'
                f'<td style="padding:6px 12px;font-size:12px">'
                f'<div style="color:#e6edf3">{_esc(short)}</div>'
                f'<div style="color:#484f58;font-size:10px">{_esc(harness)}</div>'
                f'</td>'
                f'{bench_tds}'
                f'{avg_td}'
                f'</tr>'
            )

        # Winner badge
        if winner_idxs:
            wi = next(iter(winner_idxs))
            ws = run_accs[wi]["run"]
            wmodel = ws.get("model", "unknown").split("/")[-1]
            wharness = ws.get("harness", "") or "unknown"
            wavg = run_accs[wi]["avg"]
            badge = (
                f'<div style="display:inline-flex;align-items:center;gap:6px;'
                f'background:#0d2818;border:1px solid #238636;border-radius:4px;'
                f'padding:3px 10px;font-size:11px;color:#3fb950;margin-bottom:10px">'
                f'▲ {_esc(wmodel)} [{_esc(wharness)}]'
                f'<span style="color:#484f58">·</span>'
                f'<span style="color:#e6edf3">{wavg*100:.1f}%</span>'
                f'</div>'
            )
        else:
            badge = ""

        cards_html += (
            f'<div style="background:#161b22;border:1px solid #30363d;border-radius:8px;'
            f'padding:16px 20px;margin-bottom:12px">'
            f'<div style="font-size:14px;font-weight:600;color:#e6edf3;margin-bottom:10px">'
            f'{_esc(cat_name)}</div>'
            f'{badge}'
            f'<div style="overflow-x:auto">'
            f'<table style="width:100%;border-collapse:collapse">'
            f'<thead><tr>'
            f'<th style="padding:6px 12px;text-align:left;color:#8b949e;font-weight:400;font-size:11px">Модель / Harness</th>'
            f'{bench_ths}{avg_th}'
            f'</tr></thead>'
            f'<tbody>{rows}</tbody>'
            f'</table></div>'
            f'</div>'
        )

    return cards_html


# ──────────────────────────────────────────────────────────────────────────────
# Tokens & cost summary
# ──────────────────────────────────────────────────────────────────────────────

def _tokens_section(runs: list[dict]) -> str:
    labels = [_run_label(s) for s in runs]
    rows = ""
    for i, s in enumerate(runs):
        benches = s.get("benchmarks", {})
        total_in = sum(b.get("input_tokens", 0) for b in benches.values())
        total_out = sum(b.get("output_tokens", 0) for b in benches.values())
        rows += (
            f'<tr>'
            f'<td style="padding:8px 14px;color:#e6edf3">{_esc(labels[i])}</td>'
            f'<td style="padding:8px 14px;color:#8b949e;text-align:right">{_fmt_tokens(total_in)}</td>'
            f'<td style="padding:8px 14px;color:#8b949e;text-align:right">{_fmt_tokens(total_out)}</td>'
            f'<td style="padding:8px 14px;color:#8b949e;text-align:right">{_fmt_tokens(total_in + total_out)}</td>'
            f'</tr>'
        )

    return (
        '<div style="background:#161b22;border:1px solid #30363d;border-radius:8px;'
        'padding:16px 20px;margin-bottom:16px">'
        '<div style="font-size:14px;font-weight:600;color:#e6edf3;margin-bottom:12px">Token Usage</div>'
        '<table style="width:100%;border-collapse:collapse;font-size:12px">'
        '<thead><tr>'
        '<th style="padding:8px 14px;text-align:left;color:#8b949e;font-weight:400">Run</th>'
        '<th style="padding:8px 14px;text-align:right;color:#8b949e;font-weight:400">Input</th>'
        '<th style="padding:8px 14px;text-align:right;color:#8b949e;font-weight:400">Output</th>'
        '<th style="padding:8px 14px;text-align:right;color:#8b949e;font-weight:400">Total</th>'
        '</tr></thead>'
        f'<tbody>{rows}</tbody>'
        '</table>'
        '</div>'
    )


# ──────────────────────────────────────────────────────────────────────────────
# Full report
# ──────────────────────────────────────────────────────────────────────────────

def _esc(s: str) -> str:
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


async def generate_report(
    run_ids: list[str],
    db=None,
    analysis: dict[str, str] | None = None,
) -> str:
    """Generate a self-contained HTML report for the given run_ids.

    db: Database instance to load summaries and F2P/P2P stats from.
    analysis: optional dict {bench_name: text} from analyzer.analyze_runs()
    Returns the HTML as a string.
    """
    summaries: list[dict] = []
    missing: list[str] = []
    for rid in run_ids:
        if ".." in rid or "/" in rid or "\\" in rid:
            continue
        s = None
        if db and db._enabled:
            s = await db.fetch_run_summary(rid)
        if s:
            summaries.append(s)
        else:
            missing.append(rid)

    if not summaries:
        return "<html><body><p>No valid runs found.</p></body></html>"

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    title = "Benchmark Report" if len(summaries) == 1 else f"Benchmark Comparison ({len(summaries)} runs)"
    if analysis:
        title += " + AI Analysis"

    # collect all benchmarks in order
    all_benches: list[str] = []
    seen: set[str] = set()
    for s in summaries:
        for b in s.get("benchmarks", {}):
            if b not in seen:
                all_benches.append(b)
                seen.add(b)

    color_map = _harness_color_map(summaries)

    # Pre-load F2P/P2P stats for each run × swe bench
    swe_benches = ("swe_bench", "swe_bench_multilingual")
    safe_run_ids = [rid for rid in run_ids if not (".." in rid or "/" in rid or "\\" in rid)]
    swe_stats_by_bench: dict[str, list[dict | None]] = {}
    for b in swe_benches:
        stats_list = []
        for rid in safe_run_ids:
            st = None
            if db and db._enabled:
                st = await db.fetch_swe_f2p_p2p(rid, b)
            stats_list.append(st)
        swe_stats_by_bench[b] = stats_list
    # Normalize denominators: use max across runs so all runs share the same total
    for b, stats_list in swe_stats_by_bench.items():
        valid = [s for s in stats_list if s]
        if not valid:
            continue
        max_f2p = max(s["f2p_total"] for s in valid)
        max_p2p = max(s["p2p_total"] for s in valid)
        for s in stats_list:
            if s:
                s["f2p_total"] = max_f2p
                s["p2p_total"] = max_p2p

    def _card(b: str) -> str:
        swe_stats = swe_stats_by_bench.get(b) if b in swe_benches else None
        return _bench_card(b, summaries, analysis, color_map, swe_stats)

    bench_cards = "".join(_card(b) for b in all_benches)
    summary_tbl = _summary_table(summaries)
    task_cats_html = _task_categories_section(summaries)

    missing_note = ""
    if missing:
        missing_note = (
            f'<div style="background:#2d0f0f;border:1px solid #6e1a1a;border-radius:6px;'
            f'padding:10px 14px;margin-bottom:16px;color:#f85149;font-size:12px">'
            f'Runs not found: {", ".join(_esc(m) for m in missing)}</div>'
        )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)}</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js" charset="utf-8"></script>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", monospace;
    background: #0d1117;
    color: #e6edf3;
    padding: 32px 24px;
    line-height: 1.5;
  }}
  .container {{ max-width: 1100px; margin: 0 auto; }}
  h1 {{ font-size: 20px; font-weight: 600; margin-bottom: 4px; }}
  h2 {{ font-size: 14px; font-weight: 600; color: #e6edf3; margin-bottom: 14px; }}
  .meta {{ color: #8b949e; font-size: 12px; margin-bottom: 28px; }}
  .section-title {{
    font-size: 13px; font-weight: 600; color: #8b949e;
    text-transform: uppercase; letter-spacing: .06em;
    margin: 28px 0 12px;
  }}
  .summary-wrap {{
    background: #161b22; border: 1px solid #30363d;
    border-radius: 8px; overflow-x: auto; margin-bottom: 28px;
  }}
  details[open] summary .det-arrow {{ transform: rotate(90deg); }}
  details summary::-webkit-details-marker {{ display:none; }}
  @media print {{
    body {{ background: white; color: black; }}
    details {{ open: true; }}
  }}
</style>
</head>
<body>
<div class="container">
  <h1>{_esc(title)}</h1>
  <div class="meta">Generated {now}</div>

  {missing_note}

  <div class="section-title">Overall Comparison</div>
  <div class="summary-wrap">{summary_tbl}</div>

  <div class="section-title">По типу задач</div>
  {task_cats_html}

  <div class="section-title">Per-Benchmark Details</div>
  {bench_cards}
</div>
</body>
</html>"""
    return html
