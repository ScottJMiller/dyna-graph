# Phase 2 Verification and Testing Protocol

Whole-suite totals in this phase protocol reflect its completion checkpoint.
See the [current workflow protocol](phase6.md) for current aggregate counts
and schema migration guidance. Dedicated phase checks remain applicable.

Verify embedded SQLite storage on Node 1. The storage implementation
uses Python's standard-library `sqlite3`, with no additional Python dependencies,
services, or API calls. Tests use isolated databases and require no Ollama server.
The deployed database remains on Node 1's local filesystem. SQLite documents why
[WAL requires host-local shared memory](https://www.sqlite.org/wal.html).

## 1. Setup & Pre-requisites

Run from the repository root after completing the environment setup in the
[Phase 1 guide](phase1.md).

```bash
source .venv/bin/activate
command -v python3
python3 --version
python3 -m pip check
python3 - <<'PY'
import sqlite3
print(f"SQLite version={sqlite3.sqlite_version}")
assert sqlite3.sqlite_version_info >= (3, 38, 0)
with sqlite3.connect(":memory:") as connection:
    connection.execute("CREATE VIRTUAL TABLE check_fts USING fts5(text)")
    assert connection.execute("SELECT json_valid('{}')").fetchone()[0] == 1
print("SQLITE CAPABILITIES OK")
PY
```

Expected: the `dynagraph/.venv/bin/python3` path, Python 3.11+, and
`No broken requirements found.` SQLite must be 3.38+ with FTS5 and JSON functions;
the version depends on your Python build. The script ends with `SQLITE CAPABILITIES OK`.
The capability probe's temporary in-memory table is separate from the file-backed
storage/benchmark databases.

Configure `.env` with `DATABASE_PATH=data/dynagraph.db`,
`SQLITE_WAL_MODE=true`, and `SQLITE_BUSY_TIMEOUT_MS=5000`. Pool size defaults to
four; the example includes `SQLITE_POOL_SIZE=4`. WAL is required, busy timeout is
validated within 1–60,000 ms, and the pool is bounded to 1–16 connections.

Validate the configuration without creating storage:

```bash
python3 - <<'PY'
from src.config import get_settings
s = get_settings()
print(f"DATABASE_PATH={s.database_path}")
print(f"SQLITE_WAL_MODE={s.sqlite_wal_mode}")
print(f"SQLITE_BUSY_TIMEOUT_MS={s.sqlite_busy_timeout_ms}")
print(f"SQLITE_POOL_SIZE={s.sqlite_pool_size}")
PY
```

Expected:

```text
DATABASE_PATH=<repository-root>/data/dynagraph.db
SQLITE_WAL_MODE=True
SQLITE_BUSY_TIMEOUT_MS=5000
SQLITE_POOL_SIZE=4
```

## 2. Automated Test Execution

Run the dedicated functional suite:

```bash
python3 -m pytest tests/test_storage.py -v -s
```

Expected: **82 passed, 1 skipped**. The skipped test is the explicit hardware
latency benchmark. Functional assertions cover:

- Schema version 1, tables, relationship indexes, and all three FTS triggers.
- WAL, foreign keys, 5,000 ms busy timeout, and FULL synchronization on every connection.
- Initialization/reopen persistence, schema-error rollback, and future-version rejection.
- Entity/triple create, read, update, delete, filtering, and pagination.
- Canonical-name uniqueness, JSON round trips, valid confidence/status/pin values.
- Foreign-key rejection and cascading deletion of incoming/outgoing/self-loop triples.
- FTS prefix matches, accents, descriptions, literal-query safety, and index synchronization.
- Atomic multi-operation rollback, nested savepoints, and FTS content integrity.
- Reader snapshots while a writer commits, thread-safe leases, and concurrent atomic updates.
- Pool-wait and inter-instance busy timeouts, recovery, and safe connection closure.
- Async FIFO writes off the event loop, rollback after callback errors, and queue recovery.
- Queued/start-of-write cancellation behavior, resumable cancelled shutdown, and queue draining.

Run the required storage-host benchmark and full Phase 2 approval check:

```bash
mkdir -p .verification
python3 -m pytest tests/test_storage.py --run-storage-benchmark --basetemp .verification/phase2 -v -s
```

Expected: **83 passed, no skips or failures**, plus a line like:

```text
INSERT BENCHMARK committed_inserts=200 median_ms=<number> p95_ms=<number> max_ms=<number> synchronous=FULL path=<isolated-database>
```

Values vary with hardware, storage, and load. The assertion requires
**median_ms < 1.0**; it does not claim every insert or the p95 is sub-millisecond.
After ten warmups, each sample includes the public entity-insert API, JSON
serialization, indexes, FTS trigger, returned record, and its own durable commit.
FULL synchronization remains enabled. The test also checks all 210 records
exist. `--basetemp .verification/phase2` places isolated files on the project's
filesystem; pytest recreates that test-artifact directory on subsequent runs.
This directory is ignored by Git and separate from `data/dynagraph.db`.

Verify the Phase 1 offline regression suite alongside storage:

```bash
python3 -m pytest tests/test_network.py tests/test_storage.py -q
python3 -m pip check
```

Expected: **196 passed, 9 skipped** followed by `No broken requirements found.`
The skips are eight physical LAN checks and the explicit storage benchmark.
Physical LAN tests remain available through Phase 1's `--run-lan` switch.

## 3. Initialize and inspect the configured database

This step creates the configured directory/database if missing, or opens the
existing version-1 database without deleting records:

```bash
python3 - <<'PY'
from src.storage.database import Database
with Database.from_settings() as database:
    with database.connection() as connection:
        for name in ("journal_mode", "foreign_keys", "busy_timeout", "synchronous", "user_version"):
            print(f"{name}={connection.execute('PRAGMA ' + name).fetchone()[0]}")
        print(f"integrity_check={connection.execute('PRAGMA integrity_check').fetchone()[0]}")
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        assert not violations
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"entities", "triples", "fts_entities"} <= tables
    with database.transaction() as connection:
        connection.execute("INSERT INTO fts_entities(fts_entities, rank) VALUES ('integrity-check', 1)")
    print("SCHEMA AND FTS VALID")
    print(f"DATABASE READY: {database.path}")
PY
```

Expected:

```text
journal_mode=wal
foreign_keys=1
busy_timeout=5000
synchronous=2
user_version=1
integrity_check=ok
SCHEMA AND FTS VALID
DATABASE READY: <repository-root>/data/dynagraph.db
```

`synchronous=2` means FULL. Schema setup is transactional and idempotent.
The connection pool enables foreign keys and busy timeout individually on each
connection. FTS5 external-content triggers update the search index in the same
transaction as the entity mutation, following SQLite's
[external-content guidance](https://www.sqlite.org/fts5.html#external_content_tables).

## 4. Manual atomic CRUD and prefix-search smoke check

This script uses a temporary database and leaves your configured graph untouched:

```bash
python3 - <<'PY'
from pathlib import Path
from tempfile import TemporaryDirectory
from src.storage.database import Database
from src.storage.graph_store import GraphStore
with TemporaryDirectory(prefix="dynagraph-phase2-smoke-") as directory:
    with Database(Path(directory) / "smoke.db") as database:
        graph = GraphStore(database)
        with graph.transaction():
            project = graph.create_entity("DynaGraph", entity_type="project", is_pinned=True)
            sqlite = graph.create_entity("SQLite", entity_type="software")
            edge = graph.create_triple(project.id, "uses", sqlite.id)
        assert graph.get_entity(project.id).is_pinned
        assert graph.get_triple(edge.id).predicate == "uses"
        assert [entity.name for entity in graph.search_prefix("sql")] == ["SQLite"]
        print("CRUD AND PREFIX SEARCH OK")
        graph.delete_entity(sqlite.id)
        assert graph.get_triple(edge.id) is None
        assert not graph.search_prefix("sql")
        print("CASCADE AND FTS DELETE OK")
PY
```

Expected:

```text
CRUD AND PREFIX SEARCH OK
CASCADE AND FTS DELETE OK
```

## 5. Async writes and lifecycle contract

`database.connection()` opens a read-only snapshot. Writes use
`database.transaction()` or `graph.transaction()`. Nested CRUD operations share
the current thread's connection and use savepoints, so an outer operation is
atomic. Do not manually commit, close, or change PRAGMAs on managed connections.
Other processes use SQLite's writer lock and configured busy timeout; the
instance's writer lock serializes its own transactions.

Queued callbacks must be synchronous and short. They execute in worker threads,
with one atomic transaction per callback and a bounded queue of `pool_size * 4`.
Use the async context manager to drain and close:

```python
async with Database.from_settings() as database:
    graph = GraphStore(database)
    record = await graph.write_async(lambda tx: tx.create_entity("Async fact"))
```

A cancelled queued job is skipped before execution. A started callback finishes
atomically even if its caller cancels; cancellation is not an undo of a running
SQLite transaction. `aclose()` drains accepted jobs before closing connections,
and may be retried if its caller was cancelled. One database's async API belongs
to one event loop. Synchronous users use `with Database(...)` / `close()`.

Names are casefolded and whitespace-normalized; predicates are normalized in the
same way. Duplicate canonical entities or duplicate `(subject, predicate, object)`
facts raise `sqlite3.IntegrityError`. `get_or_create_entity()` preserves an
existing entity's attributes. Property updates replace the JSON object. Physical
entity deletion cascades; status/pin values are stored fields, while decay,
pruning, supersession, and semantic extraction algorithms remain future phases.

## 6. Failure logs and acceptance

```bash
mkdir -p .verification
python3 -m pytest tests/test_storage.py --run-storage-benchmark --basetemp .verification/phase2 -v -s --tb=long
```

| Failure | Next check |
| --- | --- |
| SQLite version/FTS5/JSON missing | Check the Python-linked SQLite build; use SQLite 3.38+ with FTS5 and JSON enabled. |
| WAL unavailable | Check that `DATABASE_PATH` points to a writable local filesystem on Node 1. |
| `database is locked` | Inspect another long-running writer; keep transactions short and verify the busy timeout. |
| Pool timeout | Inspect long-lived read leases and connection-pool sizing. |
| Median exceeds 1 ms | Preserve benchmark output; inspect disk/load and rerun on the coordinator's local NVMe. Keep FULL synchronization enabled. |
| Foreign-key/unique/check constraint | Validate entity IDs, duplicate facts, JSON properties, and supplied field values. |
| Read transaction promotion error | Open a write transaction before CRUD updates. |
| Async close/event-loop error | Use `async with Database(...)` and the same event loop for its async lifecycle. |

Phase 2 acceptance requires all 83 dedicated checks passing, median committed
insertion below 1 ms, the configured database inspection passing, and the Phase
1 offline regression suite remaining green. Tests and inspection require no
external API calls, network services, or financial cost.

## 7. Commit message

For a Phase 2 implementation step:

```text
feat(phase2): add transactional SQLite graph storage

Add WAL storage, pooled connections, graph CRUD, and transactional FTS5 search.
Serialize asynchronous writes with atomic rollback and safe shutdown.
Add storage verification and a durable-insert latency benchmark.
Provide generic user documentation and exclude private deployment notes.
```

Before committing, run `git diff --check` and review the staged changes.
Keep `.env`, database files, local reference notes, and virtual environments untracked.
