from __future__ import annotations

from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


def plot_result(result, path=None, show=False):
    fig, axes = plt.subplots(3, 1, figsize=(10, 8), sharex=True)
    axes[0].plot(result.equity.index, result.equity.values)
    axes[0].set_title("Equity")

    drawdown = result.equity / result.equity.cummax() - 1
    axes[1].plot(result.equity.index, drawdown.values * 100)
    axes[1].set_title("Drawdown %")

    axes[2].plot(result.equity.index, result.candles["close"].values, color="black", alpha=0.5)
    mask = result.position.abs() > 0
    axes[2].scatter(result.equity.index[mask], result.candles["close"].values[mask], color="tab:blue", s=8)
    axes[2].set_title("Close with position")

    fig.tight_layout()
    if path is not None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(path)
    if show:
        plt.show()
    plt.close(fig)
    return fig


def plot_comparison(results: dict, path=None, show=False):
    fig, axes = plt.subplots(2, 1, figsize=(10, 8), sharex=True)
    for name, result in results.items():
        axes[0].plot(result.equity.index, result.equity.values, label=name)
    axes[0].legend()
    axes[0].set_title("Equity comparison")

    for name, result in results.items():
        drawdown = result.equity / result.equity.cummax() - 1
        axes[1].plot(result.equity.index, drawdown.values * 100, label=name)
    axes[1].legend()
    axes[1].set_title("Drawdown %")

    fig.tight_layout()
    if path is not None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(path)
    if show:
        plt.show()
    plt.close(fig)
    return fig
