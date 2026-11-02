# Phase 4: Memory governance verification

Whole-suite totals in this phase protocol reflect its completion checkpoint.
See the [current workflow protocol](phase6.md) for current aggregate counts
and schema migration guidance. Dedicated phase checks remain applicable.

Phase 4 implements deterministic four-factor retention scoring, atomic
single-valued fact replacement, and bounded soft-pruning. Every check below is
local and offline. No Ollama process, model download, Node 2 connection, or new
dependency is required. Phase 5 retrieval and Phase 6 agent scheduling remain
future work.

## 1. Setup and prerequisites

Run from the repository root:

```bash
source .venv/bin/activate
command -v python3
python3 --version
python3 -m pip check
```

Expected: the virtual environment's `python3`, Python 3.12 or later, then
`No broken requirements found.` Existing pinned requirements are unchanged.

Keep your working `.env`. New settings have defaults; add only the keys you want
to change, using `.env.example` as a reference:

```dotenv
SINGLE_VALUED_PREDICATES='["primary_database", "status"]'
PRUNING_THRESHOLD=0.2
PRUNING_BATCH_SIZE=100
PRUNING_INTERVAL_SECONDS=60
DECAY_RECENCY_HALF_LIFE_SECONDS=86400
DECAY_TURN_HALF_LIFE=10
```

The predicate array above is an **example domain policy**. The default is `[]`:
no predicate is assumed single-valued. A subject may legitimately use multiple
tools, so `uses` should normally remain multi-valued. Predicates normalize case
and whitespace; the array is JSON. Environment variables override `.env`.

## 2. Dedicated automated tests

```bash
python3 -m pytest tests/test_phase4_governance.py -v -s
```

Expected: **102 passed**, no failures or skips. Diagnostic lines include:

```text
SUPERSESSION OK: old=SUPERSEDED new=ACTIVE relation=SUPERSEDES
PRUNING OK: stale leaves=PRUNED hub/fresh=ACTIVE history=SUPERSEDED
MIGRATION OK: version=3 legacy graph and FTS preserved
102 passed in <seconds>s
```

Coverage includes:

- Exact four-factor arithmetic, score bounds, time/turn half-lives, deterministic
  aging, frequency reinforcement, and invalid parameter rejection.
- Persistent access history, deduplicated accesses, monotonic timestamps/turns,
  and scoring without accidentally counting the scan as a use.
- Immediate stale-fact invalidation, explicit fact-to-fact `SUPERSEDES` edges,
  legitimate multi-values, repeated assertions, reversals, and concurrent writes.
- Atomic rollback on invalid metadata, injected SQL errors, and caller aborts.
- Active topology, leaf eligibility, pin protection in both edge directions,
  strict threshold comparison, bounded pagination, and preserved historical rows.
- Queued async writes, observable daemon failures, restart, and shutdown that
  waits for a running write while the event loop remains responsive.
- Additive schema upgrades, failed-upgrade rollback, reopen/integrity checks,
  foreign keys, physical-delete cascades, and validated settings factories.

Then run regression and documentation checks:

```bash
python3 -m pytest -q
python3 -m pip check
git diff --check
```

Expected: **403 passed, 15 skipped**, `No broken requirements found.`, and no
whitespace-error output. The skips are eight Phase 1 LAN tests, six Phase 3 LAN
tests, and the opt-in storage benchmark. Tests create isolated temporary
databases; they do not migrate or prune your configured project database.

Because storage initialization changed, recheck the existing durable-insert
benchmark on your storage host:

```bash
mkdir -p .verification
python3 -m pytest tests/test_phase2_storage.py --run-storage-benchmark --basetemp .verification/phase4-storage -v -s
```

Expected: **83 passed**; the printed median for 200 individually FULL-committed
inserts must remain **below 1 ms**. Actual timing varies with disk and load.

If Node.js is installed, verify the updated component map:

```bash
node --test docs/testing/architecture-routing.mjs
```

Expected: **7 passed**, no failed connector-geometry checks. Open
`docs/architecture.html`; Phase 4 should appear under Implemented, with phases
5–8 still planned. This optional documentation check adds no runtime dependency.

## 3. Reproducible end-to-end governance smoke check

This command uses a temporary database and fixed time/turn values. It does not
load `.env` or modify your project data:

```bash
python3 - <<'PY'
from pathlib import Path
from tempfile import TemporaryDirectory
from src.governance import GovernedWriter, Pruner, record_access, score_entity
from src.storage.database import Database
from src.storage.graph_store import GraphStore

with TemporaryDirectory() as directory:
    with Database(Path(directory) / "memory.db") as database:
        graph = GraphStore(database)
        project = graph.create_entity("Project Cedar", is_pinned=True)
        old_value = graph.create_entity("SQLite")
        new_value = graph.create_entity("PostgreSQL")
        unused = graph.create_entity("Temporary observation")
        record_access(graph, [unused.id], turn=0, now=0)
        writer = GovernedWriter(graph, single_valued_predicates=["primary_database"])
        old = writer.write_fact(project.id, "primary_database", old_value.id, turn=1, now=0)
        new = writer.write_fact(project.id, "primary_database", new_value.id, turn=2, now=1)
        assert graph.get_triple(old.triple.id).status == "SUPERSEDED"
        assert graph.get_triple(new.triple.id).status == "ACTIVE"
        edge, = writer.list_supersessions()
        assert edge.relation == "SUPERSEDES"
        print("SUPERSESSION:", graph.get_triple(old.triple.id).status, new.triple.status, edge.relation)
        score = score_entity(graph, unused.id, current_turn=200, now=1_000_000)
        assert score.retention < 0.2
        print(f"UNUSED RETENTION: {score.retention:.6f}")
        result = Pruner(graph).run_once(current_turn=200, now=1_000_000)
        assert unused.id in result.pruned_ids
        assert graph.get_entity(unused.id).status == "PRUNED"
        assert graph.get_entity(project.id).status == "ACTIVE"
        assert graph.get_entity(new_value.id).status == "ACTIVE"
        assert graph.get_triple(new.triple.id).status == "ACTIVE"
        assert graph.search_prefix("Temporary") == []
        print("PRUNING: unused=PRUNED pinned project/current value=ACTIVE")
        with database.connection() as connection:
            assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert not connection.execute("PRAGMA foreign_key_check").fetchall()
        print("DATABASE INTEGRITY: ok")
PY
```

Expected exactly:

```text
SUPERSESSION: SUPERSEDED ACTIVE SUPERSEDES
UNUSED RETENTION: 0.000128
PRUNING: unused=PRUNED pinned project/current value=ACTIVE
DATABASE INTEGRITY: ok
```

## 4. Scoring and pruning rules

Let `a` be seconds since last access, `t` turns since last access, `n` recorded
accesses, and `d` the number of active incident semantic fact edges:

```text
S_recency    = 2 ** (-a / recency_half_life_seconds)
S_frequency  = S_recency * n / (n + 4)
S_centrality = min(1, d / 4)
S_turn_decay = 2 ** (-t / turn_half_life)
retention    = 0.35*S_recency + 0.20*S_frequency
             + 0.15*S_centrality + 0.30*S_turn_decay
```

`DecayPolicy` exposes the four weights and frequency/centrality scales to Python
callers. Weights must be nonnegative and sum to one. The two half-lives are also
environment-configurable. Scores lie in [0, 1]; lower means less retention.
Frequency reinforcement fades with elapsed time so a once-popular isolated
entity can eventually expire. These are explicit engineering defaults, **not
experimentally validated cognitive parameters**. Tune time scale, turn scale,
and threshold for the study; defaults need not prune during a short 20-turn run.

`record_access()` counts actual caller-declared use, once per distinct entity per
call. A governed fact write records use of its endpoints. Reads, scoring, FTS
searches, and pruning scans do not increment usage. Future retrieval must call
`record_access()` for memory actually supplied to an agent. Legacy raw-CRUD
entities start from their creation time, turn zero, and zero accesses until used.
Supply nondecreasing Unix timestamps and turn numbers per entity. Resume turn
numbers consistently when reopening a database; backward time/turns raise errors
and roll back a maintenance pass instead of pruning on invalid metadata.

One `Pruner.run_once()` scans at most `batch_size` active entities from a stable
snapshot. Use `next_after_id` as the next call's `after_id`; zero starts another
sweep. A full batch can require one final empty pass to wrap. A leaf has zero or
one **active incident fact edge**, counting a self-loop once. Parallel semantic
facts count separately. Both endpoints must be active; superseded/pruned facts
and provenance edges do not count. Degree > 1 is protected in that pass.

The score must be strictly below the threshold. Threshold zero disables pruning.
Pinned entities and either endpoint of their active facts are protected. Only
eligible entities and their **active** incident triples become `PRUNED`; existing
`SUPERSEDED` history, properties, usage, and provenance remain stored. FTS active
search hides pruned entities. Later sweeps may expose additional leaves as a
chain loses edges; soft-pruning does not put a hard bound on database size.

## 5. Governed writes, provenance, and lifecycle

`GovernedWriter.write_fact(subject_id, predicate, object_id, turn=..., now=...)`
requires existing entities. It normalizes the predicate, supersedes different
active objects only for configured single-valued predicates, records provenance,
updates usage, and commits all changes in one transaction. The latest accepted
assertion wins; it does not infer truth, negate facts, resolve aliases, or detect
contradictions between different predicate names. Confidence is metadata, not
an automatic conflict-resolution rule. Pins protect against pruning and do not
block deliberate fact updates.

An identical fact confirms its existing row and creates no supersession edge.
Reasserting an older value reactivates that row because SPO identity remains
unique. Pruned endpoints are explicitly reactivated by a new governed assertion;
unrelated historical facts stay inactive. Supersession does not automatically
pin entities or prune the former object.

`supersession_edges` contains explicit `newer_triple_id → SUPERSEDES →
older_triple_id` edges, each with an event ID, timestamp, and turn. Its foreign
keys reference **triples**, not entities. It is separate from semantic entity
edges, so retrieval must not traverse it as ordinary knowledge. Reversals can
produce both directions between reused fact rows; inspect event order to
interpret history. This records replacement events rather than immutable copies
of every assertion/confidence/property version or original source evidence.
`list_supersessions(after_id=..., limit=...)` returns ordered, bounded pages.

Raw `GraphStore.create_triple()` and `update_triple()` remain low-level CRUD and
can bypass policy. Use `GovernedWriter` for accepted memory facts. Extraction
still returns data without persisting it; automatic extraction/orchestration is
not implemented in this phase. Physical `GraphStore.delete_*()` calls still
delete rows and cascade provenance/usage; use the pruner for retention governance.

Async callers use `write_fact_async()` and `run_once_async()`, both serialized
through the existing database write queue. Callbacks execute off the event loop.
For background maintenance, use caller-owned nested contexts:

```python
async with Database.from_settings(settings) as database:
    graph = GraphStore(database)
    pruner = Pruner.from_settings(graph, settings)
    async with PruningDaemon(
        pruner, turn_provider=lambda: current_turn,
        interval_seconds=settings.pruning_interval_seconds,
    ) as daemon:
        # Await your application's work here; it owns current_turn.
        await application_work()
```

Import `PruningDaemon` from `src.governance`. This is a lifecycle sketch, not a
standalone runner: `application_work` and `current_turn` belong to the caller.
Nothing starts on import. The daemon runs one batch immediately, then one per
interval, advancing its cursor. `start()` requires a running asyncio loop;
`stop()` is repeatable and waits for accepted work before database shutdown.
Cancelling a `stop()` wait leaves the maintenance task shielded; call `stop()`
again to finish cleanup. A failed pass terminates the task and is re-raised by
`stop()`; inspect `is_running` and `last_result` for status. An already-started
database write completes atomically even if its caller stops waiting.

## 6. Optional upgrade of an existing project database

Phase 4 introduced **schema version 2**; the current checkout uses **version 3**
with Phase 6 workflow tables. Opening a version 1 database
transactionally adds `entity_memory` and `supersession_edges` plus indexes;
existing entities, triples, IDs, pins, FTS content, and uniqueness rules are
preserved. A failed upgrade leaves the prior version intact. Future schema versions are
rejected. Older application versions cannot open a newer schema; see the Phase 6 guide.

Stop other application processes first. If you already have project data, the
following optional command creates a consistent SQLite backup (including WAL
contents) before opening it with the new schema. It does **not** run pruning:

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
    name = "pre-phase4-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".db"
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

Expected: `BACKUP: .verification/pre-phase4-<timestamp>.db` if a database already
existed, followed by `SCHEMA VERSION: 3` and `INTEGRITY: ok`. No backup line is
printed for a new database. `.verification/` and `.env` stay ignored by Git.

## 7. Failure logs, acceptance, and commit message

```bash
python3 -m pytest tests/test_phase4_governance.py -v -s --tb=long
git status --short
git check-ignore .env .verification/
```

If a check fails, provide its full traceback. For schema failures include SQLite
version and the failing version/integrity output; for time errors check resumed
turns and timestamps. A raw CRUD write retaining two single-valued facts means
the caller bypassed `GovernedWriter`. A protected leaf can remain active because
it is pinned, touches an active pinned fact, or has not crossed the threshold.

Acceptance requires 102 dedicated tests and 403 offline regression tests passing,
no unexpected skips/failures, successful temporary smoke output, and the existing
storage benchmark passing on the target storage host. Network acceptance from
earlier phases remains independent. Review diffs and keep private configuration,
database files, backups, and local notes untracked.

```text
feat(phase4): add deterministic memory governance

Add four-factor retention scores and persistent access/turn metadata.
Atomically supersede configured single-valued facts with explicit provenance.
Soft-prune stale leaf memory while protecting pins and active pinned facts.
Add bounded queued maintenance, observable daemon shutdown, and v1 schema upgrade.
Verify rollback, reversals, migration, and async lifecycle with offline tests.
Update generic setup, architecture, and Phase 4 verification documentation.
```
