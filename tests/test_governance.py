"""Offline deterministic governance, provenance, migration, and maintenance tests."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
import sqlite3
from threading import Event

import pytest
from pydantic import ValidationError

from src.config import Settings
from src.governance import(
    DecayPolicy, GovernedWriter, MemoryUsage, Pruner, PruningDaemon,
    record_access, score_entity,
)
from src.storage.database import Database
from src.storage.graph_store import GraphStore

NOW = 1_800_000_000.0
OLD = NOW - 1_000_000


@pytest.fixture
def graph(tmp_path):
    """Provide fresh temporary graph storage for deterministic governance checks."""
    with Database(tmp_path / "governance.db") as database:
        yield GraphStore(database)


def entity(graph, name, *, pinned=False, turn=0, now=OLD):
    """Create a test entity and record its explicit initial access metadata."""
    item = graph.create_entity(name, is_pinned=pinned)
    record_access(graph, [item.id], turn=turn, now=now)
    return item


def usage(graph, entity_id):
    """Read persisted usage without refreshing its access count."""
    with graph.database.connection() as connection:
        row = connection.execute("SELECT * FROM entity_memory WHERE entity_id=?", (entity_id,)).fetchone()
        return MemoryUsage(row["last_access_at"], row["last_access_turn"], row["access_count"])


def test_four_factor_formula_and_weighted_total():
    policy = DecayPolicy(recency_half_life_seconds=10, turn_half_life=2)
    score = policy.score(MemoryUsage(100, 4, 4), degree=2, now=110, current_turn=6)
    assert (score.recency, score.frequency, score.centrality, score.turn_decay) == (0.5, 0.25, 0.5, 0.5)
    assert score.retention == pytest.approx(0.45)


def test_unreferenced_memory_decays_deterministically():
    policy = DecayPolicy(recency_half_life_seconds=10, turn_half_life=2)
    metadata = MemoryUsage(0, 0, 1)
    scores = [policy.score(metadata, degree=0, now=t, current_turn=t) for t in (0, 10, 20, 100)]
    assert all(a.retention > b.retention for a, b in zip(scores, scores[1:]))
    assert scores[-1].retention < 0.001
    assert scores == [policy.score(metadata, degree=0, now=t, current_turn=t) for t in (0, 10, 20, 100)]


def test_frequency_boost_fades_and_centrality_saturates():
    policy = DecayPolicy()
    low = policy.score(MemoryUsage(0, 0, 1), degree=0, now=0, current_turn=0)
    high = policy.score(MemoryUsage(0, 0, 10000), degree=100, now=0, current_turn=0)
    stale = policy.score(MemoryUsage(0, 0, 10000), degree=0, now=1e9, current_turn=10000)
    assert high.frequency > low.frequency
    assert high.centrality == 1
    assert 0 <= low.retention < high.retention <= 1
    assert stale.frequency == 0 and stale.retention < 1e-100


@pytest.mark.parametrize("kwargs", [
    {"recency_half_life_seconds": 0}, {"recency_half_life_seconds": -1},
    {"turn_half_life": 0}, {"frequency_scale": float("nan")},
    {"centrality_scale": float("inf")}, {"turn_half_life": True},
    {"weights": (1, 1, 1, 1)}, {"weights": (0.5, -0.5, 0.5, 0.5)},
    {"weights": (0.5, 0.5)}, {"weights": [0.35, 0.2, 0.15, 0.3]},
])
def test_invalid_decay_policy(kwargs):
    with pytest.raises(ValueError):
        DecayPolicy(**kwargs)


@pytest.mark.parametrize("kwargs", [
    {"last_access_at": -1}, {"last_access_at": float("nan")},
    {"last_access_turn": -1}, {"last_access_turn": True},
    {"access_count": -1}, {"access_count": 1.5},
])
def test_invalid_memory_usage(kwargs):
    with pytest.raises(ValueError):
        MemoryUsage(**({"last_access_at": 0} | kwargs))


@pytest.mark.parametrize("kwargs", [
    {"now": -1}, {"now": float("inf")}, {"now": 5},
    {"current_turn": -1}, {"current_turn": True}, {"current_turn": 0},
    {"degree": -1}, {"degree": 0.5},
])
def test_invalid_score_inputs(kwargs):
    with pytest.raises(ValueError):
        DecayPolicy().score(MemoryUsage(10, 1), **({"now": 10, "current_turn": 1, "degree": 0} | kwargs))


def test_access_is_deduplicated_atomic_and_persistent(graph):
    first, second = entity(graph, "First"), entity(graph, "Second")
    record_access(graph, [first.id, first.id], turn=2, now=NOW)
    assert usage(graph, first.id) == MemoryUsage(NOW, 2, 2)
    with pytest.raises(ValueError, match="existing ACTIVE"):
        record_access(graph, [second.id, 999999], turn=3, now=NOW)
    assert usage(graph, second.id) == MemoryUsage(OLD, 0, 1)
    with Database(graph.database.path) as reopened:
        assert usage(GraphStore(reopened), first.id) == MemoryUsage(NOW, 2, 2)


@pytest.mark.parametrize("ids,turn,now", [
    ([True], 0, NOW), ([0], 0, NOW), ("1", 0, NOW),
    (list(range(1, 1002)), 0, NOW), ([], -1, NOW), ([], 0, float("nan")),
])
def test_invalid_access_inputs(graph, ids, turn, now):
    with pytest.raises(ValueError):
        record_access(graph, ids, turn=turn, now=now)


def test_access_requires_active_and_monotonic_metadata(graph):
    item = entity(graph, "Used", now=NOW, turn=5)
    for turn, now in ((4, NOW), (5, NOW - 1)):
        with pytest.raises(ValueError, match="precede"):
            record_access(graph, [item.id], turn=turn, now=now)
    graph.update_entity(item.id, status="PRUNED")
    with pytest.raises(ValueError, match="ACTIVE"):
        record_access(graph, [item.id], turn=6, now=NOW)


def test_scoring_does_not_count_as_access(graph):
    item = entity(graph, "Unreferenced")
    before = usage(graph, item.id)
    a = score_entity(graph, item.id, current_turn=200, now=NOW)
    b = score_entity(graph, item.id, current_turn=200, now=NOW)
    assert a == b and a.retention < 0.2
    assert usage(graph, item.id) == before


def test_active_edge_degree_ignores_history_inactive_endpoints_and_counts_self_loop_once(graph):
    a, b, c = [entity(graph, name) for name in ("A", "B", "C")]
    graph.create_triple(a.id, "self", a.id)
    graph.create_triple(a.id, "old", b.id, status="SUPERSEDED")
    graph.create_triple(a.id, "inactive", c.id)
    graph.update_entity(c.id, status="PRUNED")
    assert score_entity(graph, a.id, current_turn=0, now=NOW).centrality == 0.25
    assert score_entity(graph, b.id, current_turn=0, now=NOW).centrality == 0


def test_legacy_entity_uses_creation_time_and_no_access_backfill(graph):
    item = graph.create_entity("Legacy")
    created = datetime.fromisoformat(item.created_at).timestamp()
    score = score_entity(graph, item.id, current_turn=10, now=created + 86400)
    assert score.recency == score.turn_decay == 0.5
    assert score.frequency == score.centrality == 0
    with graph.database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM entity_memory").fetchone()[0] == 0


def test_immediate_supersession_has_explicit_fact_edges(graph):
    project, sqlite, postgres = [entity(graph, name) for name in ("Project Cedar", "SQLite", "PostgreSQL")]
    writer = GovernedWriter(graph, single_valued_predicates=[" PRIMARY_DATABASE "])
    old = writer.write_fact(project.id, "primary_database", sqlite.id, turn=1, now=NOW)
    new = writer.write_fact(project.id, "PRIMARY_DATABASE", postgres.id, turn=2, now=NOW + 1)
    assert graph.get_triple(old.triple.id).status == "SUPERSEDED"
    assert new.triple.status == "ACTIVE" and new.superseded_ids == (old.triple.id,)
    assert [t.id for t in graph.list_triples(status="ACTIVE")] == [new.triple.id]
    edge, = writer.list_supersessions()
    assert edge == new.edges[0]
    assert (
        (edge.newer_triple_id, edge.relation, edge.older_triple_id) == (new.triple.id, "SUPERSEDES", old.triple.id)
    )
    assert (edge.turn, edge.asserted_at) == (2, NOW + 1)
    print("SUPERSESSION OK: old=SUPERSEDED new=ACTIVE relation=SUPERSEDES")


def test_multi_valued_and_other_subjects_are_not_superseded(graph):
    a, b, c, d = [entity(graph, name) for name in ("A", "B", "C", "D")]
    writer = GovernedWriter(graph, single_valued_predicates=["owner"])
    for subject, predicate, obj in ((a, "uses", b), (a, "uses", c), (a, "owner", b), (d, "owner", c)):
        writer.write_fact(subject.id, predicate, obj.id, turn=1, now=NOW)
    assert len(graph.list_triples(status="ACTIVE")) == 4
    assert writer.list_supersessions() == []


def test_no_predicate_is_single_valued_by_default(graph):
    a, b, c = [entity(graph, name) for name in ("A", "B", "C")]
    writer = GovernedWriter(graph)
    for obj in (b, c):
        writer.write_fact(a.id, "status", obj.id, turn=0, now=NOW)
    assert len(graph.list_triples(status="ACTIVE")) == 2


def test_duplicate_confirmation_and_historical_reversal_preserve_event_order(graph):
    a, b, c = [entity(graph, name) for name in ("A", "B", "C")]
    writer = GovernedWriter(graph, single_valued_predicates=["owner"])
    first = writer.write_fact(a.id, "owner", b.id, turn=1, now=NOW)
    again = writer.write_fact(a.id, "owner", b.id, turn=2, now=NOW + 1, confidence=0.8)
    assert again.triple.id == first.triple.id and again.edges == ()
    second = writer.write_fact(a.id, "owner", c.id, turn=3, now=NOW + 2)
    restored = writer.write_fact(a.id, "owner", b.id, turn=4, now=NOW + 3)
    assert restored.triple.id == first.triple.id
    assert graph.get_triple(second.triple.id).status == "SUPERSEDED"
    assert len(graph.list_triples()) == 2
    edges = writer.list_supersessions()
    assert [(edge.newer_triple_id, edge.older_triple_id, edge.turn) for edge in edges] == [
        (second.triple.id, first.triple.id, 3), (first.triple.id, second.triple.id, 4),
    ]
    assert writer.list_supersessions(after_id=edges[0].id, limit=1) == edges[1:]


def test_existing_multiple_stale_values_are_all_replaced(graph):
    a, b, c, d = [entity(graph, name) for name in ("A", "B", "C", "D")]
    old = [graph.create_triple(a.id, "owner", obj.id) for obj in (b, c)]
    new = GovernedWriter(graph, single_valued_predicates=["owner"]).write_fact(a.id, "owner", d.id, turn=1, now=NOW)
    assert new.superseded_ids == tuple(t.id for t in old)
    assert len(new.edges) == 2


def test_supersession_and_metadata_roll_back_on_late_failure(graph):
    a, b, c = [entity(graph, name) for name in ("A", "B", "C")]
    writer = GovernedWriter(graph, single_valued_predicates=["owner"])
    old = writer.write_fact(a.id, "owner", b.id, turn=5, now=NOW)
    with pytest.raises(ValueError, match="precede"):
        writer.write_fact(a.id, "owner", c.id, turn=4, now=NOW)
    assert graph.get_triple(old.triple.id).status == "ACTIVE"
    assert len(graph.list_triples()) == 1 and writer.list_supersessions() == []
    assert usage(graph, c.id) == MemoryUsage(OLD, 0, 1)


def test_explicit_write_revives_pruned_endpoints_without_reviving_unrelated_facts(graph):
    a, b, c = [entity(graph, name) for name in ("A", "B", "C")]
    old = graph.create_triple(a.id, "uses", c.id, status="PRUNED")
    target = graph.create_triple(a.id, "uses", b.id, status="PRUNED")
    graph.update_entity(a.id, status="PRUNED")
    result = GovernedWriter(graph).write_fact(a.id, "uses", b.id, turn=1, now=NOW)
    assert result.triple.id == target.id and result.triple.status == "ACTIVE"
    assert graph.get_entity(a.id).status == "ACTIVE"
    assert graph.get_triple(old.id).status == "PRUNED"
    assert [e.id for e in graph.search_prefix("A")] == [a.id]


@pytest.mark.parametrize("kwargs", [
    {"subject_id": True}, {"object_id": 99999}, {"predicate": " "},
    {"turn": -1}, {"now": float("inf")}, {"confidence": 2}, {"properties": {"bad": float("nan")}},
])
def test_invalid_fact_write_is_atomic(graph, kwargs):
    a, b = entity(graph, "A"), entity(graph, "B")
    with pytest.raises(ValueError):
        GovernedWriter(graph).write_fact(**({"subject_id": a.id, "object_id": b.id, "predicate": "uses",
                                            "turn": 1, "now": NOW} | kwargs))
    assert graph.list_triples() == []


def test_concurrent_single_value_writes_leave_one_active_value(graph):
    a, b, c = [entity(graph, name) for name in ("A", "B", "C")]
    writer = GovernedWriter(graph, single_valued_predicates=["owner"])
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda obj: writer.write_fact(a.id, "owner", obj.id, turn=1, now=NOW), (b, c)))
    assert len(graph.list_triples(status="ACTIVE")) == 1
    assert len(graph.list_triples(status="SUPERSEDED")) == 1
    assert len(writer.list_supersessions()) == 1
    assert {r.triple.object_id for r in results} == {b.id, c.id}


def test_governed_write_uses_caller_transaction(graph):
    a, b = entity(graph, "A"), entity(graph, "B")
    with pytest.raises(RuntimeError):
        with graph.transaction():
            GovernedWriter(graph).write_fact(a.id, "uses", b.id, turn=1, now=NOW)
            raise RuntimeError("caller abort")
    assert graph.list_triples() == [] and usage(graph, a.id).access_count == 1


def test_pruning_removes_only_stale_leaf_memory_and_preserves_history(graph):
    stale, fresh, hub, leaf1, leaf2 = [entity(graph, name) for name in ("Stale", "Fresh", "Hub", "Leaf 1", "Leaf 2")]
    record_access(graph, [fresh.id], turn=200, now=NOW)
    active1 = graph.create_triple(hub.id, "uses", leaf1.id)
    active2 = graph.create_triple(hub.id, "uses", leaf2.id)
    history = graph.create_triple(stale.id, "old", hub.id, status="SUPERSEDED")
    result = Pruner(graph).run_once(current_turn=200, now=NOW)
    assert result.pruned_ids == (stale.id, leaf1.id, leaf2.id)
    assert graph.get_entity(hub.id).status == graph.get_entity(fresh.id).status == "ACTIVE"
    assert graph.get_triple(active1.id).status == graph.get_triple(active2.id).status == "PRUNED"
    assert graph.get_triple(history.id).status == "SUPERSEDED"
    assert graph.search_prefix("Stale") == []
    assert len(graph.list_entities()) == 5 and len(graph.list_triples()) == 3
    print("PRUNING OK: stale leaves=PRUNED hub/fresh=ACTIVE history=SUPERSEDED")


def test_pins_protect_active_facts_in_both_directions(graph):
    pinned = entity(graph, "Pinned", pinned=True)
    incoming, outgoing = entity(graph, "Incoming"), entity(graph, "Outgoing")
    graph.create_triple(incoming.id, "needs", pinned.id)
    graph.create_triple(pinned.id, "needs", outgoing.id)
    assert Pruner(graph, threshold=1).run_once(current_turn=200, now=NOW).pruned_ids == ()
    assert all(e.status == "ACTIVE" for e in graph.list_entities())


def test_inactive_edges_to_pins_do_not_block_pruning(graph):
    pinned, stale = entity(graph, "Pinned", pinned=True), entity(graph, "Stale")
    graph.create_triple(stale.id, "old", pinned.id, status="SUPERSEDED")
    assert Pruner(graph).run_once(current_turn=200, now=NOW).pruned_ids == (stale.id,)


def test_threshold_boundary_is_strict_and_zero_disables_pruning(graph):
    item = entity(graph, "Boundary")
    score = score_entity(graph, item.id, current_turn=200, now=NOW)
    assert Pruner(graph, threshold=0).run_once(current_turn=200, now=NOW).pruned_ids == ()
    assert Pruner(graph, threshold=score.retention).run_once(current_turn=200, now=NOW).pruned_ids == ()
    assert (
        Pruner(graph, threshold=score.retention + 0.001).run_once(current_turn=200, now=NOW).pruned_ids == (item.id,)
    )


def test_batched_cursor_eventually_visits_later_ids_and_wraps(graph):
    items = [entity(graph, str(n), pinned=n < 2) for n in range(5)]
    pruner = Pruner(graph, batch_size=2)
    cursor, seen = 0, []
    for _ in range(3):
        result = pruner.run_once(current_turn=200, now=NOW, after_id=cursor)
        assert result.scanned <= 2
        seen.extend(result.pruned_ids)
        cursor = result.next_after_id
    assert seen == [item.id for item in items[2:]] and cursor == 0


def test_future_metadata_rolls_back_entire_pruning_batch(graph):
    a, b = entity(graph, "A"), entity(graph, "B", now=NOW + 1)
    with pytest.raises(ValueError, match="precede"):
        Pruner(graph).run_once(current_turn=200, now=NOW)
    assert graph.get_entity(a.id).status == graph.get_entity(b.id).status == "ACTIVE"


def test_sql_failure_rolls_back_soft_pruning_and_fts_visibility(graph):
    a, b = entity(graph, "A"), entity(graph, "B")
    edge = graph.create_triple(a.id, "uses", b.id)
    with graph.database.transaction() as connection:
        connection.execute("CREATE TRIGGER reject_prune BEFORE UPDATE ON triples "
                           "WHEN NEW.status='PRUNED' BEGIN SELECT RAISE(ABORT,'injected failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="injected failure"):
        Pruner(graph).run_once(current_turn=200, now=NOW)
    assert graph.get_entity(a.id).status == graph.get_entity(b.id).status == "ACTIVE"
    assert graph.get_triple(edge.id).status == "ACTIVE"
    assert [e.id for e in graph.search_prefix("A")] == [a.id]


def test_sql_failure_rolls_back_supersession_and_provenance(graph):
    a, b, c = [entity(graph, name) for name in ("A", "B", "C")]
    writer = GovernedWriter(graph, single_valued_predicates=["owner"])
    old = writer.write_fact(a.id, "owner", b.id, turn=1, now=NOW)
    with graph.database.transaction() as connection:
        connection.execute("CREATE TRIGGER reject_provenance BEFORE INSERT ON supersession_edges "
                           "BEGIN SELECT RAISE(ABORT,'injected failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="injected failure"):
        writer.write_fact(a.id, "owner", c.id, turn=2, now=NOW)
    assert graph.get_triple(old.triple.id).status == "ACTIVE"
    assert len(graph.list_triples()) == 1 and writer.list_supersessions() == []


def test_pin_changes_are_respected_by_next_pass(graph):
    item = entity(graph, "Pinned", pinned=True)
    pruner = Pruner(graph)
    assert pruner.run_once(current_turn=200, now=NOW).pruned_ids == ()
    graph.update_entity(item.id, is_pinned=False)
    assert pruner.run_once(current_turn=200, now=NOW).pruned_ids == (item.id,)


@pytest.mark.parametrize("kwargs", [{"after_id": -1}, {"current_turn": True}, {"now": float("inf")}])
def test_invalid_pruning_pass(graph, kwargs):
    with pytest.raises(ValueError):
        Pruner(graph).run_once(**({"current_turn": 0, "now": NOW} | kwargs))


@pytest.mark.parametrize("kwargs", [
    {"threshold": -0.1}, {"threshold": 1.1}, {"threshold": float("nan")},
    {"threshold": True}, {"batch_size": 0}, {"batch_size": 1001}, {"batch_size": True},
])
def test_invalid_pruner_configuration(graph, kwargs):
    with pytest.raises(ValueError):
        Pruner(graph, **kwargs)


def test_async_writer_and_pruner_use_serialized_queue(tmp_path):
    async def scenario():
        async with Database(tmp_path / "async.db") as database:
            graph = GraphStore(database)
            a, b, c = [entity(graph, name) for name in ("A", "B", "C")]
            writer = GovernedWriter(graph, single_valued_predicates=["owner"])
            old, new = await asyncio.gather(
                writer.write_fact_async(a.id, "owner", b.id, turn=1, now=OLD),
                writer.write_fact_async(a.id, "owner", c.id, turn=2, now=OLD),
            )
            assert graph.get_triple(old.triple.id).status == "SUPERSEDED"
            assert graph.get_triple(new.triple.id).status == "ACTIVE"
            result = await Pruner(graph).run_once_async(current_turn=200, now=NOW)
            assert set(result.pruned_ids) == {a.id, b.id, c.id}
    asyncio.run(scenario())


def test_daemon_runs_stops_idempotently_and_restarts(tmp_path):
    async def scenario():
        async with Database(tmp_path / "daemon.db") as database:
            graph = GraphStore(database)
            item = entity(graph, "Stale")
            daemon = PruningDaemon(Pruner(graph), lambda: 200, clock=lambda: NOW, interval_seconds=0.001)
            await daemon.stop()
            async with daemon:
                with pytest.raises(RuntimeError):
                    daemon.start()
                for _ in range(100):
                    if daemon.last_result is not None:
                        break
                    await asyncio.sleep(0.01)
                assert daemon.last_result is not None and daemon.is_running
            assert not daemon.is_running and graph.get_entity(item.id).status == "PRUNED"
            await daemon.stop()
            async with daemon:
                await asyncio.sleep(0)
    asyncio.run(scenario())


def test_daemon_surfaces_failed_pass_and_can_be_stopped_again(tmp_path):
    async def scenario():
        async with Database(tmp_path / "failed-daemon.db") as database:
            graph = GraphStore(database)
            entity(graph, "Future", now=NOW + 1)
            daemon = PruningDaemon(Pruner(graph), lambda: 200, clock=lambda: NOW)
            daemon.start()
            for _ in range(100):
                if not daemon.is_running:
                    break
                await asyncio.sleep(0.01)
            with pytest.raises(ValueError, match="precede"):
                await daemon.stop()
            await daemon.stop()
    asyncio.run(scenario())


def test_daemon_stop_waits_for_inflight_write_without_blocking_loop(tmp_path):
    async def scenario():
        entered, release = Event(), Event()
        async with Database(tmp_path / "slow-daemon.db") as database:
            graph = GraphStore(database)
            item = entity(graph, "Stale")
            class SlowPruner(Pruner):
                def run_once(self, **kwargs):
                    entered.set()
                    if not release.wait(timeout=3):
                        raise RuntimeError("test write was never released")
                    return super().run_once(**kwargs)
            daemon = PruningDaemon(SlowPruner(graph), lambda: 200, clock=lambda: NOW)
            daemon.start()
            try:
                for _ in range(100):
                    if entered.is_set():
                        break
                    await asyncio.sleep(0.01)
                assert entered.is_set()
                stopping = asyncio.create_task(daemon.stop())
                await asyncio.sleep(0.01)
                assert not stopping.done()
            finally:
                release.set()
            await asyncio.wait_for(stopping, timeout=3)
            assert graph.get_entity(item.id).status == "PRUNED"
    asyncio.run(scenario())


@pytest.mark.parametrize("interval", [0, -1, True, float("nan"), float("inf")])
def test_invalid_daemon_interval(graph, interval):
    with pytest.raises(ValueError):
        PruningDaemon(Pruner(graph), lambda: 0, interval_seconds=interval)


def test_additive_v1_migration_preserves_graph_fts_and_is_idempotent(tmp_path):
    path = tmp_path / "v1.db"
    schema = (Path(__file__).parents[1] / "src/storage/schema.sql").read_text()
    legacy_schema = schema.split("-- Access metadata")[0]
    with sqlite3.connect(path) as connection:
        connection.executescript(legacy_schema)
        connection.execute("INSERT INTO entities(name,canonical_name,is_pinned) VALUES('Legacy','legacy',1)")
        connection.execute("INSERT INTO triples(subject_id,predicate,object_id) VALUES(1,'self',1)")
        connection.execute("PRAGMA user_version=1")
    for _ in range(2):
        with Database(path) as database:
            graph = GraphStore(database)
            assert graph.get_entity(1).is_pinned
            assert graph.get_triple(1).predicate == "self"
            assert [e.id for e in graph.search_prefix("leg")] == [1]
            with database.connection() as connection:
                assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
                assert connection.execute("SELECT COUNT(*) FROM entity_memory").fetchone()[0] == 0
                assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                assert not connection.execute("PRAGMA foreign_key_check").fetchall()
    print("MIGRATION OK: version=3 legacy graph and FTS preserved")


def test_failed_v1_upgrade_is_atomic(tmp_path, monkeypatch):
    import src.storage.database as module
    path, bad = tmp_path / "v1-failed.db", tmp_path / "bad.sql"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE sentinel(value TEXT)")
        connection.execute("INSERT INTO sentinel VALUES('preserved')")
        connection.execute("PRAGMA user_version=1")
    bad.write_text("CREATE TABLE partial(id INTEGER); INVALID SQL;", encoding="utf-8")
    monkeypatch.setattr(module, "SCHEMA_PATH", bad)
    with pytest.raises(sqlite3.OperationalError):
        Database(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert connection.execute("SELECT value FROM sentinel").fetchone()[0] == "preserved"
        assert not connection.execute("SELECT name FROM sqlite_master WHERE name='partial'").fetchall()


def test_provenance_and_usage_cascade_only_on_physical_deletion(graph):
    a, b, c = [entity(graph, name) for name in ("A", "B", "C")]
    writer = GovernedWriter(graph, single_valued_predicates=["owner"])
    first = writer.write_fact(a.id, "owner", b.id, turn=1, now=NOW)
    writer.write_fact(a.id, "owner", c.id, turn=2, now=NOW)
    assert len(writer.list_supersessions()) == 1
    graph.delete_triple(first.triple.id)
    assert writer.list_supersessions() == []
    graph.delete_entity(b.id)
    with graph.database.connection() as connection:
        assert connection.execute("SELECT 1 FROM entity_memory WHERE entity_id=?", (b.id,)).fetchone() is None


@pytest.mark.parametrize("table,columns,values", [
    ("entity_memory", "entity_id,last_access_at,last_access_turn,access_count", (999, NOW, 0, 1)),
    ("entity_memory", "entity_id,last_access_at,last_access_turn,access_count", (1, float("inf"), 0, 1)),
    ("entity_memory", "entity_id,last_access_at,last_access_turn,access_count", (1, NOW, -1, 1)),
    ("supersession_edges", "newer_triple_id,older_triple_id,asserted_at,turn", (1, 1, NOW, 0)),
    ("supersession_edges", "newer_triple_id,older_triple_id,asserted_at,turn", (99, 98, NOW, 0)),
])
def test_governance_schema_constraints(graph, table, columns, values):
    a = graph.create_entity("A")
    graph.create_triple(a.id, "self", a.id)
    with pytest.raises(sqlite3.IntegrityError):
        with graph.database.transaction() as connection:
            connection.execute(f"INSERT INTO {table}({columns}) VALUES(?,?,?,?)", values)


def test_governance_settings_and_factories(graph, monkeypatch):
    for field in Settings.model_fields:
        monkeypatch.delenv(field.upper(), raising=False)
    monkeypatch.setenv("SINGLE_VALUED_PREDICATES", '[" Owner ","owner","primary_database"]')
    settings = Settings(_env_file=None, node2_ollama_url="http://192.168.1.20:11434")
    assert settings.single_valued_predicates == ("owner", "primary_database")
    assert (
        GovernedWriter.from_settings(graph, settings).single_valued_predicates == frozenset(settings.single_valued_predicates)
    )
    assert Pruner.from_settings(graph, settings).threshold == 0.2
    assert DecayPolicy.from_settings(settings) == DecayPolicy()


@pytest.mark.parametrize("kwargs", [
    {"pruning_threshold": -1}, {"pruning_threshold": float("inf")}, {"pruning_threshold": 2},
    {"pruning_batch_size": 0}, {"pruning_batch_size": 1001}, {"pruning_interval_seconds": 0},
    {"decay_recency_half_life_seconds": 0}, {"decay_turn_half_life": -1},
    {"single_valued_predicates": [" "]}, {"single_valued_predicates": ["x" * 257]},
    {"single_valued_predicates": [str(n) for n in range(101)]},
])
def test_invalid_governance_settings(kwargs):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, node2_ollama_url="http://192.168.1.20:11434", **kwargs)
