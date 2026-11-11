# Excluded integrated experiment attempt

The first model-driven integrated attempt failed during D1 turn 59. Its **178
completed turns are excluded from condition comparisons**. This is a failure
report with selected diagnostic evidence, not a completed research result.

Bundle: `269d96eb49d14808bc27330e5f9259a5`. Scenario: `integrated-memory-v1`.
The run lasted 82.76 minutes, from 2026-11-11T22:05:58.958828+00:00 to 2026-11-11T23:28:44.660126+00:00.
A1 and B1 completed 60 turns each; D1 completed 58; C1 and variants 2–4 did
not run. The plan required 960 scored turns across all four conditions and
four variants. There is no paired four-condition cohort to analyze.

## Failure and retained evidence

At step 59 the researcher on node2 was asked for the current `links` values
of `CycleAXEZP`. The installed `llama3.2:3b` model emitted repeated and
unsupported cycle relationships. Both the original request and its one
permitted correction exhausted the 128-token cap (`done_reason=length`),
leaving unfinished JSON. Strict validation raised `AgentError` before the
response was extracted or checkpointed. The latest durable workflow turn is 58;
observation ingestion for turn 59 had already occurred. Saved completed-turn
rows therefore do not include all failed-turn work or its cost.

[Original raw failure](failure.json) retains both responses, token counts,
latencies and the exact task. Its `repetition` field denotes the integrated
workload variant. The [original manifest](manifest.json) preserves the plan,
models, source hashes, rendered tasks/oracle, completion state and original
artifact checksums. The complete failed bundle remains under ignored
`data/experiments/integrated-20261111-001`; these selected public files do not
include all artifacts referenced by its manifest. Analysis rejects the FAILED
status rather than treating this folder as a valid cohort. Private configuration,
application data and earlier result bundles were unchanged.

The graph copied for diagnosis already contained unsupported `linked to`,
`links` and `period` assertions originating from earlier role responses.
Source-grounded extraction verifies that a fact appears in an agent's text;
it does not independently establish that the agent's assertion is true. No
oracle filtering or cleanup was applied to that graph.

## Prompt revision and unscored checks

Scenario `integrated-memory-v2` narrows the common task instructions to explicit
direct facts about the requested subject/predicate, without repetitions,
explanations, reverse links or transitive inferences. Seeded observations,
questions, oracle values, recall schedules, policies, models, scoring and the
128-token cap remain unchanged. The rendered tasks and fingerprints change;
longer task framing can reduce the actual memory allowance under the existing
shared input/output budget. All four conditions must use the revised framing.
The container remains protocol 2; its protocol identifier is unchanged.

[Diagnostic records](diagnostic.json) describe three unscored generation checks:

| Input | Node | Generated tokens | JSON completed | Strict answer contract |
| --- | --- | ---: | --- | --- |
| Failed cycle recall, reconstructed on disposable D1 graph copy | node2 | 63 | Yes | Invalid |
| Multi-valued probe, recorded B1 context | node1 | 48 | Yes | Invalid |
| Unknown probe, recorded B1 context | node1 | 31 | Yes | Invalid |

Each finished in one attempt below the unchanged cap. **None was a correct
strict-contract answer.** The diagnostic program returned a nonzero exit because
its additional format-validity criterion failed; no generation call raised
`AgentError`. These observations establish neither experiment completion nor
improved accuracy, and do not guarantee that later requests will avoid
truncation. The disposable graph reconstruction is not the original failed
request replay, and no diagnostic answer is a scored experimental observation.
The original bundle's file hashes were verified unchanged afterward.

A dedicated offline regression replays both original truncated responses at
D1 turn 59, verifies the FAILED status, 178 retained rows, 58 durable D1 turns,
all artifact checksums and analysis rejection. Additional frozen schedule
hashes verify that the revised task instructions do not change the four
variants' observations or oracle metadata. A fresh full cohort is required;
there is no resume, pooling with this attempt or automatic restart.

## Verification

From the repository root, without contacting either model:

```bash
python3 -m pytest tests/test_phase8_integrated.py -q
```

Expected: **64 passed**. The tests use synthetic HTTP, including the saved
failure response fixture; their token counts and accuracy are not measured
research findings. See the [integrated verification protocol](../../testing/phase8_integrated.md)
for complete checks and separately gated fresh-run commands.

`published-sha256.json` checks the selected report/evidence files for accidental
corruption; it is not a signature or a substitute for a valid complete cohort.
