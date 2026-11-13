# Refactor Step 2: readable experiments and docstrings

This step improves the experiment implementation without changing the research
protocol. The next step, reorganizing shared helpers, is not included here.
No experiment was rerun, and no private configuration was changed.

## Changes to review

| Module | Readability change |
| --- | --- |
| `experiments/analyze_results.py` | Separate original-study manifest, artifact, turn, run-order and CSV validation; document summary calculations and entry points. |
| `experiments/benchmark_harness.py` | Add purpose/constraint docstrings to the original runner's helpers and role override; executable statements are unchanged. |
| `experiments/generate_plots.py` | Document original-study plot generation and its CLI; executable statements are unchanged. |
| `experiments/integrated_analysis.py` | Give manifest/policy checks, artifacts, turn identity, memory replay, durable checkpoints and final CSV validation named stages; expand summary calculations and document unknown-counter handling. |
| `experiments/integrated_harness.py` | Expand compressed statements, give temporary values descriptive names, separate manifest construction, and document clock, input ingestion, execution and CLI behavior. |
| `experiments/integrated_contracts.py` | Expand policy records and projection logic; make row validation a sequence of attempt-cost, role-reply, extraction, total and outcome checks; document models and their methods. |

All classes and functions in these six modules, including nested helpers, now
have docstrings. Scenario files, prompts, policy values, scoring, output fields,
retry budgets, failure classification and public entry points are unchanged.
Historical manifests retain their original source hashes; their hashes are not
updated to describe this maintenance code. The original
[baseline inventory](refactor_baseline.json) remains unchanged.

## 1. Setup and evidence verification

From the repository root:

```bash
source .venv/bin/activate
python3 -m pip check
python3 docs/testing/verify_refactor_baseline.py
```

Expected:

```text
No broken requirements found.
PUBLISHED EVIDENCE OK: 115 unchanged files; 6 publication manifests
ORIGINAL ANALYSIS OK: 80 turns; full summary unchanged
INTEGRATED ANALYSIS OK: 960 turns; full summary unchanged
REFACTOR BASELINE OK: no inference requests or evidence writes
```

Omit `--check-source` in this step. Six source files intentionally differ from
the pre-refactor snapshot; the inventory remains the historical reference. A
source mismatch is not a reason to regenerate the baseline. The evidence and
complete saved JSON summaries must continue to match exactly.

## 2. Dedicated automated experiment tests

```bash
python3 -m pytest tests/test_phase8_benchmark.py tests/test_phase8_integrated.py -q
```

Expected: **180 passed**, no skips. The tests block real HTTP and cover planning,
execution gates, policy and route isolation, recorded failure/correction costs,
rejected replies, source grounding, independent answer scoring, memory replay,
SQLite agreement, missing counters, tampering rejection and exported figures.
These are synthetic checks, not another measured experiment.

For complete regression acceptance, run:

```bash
python3 -m pytest -q
```

Expected: **848 passed, 18 skipped**. The 16 LAN cases and two optional performance
cases remain skipped. The full run includes the 180 tests above, so there is no
need to run both commands for the same unchanged checkout. Do not enable live
LAN checks or launch either harness with `--execute` for this refactor.

## 3. Check the plan-only entry points

```bash
python3 -m experiments.benchmark_harness
python3 -m experiments.integrated_harness
```

Expected first lines, respectively:

```text
PLAN ONLY: 4 condition runs; 80 scored turns; no model requests or experiment files
PLAN ONLY: 16 condition runs; 960 scored turns; no model requests or experiment files
```

Both commands also print their plans as JSON. Their complete plan-only output
must match the pre-Step-2 implementation. These commands do not load `.env`,
contact models, create experiment outputs or initialize application databases.

## 4. Review scope and whitespace

```bash
git diff --check
git status --short
git diff --stat
```

Expected: no whitespace errors. Review the six experiment modules, updated
refactoring notes and this new protocol. There must be no changes to `src/`,
existing tests, scenario files, dependency pins, baseline inventory, published
results, private configuration or runtime data. The architecture diagram is
unchanged, so no additional Node.js checks are required for this step.

## Acceptance record

The working implementation reproduces both completed JSON analyses exactly and
preserves every published artifact checksum. The original runner and original
plotter were also checked for identical executable Python syntax trees with
docstrings excluded. All six changed modules were checked for class/function
docstrings. Verified on November 12, 2026:

- Full offline regression: **848 passed, 18 skipped in 89.95 seconds**.
- All 180 experiment tests are included in that passing full run.
- Both harnesses produced byte-identical plan-only stdout relative to the
  pre-Step-2 checkout.
- Both historical analyses matched the entire saved JSON summaries, and all
  115 published evidence files and six publication manifests passed.
- The live checkout, private configuration and runtime data were verified
  unchanged by isolated test execution. Only the reviewed Step 2 source and
  documentation files were subsequently applied.

Final expression wrapping was checked for identical executable syntax trees
against the passing source; no protocol or calculation was changed.

Verification does not establish identical future model answers or latency.
The changed source is a maintenance revision, not the measured experiment's
original implementation. See the [refactoring scope](../refactoring.md).

Suggested commit message:

```text
refactor: clarify experiment validation and document execution flow

Expand dense integrated experiment code and name validation stages.
Add class and function docstrings across the changed experiment modules.
Preserve published evidence, historical analyses and existing entry points.
```

Stop after this step for review before reorganizing shared helpers in Step 3.
