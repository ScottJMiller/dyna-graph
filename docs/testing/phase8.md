# Phase 8: Experiment implementation verification

The harness, analysis and figure tools are implemented. **Do not run the real
experiment during this protocol.** Verification uses simulated HTTP responses
and temporary synthetic data. It does not contact either Ollama server, start
the physical-node scenario, or produce measured research results.

The [integrated protocol extension](phase8_integrated.md) adds four conditions and
longer workloads. Its model-driven execution is separately gated. The commands
below continue to verify the original A/B protocol; aggregate counts include the
extension.

## 1. Setup and prerequisites

From the repository root:

```bash
source .venv/bin/activate
command -v python3
python3 --version
python3 -m pip install -r requirements.txt
python3 -m pip check
python3 -c "import matplotlib; print('Matplotlib', matplotlib.__version__)"
```

Expected: your virtual environment's `python3`, Python 3.11 or later,
`No broken requirements found.`, and `Matplotlib 3.10.7`. Matplotlib is the new
pinned direct dependency. Plotting stays local and headless; no GUI service,
remote analytics, cloud API, API key, or paid library is required. Package
installation can require internet access once; runtime inference uses the LAN.
Do not replace the private `.env` with the illustrative example.

## 2. Dedicated synthetic-only tests

```bash
python3 -m pytest tests/test_phase8_benchmark.py -v -s
```

Expected: **64 passed**, without skips or real network access. Real HTTP is
explicitly blocked in this test module; all model replies and token counts are
invented fixtures. Temporary outputs are marked `source=synthetic` and figures
carry `SYNTHETIC TEST DATA — NOT EXPERIMENT RESULTS`.

Coverage includes:

- Plan-only defaults and explicit execution gates, fresh output directories,
  application-database protection, and rejection of ambiguous test transports.
- The versioned 15–20 turn schedule, independent oracle, update/reversion cases,
  strict answer scoring, and oldest-first whole-turn history truncation.
- A synthetic 20-turn, two-repetition A/B pipeline using real role contracts,
  SQLite, extraction, supersession, retrieval, pruning and checkpoints.
- Fixed node routes, model inventories/digests, AB/BA order, per-condition graph
  isolation, corrective generation costs, and separate warmup accounting.
- Missing counters, negative savings, failed/cancelled evidence, checksums,
  duplicated/inconsistent rows, CSV/JSONL disagreement, and scoring validation.
- Descriptive paired analysis and standalone SVG/PDF/PNG figure export.
- Per-step task/sub-goal validation and durable recording in the controller.

For the focused synthetic acceptance checks:

```bash
python3 -m pytest tests/test_phase8_benchmark.py -k 'default_cli or synthetic_end_to_end or figures_are_standalone' -v -s
```

Expected: **3 passed, 61 deselected**, including:

```text
SYNTHETIC PIPELINE OK: 80 scored turns; AB/BA order; isolated graphs; no real model requests
FIGURES SYNTHETIC OK: 12 standalone files; synthetic labels retained
```

These are software-verification diagnostics. Do not cite synthetic counters,
latencies, accuracy or figures as experiment observations.

## 3. Inspect the plan safely

```bash
python3 -m experiments.benchmark_harness
```

Expected first line:

```text
PLAN ONLY: 4 condition runs; 80 scored turns; no model requests or experiment files
```

The JSON plan follows, with 20 turns, two repetitions, a 1,000-byte conservative
memory allowance, 128 reply tokens, one warmup per node before each condition,
scenario SHA-256 (including rendered task text), and this order:

```text
repetition 1: A, B
repetition 2: B, A
```

The default command does not load deployment settings, construct an HTTP client,
open a database, or create experiment artifacts. `--output` without `--execute`
also remains plan-only. Ordinary Python cache files may be created on import.
Options can be inspected without performing work:

```bash
python3 -m experiments.benchmark_harness --help
python3 -m experiments.analyze_results --help
python3 -m experiments.generate_plots --help
```

**Do not add `--execute` during Phase 8 verification.** No Phase 8 LAN test or
automatic experiment startup is registered in pytest.

## 4. Full offline regression and documentation checks

```bash
python3 -m pytest -q
node docs/testing/architecture-routing.mjs
git diff --check
git check-ignore .env .verification/ data/telemetry/ data/experiments/
git status --short
```

Expected: **791 passed, 18 skipped**, **7 geometry tests passed**, no whitespace
errors, and all four runtime/private paths ignored. The skips are 16 LAN tests
from earlier phases and the two opt-in storage/retrieval benchmarks. Do not
enable `--run-lan` or execute an experiment to verify this implementation.

The atlas now describes 79 components and 154 connections. All eight phases are
implemented, and the initial 80-turn measured case study is complete. Connectors keep
distinct perimeter attachments, avoid card interiors, and share no collinear
tracks. Source syntax, public links, component coverage and generic wording
should be reviewed before committing.

Earlier performance benchmarks remain available under their own protocols. With
both enabled, the aggregate count would be **793 passed, 16 skipped**; running
them is separate from this synthetic Phase 8 acceptance.

## 5. Comparison design and interpretation

Scenario version 2 replaces a literal answer placeholder with plain-language
format instructions. A first measured version-1 attempt stopped after 30 scored
turns because a malformed copied placeholder led to a grounding failure. That
attempt is preserved separately and cannot be combined with version-2 runs.
The observation schedule, oracle answers, models, limits and scoring rule are
unchanged. Rendered task text is now retained in the manifest and fingerprinted.

This is a controlled developmental case study of a **bounded context window**.
It is not a demonstration that an unbounded transcript can run on the baseline
hardware, or a proof of asymptotic complexity from 20 observations.

| Design choice | Contract |
| --- | --- |
| Scenario | `database-updates-v2`: 20 fixed turns, or an explicitly selected 15–20 turn prefix. Several projects each have one current database. Observations introduce changes and reversions, interleaved with immediate and delayed questions. |
| Oracle | `experiments/scenario.py` independently derives current answers from scheduled observations. Expected answers are retained for scoring and never supplied as a prompt field or used to filter accepted graph facts. |
| A: linear history | Supply prior task/answer turns in order. Drop oldest complete turns until serialized ASCII JSON fits the current memory allowance. Omitted turns are counted; no message is silently sliced. |
| B: graph memory | Extract each new observation and commit its validated facts before recall. Run the ordinary controller for recall, role execution, response extraction, governed writes and pruning. Extraction errors or stale model assertions can affect memory; the oracle does not repair it. |
| Incoming evidence | Both conditions receive the same current observation and question in the task. B additionally pays the cost of extracting/persisting that observation. |
| Domain policy | Within this scenario only, `uses` is explicitly single-valued per project. Real applications may allow several databases; no global production-policy change is made. |
| Shared limits | Same condition-neutral role prompts/schedule, model tags/digests, node thread settings, temperature 0, output cap, correction policy, no tools, 2,048-token model context, and at-most-1,000-byte conservative memory allowance. The actual allowance is reduced equally when a role/task needs more framing space. |
| Roles | Planner/node1 → researcher/node2 → verifier/node1, repeated for the chosen turn count. Verifier completion does not stop either condition early. Extraction stays on node2. |
| Node failures | Fallback is disabled. Missing Q4_K_M models, digest changes, wrong model routes, or inference failure fail the bundle rather than substituting another model. Models are never downloaded by the harness. |
| Repetitions/order | Default two repetitions: AB then BA. Additional repetitions alternate order. One repetition is allowed but gives no between-pair standard deviation. Maximum ten repetitions. |
| Isolation | Fresh graph/transcript for each condition and repetition. Servers are not restarted, and identical configured warmup counts run before each condition. Ollama can unload idle models; any later reload contributes to request timing. Setup and warmups are excluded from scored turns and recorded separately. |
| Quantization | Both configured installed tags must report `Q4_K_M`. Digests, Ollama versions, model sizes and thread counts are recorded and rechecked before each condition. |
| Reproducibility | Scenario hash, source hashes, package/Python versions, policy, ordering and inventories are recorded. Temperature 0 does not guarantee identical replies on every machine/run. |

The current default governance policy uses a long wall-time recency half-life.
A short scenario may exercise supersession and bounded recall without visibly
pruning many entities. Do not infer pruning efficacy merely because it is
enabled. Resource data covers coordinator Python RSS, not either Ollama process
or total cluster RAM.

## 6. Usage, timing and scoring boundaries

Each `BenchmarkRow` contains ordinary Phase 7 `TurnMetrics` plus condition,
repetition, order, observation, expected/actual answers, scoring labels, context
hash, history-window diagnostics, and observation-ingestion costs.

| Measurement | Interpretation |
| --- | --- |
| Role prompt tokens | Measured Ollama `prompt_eval_count` summed over role generations, including JSON corrections. This isolates what the responding role consumes. |
| Output extraction tokens | Ordinary Phase 7 extraction subtotal; zero for A. |
| Observation extraction tokens | Separate measured input-ingestion subtotal for B; zero for A or a turn without a new observation. |
| Total pipeline prompt/generated tokens | Role plus output extraction plus observation extraction. Missing any contributing counter makes the total unknown; no partial sum is presented as complete. |
| Prompt savings | `(A - B) / A * 100`, calculated separately for role-only and whole-pipeline totals. Negative values are retained. Missing counters or zero denominators produce null, not invented savings. |
| Inference latency | Sum of all returned role/output/input extraction request wall times, including corrections. Warmups are separate. No fallback is used. |
| Turn wall latency | Starts before history selection or input extraction; ends after role completion and memory/checkpoint commit, before CSV/JSONL export and plotting. Includes graph overhead and input ingestion in B. |
| History/context bytes | Conservative ASCII serialization budget, distinct from measured whole-prompt tokens. Raw accumulated history size and dropped-turn counts expose window saturation. |
| Strict accuracy | Match the declared `Project <name> uses <database>.` answer contract against the oracle, ignoring case, whitespace and terminal periods. Paraphrases or additional/conflicting claims fail this operational score. It is not a general semantic truth measure. |
| Update accuracy | Accuracy on questions whose subject has undergone a scheduled value change. |
| Delayed accuracy | Accuracy when the question is not immediately accompanied by a new observation about that subject. |
| Observed token slope | Descriptive least-squares slope of mean measured role-token counts against turn index, only when all counts are known. It is not an asymptotic inference; A also plateaus after history truncation. |
| Paired results | Per-repetition savings, wall-time difference and accuracy difference; mean/SD across complete pairs. With one pair SD is null. No significance test, confidence interval or independence of individual turns is claimed. |

Invalid responses that exhaust correction, missing servers and other failures
leave a failed/cancelled bundle. Analysis rejects the entire incomplete bundle;
it does not silently drop difficult turns or report a partial cohort as complete.

Incoming observation writes in B are separate accepted input transactions before
the usual atomic role checkpoint. If a later operation fails, that isolated
experimental graph can contain the observation even though the scored turn did
not complete. The bundle is incomplete and must not be analyzed as results.
Governance turns can therefore differ from scenario step numbers.

## 7. Artifact and analysis contract

An explicitly executed bundle contains:

```text
manifest.json       status, plan/oracle, role prompts, hashes, policy, versions, inventory, warmups, run completeness
turns.jsonl         strict authoritative per-turn benchmark records
metrics.csv         flat paper-friendly projection (m_* columns contain ordinary turn metrics)
A-1.csv, B-1.csv     ordinary per-run Phase 7 telemetry (input ingestion is additional in benchmark records)
A-1.db, B-1.db       isolated storage; B retains ordinary workflow checkpoints and graph history
...                  corresponding files for additional repetitions
```

The existing application database and `.env` are not overwritten. Experiment
bundles normally belong under ignored `data/experiments/` or `.verification/`.
Manifests omit deployment URLs, coordinator addresses and application DB paths.
Review outputs before publishing; checksums detect accidental corruption but do
not establish scientific validity or replace review of the experimental design.

Analysis requires a `COMPLETED` version-1 manifest, the expected condition/step
order, matching scenario/oracle, consistent scoring, fixed routes, all required
files and checksums, and exact CSV/JSONL agreement. Unknown counters remain null.
It writes `summary.json`, `paired.csv`, and `turn_means.csv` to a new directory.

Figure generation revalidates the original bundle. It exports four figure sets:
role prompt usage, complete-pipeline prompt usage, wall latency, and strict
accuracy. Each set has SVG, PDF and PNG output, for **12 files**. Plots show
per-turn means, without implied confidence bands; missing counters appear as
gaps. Synthetic input is prominently labeled in all figures. Matplotlib uses
its local file backends; see the [official backend documentation](https://matplotlib.org/3.10.7/users/explain/figure/backends.html).

Failed/cancelled bundles retain partial evidence, including `failure.json` with
the stage, exact extraction source message when available, raw generation
attempts and validation errors. No invalid triple is accepted to continue a run. There is no automatic
experimental resume; retry later in a **new directory**, preserving the failed
attempt and documenting any exclusions. Existing output/analysis/figure
directories are rejected rather than overwritten.

## 8. Explicit execution reference — do not run during verification

The initial approved physical-node experiment has completed. See the
[measured case study](../results/study-20261111-002/README.md) for all 80 scored turns,
paired comparisons, figures, preserved evidence, and the excluded unsuccessful
version-1 attempt. This verification protocol continues to run offline.

The following commands start a **new measured experiment**, separate from
implementation verification and from reproducing the saved analysis.

After both configured models are installed, routing is verified, and the
experiment is explicitly approved, choose a new directory:

```bash
python3 -m experiments.benchmark_harness --execute --output data/experiments/study-001 --turns 20 --repetitions 2
```

Expected: per-turn `TURN ...` progress, followed by
`EXPERIMENT COMPLETE: <bundle_id> (...)`. This plan scores 80 role turns plus
graph extraction and warmup requests. It can take a substantial time on baseline
hardware; the case study records its observed duration for that configuration.
Do not extrapolate from fast synthetic tests or treat one deployment as a baseline
hardware benchmark. Ctrl+C marks partial evidence cancelled and returns exit code
130. Ordinary errors return 1; completed runs return 0.

Only after a complete measured bundle exists:

```bash
python3 -m experiments.analyze_results --input data/experiments/study-001 --output data/experiments/study-001-analysis
python3 -m experiments.generate_plots --input data/experiments/study-001 --output data/experiments/study-001-figures
```

Expected: `ANALYSIS OK: source=measured paired_runs=2` and
`FIGURES OK: 12 files (SVG, PDF, PNG)`. The initial case study includes these
measured outputs. To reproduce its analysis without inference, use a fresh output:

```bash
python3 -m experiments.analyze_results --input docs/results/study-20261111-002 --output .verification/study-20261111-002-analysis
```

## 9. Commit message

```text
fix(experiments): remove answer placeholder and record initial measured study

- version rendered task prompts and retain extraction failure diagnostics
- preserve the excluded failed attempt and restart an isolated AB/BA cohort
- retain all 80 measured turns, validated bundles, paired analysis and figures
- report role-only and pipeline costs alongside strict accuracy and limits
- update generic documentation and add prompt/failure regression coverage
```

Commit and push after reviewing the results, disclosure, implementation changes,
and safe verification steps. Reproducing saved analysis does not run the models.
