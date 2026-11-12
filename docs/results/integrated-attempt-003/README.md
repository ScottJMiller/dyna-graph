# Third excluded integrated experiment attempt

The first protocol-3 run stopped during **B2 turn 12** after **251 saved turns**.
All four runs in variant 1 completed, followed by 11 B2 turns. The planned cohort
required 960 turns; these partial rows remain excluded from condition comparisons.
The attempt lasted 125.33 minutes. Bundle: `af496fddf0324b488647784356f0efeb`.
Scenario: `integrated-memory-v2`. The original run recorded 1 rejected
agent replies under its declared policy before encountering a different failure.

## Server-side failure and evidence limits

The role returned a reply accepted by the agent JSON validator. During subsequent
response extraction, node2 returned HTTP 500 with:

```text
prediction aborted, token repeat limit reached
```

The [sanitized server event](server-event.json) records the error body and
58161.2 ms of failed-request latency from the original execution
log. No generated text or prompt/generated-token counters were returned. They
are unknown, not zero. The [original failure record](failure.json) contains no
source message or generation attempts for this HTTP exception. The role answer
and exact extraction prompt for the failed turn were not retained; the saved
prior checkpoint cannot reconstruct them. No claim is made about the answer's
correctness. The unreturned extraction output is also unavailable.

The [manifest](manifest.json) preserves the original plan, workload/oracle,
models, policies, source hashes, completion state and original artifact hashes.
The full failed bundle and private execution log remain under ignored
`data/experiments/`. This folder publishes selected evidence, not every artifact
referenced by the manifest. Saved-row costs omit the failed turn's work and are
not complete cohort costs. Analysis rejects the FAILED bundle. Earlier attempts,
production data and private configuration remain unchanged; there is no resume
or pooling of partial rows.

## Protocol 4: inference outcomes across all stages

The new container version retains the same scenarios, prompts, models, memory
policies, output caps and one-correction budgets. It adds one common explicit
policy across the applicable stages and conditions:

- Recognize only the exact structured HTTP 500 repetition-abort message above.
  Record a failed prediction attempt with its requested node/model, HTTP status,
  canonical error and measured latency. Text, token counters and server duration
  remain null because no generation result was returned. The model label on
  this record identifies the requested installed model, not a model identity
  confirmed by a returned generation payload. Use the existing same-node
  correction budget; add no extra retry or fallback.
- Retain every observation-extraction, role and response-extraction attempt,
  including an abort followed by a successful correction. Unknown counters
  propagate through totals even when a later correction succeeds.
- Exhausted invalid/aborted role replies retain a null accepted answer and an
  incorrect score, with no response extraction, as in protocol 3.
- Exhausted extraction syntax/schema/grounding/truncation validation or known
  repetition abort accepts **no facts** from that extraction. Preserve source,
  validation issues, raw returned attempts and failed-call metadata. Independent
  role-answer scoring remains unchanged: a correct answer can coexist with a
  failed response extraction. Observation-extraction failure can leave required
  memory absent. No oracle filtering, repair or restoration is performed.
- Graph usage, normal pruning and scheduled checkpoint advancement still occur.
  SQLite and JSONL retain explicit extraction outcomes and attempt evidence;
  the baseline has no extraction calls. Valid empty extraction remains distinct
  from failed extraction. Summaries report failures at both extraction stages,
  prediction-abort counts and token-counter coverage alongside accuracy/costs.

Complete pipeline token totals and paired token differences can be **unknown**
when a server aborts without counters. Analysis retains those turns and reports
unknown totals rather than zero-filling or silently dropping failed calls. Curves
show gaps for unknown means. These limitations constrain efficiency claims even
when all scheduled turns complete.

Client prediction-abort recording and extractor error recording are opt-in;
ordinary application defaults still raise. The workflow compatibility hash
records both choices. Unrelated HTTP errors, connection failures, model changes,
cancellation, oversized evidence, tools, storage errors and invalid evidence
still abort. Warmup prediction aborts also stop setup rather than produce a
measured turn. New fatal HTTP diagnostics retain available source and status
metadata without endpoint addresses. Both earlier integrated protocols and all
historical evidence remain preserved; the original A/B entry points keep their
default behavior.

No real model requests were made for this fix. Offline tests inject an equivalent
known server response at all three inference stages, with recovered and exhausted
correction budgets; this is not a replay of the lost original extraction source.
They preserve null counters and latency, score valid role answers independently,
reject fabricated zero counts/text/source/facts/SQLite edits, and prove unrelated
HTTP errors still stop each pipeline stage. Existing tests cover entire synthetic
cohorts of invalid replies and invalid extractions. These fixtures are not
research results. A new full protocol-4 cohort awaits approval.

## Verification

```bash
python3 -m pytest tests/test_phase8_integrated.py -q
python3 -m experiments.integrated_harness
```

Expected: **116 passed**, then a plan-only **960-turn**, version **4** declaration
with `reply_error_policy` and `inference_error_policy`. See the
[full verification guide](../../testing/phase8_integrated.md) for regression and
separately gated fresh-run commands. `published-sha256.json` checks these selected
files for accidental corruption; it is not a signature or a completed study.
