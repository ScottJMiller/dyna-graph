"""Local persistence, FTS5 consistency, transactional concurrency, and latency."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import math
import sqlite3
import statistics
from threading import Barrier, Event
from time import perf_counter_ns

import pytest
from pydantic import ValidationError

from src.config import Settings
from src.storage.database import Database, DatabaseError, PoolTimeoutError
from src.storage.graph_store import GraphStore


@pytest.fixture
def database(tmp_path):
    with Database(tmp_path / "graph.db", pool_size=3) as db:
        yield db


@pytest.fixture
def store(database):
    return GraphStore(database)


def test_schema_indexes_and_triggers(database):
    with database.connection() as connection:
        objects = {row["name"]: row["type"] for row in connection.execute("SELECT name, type FROM sqlite_master")}
        assert all(objects[name] == "table" for name in ("entities", "triples", "fts_entities"))
        assert all(objects[name] == "index" for name in (
            "idx_entities_status_updated", "idx_triples_subject_status",
            "idx_triples_object_status", "idx_triples_predicate_status",
        ))
        assert all(objects[name] == "trigger" for name in (
            "entities_fts_insert", "entities_fts_delete", "entities_fts_update",
        ))
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()


def test_every_pooled_connection_has_required_pragmas(database):
    barrier = Barrier(database.pool_size)
    def inspect_connection(_):
        with database.connection() as connection:
            barrier.wait(timeout=3)
            values = tuple(connection.execute(f"PRAGMA {name}").fetchone()[0] for name in (
                "journal_mode", "foreign_keys", "busy_timeout", "synchronous",
            ))
            return id(connection), values
    with ThreadPoolExecutor(max_workers=database.pool_size) as executor:
        results = list(executor.map(inspect_connection, range(database.pool_size)))
    assert len({identity for identity, _ in results}) == database.pool_size
    assert all(values == ("wal", 1, 5000, 2) for _, values in results)


def test_initialization_and_reopen_preserve_graph_and_fts(tmp_path):
    path = tmp_path / "nested" / "graph.db"
    with Database(path) as first:
        graph = GraphStore(first)
        entity = graph.create_entity("Persistent Memory")
        edge = graph.create_triple(entity.id, "remembers", entity.id)
    with Database(path) as reopened:
        graph = GraphStore(reopened)
        assert graph.get_entity(entity.id) == entity
        assert graph.get_triple(edge.id) == edge
        assert [result.id for result in graph.search_prefix("pers mem")] == [entity.id]


def test_future_schema_version_is_rejected(tmp_path):
    path = tmp_path / "future.db"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version=99")
    with pytest.raises(DatabaseError, match="Unsupported schema version 99"):
        Database(path)


def test_schema_failure_is_atomic(tmp_path, monkeypatch):
    import src.storage.database as module
    schema = tmp_path / "bad.sql"
    schema.write_text("CREATE TABLE partial(id INTEGER); THIS IS NOT SQL;", encoding="utf-8")
    monkeypatch.setattr(module, "SCHEMA_PATH", schema)
    path = tmp_path / "failed.db"
    with pytest.raises(sqlite3.OperationalError):
        Database(path)
    with sqlite3.connect(path) as connection:
        assert not connection.execute("SELECT name FROM sqlite_master WHERE name='partial'").fetchall()
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0


@pytest.mark.parametrize("kwargs", [
    {"path": ":memory:"}, {"path": ""}, {"path": "file:graph.db"},
    {"pool_size": 0}, {"pool_size": 17}, {"pool_size": True},
    {"busy_timeout_ms": 0}, {"busy_timeout_ms": 60001},
])
def test_invalid_database_configuration(tmp_path, kwargs):
    options = {"path": tmp_path / "graph.db"} | kwargs
    with pytest.raises(ValueError):
        Database(**options)


def test_sqlite_environment_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("SQLITE_WAL_MODE", "true")
    monkeypatch.setenv("SQLITE_BUSY_TIMEOUT_MS", "5000")
    monkeypatch.setenv("SQLITE_POOL_SIZE", "2")
    settings = Settings(_env_file=None, node2_ollama_url="http://10.0.60.181:11434", database_path=tmp_path / "configured.db")
    with Database.from_settings(settings) as database:
        assert database.path == settings.database_path
        assert database.pool_size == 2
        with database.connection() as connection:
            assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


@pytest.mark.parametrize("setting, value", [
    ("sqlite_wal_mode", False), ("sqlite_busy_timeout_ms", 0),
    ("sqlite_busy_timeout_ms", 60001), ("sqlite_pool_size", 0), ("sqlite_pool_size", 17),
])
def test_invalid_sqlite_settings(setting, value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, node2_ollama_url="http://10.0.60.181:11434", **{setting: value})


def test_entity_crud_and_property_roundtrip(store):
    properties = {"owner": "Scott", "details": {"tags": ["mémoire", 1, True, None]}}
    entity = store.create_entity("  Graph   Memory ", entity_type="Concept", description="Persistent context", properties=properties, is_pinned=True)
    assert entity.name == "Graph Memory"
    assert entity.canonical_name == "graph memory"
    assert entity.entity_type == "concept"
    assert entity.is_pinned is True
    assert entity.properties == properties
    assert entity.created_at and entity.updated_at
    assert store.get_entity(entity.id) == entity
    assert store.find_entity("GRAPH  memory") == entity
    changed = store.update_entity(entity.id, name="Working Memory", entity_type="System", description="New description", properties={"revision": 2}, is_pinned=False)
    assert changed.name == "Working Memory"
    assert changed.properties == {"revision": 2}
    assert changed.created_at == entity.created_at
    assert not changed.is_pinned
    assert store.find_entity("Graph Memory") is None
    assert store.delete_entity(entity.id)
    assert not store.delete_entity(entity.id)
    assert store.get_entity(entity.id) is None
    assert store.update_entity(entity.id, description="missing") is None
    assert store.create_entity("New entity").id > entity.id


def test_unicode_canonical_names_and_duplicate_rejection(store):
    entity = store.create_entity("Straße")
    assert store.find_entity("STRASSE") == entity
    with pytest.raises(sqlite3.IntegrityError):
        store.create_entity("  STRASSE ")
    assert len(store.list_entities()) == 1


def test_get_or_create_preserves_existing_properties_and_handles_threads(store):
    original = store.create_entity("Shared Fact", properties={"version": 1})
    def worker(_):
        return store.get_or_create_entity(" SHARED   fact ", properties={"version": 2}).id
    with ThreadPoolExecutor(max_workers=8) as executor:
        identifiers = list(executor.map(worker, range(32)))
    assert set(identifiers) == {original.id}
    assert store.get_entity(original.id).properties == {"version": 1}
    assert len(store.list_entities()) == 1


def test_get_or_create_is_atomic_for_new_name(store):
    with ThreadPoolExecutor(max_workers=8) as executor:
        identifiers = list(executor.map(lambda _: store.get_or_create_entity("New shared name").id, range(32)))
    assert len(set(identifiers)) == 1
    assert len(store.list_entities()) == 1


def test_triple_crud_filters_and_uniqueness(store):
    alice = store.create_entity("Alice")
    bob = store.create_entity("Bob")
    carol = store.create_entity("Carol")
    edge = store.create_triple(alice.id, "  KNOWS ", bob.id, properties={"source": "test"}, confidence=0.8)
    assert edge.predicate == "knows"
    assert edge.properties == {"source": "test"}
    assert edge.confidence == 0.8
    assert store.get_triple(edge.id) == edge
    with pytest.raises(sqlite3.IntegrityError):
        store.create_triple(alice.id, "Knows", bob.id)
    changed = store.update_triple(edge.id, subject_id=carol.id, predicate="VERIFIES", object_id=alice.id, confidence=0.5, properties={}, status="SUPERSEDED")
    assert changed.created_at == edge.created_at
    assert store.list_triples(subject_id=carol.id, predicate="VERIFIES", object_id=alice.id, status="SUPERSEDED") == [changed]
    assert not store.list_triples(subject_id=alice.id)
    assert store.delete_triple(edge.id)
    assert not store.delete_triple(edge.id)
    assert store.get_triple(edge.id) is None
    assert store.update_triple(edge.id, predicate="missing") is None
    assert len(store.list_entities()) == 3


@pytest.mark.parametrize("endpoint", ["subject", "object"])
def test_missing_foreign_key_rejects_insert_and_update(store, endpoint):
    a = store.create_entity("A")
    b = store.create_entity("B")
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        store.create_triple(999 if endpoint == "subject" else a.id, "knows", 999 if endpoint == "object" else b.id)
    edge = store.create_triple(a.id, "knows", b.id)
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        store.update_triple(edge.id, **{f"{endpoint}_id": 999})
    assert store.get_triple(edge.id) == edge


def test_foreign_key_delete_cascades_and_fts_removal(store, database):
    a, b, c = [store.create_entity(name) for name in ("Cascade A", "B", "C")]
    incident = [store.create_triple(a.id, "knows", b.id), store.create_triple(c.id, "knows", a.id), store.create_triple(a.id, "knows", a.id)]
    survivor = store.create_triple(b.id, "knows", c.id)
    assert store.delete_entity(a.id)
    assert all(store.get_triple(edge.id) is None for edge in incident)
    assert store.list_triples() == [survivor]
    assert not store.search_prefix("casc")
    with database.connection() as connection:
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()


@pytest.mark.parametrize("properties", [[], {1: "bad key"}, {"bad": float("nan")}, {"bad": float("inf")}, {"bad": {1, 2}}, {"bad": b"bytes"}])
def test_invalid_properties_do_not_mutate_graph(store, properties):
    with pytest.raises(ValueError, match="properties"):
        store.create_entity("Invalid properties", properties=properties)
    assert not store.list_entities()


@pytest.mark.parametrize("properties", ["not JSON", "[]"])
def test_database_rejects_invalid_json_properties(store, database, properties):
    entity = store.create_entity("Valid JSON")
    with pytest.raises(sqlite3.IntegrityError):
        with database.transaction() as connection:
            connection.execute("UPDATE entities SET properties=? WHERE id=?", (properties, entity.id))
    assert store.get_entity(entity.id).properties == {}


@pytest.mark.parametrize("confidence", [-0.1, 1.1, float("nan"), float("inf"), True, "0.5"])
def test_invalid_confidence(store, confidence):
    entity = store.create_entity("Fact")
    with pytest.raises(ValueError, match="confidence"):
        store.create_triple(entity.id, "refers to", entity.id, confidence=confidence)
    assert not store.list_triples()


@pytest.mark.parametrize("identifier", [0, -1, True, "1", 1.5])
def test_invalid_ids(store, identifier):
    with pytest.raises(ValueError, match="IDs"):
        store.get_entity(identifier)


@pytest.mark.parametrize("attributes", [{"name": ""}, {"name": "   "}, {"entity_type": ""}, {"description": 1}, {"is_pinned": 1}, {"status": "UNKNOWN"}])
def test_invalid_entity_fields(store, attributes):
    options = {"name": "Valid name"} | attributes
    with pytest.raises(ValueError):
        store.create_entity(**options)
    assert not store.list_entities()


def test_noop_updates_and_missing_lookups(store):
    entity = store.create_entity("No-op")
    triple = store.create_triple(entity.id, "self", entity.id)
    assert store.update_entity(entity.id) == entity
    assert store.update_triple(triple.id) == triple
    assert store.get_entity(999) is None
    assert store.get_triple(999) is None


def test_entity_pagination_and_status_filters(store):
    entities = [store.create_entity(f"Item {index}") for index in range(5)]
    store.update_entity(entities[-1].id, status="PRUNED")
    assert store.list_entities(limit=2, offset=1) == entities[1:3]
    assert [entity.id for entity in store.list_entities(status="ACTIVE")] == [entity.id for entity in entities[:-1]]
    assert [entity.id for entity in store.list_entities(status="PRUNED")] == [entities[-1].id]


@pytest.mark.parametrize("options", [{"limit": 0}, {"limit": 1001}, {"limit": True}, {"offset": -1}])
def test_invalid_pagination(store, options):
    with pytest.raises(ValueError):
        store.list_entities(**options)


def test_fts_prefix_case_accents_description_and_multiple_words(store):
    target = store.create_entity("Dynamic Graph Memory", description="Université research substrate")
    store.create_entity("Dynamic Planner")
    store.create_entity("Graph Engine")
    assert store.search_prefix("DYN gra") == [target]
    assert store.search_prefix("universite") == [target]
    assert store.search_prefix("substr") == [target]


def test_fts_updates_and_status_filter_are_consistent(store):
    entity = store.create_entity("Old label", description="Former description")
    store.update_entity(entity.id, name="New label", description="Current description")
    assert not store.search_prefix("old")
    assert not store.search_prefix("former")
    assert [row.id for row in store.search_prefix("new curr")] == [entity.id]
    store.update_entity(entity.id, status="PRUNED")
    assert not store.search_prefix("new")
    store.update_entity(entity.id, status="ACTIVE")
    assert [row.id for row in store.search_prefix("new")] == [entity.id]


def test_literal_search_and_sql_injection_input_are_safe(store):
    name = "Alice'); DROP TABLE entities; --"
    entity = store.create_entity(name)
    assert store.find_entity(name) == entity
    orchard = store.create_entity("Orchard")
    assert store.search_prefix('" OR *') == [orchard]
    assert not store.search_prefix("***")
    assert not store.search_prefix("alice OR orchard")
    assert len(store.list_entities()) == 2


def test_outer_transaction_rolls_back_entities_triples_and_fts(store):
    anchor = store.create_entity("Anchor")
    with pytest.raises(sqlite3.IntegrityError):
        with store.transaction():
            pending = store.create_entity("Rollback memory")
            store.create_triple(anchor.id, "links", pending.id)
            store.create_triple(pending.id, "invalid", 999)
    assert store.list_entities() == [anchor]
    assert not store.list_triples()
    assert not store.search_prefix("rollback")


def test_nested_savepoint_rollback_keeps_outer_transaction(store, database):
    with store.transaction():
        first = store.create_entity("Original label")
        with pytest.raises(RuntimeError, match="abort inner"):
            with database.transaction():
                store.update_entity(first.id, name="Discarded label")
                store.create_entity("Discarded addition")
                raise RuntimeError("abort inner")
        second = store.create_entity("Committed addition")
    assert [row.id for row in store.list_entities()] == [first.id, second.id]
    assert store.get_entity(first.id).name == "Original label"
    assert not store.search_prefix("discarded")
    assert store.search_prefix("original") == [first]


def test_constraint_failure_restores_fts_on_rename(store):
    first = store.create_entity("First label")
    second = store.create_entity("Second label")
    with pytest.raises(sqlite3.IntegrityError):
        store.update_entity(first.id, name="SECOND LABEL")
    assert store.search_prefix("first") == [first]
    assert store.search_prefix("second") == [second]


def test_fts_external_content_integrity_after_updates_and_deletes(store, database):
    entity = store.create_entity("Original FTS")
    store.update_entity(entity.id, name="Updated FTS")
    other = store.create_entity("Removed FTS")
    store.delete_entity(other.id)
    with database.transaction() as connection:
        connection.execute("INSERT INTO fts_entities(fts_entities, rank) VALUES ('integrity-check', 1)")


def test_read_transactions_are_readonly_and_cannot_be_promoted(database, store):
    with database.connection() as connection:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("INSERT INTO entities(name, canonical_name) VALUES ('bad', 'bad')")
        with pytest.raises(DatabaseError, match="promote"):
            store.create_entity("Forbidden promotion")
    assert store.create_entity("Allowed write").id > 0


def test_wal_reader_snapshot_does_not_block_writer(database, store):
    store.create_entity("Before")
    with database.connection() as reader:
        assert reader.execute("SELECT count(*) FROM entities").fetchone()[0] == 1
        with ThreadPoolExecutor(max_workers=1) as executor:
            written = executor.submit(store.create_entity, "During snapshot").result(timeout=3)
        assert reader.execute("SELECT count(*) FROM entities").fetchone()[0] == 1
        assert store.get_entity(written.id) is None
    assert store.get_entity(written.id) == written


def test_cross_instance_busy_timeout_and_recovery(tmp_path):
    path = tmp_path / "contended.db"
    with Database(path) as first, Database(path, busy_timeout_ms=40) as second:
        with first.transaction():
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                GraphStore(second).create_entity("Blocked")
        assert GraphStore(second).create_entity("Recovered").id > 0


def test_pool_wait_timeout_recovers_without_losing_connection(tmp_path):
    with Database(tmp_path / "small.db", pool_size=1, busy_timeout_ms=40) as database:
        def reader():
            with database.connection():
                return True
        with database.connection():
            with ThreadPoolExecutor(max_workers=1) as executor:
                with pytest.raises(PoolTimeoutError):
                    executor.submit(reader).result(timeout=2)
        assert reader()


def test_threaded_writers_preserve_atomic_updates(store):
    counter = store.create_entity("Counter", properties={"count": 0})
    def increment(index):
        with store.transaction():
            current = store.get_entity(counter.id)
            store.update_entity(counter.id, properties={"count": current.properties["count"] + 1})
            entity = store.create_entity(f"Thread fact {index}")
            store.create_triple(counter.id, "records", entity.id)
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(increment, range(48)))
    assert store.get_entity(counter.id).properties == {"count": 48}
    assert len(store.list_entities()) == 49
    assert len(store.list_triples(subject_id=counter.id)) == 48


def test_close_inside_transaction_rejected_and_close_is_idempotent(tmp_path):
    database = Database(tmp_path / "close.db")
    with database.connection():
        with pytest.raises(DatabaseError, match="inside a transaction"):
            database.close()
    database.close()
    database.close()
    with pytest.raises(DatabaseError, match="closed"):
        with database.connection():
            pass


def test_close_waits_for_active_read_lease(tmp_path):
    database = Database(tmp_path / "lease.db")
    started = Event()
    def close():
        started.set()
        database.close()
    with ThreadPoolExecutor(max_workers=1) as executor:
        with database.connection():
            closing = executor.submit(close)
            assert started.wait(timeout=1)
            assert not closing.done()
        closing.result(timeout=2)
    with pytest.raises(DatabaseError, match="closed"):
        GraphStore(database).list_entities()


def test_async_write_queue_is_fifo_and_keeps_event_loop_responsive(tmp_path):
    async def scenario():
        async with Database(tmp_path / "async.db") as database:
            graph = GraphStore(database)
            started, release = Event(), Event()
            def blocking_write(store):
                started.set()
                assert release.wait(timeout=3)
                return store.create_entity("Queued 0")
            first = asyncio.create_task(graph.write_async(blocking_write))
            assert await asyncio.to_thread(started.wait, 1)
            others = [asyncio.create_task(graph.write_async(lambda store, index=index: store.create_entity(f"Queued {index}"))) for index in range(1, 8)]
            await asyncio.sleep(0)
            assert not first.done()
            release.set()
            results = await asyncio.gather(first, *others)
            assert [row.id for row in results] == sorted(row.id for row in results)
            assert [row.name for row in graph.list_entities()] == [f"Queued {index}" for index in range(8)]
    asyncio.run(scenario())


def test_async_failure_rolls_back_and_worker_continues(tmp_path):
    async def scenario():
        async with Database(tmp_path / "async-failure.db") as database:
            graph = GraphStore(database)
            def fail(store):
                store.create_entity("Discard async")
                raise ValueError("abort queued write")
            with pytest.raises(ValueError, match="abort queued write"):
                await graph.write_async(fail)
            assert not graph.list_entities()
            assert not graph.search_prefix("discard")
            assert (await graph.write_async(lambda store: store.create_entity("Next job"))).id > 0
            with pytest.raises(DatabaseError, match="aclose"):
                database.close()
    asyncio.run(scenario())


def test_queued_cancellation_skips_write(tmp_path):
    async def scenario():
        async with Database(tmp_path / "cancel.db") as database:
            graph = GraphStore(database)
            started, release = Event(), Event()
            def hold(store):
                started.set()
                assert release.wait(timeout=3)
                return store.create_entity("Started")
            first = asyncio.create_task(graph.write_async(hold))
            assert await asyncio.to_thread(started.wait, 1)
            cancelled = asyncio.create_task(graph.write_async(lambda store: store.create_entity("Cancelled")))
            await asyncio.sleep(0)
            cancelled.cancel()
            with pytest.raises(asyncio.CancelledError):
                await cancelled
            release.set()
            await first
        with Database(tmp_path / "cancel.db") as reopened:
            assert [row.name for row in GraphStore(reopened).list_entities()] == ["Started"]
    asyncio.run(scenario())


def test_started_cancelled_write_finishes_atomically_and_aclose_drains(tmp_path):
    async def scenario():
        database = Database(tmp_path / "started-cancel.db")
        graph = GraphStore(database)
        started, release = Event(), Event()
        def write(store):
            store.create_entity("Committed before cancellation")
            started.set()
            assert release.wait(timeout=3)
            store.create_entity("Committed after cancellation")
        task = asyncio.create_task(graph.write_async(write))
        assert await asyncio.to_thread(started.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        await database.aclose()
        await database.aclose()
        with Database(tmp_path / "started-cancel.db") as reopened:
            assert len(GraphStore(reopened).list_entities()) == 2
    asyncio.run(scenario())


def test_async_callback_is_rejected_without_leaking_coroutine(tmp_path):
    async def scenario():
        async with Database(tmp_path / "callback.db") as database:
            async def invalid(_):
                return 1
            with pytest.raises(TypeError, match="synchronous"):
                await database.write_async(invalid)
            assert await database.write_async(lambda _: 2) == 2
    asyncio.run(scenario())


def test_cancelled_shutdown_can_resume_and_preserves_accepted_write(tmp_path):
    async def scenario():
        database = Database(tmp_path / "cancel-close.db")
        graph = GraphStore(database)
        started, release = Event(), Event()
        def hold(store):
            started.set()
            assert release.wait(timeout=3)
            return store.create_entity("Accepted before shutdown")
        write = asyncio.create_task(graph.write_async(hold))
        assert await asyncio.to_thread(started.wait, 1)
        close = asyncio.create_task(database.aclose())
        await asyncio.sleep(0)
        close.cancel()
        with pytest.raises(asyncio.CancelledError):
            await close
        release.set()
        await asyncio.wait_for(write, timeout=2)
        await asyncio.wait_for(database.aclose(), timeout=2)
        with Database(tmp_path / "cancel-close.db") as reopened:
            assert GraphStore(reopened).find_entity("Accepted before shutdown") is not None
    asyncio.run(scenario())


def test_callback_cancellation_rolls_back_and_worker_continues(tmp_path):
    async def scenario():
        async with Database(tmp_path / "callback-cancel.db") as database:
            graph = GraphStore(database)
            def cancel(store):
                store.create_entity("Rolled back callback")
                raise asyncio.CancelledError()
            with pytest.raises(asyncio.CancelledError):
                await graph.write_async(cancel)
            assert not graph.list_entities()
            assert await database.write_async(lambda _: "worker alive") == "worker alive"
    asyncio.run(scenario())


def test_cancelled_shutdown_with_full_queue_can_be_retried(tmp_path):
    async def scenario():
        database = Database(tmp_path / "full-queue.db", pool_size=1)
        graph = GraphStore(database)
        started, release = Event(), Event()
        def hold(store):
            started.set()
            assert release.wait(timeout=3)
            return store.create_entity("First accepted")
        first = asyncio.create_task(graph.write_async(hold))
        assert await asyncio.to_thread(started.wait, 1)
        queued = [asyncio.create_task(graph.write_async(lambda store, i=i: store.create_entity(f"Accepted {i}"))) for i in range(4)]
        await asyncio.sleep(0)
        assert database._queue.qsize() == 4
        close = asyncio.create_task(database.aclose())
        await asyncio.sleep(0)
        close.cancel()
        with pytest.raises(asyncio.CancelledError):
            await close
        release.set()
        await asyncio.wait_for(asyncio.gather(first, *queued), timeout=3)
        await asyncio.wait_for(database.aclose(), timeout=2)
        with Database(tmp_path / "full-queue.db") as reopened:
            assert len(GraphStore(reopened).list_entities()) == 5
    asyncio.run(scenario())


def test_async_writes_reject_closed_database(tmp_path):
    async def scenario():
        database = Database(tmp_path / "closed-async.db")
        await database.aclose()
        with pytest.raises(DatabaseError, match="closed"):
            await database.write_async(lambda _: None)
    asyncio.run(scenario())


def test_async_api_rejects_second_event_loop(tmp_path):
    database = Database(tmp_path / "event-loop.db")
    loop = asyncio.new_event_loop()
    try:
        assert loop.run_until_complete(database.write_async(lambda _: 1)) == 1
        with pytest.raises(DatabaseError, match="original event loop"):
            asyncio.run(database.write_async(lambda _: 2))
        loop.run_until_complete(database.aclose())
    finally:
        loop.close()


@pytest.mark.storage_benchmark
def test_committed_insert_latency_below_one_millisecond(store, database):
    for index in range(10):
        store.create_entity(f"Warmup {index}")
    latencies = []
    for index in range(200):
        started = perf_counter_ns()
        store.create_entity(f"Benchmark {index}", properties={"sample": index})
        latencies.append((perf_counter_ns() - started) / 1_000_000)
    median = statistics.median(latencies)
    p95 = sorted(latencies)[math.ceil(len(latencies) * 0.95) - 1]
    print(f"INSERT BENCHMARK committed_inserts=200 median_ms={median:.4f} p95_ms={p95:.4f} max_ms={max(latencies):.4f} synchronous=FULL path={database.path}")
    assert len(store.list_entities(limit=1000)) == 210
    assert median < 1.0, f"Median committed insert {median:.4f} ms exceeds the 1 ms target; inspect storage/load and preserve the benchmark output"
