# Second excluded integrated experiment attempt

The revised-prompt integrated experiment failed during **C1 turn 59**. Its **238
completed turns are excluded from condition comparisons**. This folder contains
selected failure evidence, not a completed experimental cohort.

Bundle: `bc5538de8a2d4be8af9518678d353955`. Scenario: `integrated-memory-v2`; container protocol 2.
The attempt lasted 107.39 minutes, from 2026-11-11T23:52:15.643759+00:00 to 2026-11-12T01:39:39.112738+00:00.
A1, B1 and D1 completed 60 turns each; C1 completed 58. Variants 2–4 did not run.
The frozen plan required 960 scored turns. No analysis or figures were generated.

## Diagnosis

At the cycle probe, node2's `llama3.2:3b` repeated relationship text until it
exhausted 128 tokens. Both the initial response and its one correction stopped
with `done_reason=length`. The unfinished JSON was correctly rejected. The
[raw failure](failure.json) preserves both responses, counters and latencies;
its `repetition` field denotes workload variant 1. The [manifest](manifest.json)
records the exact plan, rendered inputs/oracle, policies, source hashes, model
inventory, completion state and original artifact checksums.

The earlier [attempt](../integrated-attempt-001/README.md) failed at the same
probe in D1. Narrower task instructions allowed the second attempt to proceed
through D1, but did not prevent the failure in C1. The earlier three unscored
checks established only that those particular requests completed JSON; they did
not establish robust completion or factual correctness. Further prompt tuning
or a larger output cap would not correct the benchmark's treatment of an invalid
model reply as an aborted cohort.

The original failed bundle remains under ignored
`data/experiments/integrated-20261111-002`. The public files here are a selected
subset; the manifest references other artifacts retained in that original
bundle. The latest durable C1 workflow turn is 58, with observation ingestion for
turn 59 already applied. Partial saved-row totals omit failed-turn work and are
not complete costs. Both failed attempts, private configuration, production data
and previous studies remain unchanged. There is no resume or pooling of rows.

## Protocol 3: explicitly recorded reply failures

A fresh full experiment will keep the `integrated-memory-v2` tasks, models,
observations, oracle, 128-token cap, retry count and memory policies. The evidence
container changes to protocol 3 with a declared reply-error policy applied to
**all four conditions**:

1. Run normal strict reply validation and the existing bounded correction.
2. If every attempt is an invalid/truncated bounded agent reply, retain the raw
   responses, done reasons, node/model, counters and measured latencies.
3. Set the accepted answer to `null`; score the turn as incorrect and format
   invalid, including when the reference answer is `Unknown.`.
4. Skip response extraction and accept no response facts. Previously accepted
   observation facts remain; graph recall usage and normal pruning/checkpoint
   behavior still occur on the scheduled simulated turn.
5. Include all attempted role calls in costs and advance to the next scheduled
   turn. The baseline retains the observation/task with a null answer in history.

SQLite uses an explicit rejection marker in its required message field, plus
structured `reply_failure` evidence. That marker is not a model answer, is not
extracted and is never used as a successful answer. The JSONL/CSV projection
retains the rejected outcome; analysis verifies its durable evidence and costs.
Summary reports include invalid-reply counts/rates and their paired rate changes.
Accuracy uses every scheduled turn, without filtering to valid replies.
Costs may be lower when extraction is skipped, so failure rates must accompany
cost and accuracy comparisons. Completion of the schedule is distinct from
model reliability or preservation of facts.

The opt-in applies only to bounded invalid agent reply content. HTTP/transport
errors, model changes, extraction failures, tools, cancellation, oversized
failure evidence and storage/validation errors still abort the cohort. Ordinary
application workflows keep their default exception behavior. The new policy is
part of workflow compatibility and cannot be switched during a resumed run.

No real model calls were made for this change. Offline tests replay both saved
failure cases in all four conditions; each retains an incorrect turn and commits
the following turn. Further checks run every role reply as invalid across an
entire synthetic cohort, retain unknown counters, reject false success/cost/
SQLite edits, and verify that default workflows still fail on invalid replies.
These synthetic outcomes are not research results. The original A/B protocol and
historical studies are unchanged. A fresh protocol-3 cohort awaits approval.

## Verification

From the repository root:

```bash
python3 -m pytest tests/test_phase8_integrated.py -q
python3 -m experiments.integrated_harness
```

Expected: **88 passed**, followed by a **plan-only 960-turn** declaration that
includes `reply_error_policy`. No model requests are made. See the
[integrated verification protocol](../../testing/phase8_integrated.md) for complete
regression and separately gated fresh-run commands.

`published-sha256.json` checks these selected files for accidental corruption;
it is not a signature or evidence of a completed study.
