"""Звіт за результатами бенчмарку: графіки (report/*.png) і таблиці (report/tables.md).

Запуск: uv run python -m lab2.report
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "results"
REPORT = ROOT / "report"
DPI = 130

VARIANTS = ["sql", "jsonb", "mongo"]
COLORS = {"sql": "#1f77b4", "jsonb": "#ff7f0e", "mongo": "#2ca02c"}
LABELS = {
    "sql": "PostgreSQL (нормалізований)",
    "jsonb": "PostgreSQL JSONB",
    "mongo": "MongoDB",
}
READ_SC = ["S1", "S2", "S7", "S5a", "S5b"]
WRITE_SC = ["S3a", "S3b", "S6all", "S6open"]
ALL_SC = READ_SC + WRITE_SC
SCEN_NAMES = {
    "S1": "S1\nрейс за номером",
    "S2": "S2\nрейси замовника",
    "S7": "S7",
    "S5a": "S5a",
    "S5b": "S5b",
    "S3a": "S3a",
    "S3b": "S3b",
    "S6all": "S6all",
    "S6open": "S6open",
}

plt.rcParams.update({
    "axes.grid": True,
    "grid.alpha": 0.3,
    "axes.axisbelow": True,
    "font.size": 10,
})


S8_COLS = ["scale", "variant", "workers", "op", "count", "ops_per_s", "p50_ms", "p95_ms", "p99_ms", "errors"]


def warn(msg: str) -> None:
    print(f"УВАГА: {msg}", file=sys.stderr)


# ---------------------------------------------------------------- дані

def scale_num(name: str) -> float:
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([kKmM]?)", name)
    if not m:
        return float("inf")
    return float(m.group(1)) * {"": 1, "k": 1e3, "m": 1e6}[m.group(2).lower()]


def find_scales() -> list[str]:
    if not RESULTS.is_dir():
        return []
    names = [p.name for p in RESULTS.iterdir() if p.is_dir() and scale_num(p.name) != float("inf")]
    return sorted(names, key=scale_num)


def read_csv(scale: str, name: str) -> pd.DataFrame | None:
    path = RESULTS / scale / name
    if not path.exists():
        warn(f"{path.relative_to(ROOT)} відсутній")
        return None
    try:
        df = pd.read_csv(path)
    except Exception as e:  # порожній або пошкоджений файл
        warn(f"{path.relative_to(ROOT)}: не вдалося прочитати ({e.__class__.__name__})")
        return None
    if df.empty:
        warn(f"{path.relative_to(ROOT)} порожній")
        return None
    return df


def load_all(scales: list[str]) -> dict[str, dict[str, pd.DataFrame]]:
    names = [
        "warm_summary", "cold_summary", "writes_summary", "server_server", "writes_s4_raw",
        "writes_s6_raw", "s8_throughput", "load", "sizes",
    ]
    data: dict[str, dict[str, pd.DataFrame]] = {n: {} for n in names}
    for s in scales:
        for n in names:
            df = read_csv(s, n + ".csv")
            if df is not None:
                data[n][s] = df
    return data


def present(variants_in) -> list[str]:
    return [v for v in VARIANTS if v in set(variants_in)]


def fmt(x, digits: int = 3) -> str:
    if x is None or (isinstance(x, float) and (np.isnan(x) or np.isinf(x))):
        return "—"
    x = float(x)
    if x == 0:
        return "0"
    mag = int(np.floor(np.log10(abs(x))))
    dec = max(digits - 1 - mag, 0)
    return f"{round(x, dec):.{dec}f}"


def save(fig, name: str) -> None:
    REPORT.mkdir(parents=True, exist_ok=True)
    fig.savefig(REPORT / name, dpi=DPI, bbox_inches="tight")
    plt.close(fig)
    print(f"  записано report/{name}")


def variant_legend(ax_or_fig, extra=None, **kw):
    handles = [plt.Rectangle((0, 0), 1, 1, color=COLORS[v], label=LABELS[v]) for v in VARIANTS]
    if extra:
        handles += extra
    return ax_or_fig.legend(handles=handles, **kw)


def combined_summary(data, scale: str) -> pd.DataFrame | None:
    parts = [data[n][scale] for n in ("warm_summary", "writes_summary") if scale in data[n]]
    return pd.concat(parts, ignore_index=True) if parts else None


def p50(df: pd.DataFrame, scenario: str, variant: str, col="p50_ms"):
    r = df[(df.scenario == scenario) & (df.variant == variant)]
    return float(r[col].iloc[0]) if len(r) else np.nan


def server_median(df: pd.DataFrame, scenario: str, variant: str):
    metric = "profiler_cpu" if variant == "mongo" else "explain"
    r = df[(df.scenario == scenario) & (df.variant == variant) & (df.metric == metric)]
    return float(r.server_ms.median()) if len(r) else np.nan


# ---------------------------------------------------------------- графіки

def grouped_bars(ax, groups, series_vals, series_colors, series_hatch=None, yerr=None, width=0.8):
    """series_vals: список масивів, по одному на серію."""
    n = len(series_vals)
    w = width / n
    x = np.arange(len(groups))
    for i, vals in enumerate(series_vals):
        vals = np.asarray(vals, dtype=float)
        err = None
        if yerr is not None and yerr[i] is not None:
            err = np.vstack([np.zeros(len(vals)), np.nan_to_num(np.asarray(yerr[i], dtype=float))])
        ax.bar(x - width / 2 + w * (i + 0.5), np.nan_to_num(vals, nan=0), w, color=series_colors[i],
               hatch=(series_hatch[i] if series_hatch else None), edgecolor="white" if not series_hatch else "black",
               linewidth=0.6, yerr=err, capsize=2, error_kw={"elinewidth": 0.9, "ecolor": "#333"})
    ax.set_xticks(x)
    ax.set_xticklabels(groups)


def plot_latency_warm(data, scale):
    df = combined_summary(data, scale)
    if df is None:
        return
    scen = [s for s in ALL_SC if s in set(df.scenario)]
    vs = present(df.variant)
    if not scen:
        return
    fig, ax = plt.subplots(figsize=(11, 5))
    vals = [[p50(df, s, v) for s in scen] for v in vs]
    err = [[p50(df, s, v, "p95_ms") - p50(df, s, v) for s in scen] for v in vs]
    grouped_bars(ax, scen, vals, [COLORS[v] for v in vs], yerr=err)
    ax.set_yscale("log")
    ax.set_ylabel("Латентність p50, мс (лог. шкала; «вус» — p95)")
    ax.set_xlabel("Сценарій")
    ax.set_title(f"Латентність при теплому кеші, масштаб {scale} рейсів")
    ax.legend(handles=[plt.Rectangle((0, 0), 1, 1, color=COLORS[v], label=LABELS[v]) for v in vs],
              loc="upper center", bbox_to_anchor=(0.5, -0.12), ncol=3, frameon=False)
    save(fig, f"latency_warm_{scale}.png")


def plot_latency_cold(data, scale):
    if scale not in data["cold_summary"] or scale not in data["warm_summary"]:
        warn(f"latency_cold_{scale}.png пропущено: немає warm/cold_summary")
        return
    w, c = data["warm_summary"][scale], data["cold_summary"][scale]
    scen = [s for s in READ_SC if s in set(c.scenario)]
    vs = present(c.variant)
    fig, ax = plt.subplots(figsize=(12, 5))
    series, colors, hatches = [], [], []
    for v in vs:
        series.append([p50(w, s, v) for s in scen]); colors.append(COLORS[v]); hatches.append(None)
        series.append([p50(c, s, v) for s in scen]); colors.append(COLORS[v]); hatches.append("///")
    grouped_bars(ax, scen, series, colors, series_hatch=hatches)
    ax.set_yscale("log")
    ax.set_ylabel("Латентність p50, мс (лог. шкала)")
    ax.set_xlabel("Сценарій")
    ax.set_title(f"Теплий і холодний кеш, масштаб {scale} рейсів")
    extra = [plt.Rectangle((0, 0), 1, 1, facecolor="white", edgecolor="black", label="теплий кеш"),
             plt.Rectangle((0, 0), 1, 1, facecolor="white", edgecolor="black", hatch="///", label="холодний кеш")]
    handles = [plt.Rectangle((0, 0), 1, 1, color=COLORS[v], label=LABELS[v]) for v in vs] + extra
    ax.legend(handles=handles, loc="upper center", bbox_to_anchor=(0.5, -0.12), ncol=5, frameon=False)
    save(fig, f"latency_cold_{scale}.png")


def plot_scaling(data, scales):
    frames = []
    for s in scales:
        d = combined_summary(data, s)
        if d is not None:
            d = d.copy(); d["trips"] = scale_num(s); frames.append(d)
    if not frames:
        return
    df = pd.concat(frames)
    scen = ["S1", "S2", "S7", "S5a", "S5b", "S3a", "S6all"]
    fig, axes = plt.subplots(3, 3, figsize=(13, 10))
    axes = axes.ravel()
    for ax, sc in zip(axes, scen):
        for v in VARIANTS:
            r = df[(df.scenario == sc) & (df.variant == v)].sort_values("trips")
            if len(r):
                ax.plot(r.trips, r.p50_ms, "o-", color=COLORS[v], label=LABELS[v])
        ax.set_xscale("log"); ax.set_yscale("log")
        ax.set_title(sc)
        ax.set_xlabel("Кількість рейсів"); ax.set_ylabel("p50, мс")
    for ax in axes[len(scen):-1]:
        ax.set_visible(False)
    axes[-1].axis("off")
    variant_legend(axes[-1], loc="center", frameon=False)
    fig.suptitle("Масштабування: p50 (теплий кеш) залежно від кількості рейсів")
    fig.tight_layout()
    save(fig, "scaling.png")


def plot_s8(data, scales):
    sc = [s for s in scales if s in data["s8_throughput"]]
    if not sc:
        warn("s8_throughput.png / s8_latency.png пропущено: немає s8_throughput")
        return
    for kind in ("throughput", "latency"):
        fig, axes = plt.subplots(1, len(sc), figsize=(5 * len(sc) + 1, 4.5), squeeze=False)
        for ax, s in zip(axes[0], sc):
            d = data["s8_throughput"][s]
            d = d[d.op == "ALL"]
            for v in VARIANTS:
                r = d[d.variant == v].sort_values("workers")
                if not len(r):
                    continue
                if kind == "throughput":
                    ax.plot(r.workers, r.ops_per_s, "o-", color=COLORS[v], label=LABELS[v])
                else:
                    ax.plot(r.workers, r.p50_ms, "o-", color=COLORS[v])
                    ax.plot(r.workers, r.p99_ms, "s--", color=COLORS[v])
            ax.set_xscale("log"); ax.set_xticks([1, 8, 32]); ax.set_xticklabels([1, 8, 32])
            ax.minorticks_off()
            ax.set_title(f"{s} рейсів"); ax.set_xlabel("Кількість клієнтів")
            if kind == "throughput":
                ax.set_ylabel("Пропускна здатність, оп./с"); ax.set_ylim(bottom=0)
            else:
                ax.set_yscale("log"); ax.set_ylabel("Латентність, мс (лог. шкала)")
        if kind == "throughput":
            fig.suptitle("S8: змішане навантаження, пропускна здатність")
            variant_legend(fig, loc="lower center", ncol=3, frameon=False, bbox_to_anchor=(0.5, -0.06))
        else:
            fig.suptitle("S8: змішане навантаження, латентність (суцільна — p50, пунктир — p99)")
            variant_legend(fig, loc="lower center", ncol=3, frameon=False, bbox_to_anchor=(0.5, -0.06))
        fig.tight_layout()
        save(fig, f"s8_{kind}.png")


def s4_throughput(df: pd.DataFrame) -> pd.DataFrame:
    g = df.groupby(["variant", "batch_size"]).agg(docs=("docs", "sum"), us=("client_us", "sum")).reset_index()
    g["trips_per_s"] = g.docs / (g.us / 1e6)
    return g


def plot_s4(data, scales):
    sc = [s for s in scales if s in data["writes_s4_raw"]]
    if not sc:
        warn("s4_throughput.png пропущено: немає writes_s4_raw")
        return
    fig, axes = plt.subplots(1, len(sc), figsize=(5 * len(sc) + 1, 4.5), squeeze=False)
    for ax, s in zip(axes[0], sc):
        g = s4_throughput(data["writes_s4_raw"][s])
        for v in VARIANTS:
            r = g[g.variant == v].sort_values("batch_size")
            if len(r):
                ax.plot(r.batch_size, r.trips_per_s, "o-", color=COLORS[v])
        ax.set_xscale("log"); ax.set_yscale("log")
        ax.set_title(f"{s} рейсів"); ax.set_xlabel("Розмір батча, рейсів")
        ax.set_ylabel("Пропускна здатність, рейсів/с")
    fig.suptitle("S4: масове вставляння, пропускна здатність залежно від розміру батча")
    variant_legend(fig, loc="lower center", ncol=3, frameon=False, bbox_to_anchor=(0.5, -0.06))
    fig.tight_layout()
    save(fig, "s4_throughput.png")


def plot_s6(data, scales):
    sc = [s for s in scales if s in data["writes_s6_raw"]]
    frames = []
    for s in sc:
        d = data["writes_s6_raw"][s]
        d = d[d.scenario == "S6all"].copy(); d["scale"] = s; frames.append(d)
    if not frames or all(f.empty for f in frames):
        warn("s6_scatter.png пропущено: немає writes_s6_raw (S6all)")
        return
    fig, axes = plt.subplots(1, len(frames), figsize=(6 * len(frames) + 1, 5), squeeze=False)
    for ax, d in zip(axes[0], frames):
        d = d[(d.changed > 0) & (d.client_us > 0)]
        for v in VARIANTS:
            r = d[d.variant == v]
            n, t = r[~r.is_top.astype(bool)], r[r.is_top.astype(bool)]
            ax.scatter(n.changed, n.client_us / 1000, s=18, alpha=0.6, color=COLORS[v])
            if len(t):
                ax.scatter(t.changed, t.client_us / 1000, s=140, marker="*", color=COLORS[v],
                           edgecolor="black", linewidth=0.8, zorder=5)
        ax.set_xscale("log"); ax.set_yscale("log")
        ax.set_xlabel("Кількість змінених копій (changed)")
        ax.set_ylabel("Латентність, мс")
        ax.set_title(f"{d.scale.iloc[0] if len(d) else ''} рейсів")
    extra = [plt.Line2D([], [], marker="*", color="w", markerfacecolor="#999", markeredgecolor="black",
                        markersize=14, label="найбільший замовник")]
    fig.suptitle("S6all: латентність оновлення залежно від кількості змінених копій")
    variant_legend(fig, extra=extra, loc="lower center", ncol=4, frameon=False, bbox_to_anchor=(0.5, -0.06))
    fig.tight_layout()
    save(fig, "s6_scatter.png")


def plot_server_vs_client(data, scale):
    if scale not in data["server_server"] or scale not in data["warm_summary"]:
        warn(f"server_vs_client_{scale}.png пропущено: немає server_server/warm_summary")
        return
    srv, w = data["server_server"][scale], data["warm_summary"][scale]
    scen = [s for s in READ_SC if s in set(srv.scenario)]
    vs = present(w.variant)
    fig, ax = plt.subplots(figsize=(12, 5))
    series, colors, hatches = [], [], []
    for v in vs:
        series.append([server_median(srv, s, v) for s in scen]); colors.append(COLORS[v]); hatches.append("///")
        series.append([p50(w, s, v) for s in scen]); colors.append(COLORS[v]); hatches.append(None)
    grouped_bars(ax, scen, series, colors, series_hatch=hatches)
    ax.set_yscale("log")
    ax.set_ylabel("Час, мс (лог. шкала)")
    ax.set_xlabel("Сценарій")
    ax.set_title(f"Серверний час і клієнтська латентність p50, масштаб {scale} рейсів")
    extra = [plt.Rectangle((0, 0), 1, 1, facecolor="white", edgecolor="black", hatch="///",
                           label="серверний (медіана)"),
             plt.Rectangle((0, 0), 1, 1, facecolor="white", edgecolor="black", label="клієнтський p50")]
    handles = [plt.Rectangle((0, 0), 1, 1, color=COLORS[v], label=LABELS[v]) for v in vs] + extra
    ax.legend(handles=handles, loc="upper center", bbox_to_anchor=(0.5, -0.12), ncol=5, frameon=False)
    fig.text(0.01, -0.02, "Серверний час: EXPLAIN ANALYZE для PostgreSQL, CPU-час профайлера для MongoDB.",
             fontsize=8, color="#555")
    save(fig, f"server_vs_client_{scale}.png")


def load_phases(df: pd.DataFrame) -> pd.DataFrame:
    return df[df.phase.isin(["data", "indexes", "analyze"])]


PHASE_COLORS = {"data": "#4c72b0", "indexes": "#dd8452", "analyze": "#8c8c8c"}
PHASE_LABELS = {"data": "завантаження даних", "indexes": "побудова індексів", "analyze": "ANALYZE / статистика"}


def stacked(ax, vs, layers: dict[str, list[float]], colors, labels):
    bottom = np.zeros(len(vs))
    for k, vals in layers.items():
        vals = np.nan_to_num(np.asarray(vals, dtype=float))
        ax.bar(range(len(vs)), vals, bottom=bottom, color=colors[k], label=labels[k],
               edgecolor="white", linewidth=0.6)
        bottom += vals
    ax.set_xticks(range(len(vs)))
    ax.set_xticklabels([LABELS[v].replace(" (", "\n(").replace("PostgreSQL JSONB", "PostgreSQL\nJSONB") for v in vs], fontsize=8)


def wrap_labels(vs):
    return vs


def plot_load(data, scales):
    sc = [s for s in scales if s in data["load"]]
    if not sc:
        warn("load_times.png пропущено: немає load.csv")
        return
    fig, axes = plt.subplots(1, len(sc), figsize=(5 * len(sc) + 1, 5), squeeze=False)
    for ax, s in zip(axes[0], sc):
        d = load_phases(data["load"][s])
        vs = present(d.variant)
        layers = {ph: [d[(d.variant == v) & (d.phase == ph)].seconds.sum() for v in vs] for ph in PHASE_COLORS}
        stacked(ax, vs, layers, PHASE_COLORS, PHASE_LABELS)
        ax.set_title(f"{s} рейсів"); ax.set_ylabel("Час, с")
    h, l = axes[0][0].get_legend_handles_labels()
    fig.legend(h, l, loc="lower center", ncol=3, frameon=False, bbox_to_anchor=(0.5, -0.04))
    fig.suptitle("Час завантаження за фазами")
    fig.tight_layout()
    save(fig, "load_times.png")


def sizes_by_variant(df: pd.DataFrame) -> pd.DataFrame:
    g = df.groupby("variant").agg(table=("table_bytes", "sum"), index=("index_bytes", "sum"),
                                   rows=("rows", "sum"), uncompressed=("uncompressed_bytes", "sum")).reset_index()
    return g.set_index("variant")


def plot_sizes(data, scales):
    sc = [s for s in scales if s in data["sizes"]]
    if not sc:
        warn("sizes.png пропущено: немає sizes.csv")
        return
    fig, axes = plt.subplots(1, len(sc), figsize=(5 * len(sc) + 1, 5), squeeze=False)
    cl = {"table": "#4c72b0", "index": "#dd8452"}
    lb = {"table": "таблиці / колекції", "index": "індекси"}
    for ax, s in zip(axes[0], sc):
        g = sizes_by_variant(data["sizes"][s])
        vs = [v for v in VARIANTS if v in g.index]
        layers = {k: [g.loc[v, k] / 2**20 for v in vs] for k in ("table", "index")}
        stacked(ax, vs, layers, cl, lb)
        ax.set_title(f"{s} рейсів"); ax.set_ylabel("Обсяг на диску, МБ")
    h, l = axes[0][0].get_legend_handles_labels()
    fig.legend(h, l, loc="lower center", ncol=2, frameon=False, bbox_to_anchor=(0.5, -0.04))
    fig.suptitle("Обсяг на диску: таблиці та індекси")
    fig.tight_layout()
    save(fig, "sizes.png")


# ---------------------------------------------------------------- таблиці

def md_table(header: list[str], rows: list[list[str]]) -> str:
    out = ["| " + " | ".join(header) + " |", "|" + "|".join(["---"] + ["---:"] * (len(header) - 1)) + "|"]
    out += ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join(out) + "\n"


def ratio_cols(vals: dict[str, float]):
    ok = {v: x for v, x in vals.items() if not np.isnan(x)}
    if not ok:
        return "—", "—"
    best = min(ok, key=ok.get); worst = max(ok, key=ok.get)
    r = ok[worst] / ok[best] if ok[best] > 0 else float("nan")
    return best, fmt(r)


def tables_for_scale(data, scale: str) -> str:
    out = [f"## Масштаб {scale} рейсів\n"]
    short = {"sql": "sql", "jsonb": "jsonb", "mongo": "mongo"}

    # (а)
    df = combined_summary(data, scale)
    if df is not None:
        vs = present(df.variant)
        scen = [s for s in ALL_SC if s in set(df.scenario)]
        hdr = ["Сценарій"] + [f"{short[v]} {m}" for v in vs for m in ("p50", "p95", "p99")] + \
              ["найшвидший (p50)", "найповільніший / найшвидший (p50)"]
        rows = []
        for s in scen:
            row = [s]
            for v in vs:
                row += [fmt(p50(df, s, v, c)) for c in ("p50_ms", "p95_ms", "p99_ms")]
            b, r = ratio_cols({v: p50(df, s, v) for v in vs})
            rows += [row + [b, r]]
        out += ["### (а) Теплий кеш: латентність, мс\n", md_table(hdr, rows)]

    # (б)
    if scale in data["cold_summary"] and scale in data["warm_summary"]:
        c, w = data["cold_summary"][scale], data["warm_summary"][scale]
        vs = present(c.variant)
        scen = [s for s in READ_SC if s in set(c.scenario)]
        hdr = ["Сценарій"] + [f"{short[v]} {m}" for v in vs for m in ("cold p50, мс", "cold/warm")]
        rows = []
        for s in scen:
            row = [s]
            for v in vs:
                cv, wv = p50(c, s, v), p50(w, s, v)
                row += [fmt(cv), fmt(cv / wv if wv else np.nan)]
            rows.append(row)
        out += ["### (б) Холодний кеш\n", md_table(hdr, rows)]

    # (в)
    if scale in data["server_server"] and scale in data["warm_summary"]:
        srv, w = data["server_server"][scale], data["warm_summary"][scale]
        vs = present(w.variant)
        scen = [s for s in READ_SC if s in set(srv.scenario)]
        hdr = ["Сценарій"] + [f"{short[v]} {m}" for v in vs for m in ("серверний, мс", "частка серверного")]
        rows = []
        for s in scen:
            row = [s]
            for v in vs:
                sv, cv = server_median(srv, s, v), p50(w, s, v)
                row += [fmt(sv), (fmt(100 * sv / cv) + " %") if cv and not np.isnan(sv) else "—"]
            rows.append(row)
        out += ["### (в) Серверний час (медіана) і його частка в клієнтському p50\n",
                "Серверний час: EXPLAIN ANALYZE для sql/jsonb, CPU-час профайлера для mongo.\n",
                md_table(hdr, rows)]

    # (г)
    if scale in data["writes_s4_raw"]:
        g = s4_throughput(data["writes_s4_raw"][scale])
        vs = present(g.variant)
        sizes = sorted(g.batch_size.unique())
        rows = []
        for b in sizes:
            row = [str(int(b))]
            for v in vs:
                r = g[(g.variant == v) & (g.batch_size == b)]
                row.append(fmt(r.trips_per_s.iloc[0]) if len(r) else "—")
            rows.append(row)
        out += ["### (г) S4: пропускна здатність, рейсів/с\n", md_table(["Розмір батча"] + vs, rows)]

    # (д)
    if scale in data["s8_throughput"]:
        d = data["s8_throughput"][scale]
        d = d[d.op == "ALL"]
        vs = present(d.variant)
        ws = sorted(d.workers.unique())
        hdr = ["Клієнтів"] + [f"{short[v]} {m}" for v in vs for m in ("оп./с", "p50, мс", "p99, мс")]
        rows = []
        for k in ws:
            row = [str(int(k))]
            for v in vs:
                r = d[(d.variant == v) & (d.workers == k)]
                row += [fmt(r[c].iloc[0]) if len(r) else "—" for c in ("ops_per_s", "p50_ms", "p99_ms")]
            rows.append(row)
        out += ["### (д) S8: змішане навантаження (op=ALL)\n", md_table(hdr, rows)]

    # (е)
    if scale in data["load"]:
        d = data["load"][scale]
        vs = present(d.variant)
        rows = []
        for v in vs:
            row = [v]
            tot = 0.0
            for ph in ("data", "indexes", "analyze"):
                x = d[(d.variant == v) & (d.phase == ph)].seconds.sum()
                tot += x
                row.append(fmt(x))
            row.append(fmt(tot))
            rows.append(row)
        out += ["### (е) Завантаження, с (без генерації)\n",
                md_table(["Варіант", "дані", "індекси", "analyze", "разом"], rows)]
    if scale in data["sizes"]:
        g = sizes_by_variant(data["sizes"][scale])
        rows = []
        for v in [v for v in VARIANTS if v in g.index]:
            unc = g.loc[v, "uncompressed"]
            rows.append([v, fmt(g.loc[v, "table"] / 2**20), fmt(g.loc[v, "index"] / 2**20),
                         fmt((g.loc[v, "table"] + g.loc[v, "index"]) / 2**20),
                         fmt(unc / 2**20) if v == "mongo" and unc and not np.isnan(unc) else "—"])
        out += ["### Обсяг на диску, МБ\n",
                md_table(["Варіант", "таблиці", "індекси", "разом", "без стиснення (mongo)"], rows)]

    # (є)
    if scale in data["writes_s6_raw"]:
        d = data["writes_s6_raw"][scale]
        vs = present(d.variant)
        rows = []
        for sc in ("S6all", "S6open"):
            for v in vs:
                r = d[(d.scenario == sc) & (d.variant == v)]
                if len(r):
                    rows.append([sc, v, fmt(r.client_us.median() / 1000), fmt(r.changed.median()),
                                 fmt(r.changed.max())])
        out += ["### (є) S6: оновлення замовника\n",
                md_table(["Сценарій", "Варіант", "медіана, мс", "медіана changed", "макс. changed"], rows)]
    return "\n".join(out)


def write_tables(data, scales):
    REPORT.mkdir(parents=True, exist_ok=True)
    text = "# Результати бенчмарку\n\n" + "\n".join(tables_for_scale(data, s) for s in scales)
    (REPORT / "tables.md").write_text(text, encoding="utf-8")
    print("  записано report/tables.md")


def write_summary_all(data):
    frames = [df for n in ("warm_summary", "cold_summary", "writes_summary") for df in data[n].values()]
    if not frames:
        return
    out = pd.concat(frames, ignore_index=True)
    out.to_csv(RESULTS / "summary_all.csv", index=False)
    print("  записано results/summary_all.csv")


def main() -> int:
    scales = find_scales()
    if not scales:
        warn("у results/ немає підкаталогів з масштабами")
        return 0
    print(f"Масштаби: {', '.join(scales)}")
    data = load_all(scales)
    REPORT.mkdir(parents=True, exist_ok=True)
    steps = [
        *[(f"latency_warm_{s}", lambda s=s: plot_latency_warm(data, s)) for s in scales],
        *[(f"latency_cold_{s}", lambda s=s: plot_latency_cold(data, s)) for s in scales],
        ("scaling", lambda: plot_scaling(data, scales)),
        ("s8", lambda: plot_s8(data, scales)),
        ("s4", lambda: plot_s4(data, scales)),
        ("s6", lambda: plot_s6(data, scales)),
        *[(f"server_vs_client_{s}", lambda s=s: plot_server_vs_client(data, s)) for s in scales],
        ("load", lambda: plot_load(data, scales)),
        ("sizes", lambda: plot_sizes(data, scales)),
        ("tables", lambda: write_tables(data, scales)),
        ("summary_all", lambda: write_summary_all(data)),
    ]
    for name, fn in steps:
        try:
            fn()
        except Exception as e:  # один графік не має валити весь звіт
            warn(f"крок {name} не вдався: {e.__class__.__name__}: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
