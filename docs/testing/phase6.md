# Phase 6: Multi-agent orchestration verification

This protocol records Phase 6 acceptance. For current aggregate counts and
telemetry verification, see [Phase 7](phase7.md).

Phase 6 connects bounded recall, local role execution, extraction, governed
persistence, and memory maintenance into a durable cyclic workflow. Python and
SQLite stay on Node 1. Planner/verifier inference defaults to Node 1; researcher
and extraction inference default to Node 2. Turns execute sequentially.

## 1. Setup and prerequisites

From the repository root:

```bash
source .venv/bin/activate
command -v python3
python3 --version
python3 -m pip check
```

Expected: the virtual environment's `python3`, Python 3.11 or later, and
`No broken requirements found.` No dependency was added. Keep your private
`.env`; defaults apply to these optional `.env.example` settings:

```dotenv
ORCHESTRATION_MAX_TURNS=3
ORCHESTRATION_ALLOW_FALLBACK=true
ORCHESTRATION_PRUNE_BETWEEN_TURNS=true
AGENT_MAX_TOKENS=256
AGENT_MAX_RETRIES=1
AGENT_MAX_TOOL_ROUNDS=1
AGENT_TOOL_TIMEOUT_SECONDS=5
```

One turn runs **one role**, so three turns cover the default planner → researcher
→ verifier cycle. Turn budgets permit 1–1,000; reply tokens 32–512; JSON retries
0–1; tool rounds 0–2; tool timeout more than zero and at most 60 seconds.
Both models must already be installed locally. Live acceptance requires the
configured Q4_K_M tags and LAN connectivity established in earlier phases.

Opening a project database upgrades its schema to version 3. Read the backup
procedure in section 8 before opening existing project data with the new code.
Automated tests use isolated databases and do not upgrade your project graph.

## 2. Offline correctness tests

```bash
python3 -m pytest tests/test_phase6_orchestration.py -v -s
```

Expected: **76 passed, 1 skipped**. Only the real two-node cycle skips. HTTP
responses are simulated; no Ollama server or network access is required.
Coverage includes default/custom roles, bounded JSON correction, local tool
validation and limits, whole-prompt bounds, active memory sharing, supersession,
empty extraction, acceptance filtering, atomic rollback, durable resume,
monotonic governance turns, cancellation, competing owners, and v1/v2 upgrades.

For a short deterministic smoke check:

```bash
python3 -m pytest tests/test_phase6_orchestration.py -k 'default_cycle or reopen_resumes or checkpoint_sql_failure' -v -s
```

Expected: **3 passed, 74 deselected**, with these diagnostics:

```text
CYCLE OK: planner=node1 researcher=node2 verifier=node1 checkpoints=3
RESUME OK: committed step retained; next role resumed
ROLLBACK OK: graph, usage and turn checkpoint commit together
```

## 3. Direct two-node live acceptance

```bash
python3 -m pytest tests/test_phase6_orchestration.py --run-lan -m lan -v -s
```

Expected: **1 passed, 76 deselected**. The test uses a temporary database, checks
both configured Q4_K_M model inventories, and runs three real role turns with
real extraction. **Fallback is disabled** for every generation, so one available
node cannot silently stand in for the other. The controlled task repeats
`Project Cedar uses SQLite.` and asserts the exact active fact at the end.

Diagnostics resemble:

```text
CYCLE step=1 agent=planner node=node1 extraction_node=node2 facts=1 context_budget=0/1000
CYCLE step=2 agent=researcher node=node2 extraction_node=node2 facts=1 context_budget=<number>/1000
CYCLE step=3 agent=verifier node=node1 extraction_node=node2 facts=1 context_budget=<number>/1000
TWO-NODE CYCLE OK: 3 committed turns; direct node routes; active fact preserved
1 passed, 76 deselected in <seconds>s
```

The first context is empty because the test graph starts empty. Later roles must
recall the persisted relationship. Reported context sizes are conservative byte
bounds, not measured model-token usage. Development acceptance took about two
minutes; hardware, model loading, CPU load, and retries affect elapsed time.

Full Phase 6 acceptance can be run in one command:

```bash
python3 -m pytest tests/test_phase6_orchestration.py --run-lan -q
```

Expected: **77 passed**, no skips or failures. Partial node selection cannot
verify this test:

```bash
python3 -m pytest tests/test_phase6_orchestration.py --run-lan --lan-node node2 -m lan -q
```

Expected: **1 skipped, 76 deselected**, with a reason requiring both nodes.
This is not sufficient for acceptance. No model is downloaded and no cloud API
is called. After installation, the same checks can run with WAN disconnected
while the private LAN remains connected.

## 4. Regression and existing performance checks

```bash
python3 -m pytest -q
python3 -m pip check
git diff --check
```

Expected: **586 passed, 17 skipped**, no broken requirements, and no whitespace
errors. The skips are 15 LAN checks across phases 1/3/6, plus the storage and
retrieval benchmarks. Earlier phase guides retain their historical aggregate
counts; this guide and the README give current whole-suite totals.

Because schema initialization changed, recheck both existing latency targets on
the storage host:

```bash
mkdir -p .verification
python3 -m pytest --run-storage-benchmark --run-retrieval-benchmark --basetemp .verification/phase6-regression -v -s
```

Expected: **588 passed, 15 skipped**. Durable insert median remains below **1 ms**;
each of the four warm retrieval workloads requires median and p95 below **5 ms**.
LLM generation and fixture creation are outside these earlier latency measures.

Optional atlas check, with Node.js and no npm packages:

```bash
node --test docs/testing/architecture-routing.mjs
```

Expected: **7 passed**. Open `docs/architecture.html`: phases 1–6 are implemented,
with phases 7–8 still planned. Zoom, themes, and separated connector tracks remain.

## 5. Run the framework programmatically

This example uses the configured project database and real local models. Back
up existing data first as described below. It prints the run ID before inference
so a failed/interrupted run can be inspected and resumed:

```bash
python3 - <<'PY'
import asyncio
from src.config import get_settings
from src.network.ollama_client import OllamaClient
from src.orchestration import StateMachine
from src.storage.database import Database
from src.storage.graph_store import GraphStore

async def main():
    settings = get_settings()
    async with OllamaClient(settings) as client:
        async with Database.from_settings(settings) as database:
            machine = StateMachine(client, GraphStore(database))
            run = await machine.start(
                "Project Cedar uses SQLite. Confirm the supplied relationship.",
                initial_subgoal="Project Cedar", max_turns=3,
            )
            print("RUN_ID:", run.id, flush=True)
            done = await machine.resume(run.id)
            print("STATUS:", done.status, "TURNS:", done.next_step, "REASON:", done.stop_reason)
            print("LAST RESPONSE:", done.last_response)
            for turn in await machine.history(run.id):
                print(turn.step, turn.agent_id, turn.node, "governance_turn=" + str(turn.governance_turn))

asyncio.run(main())
PY
```

Expected: a generated `RUN_ID`, then `STATUS: COMPLETED TURNS: 3 REASON:
turn_budget`, the model's last response, and three role rows. Content and fact
counts can vary with models and preexisting memory; this example is not the
controlled acceptance test. A completed turn budget does not prove task truth.

For manual stepping, call `await machine.step(run.id)`. It returns the role
result, recalled context, extraction result, and durable turn checkpoint. Read
`await machine.get_checkpoint(run.id)` and bounded
`await machine.history(run.id, after_step=0, limit=100)` for persisted state.
Reconstruct the same client/graph/controller configuration after restart, then
call `await machine.resume(saved_run_id)` to continue at the next uncommitted
step. A completed run returns immediately without another model call.

## 6. Workflow, consistency, and failure contract

The default order repeats planner → researcher → verifier until the turn budget
is exhausted. `AgentReply.next_subgoal` can update the next retrieval goal. With
`stop_on_verified=True`, a verifier's `complete=true` can stop a run early;
planner/researcher completion flags cannot. This is the verifier model's judgment,
not an independent factual guarantee. Fixed-budget execution is the default.

Stages are IDLE → RECALL → GENERATE → EXTRACT → COMMIT → IDLE, with FAILED and
CANCELLED states for failures. Durable run statuses are READY, RUNNING,
COMPLETED, FAILED, and CANCELLED. Schema version 3 stores `workflow_runs` and
ordered `workflow_turns`; the controller does not accumulate a full transcript
in each prompt or keep all run results in RAM.

Every successful commit contains context usage, accepted entity/fact writes,
supersession provenance, one optional bounded pruning batch, and the turn/run
checkpoint in **one transaction**. Failure rolls them back together. Source
agent messages and bounded result metadata remain in local checkpoint rows.
Usage is recorded for committed turns; failed model/extraction attempts may
consume compute without updating memory usage. HTTP and local tool execution
never hold a SQLite write transaction.

`AgentMessage.turn` numbers steps within its run. The separate `governance_turn`
is assigned at commit above the highest persisted usage/workflow turn, preserving
monotonic decay metadata across restarts and multiple runs. Clock timestamps
must also satisfy Phase 4's monotonic access rules. By default, the controller
runs one bounded pruning batch per commit; disable
`ORCHESTRATION_PRUNE_BETWEEN_TURNS` if maintenance is owned externally.
It does not automatically launch another pruning daemon.

Facts pass extraction syntax/schema/grounding validation, then an optional
synchronous `accept_fact(fact, message) -> bool` hook. Default policy accepts all
validated extracted facts; extraction is not a truth oracle. The hook receives
copies, cannot transform the persisted fact by mutating them, and must be local,
deterministic, and free of side effects. Canonical identity and exact FTS checks
reuse names; arbitrary aliases are not inferred. Pin essential constraints
explicitly through graph APIs.

Ownership tokens and compare-and-update guards prevent competing controllers
from committing/failing another owner's turn. Failed/cancelled runs resume at
their saved boundary. A process crash can leave RUNNING state; only after the
previous owner has stopped, use
`await machine.resume(run_id, recover_interrupted=True)`. Never use that flag
to take over a still-running process. The resume fingerprint checks role/order,
node/model routes, tool descriptors, and memory policy. Callback implementations
are trusted application code; the fingerprint does not version their source.

Cancellation during inference leaves no committed step. Once a commit is
accepted, cancellation waits for that transaction to finish before propagating.
Inspect the checkpoint afterward: it may already have advanced or completed.
Resuming then avoids duplicating the committed memory update. Model inference
or a synchronous read-only tool may still finish after its caller stops waiting.
The caller owns database/client async contexts and must close them afterward.

## 7. Agents, prompt budgets, and tool extension

Subclass `BaseAgent`, supply a concise `role_prompt` property, and construct the
role with an `agent_id`, configured `node`, and caller-owned client. Pass an
ordered collection via `StateMachine(..., agents=[...])`; IDs must be unique,
and 1–16 roles are permitted. All roles share that client and execute sequentially.
No plugin discovery or additional worker-node routing is implied.

Agent replies use application-level JSON with `content`, optional
`next_subgoal`, `complete`, and `tool_calls`. JSON mode constrains syntax; strict
schemas validate semantics. Malformed/duplicate-key/truncated replies get at
most one corrective retry, on the responding node. Tool failures do not trigger
automatic tool reexecution. Transport fallback follows the existing client
policy unless disabled. Checkpoint metadata records the actual responding node.

Tools are explicit `Tool(name, description, arguments_schema, handler)`
registrations. The schema is a Pydantic model; validated fields are passed as
keyword arguments. Register only trusted **local, read-only** callbacks. Up to
eight tools, two calls per request, and the configured round limit are allowed.
Arguments are capped at 512 serialized bytes; each result at 256. The entire
requested batch is name/schema-validated before any callback executes. Unknown
names, extra arguments, exceptions, timeouts, oversized results, and excess
rounds are visible errors. No shell, dynamic imports, model-generated code, web
search, or cloud adapter is supplied. Read-only callbacks can repeat after an
interruption; side-effecting tools are outside this phase's recovery contract.

Before generation, the controller reduces the graph-block budget to the role's
available capacity. Agent execution bounds combined system/task/memory/tool
text, a 128-token framing reserve, and its output allowance within the client's
2,048-token context. The default guard uses conservative byte bounds for the
baseline Qwen2.5/Llama 3 byte-level tokenizers. Tasks are capped at 500 characters,
sub-goals at 256, and final messages at 2,000; byte/token bounds can reject shorter
non-ASCII inputs too. For tasks over 256 characters, supply a separate short
initial sub-goal. Invalid/oversized input fails rather than being silently cut.
Other tokenizer families need a model-specific full-prompt adapter before using
this guard. Tool schemas/results and correction text also count; exceeding the
remaining prompt allowance fails before another generation.

## 8. Consistent backup and schema upgrade

Stop other application processes. This optional command backs up an existing
configured graph, including WAL contents, before opening it with version 3:

```bash
python3 - <<'PY'
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from src.config import get_settings
from src.storage.database import Database

settings = get_settings()
if settings.database_path.exists():
    folder = Path(".verification")
    folder.mkdir(exist_ok=True)
    name = "pre-phase6-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".db"
    backup = folder / name
    with sqlite3.connect(settings.database_path.as_uri() + "?mode=ro", uri=True) as source:
        with sqlite3.connect(backup) as target:
            source.backup(target)
    print("BACKUP:", backup)
with Database.from_settings(settings) as database:
    with database.connection() as connection:
        print("SCHEMA VERSION:", connection.execute("PRAGMA user_version").fetchone()[0])
        print("INTEGRITY:", connection.execute("PRAGMA integrity_check").fetchone()[0])
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
PY
```

Expected: a `BACKUP: .verification/pre-phase6-<timestamp>.db` line for existing
data, followed by `SCHEMA VERSION: 3` and `INTEGRITY: ok`. No backup line appears
for a new database. This command performs no agent generation or pruning.
Versions 1/2 upgrade additively and transactionally; existing IDs, graph facts,
FTS, pins, usage, and supersession events are preserved. Failed upgrades retain
the old version. Older application versions reject version 3 databases.

## 9. Failure logs, acceptance, and commit message

```bash
python3 -m pytest tests/test_phase6_orchestration.py --run-lan -v -s --tb=long
git status --short
git check-ignore .env .verification/
```

Provide full tracebacks and the cycle's printed diagnostics. For network/model
failures, repeat the Phase 1 checks. For reply/extraction failures, preserve
validation errors and returned content from the traceback. `AgentError.attempts`
retains raw generations; extraction errors retain attempts and validation errors.
For prompt limits,
shorten task/sub-goal/tool descriptors rather than disabling bounds. For an
interrupted run, retain its run ID and inspect its checkpoint before recovery.

Acceptance requires **77 Phase 6 checks with no skips**, **586 offline regression
checks**, existing latency targets passing on the storage host, and no unexpected
failures. Runtime model performance remains hardware-dependent. Phases 7/8's
dashboard, profiling/CSV export, and A/B benchmark are not implemented here.

```text
feat(phase6): add durable local multi-agent orchestration

Add bounded planner, researcher, and verifier roles with local tool execution.
Connect recall, extraction, governed writes, usage, and pruning in cyclic turns.
Atomically checkpoint graph updates with monotonic governance turn numbers.
Support restart, cancellation, config validation, and guarded ownership recovery.
Upgrade existing graph schemas additively and verify offline failure paths.
Add direct two-node cycle acceptance and generic usage/verification documentation.
```
