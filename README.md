![DynaGraph Logo](./dynagraph-logo.png)

DynaGraph explores persistent graph memory for local multi-agent workflows.
It stores facts as subject-predicate-object triples and provides prefix search
over entities. The goal is to retrieve a bounded set of relevant facts as tasks
evolve.

The current implementation provides validated configuration, asynchronous Ollama
health and inference requests with latency measurements and node fallback, and
transactional SQLite graph storage, and structured extraction of facts from agent
messages. Memory governance and agent orchestration are planned for later phases.

## Architecture

Explore the [interactive architecture atlas](docs/architecture.html) or read the
[complete project architecture](docs/architecture.md). The offline atlas supports
zoom, pan, component search, phase focus, connection details, and SVG export. It
covers the finished eight-phase design and labels implemented versus planned
components. Open the HTML file in a browser; viewing it requires no Ollama server.

## Requirements

- Two computers on a private LAN, each with an Intel N100 or better and 8–16 GB RAM.
- Node 1 runs Python 3.11+, the project, local storage, and Ollama.
- Node 2 runs Ollama as a secondary inference worker.
- Python's SQLite build must be 3.38+ with FTS5 and JSON functions.
- Locally installed quantized models that fit each computer's available memory.

Inference uses local Ollama servers without cloud APIs, API keys, or paid services.
Initial dependency and model downloads need Internet access; operation can then
run with only the local network. Keep the SQLite database on Node 1's local disk;
Node 2 accesses inference over HTTP rather than opening a shared database file.

## Setup

Clone or download the repository, then run these commands from its root on Node 1:

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
python3 -m pip check
cp --no-clobber .env.example .env
```

Edit `.env` before running network checks. The template's LAN addresses are
illustrative: replace them with your nodes' private addresses. Set each model tag
to an exact locally installed tag and adjust thread budgets for its CPU. Existing
`.env` files are preserved by the copy command and are ignored by Git.

| Setting | Purpose |
| --- | --- |
| `COORDINATOR_HOST` | Node 1's private address; does not bind or start a server. |
| `NODE1_OLLAMA_URL` | Node 1's Ollama endpoint, normally `http://localhost:11434`. |
| `NODE2_OLLAMA_URL` | Node 2's private LAN endpoint, including its Ollama port. |
| `NODE1_MODEL`, `NODE2_MODEL` | Exact local model tags for each node. |
| `NODE1_OLLAMA_NUM_THREADS`, `NODE2_OLLAMA_NUM_THREADS` | Per-node inference CPU budgets. |
| `DATABASE_PATH` | Local database file; relative paths resolve from the repository root. |
| `SQLITE_BUSY_TIMEOUT_MS`, `SQLITE_POOL_SIZE` | Writer wait budget and connection-pool limit. |

Settings load the root `.env` regardless of the working directory. Process
environment values override dotenv values. Legacy aliases `NODE1_HOST_IP`,
`NODE1_OLLAMA_MODEL`, and `NODE2_OLLAMA_MODEL` are accepted; use one name per setting.
WAL mode is required. The example uses four inference threads per node for the
baseline hardware; configure a different budget when appropriate.

Follow the [Phase 1 setup and verification guide](docs/testing/phase1.md) to install
and configure Ollama on both nodes, disable cloud features, install local models,
and validate LAN connectivity. Project settings do not reconfigure Ollama services.

## Verification

Run the functional tests without network access or Ollama:

```bash
python3 -m pytest -q
```

Expected: **301 passed, 15 skipped**. The skipped tests require live LAN nodes or
an explicit storage benchmark. Run those checks separately:

```bash
python3 -m pytest tests/test_phase1_network.py --run-lan -q
python3 -m pytest tests/test_phase3_extraction.py --run-lan -q
mkdir -p .verification
python3 -m pytest tests/test_phase2_storage.py --run-storage-benchmark --basetemp .verification/phase2 -v -s
```

Expected: **122 passed** for the two-node network suite, **111 passed** for
extraction, and **83 passed** for storage. The storage benchmark requires a median
below 1 ms for 200 individually
committed inserts with FULL synchronization. Results depend on disk and system
load. See the [Phase 2 verification guide](docs/testing/phase2.md) for capability
checks, expected outputs, database inspection, and troubleshooting.

The Ollama client's `ping()` measures an HTTP `/api/version` round trip.
`list_models()` reads `/api/tags`; inference latency includes model loading and
generation. Network acceptance checks require the configured tags to be Q4_K_M.
The default inference timeout is 300 seconds per attempted node; fallback can
therefore take approximately 600 seconds if both attempts time out.

## Structured extraction example

```python
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
```

Extraction prefers Node 2 and can fall back to Node 1. It requests Ollama JSON
mode with two examples, validates the response, and permits one corrective retry.
Inputs are limited to 2,000 characters and outputs to 16 triples. Endpoints must
occur in the source message; this check does not prove a relation is true or
complete. Exhausted validation attempts raise `ExtractionError`. Extraction
returns data for the caller and does not write to storage. See the
[Phase 3 verification guide](docs/testing/phase3.md) for syntax repair rules,
live-model acceptance checks, and failure diagnostics.

## Graph storage example

```python
from src.storage.database import Database
from src.storage.graph_store import GraphStore

with Database.from_settings() as database:
    graph = GraphStore(database)
    with graph.transaction():
        project = graph.get_or_create_entity("Example project", entity_type="project")
        software = graph.get_or_create_entity("SQLite", entity_type="software")
        if not graph.list_triples(subject_id=project.id, predicate="uses", object_id=software.id):
            graph.create_triple(project.id, "uses", software.id)
    print(graph.search_prefix("sql"))
```

Opening `Database.from_settings()` creates or initializes the configured database.
Entity names and predicates use casefolding and whitespace normalization.
Properties round-trip as dictionaries; updates replace the complete property
object. Deleting an entity also deletes its incident triples and search entry.

Read connections provide immutable snapshots. Use `graph.transaction()` for
atomic modifications. For async writes, use an async database context and enqueue
a short synchronous callback, such as
`await graph.write_async(lambda tx: tx.create_entity("Example"))`. Accepted writes
drain before the async context closes. See the Phase 2 guide for cancellation and
connection-lifecycle behavior.
