# Phase 5: Subgraph retrieval and context assembly verification

Phase 5 turns a task/sub-goal into a small, current graph-memory block. It finds
lexical seeds, expands their active neighborhood inside SQLite, and emits whole
facts as Markdown or JSON within a maximum 1,000-token text budget. No model
generation, external API, new dependency, or schema migration is required.

## 1. Setup and prerequisites

From the repository root:

```bash
source .venv/bin/activate
command -v python3
python3 --version
python3 -m pip check
```

Expected: the virtual environment's `python3`, Python 3.11 or later, and
`No broken requirements found.` Keep your existing `.env`; all new settings have
defaults. `.env.example` documents these optional keys:

```dotenv
CONTEXT_TOKEN_BUDGET=1000
RETRIEVAL_SEED_LIMIT=5
RETRIEVAL_HOPS=2
RETRIEVAL_MAX_NODES=64
RETRIEVAL_MAX_TRIPLES=128
RETRIEVAL_FANOUT=16
```

The context budget is restricted to 1–1,000, seeds to 1–16, hops to 1–2, nodes
to 1–256, facts to 1–512, and fan-out to 1–64 per direction per expanded node.
The node cap must accommodate the seed limit. Environment values override `.env`.
`ContextAssembler.from_settings(graph, settings)` applies these values; the
plain constructor uses its documented defaults without reading configuration.

## 2. Dedicated correctness and regression tests

```bash
python3 -m pytest tests/test_phase5_retrieval.py -v -s
```

Expected: **107 passed, 1 skipped**. Only the explicit latency benchmark skips.
These tests use temporary databases and require neither Ollama nor LAN access.
Assertions cover:

- Literal FTS prefixes, ordinary question wording, exact-name priority, Unicode,
  description matching, bounded terms, deterministic fallback, and invalid input.
- One/two-hop discovery, incoming/outgoing facts, shortest depth, cycles,
  self-loops, disconnected components, inactive bridges, and bounded hubs.
- Current facts after supersession, preserved fact direction, and no accidental
  traversal of fact-level provenance.
- Complete JSON/Markdown, inclusive budget boundaries, Unicode/control escaping,
  whole-fact omission, pinned-item priority, and explicit omission metadata.
- One consistent read snapshot during a concurrent update, no full property
  loading, read-only assembly, and usage updates only for serialized entities.
- Local counter validation, async recall/usage, visible failures, and settings.

Run the complete offline regression suite:

```bash
python3 -m pytest -q
python3 -m pip check
git diff --check
```

Expected: **510 passed, 16 skipped**, no broken requirements, and no whitespace
errors. Skips are eight Phase 1 LAN tests, six Phase 3 LAN tests, one storage
benchmark, and one retrieval benchmark. Test databases are isolated; your
configured database is not modified.

## 3. Storage-host retrieval benchmark

```bash
mkdir -p .verification
python3 -m pytest tests/test_phase5_retrieval.py --run-retrieval-benchmark --basetemp .verification/phase5 -v -s
```

Expected: **108 passed**, no skips/failures. Four diagnostic lines resemble:

```text
RETRIEVAL BENCHMARK source=lexical format=markdown runs=100 median_ms=<number> p95_ms=<number> max_ms=<number> budget_used=<number>/1000 method=utf8_upper_bound
RETRIEVAL BENCHMARK source=lexical format=json runs=100 median_ms=<number> p95_ms=<number> max_ms=<number> budget_used=<number>/1000 method=utf8_upper_bound
RETRIEVAL BENCHMARK source=recent format=markdown runs=100 median_ms=<number> p95_ms=<number> max_ms=<number> budget_used=<number>/1000 method=utf8_upper_bound
RETRIEVAL BENCHMARK source=recent format=json runs=100 median_ms=<number> p95_ms=<number> max_ms=<number> budget_used=<number>/1000 method=utf8_upper_bound
108 passed in <seconds>s
```

The fixture has **1,000 entities and 3,000 directed facts** in cyclic, connected
neighborhoods. Each workload warms ten times, then measures 100 complete calls:
seed selection, SQL expansion, result conversion, formatting, and budget checks.
Both the median and nearest-rank p95 must be **strictly below 5 ms** for each
workload; every returned block must stay within budget. Maximum latency is also
reported, but scheduler outliers are not the acceptance statistic.

Database creation, fixture inserts, model loading/inference, usage writes, and
async scheduling are outside this retrieval benchmark. This is a warm local-host
measurement on this fixture, not a guarantee for every graph, query, CPU load,
or custom tokenizer. Run on Node 1's local storage with competing heavy work
stopped. Preserve a failing result rather than increasing the threshold.

## 4. Reproducible offline smoke check

This uses a temporary database, replaces an old value, follows two active hops,
and explicitly records use of only the emitted entities:

```bash
python3 - <<'PY'
from pathlib import Path
from tempfile import TemporaryDirectory
import json
from src.governance import GovernedWriter
from src.retrieval import ContextAssembler
from src.storage.database import Database
from src.storage.graph_store import GraphStore

with TemporaryDirectory() as directory:
    with Database(Path(directory) / "memory.db") as database:
        graph = GraphStore(database)
        project = graph.create_entity("Project Cedar", is_pinned=True)
        sqlite = graph.create_entity("SQLite")
        postgres = graph.create_entity("PostgreSQL")
        disk = graph.create_entity("Local disk")
        writer = GovernedWriter(graph, single_valued_predicates=["primary_database"])
        old = writer.write_fact(project.id, "primary_database", sqlite.id, turn=1, now=100)
        current = writer.write_fact(project.id, "primary_database", postgres.id, turn=2, now=101)
        storage = writer.write_fact(postgres.id, "stores_on", disk.id, turn=2, now=101)
        assembler = ContextAssembler(graph, seed_limit=1)
        result = assembler.assemble("Which database does Project Cedar use?", format="json")
        assert result.seed_source == "lexical"
        assert result.triple_ids == (current.triple.id, storage.triple.id)
        assert "SQLite" not in result.text and "SUPERSEDES" not in result.text
        assert result.budget_used == len(result.text.encode("utf-8")) <= 1000
        payload = json.loads(result.text)
        assert [fact["o"] for fact in payload["facts"]] == ["PostgreSQL", "Local disk"]
        print("ACTIVE FACTS:", len(payload["facts"]))
        print("OLD FACT:", graph.get_triple(old.triple.id).status)
        print("CONTEXT:", result.text)
        print("BUDGET:", result.budget_used, "/", result.budget_limit, result.budget_method)
        assembler.mark_used(result, turn=3, now=102)
        with database.connection() as connection:
            rows = connection.execute("SELECT entity_id FROM entity_memory WHERE last_access_turn=3 ORDER BY entity_id").fetchall()
        assert tuple(row[0] for row in rows) == result.entity_ids
        assert sqlite.id not in result.entity_ids
        print("USAGE: only 3 emitted entities marked at turn 3")
PY
```

Expected:

```text
ACTIVE FACTS: 2
OLD FACT: SUPERSEDED
CONTEXT: {"facts":[{"id":2,"s":"Project Cedar","p":"primary_database","o":"PostgreSQL"},{"id":3,"s":"PostgreSQL","p":"stores_on","o":"Local disk"}],"entities":[]}
BUDGET: 153 / 1000 utf8_upper_bound
USAGE: only 3 emitted entities marked at turn 3
```

## 5. Selection and traversal contract

`SeedMatcher.match(subgoal)` accepts up to 2,000 characters, removes common
English question words, and uses up to 16 distinct Unicode alphanumeric terms.
Terms become quoted literal prefix matches joined with OR. Exact canonical-name
matches rank first, then FTS5 BM25 and entity ID. This handles goals such as
“Which database does Project Cedar use?” without requiring every word to match.
Operators/punctuation cannot become executable FTS or SQL syntax. It is lexical
recall, not embeddings, semantic understanding, or automatic alias resolution.

With no lexical result, the matcher selects recent active entities ordered by
`updated_at DESC, id DESC`. This uses the existing status/time index. It is
entity-update recency, not access history; a fallback result can be irrelevant.
Empty/punctuation-only goals also use fallback. A graph with no eligible active
entities returns source `empty` and no seeds.

`Traversal.expand(seed_ids)` performs breadth-first discovery through incoming
and outgoing active facts; returned facts keep their stored direction. It
deduplicates entities and returns minimum discovered depth. Both endpoints must
be active, so inactive facts/entities cannot act as bridges. Explicit
`supersession_edges` are never queried as semantic edges. Properties and
descriptions are not loaded into returned neighborhoods.

Per-direction indexed fan-out limits are applied **before** recursive queue
insertion. Recursion is capped to `max_nodes * (hops + 1)` rows, then at most
`max_nodes` distinct entities are returned. Fact output is capped separately.
Discovery ties use fact IDs/node IDs. Fact selection prioritizes edges touching
pins, then proximity to seeds, then fact ID. This is bounded neighborhood
sampling; caps can omit reachable nodes and facts. `may_be_truncated` flags
possible cap effects, not a proof that useful information was lost.

Labels/predicates must fit the extraction schema's 256-character limit and
contain no NUL. Oversized
raw-CRUD names/predicates are ineligible; they are not silently renamed by
truncation. No whole-graph adjacency map is constructed in Python. Formatting
only handles the bounded SQL result.

## 6. Token budget and serialization contract

The **default budget is a conservative upper bound, not measured tokenizer
usage**. `count_utf8_bound(text)` counts UTF-8 bytes in the complete emitted
block, including its header, punctuation, quoting, and JSON wrapper. Labels are
ASCII JSON-escaped in both formats, including Unicode, controls, and angle
brackets. Under a byte-level BPE tokenizer, each regular text token consumes at
least one input byte; therefore byte count bounds text-token count from above.
This reasoning applies to the baseline Qwen2.5/Llama 3 model families; see
[Qwen's tokenizer explanation](https://qwen.readthedocs.io/en/v2.5/getting_started/concepts.html#byte-level-byte-pair-encoding)
and [Meta's Llama tokenizer implementation](https://github.com/meta-llama/llama3/blob/main/llama/tokenizer.py).
The byte ceiling is deliberately conservative and can underfill the 1,000-token
allowance. It is an engineering bound derived from byte-level tokenization,
not a measured claim about this project's model responses.

For another tokenizer or fuller utilization, pass a **correct local
model-specific** synchronous `token_counter(text) -> int` to the assembler.
The counter must count the entire block's text tokens, return nonnegative
integers (positive for nonempty text), and make no downloads/network calls.
For a block used with both models, bound both tokenizers, for example by taking
the maximum of their exact counts. Unsupported tokenizer families require that
adapter; word counts or `characters / 4` are not hard token guarantees.

`ContextResult.budget_method` distinguishes `utf8_upper_bound` from
`model_tokens`; `budget_used` must not be logged as measured prompt-token usage
when using the default. Actual inference counts belong to the existing Ollama
generation metadata and the future telemetry layer. Role prompts, task text,
chat templates, system instructions, and output reservations are **outside this
graph block's budget**. The full prompt needs its own model-context check during
future orchestration.

Facts are admitted whole; labels are never cut. An oversized fact is skipped
while later short facts can still fit. Connected pinned facts get priority;
pinned isolated entities precede ordinary facts. Remaining uncovered entities
can be included as standalone names, not invented relationships. Pinning does
not guarantee every pinned item can fit in bounded context.

Markdown uses a graph header, `[t<ID>]` fact lines, and `[e<ID>] entity: ...`
standalone lines. JSON has `facts` (`id`, `s`, `p`, `o`) and `entities` (`id`,
`name`). No eligible/fitting item returns `text=""`, budget zero, and empty
emitted-ID tuples. Do not JSON-decode an empty block. Every nonempty JSON block
is complete and parseable.

`entity_ids` and `triple_ids` identify only serialized memory. `seed_ids` can
include seeds that did not fit. `omitted_facts`/`omitted_entities` count omissions
from the bounded candidate neighborhood; `neighborhood_limited` separately
reports possible SQL caps. `latency_ms` covers matching through serialization.

## 7. Snapshot, usage, and async lifecycle

`ContextAssembler.assemble(subgoal, format="markdown")` performs matching and
expansion in one read-only SQLite snapshot. A concurrent update can complete
under WAL while the assembled block remains consistent with its read snapshot.
It represents that snapshot, not a promise that state cannot change afterward.

Assembly/scanning does not reinforce memory. After actually supplying a returned
block to an agent, call `assembler.mark_used(result, turn=..., now=...)` to
record accesses for emitted entities only. This uses Phase 4's atomic,
monotonic time/turn rules. If an entity was pruned/deleted before marking, the
write fails atomically; the caller must decide whether to retrieve again.

Async applications can await `assemble_async()` and `mark_used_async()`: reads
run off the event loop, and usage writes use the serialized database queue.
Own the database with its existing async context, and await outstanding work
before closing. Cancelling an async wait does not forcibly stop an already
running thread/accepted database write. There is no new service or background
task on import, and no agent scheduler is implemented in this phase.

## 8. Documentation, failure logs, and acceptance

Optional atlas geometry check (Node.js, no npm packages):

```bash
node --test docs/testing/architecture-routing.mjs
```

Expected: **7 passed**. Open `docs/architecture.html`: phases 1–5 are implemented;
phases 6–8 remain planned. Existing pan/zoom, themes, and separated routes remain.

For failures:

```bash
python3 -m pytest tests/test_phase5_retrieval.py --run-retrieval-benchmark -v -s --tb=long
git status --short
git check-ignore .env .verification/
```

Preserve full tracebacks and all benchmark lines. Check active statuses and
label limits for missing seeds/edges, emitted IDs for usage errors, and
`budget_method` before interpreting counts. An empty block at a tiny budget is
expected. A failed custom counter is surfaced rather than replaced by an unsafe
heuristic.

Acceptance requires 107 functional checks, all four benchmark workloads passing,
510 offline regression checks, matching smoke output, and no unexpected skips
or failures. `.env`, local graph files, and test artifacts remain ignored.

## 9. Commit message

```text
feat(phase5): add bounded graph retrieval and context assembly

Match literal FTS5 seeds with deterministic recent-active fallback.
Expand active one/two-hop neighborhoods using capped recursive SQL.
Serialize complete Markdown/JSON facts under a strict context budget.
Distinguish conservative byte bounds from model-specific token counts.
Record explicit context use through the existing governance write queue.
Add snapshot, budget, topology, async, and sub-5ms benchmark coverage.
Update generic configuration, README, architecture, and verification docs.
```
