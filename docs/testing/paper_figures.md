# Paper figure verification and regeneration

This is an offline presentation revision. It adds SciencePlots 2.2.2 to the pinned
requirements and uses `science`, `ieee`, `grid`, `no-latex`. No TeX invocation,
model request, scenario change, new scoring rule or measured experiment occurs.
Original published artifacts and their checksum manifests must stay unchanged.

## 1. Install and check dependencies

From the repository root:

```bash
source .venv/bin/activate
python3 -m pip install -r requirements.txt
python3 -m pip check
```

Expected: `SciencePlots==2.2.2` is installed and
`No broken requirements found.` Existing dependency pins retain their versions.
Rendering remains local after package installation.

## 2. Automated scientific-data and regression checks

```bash
python3 -m pytest tests/test_benchmark.py tests/test_integrated.py -q
python3 docs/testing/verify_refactor_baseline.py
```

Expected: **183 passed** across both experiment suites. Existing tests validate
both bundles, rejected outcomes, missing counters, synthetic-only network use
and SVG/PDF/PNG exports. Added checks cover exact irregular gaps, undefined-gap
exclusion, retained failures, weighted pooled denominators, paired variant
markers, omission of A from graph size and conversion from milliseconds to
seconds. Integrated exports now contain eight sets (24 images); original exports
retain four sets (12 images). Figure folders also contain two caption/data files.

Expected evidence output:

```text
PUBLISHED EVIDENCE OK: 115 unchanged files; 6 publication manifests
ORIGINAL ANALYSIS OK: 80 turns; full summary unchanged
INTEGRATED ANALYSIS OK: 960 turns; full summary unchanged
REFACTOR BASELINE OK: no inference requests or evidence writes
```

For full acceptance, run this once instead of repeating the focused suites:

```bash
python3 -m pytest -q
```

Expected: **851 passed, 18 skipped**. LAN and optional timing checks remain opt-in.
No historical baseline hashes should be regenerated to account for source edits.

## 3. Regenerate from saved evidence into fresh directories

```bash
python3 -m experiments.integrated_analysis --input docs/results/integrated-004/bundle --output .verification/paper-integrated-001 --figures
python3 -m experiments.generate_plots --input docs/results/study-20261111-002 --output .verification/paper-original-001
```

Expected:

```text
FIGURES OK: 24 files (SVG, PDF, PNG)
FIGURES OK: 12 files (SVG, PDF, PNG)
```

Choose a fresh output name if already present. Both commands refuse to replace
existing directories. Inspect README.md for captions and condition definitions;
plot_data.json records values and the source manifest hash. Original analysis
summaries are unchanged: the added exact-gap and overview values are derived
only for presentation.

## 4. Visual and numeric review

Open [the figure index](../figures/README.md) and the generated PNGs/SVGs/PDFs.
Confirm titles are concise, legends contain only condition letters, colors are
consistent and markers distinguish coincident series. Legends sit outside axes.
Every synthetic test figure must retain its visible synthetic-data footer.

- active_edges: only B/C/D; x-axis `Turn`; active logical triples, not file size.
- role_prompt_tokens and total_prompt_tokens: `Turn`; mean prompt counts;
  missing pipeline counters remain gaps, not zero or partial-subset means.
- wall_ms: `Turn` and `Mean Turn Latency (s)`; plotted values are recorded means
  divided by 1,000. The filename retains compatibility with the source metric.
- delayed_recall: original categorical buckets, with counts in caption notes;
  distinct markers joined by thin straight lines as visual guides.
- delayed_recall_exact: actual numeric gaps 0, 1, 2, 3, 10, 12, 16, 22, 26,
  36, 37 and 50; distinct markers joined by thin straight lines at actual
  numeric positions. No smoothing, jitter or invented observations. Connecting
  lines guide the eye and do not estimate accuracy at unobserved gaps.
- overall_accuracy: overall counts A 4/240, B 76/240, C 112/240, D 102/240;
  Birch counts A 0/8, B 8/8, C 8/8, D 0/8. Percentages are calculated from
  these counts. Open points show four variant scores. There are no confidence
  intervals or significance claims. Both panels have zero baselines and ticks
  through 100%, with additional space only for annotations.

The overall score retains answer-format failures; A's low strict score cannot
be attributed entirely to forgetting. The Birch panel is a specific probe set,
not proof that C always remembers or D always forgets.

## 5. Architecture and scope

The new shared plot_style.py helper appears in the component inventory/atlas.
It is shown under Supporting files. Optional geometry verification:

```bash
node docs/testing/architecture-routing.mjs
git diff --check
git status --short
```

Expected: **7 geometry checks pass** for 81 components and 163 connections;
no whitespace errors. Review plotting source, dependency pin, plot regressions,
current documentation and the new paper-figure directories. Original measured
bundles, published figures, historical reports, private .env and runtime data
must remain unchanged. Commit/push remains a manual step.

## Acceptance record

Verified November 13, 2026: **851 passed, 18 skipped in 90.12 seconds**;
all seven connector checks passed. Both complete historical summaries matched,
with 115 published evidence files and six publication manifests unchanged.
Measured export data matched all overall/Birch counts, all 12 exact gaps and
228 eligible gap-bearing turns per condition. B's pipeline gaps remain at turns
12, 24 and 36. Latency conversion and source manifest identity were verified.
The overview, latency and exact-gap layouts were visually reviewed.

Suggested commit message:

```text
feat: add paper-ready experiment figures and accuracy overview

Apply SciencePlots/no-LaTeX styling and fixed Okabe-Ito condition colors.
Clarify titles, axes and mean latency in seconds; retain unknown cost gaps.
Add exact-gap recall and overall/Birch accuracy panels with variant points.
Export revised figures separately while preserving all published evidence.
Document captions, regeneration and scientific-data regression checks.
```

Connected-recall revision: regenerate the integrated figures into a fresh output
folder using Step 3, then compare both recall charts. Their values, denominators
and numeric gap positions must match the saved plot_data.json exactly.
