![DynaGraph Logo](./dynagraph-logo.png)

# DynaGraph

An experimental local framework for studying persistent graph memory in
multi-agent workflows. Inference and storage run on your own computers, without
cloud APIs, API keys, or paid services.

## The experiment and the framework

### The problem: remembering what still matters

In this project, an agent is a program that uses a language model to perform a
task. Several agents can take different roles, such as planning work,
investigating a question, and checking an answer. As they exchange messages, they
need access to earlier findings and decisions.

A straightforward approach includes the accumulated conversation in each new
request. That history grows over time and can contain both current and outdated
information. Repeatedly sending it may consume more prompt tokens and inference
time, while leaving the model to distinguish old facts from new ones.

DynaGraph investigates whether a persistent, selectively retrieved graph can
provide useful shared memory with less repeated context while preserving factual
consistency. The framework and experiment tools are implemented. The controlled experiment
has not been run; improved efficiency or accuracy remains a research hypothesis.

### How graph memory works

A graph represents named things, called **entities**, and the relationships
between them. A fact can be written as a **triple**: subject, relationship, object.
For example:

```text
Project Cedar → primary_database → SQLite
```

The finished workflow is designed to:

1. Extract structured facts from agent messages and store them locally.
2. Find facts relevant to the next task or sub-goal and retrieve a small connected
   neighborhood rather than the entire history.
3. Supply that selected memory as part of the next agent's prompt.
4. Mark stale facts as superseded when a new value replaces a single-valued fact.
5. Soft-inactivate eligible low-value memory while protecting pinned information.

If Project Cedar changes its primary database to PostgreSQL, the configured
supersession policy makes the new value active and retain the old fact's
history. This requires an explicit single-valued relationship policy: a project
can legitimately use several databases, so different objects do not automatically
indicate a contradiction.

The underlying idea is **bounded context**: choose a limited amount of relevant
memory for each request. The retrieval layer limits the graph-memory
block to 1,000 text tokens using a conservative bound or a supplied local tokenizer. A token is a unit of text processed by the model; this
limit does not cover the entire task prompt or prove that total context stays
constant. Selective forgetting considers recency, frequency, graph
connectivity, and the number of turns since information was used.

### What the experiment compares

The implemented benchmark uses the same versioned 15–20 turn scenario, including scheduled
information changes, under two conditions:

| Condition | Context strategy |
| --- | --- |
| A: linear history | Supply prior task/answer turns, dropping oldest whole turns when the shared memory allowance is exceeded. |
| B: graph memory | Extract and persist facts, then retrieve bounded relevant graph context. |

The comparison will measure prompt-token consumption, latency, and factual
consistency after updates. Models, hardware, scenario, and generation settings
should remain comparable, and interpretation must include extraction and memory
maintenance costs. Both strategies remain subject to the models' context limits;
the scenario and any history-truncation policy must be documented.

The experiment is driven programmatically by
[experiments/benchmark_harness.py](experiments/benchmark_harness.py). It defaults
to **plan only**: it does not load deployment settings, contact models, or create
experiment files. Execution requires an explicit `--execute` flag and a new output
directory. The default plan has 20 turns, two repetitions, and alternating AB/BA
condition order. Each repetition starts with an independent transcript/graph.

Both conditions use the same role schedule, current observations, questions,
output limit, 2,048-token model context, and at-most-1,000-byte conservative memory
allowance. Graph memory adds input/response extraction, governed writes, recall,
and pruning; analysis reports these costs separately from agent-only usage.
The resulting comparison is a **bounded-window case study**, not proof of
unbounded linear growth versus constant asymptotic cost. Exact-match scoring
checks a declared one-sentence answer contract against an independent oracle.

[tests/test_phase8_benchmark.py](tests/test_phase8_benchmark.py) verifies the
pipeline using simulated HTTP only. Analysis validates complete paired evidence
and checksums before calculating descriptive comparisons. Plotting exports SVG,
PDF, and PNG figures; synthetic test figures are visibly labeled. No measured
research results or figures are claimed by the current implementation.

### Potential benefits and limitations

| Potential benefit | Limitation or tradeoff |
| --- | --- |
| Reduce repeatedly supplied history. | Extraction, retrieval, and graph maintenance add work; total latency may not improve. |
| Preserve shared facts across turns and runs. | Extraction can omit or misinterpret information; valid JSON does not establish truth or completeness. |
| Keep current values distinct from superseded ones. | Relationship policies must distinguish genuine replacements from legitimate multiple values. |
| Select a small relevant memory neighborhood. | Lexical matching and pruning can miss useful information. |
| Operate with local models and local storage. | Available RAM and CPU throughput constrain model choice, concurrency, and response time. |

Triples preserve selected facts, not complete source documents or exact wording.
Applications that need quotations, detailed evidence, or audit trails also need
the original material and appropriate provenance. The experiment will assess
this design on a controlled scenario; broader application suitability requires
additional evaluation.

### Suitable candidate workloads

The intended fit is multi-step work with relationships and facts that change:

| Workload | Memory that could be useful |
| --- | --- |
| Local IT troubleshooting | Hosts, service dependencies, attempted fixes, and current findings. |
| Investigation support | Relationships among evidence, observations, and evolving hypotheses. |
| Research over supplied notes or documents | Findings, connections, corrections, and unresolved questions. |
| Project planning | Dependencies, assignments, constraints, and revised decisions. |

These are candidate uses for the target framework, not validated application
features. Document ingestion, log collection, and task-specific tools require
application adapters. Short, independent questions offer less opportunity to
benefit from persistent graph memory.

### Current capabilities and extensibility

| Status | Capabilities |
| --- | --- |
| Implemented: phases 1–8 | Validated local configuration; async Ollama health, model inventory, inference, latency metrics, and fallback; transactional SQLite entity/triple storage with prefix search; structured fact extraction with defensive parsing and bounded retry; configured single-valued supersession, deterministic retention scoring, and pin-protected soft-pruning; bounded SQL neighborhood recall and budgeted Markdown/JSON context; sequential planner/researcher/verifier cycles with local tools and durable atomic checkpoints; measured per-turn usage, graph/RSS snapshots, a live terminal dashboard, recoverable CSV export, and an explicitly gated paired benchmark with validated analysis and figure export. |
| Pending research | Execute the controlled experiment after approval, inspect its evidence, and interpret the measured results. |

The programmatic workflow now connects extraction, storage, governance, and
retrieval. The standalone APIs remain available; calling extraction alone does
not write memory. Start a task with `StateMachine.start()` or `run()`; an
optional terminal dashboard can also start or resume tasks.

Current configuration supports selecting installed model tags, endpoints, and
CPU budgets for the two Ollama nodes. Python callers can provide their own messages
and use the graph APIs in a larger application. Supplying a different `agent_id`
labels a message; it does not create or schedule an agent.

The shared `BaseAgent` contract supports additional roles through a concise
role prompt, registered local read-only tools, and ordered registration in the
workflow controller.
A new role can reuse an existing model and computer. New tools, relationship
policies, or different permissions require additional implementation and tests;
automatic plugin discovery is not currently provided. More agent turns also
increase inference time on the target hardware. Additional worker nodes would
require configuration and routing changes beyond the current two-node baseline.

Explore the [interactive architecture atlas](docs/architecture.html) or read the
[complete architecture](docs/architecture.md) for every specified component and
its connections. The offline atlas supports pan/zoom, search, phase focus,
light/dark themes, and SVG export, with source components and local artifacts labeled.
Open the HTML file directly in a browser; viewing the atlas needs no Ollama server.

## Getting started with DynaGraph

### 1. Check the hardware and software requirements

The supported baseline uses two computers on a private local network (LAN):

| Computer | What runs there |
| --- | --- |
| Node 1: coordinator | Python 3.11+, DynaGraph, local SQLite storage, and Ollama. All Python coordination runs here. |
| Node 2: worker | Ollama with a locally installed model, reached over the LAN. Node 2 does not need the Python project or access to the database file. |

Each computer should have an Intel N100 or better and 8–16 GB RAM. Choose
quantized models that fit the available memory; quantization reduces the memory
needed to store model weights. Python's SQLite build must be version 3.38+ with
FTS5 text-search support and JSON functions.

Initial dependency and model downloads need Internet access. Once installed,
operation can use only the local network. Keep the database on Node 1's local
disk; the worker communicates through HTTP rather than opening a network-shared
SQLite file.

### 2. Install the Python dependencies

Clone or download the repository and open a terminal in its root folder on Node 1.
The commands below use Linux and Bash. Create a virtual environment to keep the
project's Python packages separate from other software:

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
python3 -m pip check
cp --no-clobber .env.example .env
```

Expected: dependencies install and `pip check` reports
`No broken requirements found.` The copy command preserves an existing `.env`.
Activate `.venv` again when opening a new terminal for project commands.

### 3. Configure the local models and network

The `.env` file holds settings for your installation and is ignored by Git.
Edit it before running network checks: replace the illustrative LAN addresses
with your nodes' actual private addresses and select exact locally installed
model tags. A tag identifies a particular model/version in Ollama.

| Setting | Purpose |
| --- | --- |
| `COORDINATOR_HOST` | Node 1's private address; this setting does not start a server or change Ollama's listening address. |
| `NODE1_OLLAMA_URL` | Node 1's Ollama endpoint, normally `http://localhost:11434`. |
| `NODE2_OLLAMA_URL` | Node 2's private LAN endpoint, including its Ollama port. |
| `NODE1_MODEL`, `NODE2_MODEL` | Exact local model tags for each node. |
| `NODE1_OLLAMA_NUM_THREADS`, `NODE2_OLLAMA_NUM_THREADS` | CPU thread budgets; the example uses four per node. |
| `DATABASE_PATH` | Local database file; relative paths resolve from the repository root. |
| `SQLITE_BUSY_TIMEOUT_MS`, `SQLITE_POOL_SIZE` | How long a database writer waits and how many pooled connections are available. |
| `SINGLE_VALUED_PREDICATES` | JSON array of relationships that can have only one current value per subject; defaults to `[]`. |
| `PRUNING_THRESHOLD`, `PRUNING_BATCH_SIZE`, `PRUNING_INTERVAL_SECONDS` | Retention cutoff and bounded maintenance scheduling. |
| `DECAY_RECENCY_HALF_LIFE_SECONDS`, `DECAY_TURN_HALF_LIFE` | How quickly unused memory loses time-based and turn-based retention. |
| `ORCHESTRATION_MAX_TURNS`, `ORCHESTRATION_ALLOW_FALLBACK`, `ORCHESTRATION_PRUNE_BETWEEN_TURNS` | Role-turn budget, local-node fallback, and bounded checkpoint maintenance. |
| `AGENT_MAX_TOKENS`, `AGENT_MAX_RETRIES`, `AGENT_MAX_TOOL_ROUNDS`, `AGENT_TOOL_TIMEOUT_SECONDS` | Limits on role responses, JSON correction, and registered local tools. |
| `CONTEXT_TOKEN_BUDGET` | Maximum graph-context budget, restricted to 1–1,000. |
| `RETRIEVAL_SEED_LIMIT`, `RETRIEVAL_HOPS`, `RETRIEVAL_MAX_NODES`, `RETRIEVAL_MAX_TRIPLES`, `RETRIEVAL_FANOUT` | Bounds on seed matching and one/two-hop SQL neighborhood discovery. |

Follow the [Phase 1 setup and verification guide](docs/testing/phase1.md) to
install/configure Ollama on both nodes, disable cloud features, install local
models, and permit Node 1 to reach the worker. Editing the project's `.env` does
not configure or restart an Ollama service. Live acceptance tests require the
configured model tags to have Q4_K_M quantization.

Validate configuration without contacting either server or creating a database:

```bash
python3 -c 'from src.config import get_settings; get_settings(); print("CONFIG VALID")'
```

Expected: `CONFIG VALID`. Settings load the root `.env` regardless of the working
directory. Process environment values override dotenv values. Legacy aliases
`NODE1_HOST_IP`, `NODE1_OLLAMA_MODEL`, and `NODE2_OLLAMA_MODEL` are accepted; use
one name per setting. SQLite WAL mode is required for concurrent local storage.

### 4. Try extracting facts from a message

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

### 5. Try persistent graph storage

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

### 6. Keep current facts and maintain memory

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
command in the [Phase 6 guide](docs/testing/phase6.md). The
[Phase 4 guide](docs/testing/phase4.md) also provides a complete,
temporary-database example, scoring formulas, async lifecycle, and limitations.
The default decay parameters are configurable engineering choices; they have
not yet been calibrated by the planned experiment.

### 7. Recall a small current neighborhood

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
The [Phase 5 guide](docs/testing/phase5.md) explains usage lifecycle, async calls,
selection limits, tokenizer assumptions, and the offline latency benchmark.

### 8. Run a local role cycle

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
resending a full transcript. The [Phase 6 guide](docs/testing/phase6.md) explains
recovery, acceptance filtering, prompt limits, and direct two-node verification.

### 9. Watch execution and export measurements

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
or evidence of experimental benefit. See the [Phase 7 guide](docs/testing/phase7.md)
for the column contract, timing boundaries, recovery, and verification.

### 10. Inspect the experiment plan without running it

```bash
python3 -m experiments.benchmark_harness
```

Expected:

```text
PLAN ONLY: 4 condition runs; 80 scored turns; no model requests or experiment files
```

The command also prints its scenario hash and plan. Do not add `--execute` during
implementation verification. The [Phase 8 protocol](docs/testing/phase8.md)
explains synthetic tests, comparison boundaries, artifact integrity, and deferred
execution. Existing application data and the private `.env` are preserved.
Matplotlib is pinned for local, headless figure export; no plotting server or
cloud account is needed.

### 11. Verify the installed components

Run the functional suite without network access or Ollama:

```bash
python3 -m pytest -q
```

Expected: **729 passed, 18 skipped**. The skips are checks that need physical LAN
nodes or explicitly requested storage/retrieval benchmarks. Run those separately:

```bash
python3 -m pytest tests/test_phase1_network.py --run-lan -q
python3 -m pytest tests/test_phase3_extraction.py --run-lan -q
python3 -m pytest tests/test_phase6_orchestration.py --run-lan -q
python3 -m pytest tests/test_phase7_telemetry.py --run-lan -q
mkdir -p .verification
python3 -m pytest tests/test_phase2_storage.py --run-storage-benchmark --basetemp .verification/phase2 -v -s
```

The Phase 5 retrieval benchmark is also local and offline:

```bash
python3 -m pytest tests/test_phase5_retrieval.py --run-retrieval-benchmark --basetemp .verification/phase5 -v -s
```

Expected: **108 passed** for retrieval; its four warm workloads require median
and p95 below **5 ms**, with every block within budget. See the protocol for
fixture size, warm-up, and measurement boundaries.

Expected: **122 passed** for network checks, **111 passed** for extraction,
**77 passed** for orchestration, **83 passed** for telemetry, and **83 passed** for storage. The storage benchmark checks a median below 1 ms for
200 individually committed inserts with FULL synchronization. Results depend on
the storage host and system load; this does not guarantee every insert is under
1 ms or establish a complete application performance result.

| Guide | What it explains |
| --- | --- |
| [Network setup and verification](docs/testing/phase1.md) | Ollama services, installed models, HTTP health checks, inference, and controlled fallback. |
| [Storage verification](docs/testing/phase2.md) | SQLite capabilities, transactions, graph operations, prefix search, and the insert benchmark. |
| [Extraction verification](docs/testing/phase3.md) | Defensive parsing, live model checks, resource limits, and validation errors. |
| [Governance verification](docs/testing/phase4.md) | Deterministic decay, atomic replacement, pinned memory, daemon lifecycle, migration, and a reproducible offline smoke check. |
| [Retrieval verification](docs/testing/phase5.md) | Active one/two-hop recall, strict block budgets, explicit usage, snapshot consistency, and the warm retrieval benchmark. |
| [Orchestration verification](docs/testing/phase6.md) | Three-role LAN cycles, local tools, atomic checkpoints, restart/cancellation, migration, and complete workflow usage. |
| [Telemetry verification](docs/testing/phase7.md) | Measured usage, checkpoint snapshots, live controls, CSV integrity/recovery, and a direct two-node telemetry check. |
| [Experiment implementation verification](docs/testing/phase8.md) | Synthetic-only A/B pipeline checks, plan-only CLI, oracle/scoring, artifact validation, paired analysis, and standalone figures. No experiment run. |
| [Architecture atlas verification](docs/testing/architecture.md) | Optional checks for the documentation's themes, controls, SVG export, and connector geometry. |

### 12. Troubleshoot and build on the APIs

| Symptom | First check |
| --- | --- |
| Connection refused or worker unreachable | Confirm Ollama is running, the endpoint is correct, and routing/firewall rules permit Node 1 to reach Node 2. |
| Configured model not found | Compare the exact `.env` tag with that server's installed model inventory. |
| Slow inference or timeouts | Check model loading, RAM pressure, CPU load, and request budgets. |
| Wrong setting used | Check process environment overrides and legacy aliases. |
| Storage configuration failure | Use a writable local database path and verify SQLite version, FTS5, and JSON support. |
| Invalid extraction | Inspect the error and original message; use the dedicated extraction protocol for detailed logs. |

The client's `ping()` measures an HTTP health request, not an ICMP network ping.
Inference timing includes model loading and generation. The default timeout is
300 seconds per attempted node; two timed-out fallback attempts can take about
600 seconds. Extraction can make a second corrective generation, so consult its
guide for the complete retry budget.

For application development, start with [configuration](src/config.py),
the [Ollama client](src/network/ollama_client.py),
the [extractor](src/extraction/extractor.py), and
the [graph store](src/storage/graph_store.py). Keep their resource lifecycles
explicit: callers own HTTP-client and database cleanup. The
[architecture narrative](docs/architecture.md) explains the implemented integration and extension points. The experiment
implementation is available; measured results await an explicit run.
