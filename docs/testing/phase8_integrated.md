# Phase 8 extension: Integrated memory experiment verification

This implements a new protocol-3 study alongside the original A/B protocol.
**Do not execute the model-driven study during implementation verification.**
All dedicated tests use synthetic HTTP; real HTTP is explicitly blocked. Previous
measured results and the deterministic pruning report remain unchanged. Both
model-driven integrated attempts aborted on truncated JSON. Their partial rows
are excluded. Protocol 3 now records bounded invalid agent replies as incorrect
turns under one common policy; a fresh complete cohort awaits approval. See the
[first](../results/integrated-attempt-001/README.md) and
[second failure report](../results/integrated-attempt-002/README.md).

## 1. Setup

From the repository root, use the existing virtual environment and dependencies:

```bash
source .venv/bin/activate
python3 -m pip check
```

Expected: `No broken requirements found.` No new dependencies, model downloads,
private configuration changes, or database migrations are required.

## 2. Dedicated automated checks

```bash
python3 -m pytest tests/test_phase8_integrated.py -q
```

Expected: **88 passed**. Tests cover gates, counterbalanced ordering, all four
policies, 60/70/80-turn workloads, actual prompt fingerprints, set-valued scoring,
full offline execution including four variants at 80 turns, logical versus real
clocks, explicit scenario turns, pin/multi-value preservation, pruning and delayed
loss, missing counters, failed/cancelled cohorts, immutable output directories,
artifact corruption, deliberately rewritten evidence checksums, SQLite agreement,
legacy measured-bundle analysis, six figure sets and private-path exclusions.
Checks replay both real truncation cases across A/B/C/D, commit rejected turns
and the following scheduled turn, preserve attempt costs and unknown counters,
and reject tampered success/cost/SQLite records. They also run entire synthetic
cohorts with every role reply invalid (including valid-looking `Unknown.` stopped
by length), and check default workflow exceptions, recording opt-in compatibility,
oversized/non-reply error propagation and frozen four-variant schedule hashes.
Synthetic accuracy, token counts and timings are not research findings.

## 3. Inspect the declared plan without running it

```bash
python3 -m experiments.integrated_harness
```

Expected first line:

```text
PLAN ONLY: 16 condition runs; 960 scored turns; no model requests or experiment files
```

The printed JSON includes the declared reply-error policy, fixed parameters, scenario fingerprints,
and condition order. This command does not load `.env`, contact models, create
outputs or initialize databases. `--output` alone remains plan-only. A partial
plan such as `--variants 1` has four conditions but is not fully counterbalanced.
The default four variants use `ABDC`, `BCAD`, `CDBA`, `DACB`: each condition occupies
each position once and each directed adjacent pair occurs once within runs.
This balances position/predecessor exposure across the declared workload variants;
it does not make individual turns independent samples.

## 4. Full regression and documentation

```bash
python3 -m pytest -q
node docs/testing/architecture-routing.mjs
git diff --check
git check-ignore .env .verification/ data/experiments/
```

Expected: **820 passed, 18 skipped**, **7 geometry checks passed**, no whitespace
errors, and the three private/runtime paths listed as ignored. The skipped checks
are 16 earlier LAN tests and two opt-in storage/retrieval performance checks.
Do not enable `--run-lan` as part of this offline extension acceptance.

## 5. Conditions and comparison questions

| Condition | Memory | Supersession | Pruning |
| --- | --- | --- | --- |
| A | Bounded oldest-whole-turn-first history | None | None |
| B | Graph | Disabled: no single-valued predicates | Off |
| C | Graph | `uses` single-valued | Off |
| D | Graph | `uses` single-valued | On |

The planned contrasts are D minus A (complete framework), C minus B
(supersession), and D minus C (pruning). B–D share extraction, retrieval,
serialization, declared priority/pinning behavior and all other settings.
`supports` and `links` remain multi-valued. No oracle is used to accept/filter
model-extracted facts, restore missing facts, select seeds, pin future answers,
or change the workload after a result. Pinning follows an explicit critical
subject declaration supplied in every condition's task at introduction.

The fixed retention parameters match the earlier case study: 24-hour recency
half-life, ten-scenario-turn half-life, threshold 0.15 and pruning batch size 100.
Weights remain 0.35/0.20/0.15/0.30. The shared memory allowance is at most 1,000
UTF-8 bytes, further reduced when role/task framing requires it. The model
context is 2,048 tokens and replies are capped at 128 generated tokens by default.
Fallback, local tools and early completion are disabled.

## 6. Workload, clock and interpretation

Each deterministic variant has 60–80 actual scored role turns, with seeded entity
and value names and filler observations. It includes single-valued replacements
and reversions, a multi-valued relation, a declared critical pinned subject,
an abandoned pair, a delayed unpinned fact, an unused cycle, unknown questions,
and repeated frequent facts. Questions at steps 40/56 revisit the unpinned fact;
critical and cycle probes provide distinct controls. Prefix summaries are
reported at 20/40/60 and, when present, 80. The generator retains exact rendered
tasks, independent expected value sets, source-observation steps and categories.
Recall distance is the gap since the last scheduled observation or question
about each required value (the maximum gap for a multi-value question). A prior
question counts as a retrieval opportunity even if a condition answered wrongly;
this keeps the schedule identical across conditions. Source age since the latest
state-introducing observation is a separate field. Frequently rehearsed facts
therefore do not inflate the delayed-recall category. Different seeds instantiate
the same experimental structure; they are a small
controlled case-study sample, not broad application validation.

Governance time is **two simulated hours per scenario turn**, starting at 7,200
seconds on turn one. Incoming writes, priority access, recalled-context usage,
response writes and pruning use the same clock and explicit scenario turn.
Internal observation extraction does not add another decay turn. The default
application state-machine behavior still advances its global governance counter
automatically. A separate observation clock records real timestamps, while
`perf_counter` measures actual durations. SQLite audit timestamps remain real.
Simulated multi-day memory age is not multi-day operational uptime.

Every scripted observation appears in the task of the role assigned to that
turn. A later probe is labeled cross-node when a required value was introduced
to a role on the other node. The analysis separately records whether the stored
fact was live before recall and whether it was actually supplied in context.
This measures routed-input exposure and later consumers through a central graph;
it does not claim autonomous information discovery, direct peer messaging,
replicated storage, out-of-order event resolution or partition tolerance.

The answer contract is one factual sentence per value, or `Unknown.`. Scenario
`integrated-memory-v2` explicitly limits answers to direct facts for the requested
subject/predicate and forbids repetition, explanations, reverse and transitive
inferences. This applies to every condition. Compared with scenario v1, the task framing and
fingerprints changed; observations, oracle metadata, scoring and the output cap
did not. Longer framing can reduce the shared memory allowance. Protocol 3
retains that v2 framing unchanged. Scoring
compares normalized complete fact sets, accepting value order differences. Format
validity is reported separately, but invalid-format answers remain incorrect in
the primary score. After exhausted validation of bounded role replies, protocol 3
records `answer: null` plus `reply_failure`; the score and format flag are false
regardless of the oracle, even for unknown probes. No reply text is repaired,
backfilled, accepted or extracted. A baseline history entry retains the task and
a null answer. Graph observation facts stay accepted; recalled memory is marked
used and normal pruning/checkpointing advances the scheduled turn. SQLite's
required message field holds an explicit rejection marker plus structured failure
evidence; the marker is never an answer or extracted fact. The same policy applies
to A–D. Ordinary workflows raise by default; tool-free benchmark workflows opt in
with `StateMachine(..., record_reply_errors=True)`. The option is part of checkpoint
compatibility. Retry limits, prompts, generation caps and scenario fingerprints
are unchanged from `integrated-memory-v2`; the container protocol changes to 3. This is a narrow operational contract,
not semantic judging.

Both strategies have an explicit memory ceiling. A finite curve does not prove
asymptotic complexity. The code's bound is conditional on bounded task/role
framing and capped calls/replies; cumulative tokens still increase with turn
count. The baseline also records full unwindowed history bytes and dropped turns
as a reference, without sending an oversized transcript to a model.

## 7. Evidence, costs and failure handling

A new bundle contains a version-3 manifest, `turns.jsonl`, its exact `metrics.csv`
projection, and isolated per-condition/per-variant SQLite and telemetry CSV files.
The manifest records the fixed plan, primary outcomes, hypotheses, policies,
source hashes, complete rendered workload/oracle, model digests, software versions,
warmups and exact completion order. Defaults produce 960 role turns, plus graph
extraction and warmup requests; this is substantially larger than the old study.

Each turn records the actual task, observation, answer and supplied memory;
pre-recall target facts; recalled fact IDs; observation/response state changes;
post-write target facts/counts; pruning events; and usage/timing. Retained old
facts, inactive endpoints and reactivation remain visible. Pruning can lose a
later-needed fact, and graph-only conditions can perform as well as the full
framework; such findings are retained rather than treated as execution errors.

Full-pipeline counters include observation extraction, every attempted role call
and any actual response extraction, including correction attempts. A rejected
reply has zero response-extraction calls. Its role counters and latency are retained;
missing counters stay unknown. Skipping extraction can lower costs, so summary
reports include invalid-reply counts/rates and paired rate differences alongside
accuracy and costs; primary accuracy retains every scheduled turn. Role-only counters are separate.
Unknown counters stay null. Warmup, database setup and output export are excluded
from turn timing. Diagnostic snapshots are included in turn wall timing in all
conditions; graph-size-dependent diagnostic work is therefore part of this
instrumented comparison. RSS remains coordinator-Python RSS, not cluster RAM.

Analysis validates all checksums, exact order and completeness, workload/scoring,
shared allowances, clock separation, policy invariants, transition replay,
retrieval contents, durable SQLite checkpoints/final graphs, telemetry CSVs and
the benchmark CSV projection. It reports category/recall-distance accuracy,
cross-node probes, absent/superseded/pruned support, retrieval omission, answer
errors with supplied support, active conflicts, pruning events, complete costs,
and paired differences per workload variant. Descriptive contrast mean/SD uses
variants rather than turns as repetitions; there are no significance claims.

Exhausted bounded agent JSON validation is a recorded scored failure, not an
aborted cohort. Each raw response is retained with its node/model, token counters,
latency and stop reason, at most two attempts of at most 16,384 characters each.
Analysis verifies the original parser rejects every attempt, exhausted retry
counts, no accepted answer/response facts, exact cost totals, routing and SQLite
agreement. Per-turn evidence remains bounded (300,000 characters per JSONL line).

Missing servers, model changes, extraction failures, tools, cancellation,
oversized failure evidence, storage errors and invalid evidence/clocks still
abort and retain a FAILED/CANCELLED bundle. The analyzer rejects partial cohorts.
There is no automatic experiment resume or restart. Existing application data and
earlier evidence are never overwritten. Protocol-2 integrated attempts remain
unchanged and are not combined with a protocol-3 cohort.

## 8. Fresh, separately approved execution

These commands reference a fresh approved model-driven cohort after both failed
attempts. They are **not part of implementation verification**; protocol 3
has not completed a full model-driven run. Do not resume the failed
bundle or reuse its completed conditions. Choose fresh paths:

```bash
python3 -m experiments.integrated_harness --execute --output data/experiments/integrated-003
python3 -m experiments.integrated_analysis --input data/experiments/integrated-003 --output data/experiments/integrated-003-analysis
python3 -m experiments.integrated_analysis --input data/experiments/integrated-003 --output data/experiments/integrated-003-figures --figures
```

Expected after a successful future run: progress through all 960 scored turns,
`INTEGRATED EXPERIMENT COMPLETE: <bundle_id>`,
`INTEGRATED ANALYSIS OK: source=measured variants=4 contrasts=12`, and
`FIGURES OK: 18 files (SVG, PDF, PNG)`. Choose fresh output paths for every step.
Full-cohort run duration has not been measured. Figures distinguish simulated aging
from actual measured inference; synthetic test figures have a prominent label.

The original commands under `experiments.benchmark_harness`,
`experiments.analyze_results` and `experiments.generate_plots` remain protocol-1
entry points. Do not pass protocol-3 bundles to the original analyzer or mix rows
from the studies. The original [measured report](../results/study-20261111-002/README.md)
and [pruning probe](../results/pruning-probe-001/README.md) remain historical evidence.

## 9. Failure diagnosis and restart boundary

Attempt 1 failed in D1/59 after 178 saved turns; attempt 2 failed in C1/59 after
238 saved turns. Both original and corrective role responses reached 128 tokens
while repeating cycle relationships. The prompt revision did not reliably
prevent this. All partial rows remain excluded; both original bundles, raw
attempts, checksums and earlier studies are preserved.

The new policy records these model-reply failures as research outcomes instead
of discarding the rest of the planned schedule. Offline replay demonstrates
scheduled continuation and honest failure accounting, not real-model accuracy
or guaranteed completion. No model calls were made during this implementation.
A fresh full protocol-3 cohort requires approval after verification. No partial
conditions from either earlier attempt may be reused.

## 10. Commit message

```text
fix(experiments): record invalid agent replies as scored outcomes

- preserve both failed integrated attempts without pooling partial rows
- record bounded rejected replies with null answers and complete attempt costs
- skip response extraction while retaining normal graph usage and checkpoints
- apply one protocol-3 policy to all conditions with default workflows unchanged
- replay both failures and validate corrupt evidence and all-invalid cohorts
```
