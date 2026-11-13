# Post-experiment readability refactor

The completed experiments used the source and parameters recorded in their
original manifests. Later readability changes are maintenance work, not the
implementation originally measured. They must not silently alter published
results or be presented as a rerun of the experiment.

## Step 1: pre-refactor reference

The starting revision is `97d0d64dbbb4b14d6a1a2897db3f6b3a60930491`.
It includes the completed original A/B study, pruning probe, excluded integrated
attempts, completed integrated study and chronological README history.
The working tree was clean before this step. No tag or commit was created by
this step; the revision above remains the existing reference.

The [baseline inventory](testing/refactor_baseline.json) records SHA-256 hashes
for 57 source/configuration/test files and 115 published evidence files, plus
six publication checksum manifests. It intentionally omits private configuration
and runtime data. These remain local and must not be edited during refactoring.
The baseline itself is a committed reference: do not regenerate its hashes to
make an unexpected mismatch pass. Checksums detect changed files, not scientific
validity or historical authenticity.

Research provenance remains study-specific:

| Study | Measured implementation reference |
| --- | --- |
| [Original A/B study](results/study-20261111-002/README.md) | Its manifest records source hashes; the published source patch is against revision `a416592604dc766d2dd7c8a17a6edc33650cc3c5`. Use both to identify the measured working-tree source. |
| [Integrated study](results/integrated-004/README.md) | Revision `8753b25b36a3a41094c6642d4ffaf504387cc3e3`, with exact source hashes in its manifest. |

Neither the starting revision nor future maintenance commits replace these
measured-source references. Existing summaries, figures, raw records, SQLite
files, manifests and failed-attempt reports remain unchanged. Do not open saved
evidence through the application's database initializer or migrate it. Analysis
uses its existing evidence readers; integrated SQLite validation is read-only.

The [dedicated baseline verifier](testing/verify_refactor_baseline.py) checks all
published evidence hashes and reproduces the complete JSON summaries of both
completed model-driven studies using the working analysis code. This includes
all curves, contrasts, failure counts, missing counters and descriptive metrics,
not merely headline accuracy. It performs no model requests and writes no
analysis outputs. The deterministic probe and excluded attempts are protected
by their original publication checksums; they are not treated as measured
completed cohorts or rerun by this verifier.

## Scope and review sequence

Steps 1 and 2 are implemented. Each later step requires its own review before
the next starts. All changes retain local-only inference, the two-node layout,
existing CLI entry points and generic public setup documentation.

| Step | Scope | Required acceptance |
| --- | --- | --- |
| 1 | Record and verify the pre-refactor source/evidence boundary. | Source hashes, publication checksums, unchanged analyses, offline suite and optional diagram checks pass. |
| 2 | Expand dense experiment code, improve local names and divide long validation functions into readable stages. | Historical summaries remain identical; existing experiment regression tests pass. |
| 3 | Give shared experiment helpers an explicit home; review private helper imports in the core modules. | Original and integrated entry points retain their behavior; historical analyses and affected tests pass. |
| 4 | Clarify orchestration stages and explain storage ownership. Preserve atomic commits, retries, cancellation and the existing database schema. | Storage, orchestration, governance, extraction and telemetry checks pass. |
| 5 | Improve test readability and repeated setup without removing failure coverage or changing phase-based filenames. | Full offline suite passes with live requests still opt-in. |
| 6 | Make README starting points clearer and link detailed guides; retain the full experiment history and optional interactive atlas. | Documentation links and relevant commands work; diagram geometry remains valid. |

This is a behavior-preserving refactor. Changes to prompts, scenarios, answer
scoring, fact admission, abstention handling, pruning policy or recovery are
separate feature work. No new dependencies, CI/CD, build system, database schema
or model downloads are planned. Pinned requirements remain unchanged.

During Step 1 the optional `--check-source` flag must pass. After an approved
source change, omit that flag while continuing to require unchanged evidence and
identical historical analysis. Keep the original source hashes for reference;
do not overwrite the original baseline when the working source evolves.

See the [Step 1 verification protocol](testing/refactor_step1.md) for commands,
expected output and the baseline acceptance record.

## Step 2: readable experiments and docstrings

Experiment validation is organized into named stages, compressed integrated
runner statements are expanded, and temporary names describe the values they
hold. The original A/B reader separates manifest, artifact, turn, run-order and
CSV validation. The integrated reader separates protocol/policy checks, turn
identity, memory replay, SQLite checkpoint agreement and projected CSV checks.
Integrated row contracts validate attempt costs, role replies, extractions,
totals and rejected outcomes in their original order.

All classes and functions in the six changed experiment modules have docstrings,
including nested helpers. The original runner and original plotter have
only docstring additions; their executable syntax is unchanged. Shared helper
imports remain in place for Step 3. Core runtime modules and tests remain
unchanged for their later steps.

The [Step 2 verification protocol](testing/refactor_step2.md) checks both full
historical summaries, all 115 published evidence hashes, plan-only CLI output,
and the existing offline regression suite. The original baseline inventory is
retained; `--check-source` is no longer an acceptance command because these six
source files intentionally changed. No model-driven experiment is rerun.
