"""Render portfolio figures from frozen, compact research artifacts only."""
from __future__ import annotations

import csv
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np


ROOT = Path(__file__).resolve().parents[1]
FIGURES = ROOT / "docs" / "figures"


def read_csv(relative_path: str) -> list[dict[str, str]]:
    with (ROOT / relative_path).open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def save(figure: plt.Figure, filename: str) -> None:
    FIGURES.mkdir(parents=True, exist_ok=True)
    figure.savefig(FIGURES / filename, dpi=180, bbox_inches="tight")
    plt.close(figure)


def plot_obi_markout() -> None:
    rows = [
        row
        for row in read_csv("research/native_dev_v1/bucket_study.csv")
        if row["signal"] == "weighted_obi_l10"
    ]
    horizons = {
        "markout_100ms_ticks": "100 ms",
        "markout_500ms_ticks": "500 ms",
        "markout_1000ms_ticks": "1 s",
    }
    figure, axis = plt.subplots(figsize=(7.2, 4.2))
    for target, label in horizons.items():
        points = sorted(
            (row for row in rows if row["target"] == target),
            key=lambda row: int(row["bucket"]),
        )
        if len(points) != 10:
            raise ValueError(f"expected ten weighted_obi_l10 buckets for {target}")
        axis.plot(
            [int(row["bucket"]) + 1 for row in points],
            [float(row["target_mean"]) for row in points],
            marker="o",
            linewidth=2,
            label=label,
        )
    axis.axhline(0, color="0.45", linewidth=0.8)
    axis.set(
        title="Weighted OBI deciles and subsequent mid-price markout",
        xlabel="Weighted OBI L10 decile (low bid depth → high bid depth)",
        ylabel="Mean forward markout (ticks)",
        xticks=range(1, 11),
    )
    axis.grid(axis="y", color="0.9")
    axis.legend(title="Horizon", frameon=False)
    figure.tight_layout(rect=(0, 0.08, 1, 1))
    figure.text(
        0.01,
        0.01,
        "Source: research/native_dev_v1/bucket_study.csv; weighted_obi_l10.",
        fontsize=8,
        color="0.35",
    )
    save(figure, "obi-decile-forward-markout.png")


def plot_queue_sensitivity() -> None:
    rows = read_csv("research/native_economic_v1/queue_sensitivity_surface.csv")
    alphas = sorted({int(row["alpha_pct"]) for row in rows})
    betas = sorted({int(row["beta_pct"]) for row in rows})
    if len(alphas) != 5 or len(betas) != 5:
        raise ValueError("expected a 5 by 5 queue sensitivity surface")
    values = np.full((len(alphas), len(betas)), np.nan)
    for row in rows:
        values[alphas.index(int(row["alpha_pct"]))][betas.index(int(row["beta_pct"]))] = float(
            row["required_benefit_1000ms_bps"]
        )
    if not np.isfinite(values).all():
        raise ValueError("queue sensitivity surface is incomplete")
    figure, axis = plt.subplots(figsize=(7.2, 4.8))
    image = axis.imshow(values, cmap="YlOrRd", aspect="auto")
    for row_index, alpha in enumerate(alphas):
        for column_index, beta in enumerate(betas):
            axis.text(
                column_index,
                row_index,
                f"{values[row_index, column_index]:.2f}",
                ha="center",
                va="center",
                fontsize=9,
            )
    colorbar = figure.colorbar(image, ax=axis, pad=0.02)
    colorbar.set_label("Required 1 s benefit (bp)")
    axis.set(
        title="Passive-fill economics worsen with queue position",
        xlabel="Removal credit for displayed queue ahead, β (%)",
        ylabel="Initial displayed queue ahead, α (%)",
        xticks=range(len(betas)),
        xticklabels=betas,
        yticks=range(len(alphas)),
        yticklabels=alphas,
    )
    figure.tight_layout(rect=(0, 0.08, 1, 1))
    figure.text(
        0.01,
        0.01,
        "Each cell: required benefit to offset observed 1 s post-fill adverse selection. "
        "Source: research/native_economic_v1/queue_sensitivity_surface.csv.",
        fontsize=8,
        color="0.35",
    )
    save(figure, "queue-required-benefit-heatmap.png")


def plot_executable_pnl() -> None:
    rows = [
        row
        for row in read_csv("research/native_executable_pnl/oof_economics.csv")
        if row["model"] == "lightgbm"
        and row["side"] == "long"
        and row["horizon_ms"] == "60000"
        and row["tail"] == "0.01"
        and row["view"] == "non_overlapping_trade_tape"
    ]
    rows = sorted(rows, key=lambda row: float(row["additional_cost_bp_per_side"]))
    costs = [float(row["additional_cost_bp_per_side"]) for row in rows]
    net = [float(row["net_edge_bp_per_trade"]) for row in rows]
    gross = [float(row["gross_executable_edge_bp_per_trade"]) for row in rows]
    if costs != [1.0, 2.0, 3.0, 5.0] or len({round(value, 12) for value in gross}) != 1:
        raise ValueError("unexpected final executable-PnL cost rows")
    figure, axis = plt.subplots(figsize=(7.2, 4.2))
    bars = axis.bar([str(int(cost)) for cost in costs], net, color="#b55d45")
    axis.axhline(0, color="0.3", linewidth=0.9)
    axis.axhline(gross[0], color="#2f6f9f", linewidth=2, label=f"Gross edge: {gross[0]:+.4f} bp")
    for bar, value in zip(bars, net, strict=True):
        axis.text(
            bar.get_x() + bar.get_width() / 2,
            value - 0.32,
            f"{value:.2f}",
            ha="center",
            va="top",
            fontsize=9,
        )
    axis.set(
        title="Best LightGBM tape: gross edge does not clear costs",
        xlabel="Additional cost per side (bp)",
        ylabel="Net executable edge (bp / trade)",
    )
    axis.set_ylim(min(net) - 1.2, 0.8)
    axis.grid(axis="y", color="0.9")
    axis.legend(frameon=False, loc="upper right")
    figure.tight_layout(rect=(0, 0.08, 1, 1))
    figure.text(
        0.01,
        0.01,
        "Long, 60 s, top 1%, non-overlapping OOF tape; 503 executed trades. "
        "Source: research/native_executable_pnl/oof_economics.csv.",
        fontsize=8,
        color="0.35",
    )
    save(figure, "executable-pnl-cost-sensitivity.png")


def main() -> None:
    plot_obi_markout()
    plot_queue_sensitivity()
    plot_executable_pnl()


if __name__ == "__main__":
    main()
