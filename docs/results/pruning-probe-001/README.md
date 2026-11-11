# Deterministic pruning mechanism probe

This is an offline mechanism test with **simulated time**, real SQLite storage,
and the existing governance and active-only traversal APIs. It is separate from
[the model-driven A/B case study](../study-20261111-002/README.md).
There were no model requests, agent-generated answers, real-time aging waits,
model-performance measurements, or changes to the retention policy.

## Results

| Memory category | Pruning off at 96 simulated hours | Pruning on at 96 simulated hours |
| --- | --- | --- |
| Frequently consumed fact | Active and retrievable | Active and retrievable |
| Abandoned fact | Active and retrievable | PRUNED and unavailable to active-only retrieval |
| Pinned critical fact | Active and retrievable | Active and retrievable |
| Fact needed again after a long gap | Active and retrievable | PRUNED and unavailable to active-only retrieval |
| Unused three-fact cycle | All three active | All three active: every endpoint has degree two |

The pruning-off condition retained seven active triples and eleven active
entities. Pruning on ended with **five active triples, two PRUNED triples, seven
active entities and four PRUNED entities**. All original rows remained stored.
The active-triple reduction was 2/7 (28.6%); this is a count reduction in a tiny
fixture, not a performance or storage-space saving.

Both conditions had all seven triples active through 36 simulated hours. The
first sampled pruning occurred at **48 hours / logical turn 120**. The last
non-pruning sample was 36 hours / turn 100: the probe does not identify the exact
threshold-crossing instant between those checkpoints.

At 48 hours, each unused, unpinned pair endpoint scored approximately **0.13508**,
below the **0.15** threshold. The pinned pair had the same decay but remained
protected. At 96 hours, the unused cycle's endpoints scored approximately
**0.10104**, also below threshold, but the hard leaf-only eligibility rule
prevented their pruning.

Two fresh executions of each condition produced identical logical evidence.
All **12 mechanism checks passed**. These repetitions check reproducibility;
they do not provide statistical evidence about a population of workloads.

## What this demonstrates

The current implementation applies decay, pin protection, leaf eligibility and
soft status changes as intended. Frequently consumed facts can survive while
unused eligible facts disappear from active-only retrieval. The 30-minute /
turn-20 and one-hour / turn-40 checkpoints also retained every fact, consistent
with the long recency half-life.

It also demonstrates two limitations. First, the policy cannot distinguish the
abandoned fact from an equally old, equally accessed fact that will be needed
again: both are pruned. Of the three facts required at the final diagnostic
(frequent, pinned and delayed), pruning on retained **2/3**, versus **3/3** with
pruning off. This is graph-fact availability, **not model answer accuracy**.
Second, an unused cycle survives despite low scores because it contains no
eligible leaves. More elapsed time alone does not remove that cycle.

No benefit to model accuracy, token use, retrieval latency, total latency or
physical database size follows from this mechanism test. Pruning does not
physically delete the stored evidence. No model-driven follow-up was run.

## Declared design

- Policy matches the recorded A/B configuration: recency half-life 86,400 seconds,
  turn half-life 10, weights 0.35/0.20/0.15/0.30, pruning threshold 0.15 and batch
  size 100. No private configuration is loaded.
- Both conditions begin with the same hand-authored seven facts and eleven
  entities. The four primary facts have disjoint endpoint pairs; the additional
  three facts form an isolated cycle. Only the critical subject is pinned;
  its active neighbor receives the existing pin protection.
- No fact is superseded. This isolates pruning from write-time replacement.
- Checkpoints are `(simulated hours, logical turn)`: (0,1), (0.5,20), (1,40),
  (12,60), (24,80), (36,100), (48,120), (60,140), (72,160), (96,200).
  Turn numbers are explicit governance inputs, not executed agent turns.
- Before each checkpoint after initialization, the frequent fact's real
  traversal context is consumed and its entities receive one access update.
  Other facts receive no refresh. Labels such as abandoned and delayed are
  external interpretation labels; they are not supplied to the pruner.
- Pruning runs once at each listed checkpoint in the on condition and never in
  the off condition. The entire small fixture fits one batch. This does not test
  daemon scheduling or intermediate passes.
- At the final checkpoint, each subject is passed to active-only `Traversal`.
  Presence of its original triple is recorded without incrementing access
  counters. This isolates memory availability from lexical matching or model
  reasoning. Final probes do not restore pruned facts.
- The explicit governance clock starts at zero. SQLite creation/update audit
  timestamps remain actual execution timestamps; only logical evidence, not
  database bytes, is expected to reproduce identically.
- [plan.json](plan.json) was written before either condition executed. The
  source hashes and artifact checksums are retained in [manifest.json](manifest.json).

## Evidence and reproduction

- [Full logical evidence](evidence.json): every pre-prune factor/score, eligibility
  reason, post-pass state and final lookup result, for both executions.
- [Timeline CSV](timeline.csv) and [summary with all checks](summary.json).
- Separate SQLite files: [off-1](off-1.db), [on-1](on-1.db),
  [off-2](off-2.db), [on-2](on-2.db).
- [Verification protocol](../../testing/pruning.md) and
  [dedicated automated probe](../../testing/pruning_probe.py).

From the repository root, choose a fresh output directory:

```bash
source .venv/bin/activate
python3 docs/testing/pruning_probe.py --output .verification/pruning-probe-review-001
```

Expected: `PRUNING PROBE OK: 12 checks passed`, first observed pruning at 48
simulated hours, and a delayed fact available with pruning off but missing with
pruning on. Review the full expected output in the verification protocol.
`published-sha256.json` covers the original bundle plus this derived report;
these checksums detect accidental changes and are not scientific validation.
