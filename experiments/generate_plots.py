"""Export standalone scientific figures from a validated local result bundle."""

import argparse
import csv
from pathlib import Path
import sys

from experiments.analyze_results import analyze_bundle


def generate_plots(bundle, output):
    # Validate all evidence before creating a directory or loading plotting code.
    """Validate the original study and export four SVG/PDF/PNG figure sets."""
    report = analyze_bundle(bundle)
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    from matplotlib import rc_context

    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    paths = []
    label = "SYNTHETIC TEST DATA — NOT EXPERIMENT RESULTS" if report["source"] == "synthetic" else "Measured local A/B comparison"
    with rc_context({"font.size": 10, "axes.spines.top": False, "axes.spines.right": False,
                     "svg.fonttype": "none", "pdf.fonttype": 42}):
        for key, title, ylabel in (
            ("role_prompt_tokens", "Role prompt usage", "Measured prompt tokens / turn"),
            ("total_prompt_tokens", "Complete pipeline prompt usage", "Measured prompt tokens / turn"),
            ("wall_ms", "Turn wall latency", "Milliseconds / turn"),
            ("accuracy", "Strict answer-contract accuracy", "Fraction correct"),
        ):
            figure = Figure(figsize=(7.2, 4.4), layout="constrained")
            FigureCanvasAgg(figure)
            axes = figure.subplots()
            for condition, color, name in (("A", "#a35f22", "A: windowed history"), ("B", "#087f8c", "B: graph memory")):
                rows = [r for r in report["curves"] if r["condition"] == condition]
                y = [float("nan") if r[key] is None else r[key] for r in rows]
                axes.plot([r["step"] for r in rows], y, marker="o", markersize=3, color=color, label=name)
            axes.set(xlabel="Scenario turn", ylabel=ylabel, title=title)
            axes.set_xticks(range(1, report["plan"]["turns"] + 1, 2))
            axes.grid(axis="y", alpha=0.2)
            axes.legend(loc="best", frameon=False)
            if key == "accuracy":
                axes.set_ylim(-0.05, 1.05)
            if key.endswith("prompt_tokens") and any(r[key] is None for r in report["curves"]):
                axes.text(0.5, 0.02, "Missing counters are unknown (gaps), not zero", ha="center", transform=axes.transAxes, fontsize=8)
            figure.suptitle(label + f"\nMean across {report['plan']['repetitions']} repetition(s)",
                fontsize=9, color="#a33c22" if report["source"] == "synthetic" else "#555555")
            for suffix in ("svg", "pdf", "png"):
                path = output / f"{key}.{suffix}"
                figure.savefig(path, dpi=200)
                paths.append(path)
            figure.clear()
    return tuple(paths)


def main(argv=None):
    """Generate figures from existing evidence without contacting model servers."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="new figure directory")
    args = parser.parse_args(argv)
    try:
        paths = generate_plots(args.input, args.output)
        print(f"FIGURES OK: {len(paths)} files (SVG, PDF, PNG)")
    except (OSError, ValueError, KeyError, TypeError, csv.Error, ImportError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
