"""Shared, local-only styling for the original and integrated study figures."""

from contextlib import contextmanager
from importlib.metadata import version
import json
from pathlib import Path


# Fixed condition identities: omitting A must never change the other colors.
# Okabe–Ito: black, blue, bluish green and vermilion.
COLORS = {'A': '#000000', 'B': '#0072B2', 'C': '#009E73', 'D': '#D55E00'}
MARKERS = {'A': 'o', 'B': '^', 'C': 's', 'D': 'D'}
LINESTYLES = {'A': ':', 'B': '--', 'C': '-', 'D': '-.'}
STYLES = ('science', 'ieee', 'grid', 'no-latex')


@contextmanager
def paper_style():
    """Apply SciencePlots locally, keeping TeX and global settings untouched."""
    import scienceplots  # Registers styles; deliberately lazy for analysis-only use.
    from matplotlib import rc_context, style

    with style.context(list(STYLES)), rc_context({
        'font.serif': ['DejaVu Serif'],
        'font.size': 9,
        'axes.titlesize': 10,
        'axes.labelsize': 9,
        'legend.fontsize': 8,
        'axes.grid.which': 'major',
        'grid.alpha': .22,
        'grid.linestyle': ':',
        'svg.fonttype': 'none',
        'pdf.fonttype': 42,
        'text.usetex': False,
    }):
        yield


def plot_series(axes, condition, x, y, *, scatter=False):
    """Draw a letter-only series with stable color, marker and line identity."""
    values = [float('nan') if value is None else value for value in y]
    axes.plot(
        x, values, label=condition, color=COLORS[condition],
        marker=MARKERS[condition], markersize=4 if scatter else 3,
        markerfacecolor='none', markeredgewidth=.8,
        linestyle='none' if scatter else LINESTYLES[condition], linewidth=1,
        markevery=1 if scatter or len(x) < 15 else 4,
    )


def save_figure(figure, output, name, *, synthetic=False):
    """Export the same layout as SVG, PDF and 300-dpi PNG."""
    if synthetic:
        figure.text(.5, .005, 'SYNTHETIC TEST DATA — NOT EXPERIMENT RESULTS',
                    ha='center', fontsize=7, color='#A00000')
        # Reserve a footer so the synthetic provenance never overlaps axis labels.
        figure.get_layout_engine().set(rect=(0, .045, 1, .955))
    paths = []
    for suffix in ('svg', 'pdf', 'png'):
        path = Path(output) / f'{name}.{suffix}'
        figure.savefig(path, dpi=300)
        paths.append(path)
    figure.clear()
    return paths


def write_figure_notes(output, report, captions, conditions, data, bundle):
    """Keep caption details and plotted values beside fresh figure exports."""
    from experiments.contracts import sha256

    output = Path(output)
    lines = ['# Paper figures', '', f'Evidence source: **{report["source"]}**.', '',
             'Presentation revision only; no inference was run. The source bundle and',
             'published analyses/figures are unchanged. Percentages use the original',
             'strict scorer, including rejected replies as incorrect.', '',
             '## Conditions', '']
    lines += [f'- **{key}**: {name}.' for key, name in conditions.items()]
    lines += ['', '## Figure captions', '']
    for name, caption in captions.items():
        lines += [f'### {name}', '', f'![{name}]({name}.svg)', '', caption, '']
    lines += ['## Rendering and data', '',
              'SciencePlots styles: `science`, `ieee`, `grid`, `no-latex`.',
              'Okabe–Ito condition colors and distinct markers/lines are fixed across plots.',
              'DejaVu Serif is bundled with Matplotlib; no TeX or network assets are needed.',
              'Figures use a 7.16-inch two-column width; the overview uses two panels.',
              'Workload variants are paired descriptive observations, not independent turns.',
              'No confidence intervals or significance claims are added.', '',
              '`plot_data.json` contains the plotted values, denominators, variant scores,',
              'rendering versions and source manifest hash. Null costs stay unknown.', '']
    (output / 'README.md').write_text('\n'.join(lines))
    metadata = dict(
        source=report['source'],
        source_manifest_sha256=sha256(Path(bundle) / 'manifest.json'),
        styles=STYLES, colors=COLORS,
        versions={name: version(name) for name in ('matplotlib', 'SciencePlots')},
        plots=data,
    )
    (output / 'plot_data.json').write_text(json.dumps(metadata, indent=2, allow_nan=False) + '\n')
