"""Export standalone scientific figures from a validated local result bundle."""

import argparse
import csv
from pathlib import Path
import sys

from experiments.analyze_results import analyze_bundle


def generate_plots(bundle, output):
    """Export four paper figure sets from a validated original A/B bundle."""
    report = analyze_bundle(bundle)
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    from matplotlib.ticker import PercentFormatter
    from experiments.plot_style import paper_style, plot_series, save_figure, write_figure_notes

    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    paths, captions, data = [], {}, {}
    with paper_style():
        for key, title, ylabel in (
            ('role_prompt_tokens', 'Role Prompt Usage', 'Mean Role Prompt Tokens'),
            ('total_prompt_tokens', 'Full-Pipeline Prompt Usage', 'Mean Full-Pipeline Prompt Tokens'),
            ('wall_ms', 'Turn Latency', 'Mean Turn Latency (s)'),
            ('accuracy', 'Strict Accuracy by Turn', 'Strict Accuracy'),
        ):
            figure = Figure(figsize=(7.16, 3.3), layout='constrained')
            FigureCanvasAgg(figure)
            axes = figure.subplots()
            data[key] = {}
            for condition in ('A', 'B'):
                rows = [row for row in report['curves'] if row['condition'] == condition]
                x = [row['step'] for row in rows]
                y = [None if row[key] is None else row[key] / (1000 if key == 'wall_ms' else 1)
                     for row in rows]
                plot_series(axes, condition, x, y)
                data[key][condition] = [dict(turn=turn, value=value) for turn, value in zip(x, y)]
            axes.set(xlabel='Turn', ylabel=ylabel, title=title)
            axes.legend(loc='upper left', bbox_to_anchor=(1.01, 1), frameon=False)
            if key == 'accuracy':
                axes.set_ylim(-.025, 1.06)
                axes.yaxis.set_major_formatter(PercentFormatter(xmax=1))
            else:
                axes.set_ylim(bottom=0)
            paths.extend(save_figure(figure, output, key, synthetic=report['source'] == 'synthetic'))
            captions[key] = (
                f'Arithmetic means across {report["plan"]["repetitions"]} original-study repetitions. '
                'A is bounded linear history; B is the original full graph-memory condition. '
                'The original B differs from integrated-study B; these studies must not be pooled. '
            )
            if key == 'wall_ms':
                captions[key] += 'Measured elapsed turn time converted from milliseconds to seconds.'
            elif key == 'accuracy':
                captions[key] += 'The original strict answer contract is unchanged.'
            else:
                captions[key] += (
                    'Role counts omit extraction work; pipeline counts include role and extraction attempts. '
                    'Missing counters remain unknown (gaps), never zero or partial-subset means.'
                )
    write_figure_notes(output, report, captions,
                       {'A': 'Bounded linear history', 'B': 'Original full graph memory'}, data, bundle)
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
