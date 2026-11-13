# Using the Python APIs and dashboard

Run examples from the repository root with the virtual environment active.
Complete the [README setup](../README.md#getting-started-with-dynagraph) first.
Examples that call Ollama require your configured local models; storage and
retrieval examples operate on the configured database. Use a separate database
for practice, and back up existing data before experimenting.

[Extraction](#1-try-extracting-facts-from-a-message) | [Storage](#2-try-persistent-graph-storage) | [Governance](#3-keep-current-facts-and-maintain-memory) | [Retrieval](#4-recall-a-small-current-neighborhood) | [Role cycles](#5-run-a-local-role-cycle) | [Dashboard and CSV](#6-watch-execution-and-export-measurements)

## 1. Try extracting facts from a message

With both node addresses configured and the worker's Ollama service available,
run this from the repository root with the virtual environment active:

```bash
python3 - <<'PY'
import asyncio
from src.config import get_settings
from src.extraction import Extractor
from src.models import AgentMessage
from src.network.ollama_client import OllamaClient

async def main():
    async with OllamaClient(get_settings()) as client:
        result = await Extractor(client).extract(
            AgentMessage(agent_id="researcher", content="Project Cedar uses SQLite.")
        )
        print(result.graph.model_dump_json())

asyncio.run(main())
PY
```

The output is a JSON object with a `triples` array containing extracted facts;
the wording and optional confidence value can vary with the model. This example
returns data and does not write to the database.

Extraction prefers Node 2 and can fall back to Node 1. It requests JSON output
with two examples, validates the response, and permits one corrective retry.
Current limits are 2,000 input characters and 16 output triples. Subject/object
labels must occur in the source message, but that check does not prove the
relationship is correct. Exhausted validation attempts raise `ExtractionError`.

## 2. Try persistent graph storage

This example creates or opens the configured local database, stores a relationship,
and searches for entities whose text begins with `sql`. It needs valid configuration
but no running Ollama server:

```bash
python3 - <<'PY'
from src.storage.database import Database
from src.storage.graph_store import GraphStore

with Database.from_settings() as database:
    graph = GraphStore(database)
    with graph.transaction():
        project = graph.get_or_create_entity("Example project", entity_type="project")
        software = graph.get_or_create_entity("SQLite", entity_type="software")
        if not graph.list_triples(subject_id=project.id, predicate="uses", object_id=software.id):
            graph.create_triple(project.id, "uses", software.id)
    print([entity.name for entity in graph.search_prefix("sql")])
PY
```

On a new database, expect `['SQLite']`. Existing records and their statuses can
affect matches. A transaction groups changes so they either all succeed or roll back.
The example reuses existing entities and avoids inserting the same fact twice.

Names and predicates use whitespace normalization and casefolding for matching.
JSON properties round-trip as dictionaries, and property updates replace the
complete object. Physical entity deletion also removes its incident triples and
search entry. The Phase 4 pruner instead marks eligible memory `PRUNED` and
preserves its rows.

Read connections provide immutable snapshots. Use write transactions for
modifications. Async callers can use an async database context and enqueue a short
synchronous callback, such as
`await graph.write_async(lambda tx: tx.create_entity("Example"))`; accepted writes
drain before that context closes. See the storage guide for lifecycle details.

## 3. Keep current facts and maintain memory

Use `GovernedWriter` for accepted facts that need replacement policy. The writer
requires existing entities and an explicit list of single-valued relationships:

```python
from src.governance import GovernedWriter, Pruner

# Within an open Database/GraphStore context, using existing entity IDs:
writer = GovernedWriter(graph, single_valued_predicates=["primary_database"])
writer.write_fact(project.id, "primary_database", sqlite.id, turn=1)
update = writer.write_fact(project.id, "primary_database", postgres.id, turn=2)
```

The earlier fact immediately becomes `SUPERSEDED`. A separate, foreign-key-checked
provenance table records the new fact's explicit `SUPERSEDES` edge to the old
fact. The latest accepted assertion wins; this policy does not establish truth.
Relationships omitted from the policy can retain several current values.
Raw `GraphStore` writes remain low-level operations and bypass this policy.

`Pruner(graph).run_once(current_turn=...)` soft-prunes old leaf memory below its
retention threshold. Scores combine recency, usage frequency, active connectivity,
and elapsed turns. Pins protect entities and their active facts. Call
`record_access()` for memory actually used; searches and scans do not count as
use. The optional `PruningDaemon` requires explicit startup and a caller-supplied
turn number. No maintenance starts on import.

Schema version 3 preserves usage/provenance and adds durable workflow
checkpoints. Before upgrading existing data, follow the consistent-backup
command in the [Phase 6 guide](testing/phase6.md). The
[Phase 4 guide](testing/phase4.md) also provides a complete,
temporary-database example, scoring formulas, async lifecycle, and limitations.
The default decay parameters are configurable engineering choices; the completed studies do not establish general-purpose optimal settings.

## 4. Recall a small current neighborhood

A sub-goal can now retrieve active facts as Markdown or JSON. This example reads
the configured graph without contacting either model:

```bash
python3 - <<'PY'
from src.config import get_settings
from src.retrieval import ContextAssembler
from src.storage.database import Database
from src.storage.graph_store import GraphStore

settings = get_settings()
with Database.from_settings(settings) as database:
    assembler = ContextAssembler.from_settings(GraphStore(database), settings)
    memory = assembler.assemble(
        "Which database does the example project use?", format="json",
    )
    print(memory.text or "(no eligible memory fits)")
    print(f"budget={memory.budget_used}/{memory.budget_limit} method={memory.budget_method}")
PY
```

The result depends on stored data. Lexical prefix matching finds seeds, with
recent active entities as fallback. SQL explores incoming/outgoing active facts
up to two hops, using explicit node/fact/fan-out caps. Superseded/pruned facts and
inactive endpoints cannot supply old values or bridge to other memory.

The default budget counts an **upper bound from serialized bytes**, suitable
for the baseline byte-level BPE tokenizers; it is not measured model-token usage.
A correct local model-specific counter can be supplied for another tokenizer or
fuller budget utilization. Entire facts are omitted when they cannot fit;
labels are never cut, and nonempty JSON is always complete. The ceiling covers
the graph block, while role/task prompts and chat formatting need separate space.

Assembly is read-only. After actually supplying the block to an agent, call
`assembler.mark_used(memory, turn=...)` to reinforce only the emitted entities.
The [Phase 5 guide](testing/phase5.md) explains usage lifecycle, async calls,
selection limits, tokenizer assumptions, and the offline latency benchmark.

## 5. Run a local role cycle

The controller runs one role per turn: planner on Node 1, researcher on Node 2,
then verifier on Node 1. Each role recalls current memory, generates a response,
and sends it through extraction on Node 2. Accepted facts, usage, bounded pruning,
and the turn checkpoint commit together. Model requests do not hold database
write transactions. The default three turns make one complete role cycle.

After backing up existing data, run this from the repository root:

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
            print(done.status, done.next_step, done.stop_reason)
            print(done.last_response)

asyncio.run(main())
PY
```

Expect a run ID, `COMPLETED 3 turn_budget`, and the final model response. Keep
the run ID for inspection/resume. An interrupted run resumes from its next
uncommitted step; a completed run makes no further model requests. Completing
its turn budget does not establish that a model's answer is correct.

Custom roles subclass `BaseAgent` and are passed through the controller's
`agents` argument. Tools require explicit registration, Pydantic argument
validation, and local read-only callbacks; model-generated code is not executed.
The controller keeps prompts bounded and stores history locally instead of
resending a full transcript. The [Phase 6 guide](testing/phase6.md) explains
recovery, acceptance filtering, prompt limits, and direct two-node verification.

## 6. Watch execution and export measurements

Preview the terminal dashboard with synthetic data, without Ollama or database
access:

```bash
python3 -m src.telemetry.dashboard --demo --plain --csv .verification/phase7/demo.csv
```

Expect `SIMULATED DEMO OK: 3 turns; no model requests or database writes` and a
CSV path. Omit `--plain` for live display; a redirected output stream prints the
final snapshot. Demo numbers are illustrative, not experimental measurements.

Run a real task using your configured models and local graph:

```bash
python3 -m src.telemetry.dashboard --task "Repeat this fact exactly: Project Cedar uses SQLite." --subgoal "Project Cedar" --turns 3
```

The dashboard refreshes twice per second, showing the current role, stage,
committed prompt usage, inference latency, active graph size, and coordinator
Python RAM. Press **p** to pause/resume the display, or **q** / **Ctrl+C** to stop
execution. Keep the printed `RUN_ID`. Committed turns are retained; resume with
`python3 -m src.telemetry.dashboard --resume RUN_ID`. A crashed process can leave
a `RUNNING` row; use `--recover-interrupted` only after its former process has
stopped, as explained in the Phase 6 recovery protocol.

Each new turn saves telemetry with its graph checkpoint. The dashboard appends
CSV rows under ignored `data/telemetry/`, or a path supplied with `--csv`.
Programmatic workflows also save metrics automatically; use this command to
export their checkpoints without contacting either model:

```bash
python3 -m src.telemetry.dashboard --resume RUN_ID --export-only
```

A file belongs to one run and one writer. Each new task gets a separate CSV by
default; an incompatible explicit path is rejected before inference starts.
Repeated export safely skips identical rows; damaged/incompatible CSV files
require export to a new path. Export failure
does not undo committed graph state. Older checkpoints without telemetry are
reported as skipped; their historical measurements cannot be reconstructed.

Prompt counts come from Ollama's `prompt_eval_count`, including role/extraction
corrections. CSV keeps those stages separate and also reports their total.
Missing counters produce blank cells, not zero or a retrieval-byte estimate.
Failed fallback requests contribute latency, but their unknown token work cannot
be counted. Failed/uncommitted turns are excluded from this committed-turn data.
RAM is a sampled **Python process RSS**, excluding both Ollama processes and
remote-node RAM. This is useful framework telemetry, not total cluster profiling
or evidence of experimental benefit. See the [Phase 7 guide](testing/phase7.md)
for the column contract, timing boundaries, recovery, and verification.

