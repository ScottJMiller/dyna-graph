"""Offline retrieval correctness, strict budgets, snapshots, and opt-in latency."""

import asyncio
from dataclasses import replace
import json
import statistics
from time import perf_counter_ns

import pytest
from pydantic import ValidationError

from src.config import Settings
from src.governance import GovernedWriter
from src.retrieval import ContextAssembler, SeedMatcher, Traversal, count_utf8_bound
from src.retrieval.seed_matcher import MAX_TERMS
from src.storage.database import Database, DatabaseError
from src.storage.graph_store import GraphStore


@pytest.fixture
def graph(tmp_path):
    """Own fresh temporary storage for retrieval and usage assertions."""
    with Database(tmp_path / "retrieval.db") as database:
        yield GraphStore(database)


def chain(graph, length=5):
    """Create a directed chain that also permits testing undirected discovery."""
    nodes = [graph.create_entity(f"Node{i}") for i in range(length)]
    facts = [graph.create_triple(a.id, "leads_to", b.id) for a, b in zip(nodes, nodes[1:])]
    return nodes, facts


def metadata(graph):
    """Read all usage rows without marking any memory as consumed."""
    with graph.database.connection() as connection:
        return [tuple(row) for row in connection.execute("SELECT * FROM entity_memory ORDER BY entity_id")]


def test_natural_language_or_matching_and_exact_name_priority(graph):
    project = graph.create_entity("Project Cedar")
    graph.create_entity("Unrelated", description="cedar tree")
    matcher = SeedMatcher(graph)
    result = matcher.match("Which database does Project Cedar use?")
    assert project.id in result.entity_ids and result.source == "lexical"
    assert "which" not in result.terms and "database" in result.terms
    assert matcher.match("Project Cedar").entity_ids[0] == project.id


def test_literal_prefix_diacritic_unicode_and_description_matching(graph):
    sqlite = graph.create_entity("SQLite", description="embedded relational persistence")
    cafe = graph.create_entity("Café")
    chinese = graph.create_entity("数据库")
    matcher = SeedMatcher(graph)
    assert matcher.match("sql").entity_ids == (sqlite.id,)
    assert matcher.match("persist").entity_ids == (sqlite.id,)
    assert matcher.match("cafe").entity_ids == (cafe.id,)
    assert matcher.match("数据库").entity_ids == (chinese.id,)


def test_recent_active_fallback_has_stable_ties_and_respects_limit(graph):
    nodes, _ = chain(graph)
    with graph.database.transaction() as connection:
        connection.execute("UPDATE entities SET updated_at='2020-01-01T00:00:00.000Z'")
    graph.update_entity(nodes[-1].id, status="PRUNED")
    result = SeedMatcher(graph, seed_limit=2).match("unmatchedzxyz")
    assert result.source == "recent"
    assert result.entity_ids == (nodes[3].id, nodes[2].id)
    graph.update_entity(nodes[0].id, description="recently changed")
    assert SeedMatcher(graph, seed_limit=1).match("").entity_ids == (nodes[0].id,)


@pytest.mark.parametrize(
    "query",
    ["", "   ", "!!!", "the and a", '" OR * NEAR(foo) -bar', "subject_id:99", "'; DROP TABLE entities; --"],
)
def test_query_syntax_is_literal_or_falls_back(graph, query):
    item = graph.create_entity("Memory")
    assert SeedMatcher(graph).match(query).entity_ids == (item.id,)
    assert graph.get_entity(item.id) is not None


def test_seed_matching_never_returns_pruned_or_oversized_names(graph):
    active = graph.create_entity("Current Cedar")
    graph.create_entity("Pruned Cedar", status="PRUNED")
    graph.create_entity("Cedar" * 60)
    assert SeedMatcher(graph).match("cedar").entity_ids == (active.id,)


def test_empty_graph_and_updated_fts(graph):
    assert SeedMatcher(graph).match("memory").source == "empty"
    item = graph.create_entity("Before")
    graph.update_entity(item.id, name="After")
    assert SeedMatcher(graph).match("After").source == "lexical"
    assert SeedMatcher(graph).match("Before").source == "recent"


def test_terms_are_bounded_deduplicated_and_not_sql_interpolated(graph):
    graph.create_entity("Unique")
    result = SeedMatcher(graph).match("unique UNIQUE " + " ".join(f"term{i}" for i in range(50)))
    assert len(result.terms) == MAX_TERMS and result.terms[0] == "unique"
    assert len(set(result.terms)) == len(result.terms)


@pytest.mark.parametrize("query", [None, 1, False, "x" * 2001, "bad\x00goal", "\ud800"])
def test_invalid_subgoals(graph, query):
    with pytest.raises(ValueError):
        SeedMatcher(graph).match(query)
    with pytest.raises(ValueError):
        ContextAssembler(graph).assemble(query)


@pytest.mark.parametrize("limit", [0, 17, True, 1.5, "2"])
def test_invalid_seed_limit(graph, limit):
    with pytest.raises(ValueError):
        SeedMatcher(graph, seed_limit=limit)


@pytest.mark.parametrize("hops", [1, 2])
def test_bfs_depth_and_undirected_discovery_preserve_fact_direction(graph, hops):
    nodes, facts = chain(graph)
    result = Traversal(graph, hops=hops).expand([nodes[2].id])
    distances = {node.id: node.depth for node in result.entities}
    assert distances == {node.id: abs(i - 2) for i, node in enumerate(nodes) if abs(i - 2) <= hops}
    assert all(t.subject_id < t.object_id for t in result.triples)
    assert (
        {t.id for t in result.triples} == {t.id for t in facts if t.subject_id in distances and t.object_id in distances}
    )
    assert not result.may_be_truncated


def test_bfs_cycles_self_loops_duplicate_paths_and_minimum_depth(graph):
    nodes, _ = chain(graph, 4)
    graph.create_triple(nodes[0].id, "self", nodes[0].id)
    graph.create_triple(nodes[0].id, "shortcut", nodes[2].id)
    graph.create_triple(nodes[2].id, "returns", nodes[0].id)
    result = Traversal(graph).expand([nodes[0].id, nodes[0].id])
    assert (
        {e.id: e.depth for e in result.entities} == {nodes[0].id: 0, nodes[1].id: 1, nodes[2].id: 1, nodes[3].id: 2}
    )
    assert len({e.id for e in result.entities}) == len(result.entities)
    assert len({t.id for t in result.triples}) == len(result.triples)
    assert result == Traversal(graph).expand([nodes[0].id])


def test_inactive_edges_and_endpoints_cannot_bridge_to_active_memory(graph):
    nodes, facts = chain(graph, 5)
    graph.update_triple(facts[0].id, status="SUPERSEDED")
    graph.update_entity(nodes[3].id, status="PRUNED")
    result = Traversal(graph).expand([nodes[1].id])
    assert {e.id for e in result.entities} == {nodes[1].id, nodes[2].id}
    assert [t.id for t in result.triples] == [facts[1].id]
    graph.update_triple(facts[1].id, status="PRUNED")
    assert len(Traversal(graph).expand([nodes[1].id]).entities) == 1


def test_seed_absence_pruning_and_disconnected_components(graph):
    nodes, _ = chain(graph, 4)
    other = graph.create_entity("Disconnected")
    graph.update_entity(other.id, status="PRUNED")
    assert Traversal(graph).expand([]).entities == ()
    assert Traversal(graph).expand([999999, other.id]).entities == ()
    assert nodes[-1].id not in {e.id for e in Traversal(graph).expand([nodes[0].id]).entities}


def test_node_fact_and_fanout_limits_are_visible(graph):
    root = graph.create_entity("Hub")
    nodes = [graph.create_entity(f"Leaf{i}") for i in range(30)]
    for node in nodes:
        graph.create_triple(root.id, "outgoing", node.id)
        graph.create_triple(node.id, "incoming", root.id)
    bounded = Traversal(graph, fanout=2).expand([root.id])
    assert len(bounded.entities) == 3 and bounded.may_be_truncated
    node_limited = Traversal(graph, max_nodes=4).expand([root.id])
    assert len(node_limited.entities) == 4 and node_limited.may_be_truncated
    fact_limited = Traversal(graph, max_triples=3).expand([root.id])
    assert len(fact_limited.triples) == 3 and fact_limited.may_be_truncated
    assert {t.subject_id for t in fact_limited.triples} <= {e.id for e in fact_limited.entities}


def test_oversized_predicates_and_names_are_not_traversal_bridges(graph):
    a, b = graph.create_entity("A"), graph.create_entity("B")
    huge = graph.create_entity("X" * 257)
    graph.create_triple(a.id, "x" * 257, b.id)
    graph.create_triple(a.id, "uses", huge.id)
    result = Traversal(graph).expand([a.id])
    assert len(result.entities) == 1 and result.triples == ()


def test_raw_nul_labels_cannot_bypass_sql_character_limits(graph):
    root = graph.create_entity("Root")
    hidden = graph.create_entity("Hidden\x00" + "x" * 2000)
    target = graph.create_entity("Target")
    graph.create_triple(root.id, "uses", hidden.id)
    graph.create_triple(root.id, "hidden\x00" + "p" * 2000, target.id)
    assert hidden.id not in SeedMatcher(graph).match("Hidden").entity_ids
    result = Traversal(graph).expand([root.id])
    assert [entity.id for entity in result.entities] == [root.id] and result.triples == ()


@pytest.mark.parametrize("kwargs", [
    {"hops": 0}, {"hops": 3}, {"hops": True}, {"max_nodes": 0}, {"max_nodes": 257},
    {"max_triples": 0}, {"max_triples": 513}, {"fanout": 0}, {"fanout": 65}, {"fanout": 1.5},
])
def test_invalid_traversal_limits(graph, kwargs):
    with pytest.raises(ValueError):
        Traversal(graph, **kwargs)


@pytest.mark.parametrize("ids", ["1", [True], [0], [-1], [1.0], list(range(1, 18))])
def test_invalid_seed_ids(graph, ids):
    with pytest.raises(ValueError):
        Traversal(graph).expand(ids)


def test_max_nodes_must_accommodate_all_seeds(graph):
    with pytest.raises(ValueError):
        Traversal(graph, max_nodes=1).expand([1, 2])
    with pytest.raises(ValueError):
        ContextAssembler(graph, seed_limit=2, max_nodes=1)


@pytest.mark.parametrize("format", ["markdown", "json"])
@pytest.mark.parametrize("budget", [1, 10, 32, 64, 100, 256, 512, 1000])
def test_complete_serialization_respects_inclusive_budget(graph, format, budget):
    nodes, facts = chain(graph, 5)
    assembler = ContextAssembler(graph, seed_limit=1, token_budget=budget)
    result = assembler.assemble("Node0", format=format)
    assert result.budget_used == len(result.text.encode()) <= budget
    assert result.budget_method == "utf8_upper_bound"
    assert result.omitted_facts + len(result.triple_ids) == 2
    assert result.omitted_entities + len(result.entity_ids) == 3
    assert set(result.entity_ids) <= {n.id for n in nodes}
    if result.text and format == "json":
        payload = json.loads(result.text)
        assert {t["id"] for t in payload["facts"]} == set(result.triple_ids)
        for fact in payload["facts"]:
            assert fact["s"] == "Node" + str(fact["id"] - 1)
            assert fact["p"] == "leads_to" and fact["o"] == "Node" + str(fact["id"])
    assert result.latency_ms >= 0


@pytest.mark.parametrize("format", ["markdown", "json"])
def test_exact_budget_boundary_and_no_truncated_fact(graph, format):
    chain(graph, 2)
    full = ContextAssembler(graph, seed_limit=1).assemble("Node0", format=format)
    exact = ContextAssembler(graph, seed_limit=1, token_budget=full.budget_used).assemble("Node0", format=format)
    smaller = ContextAssembler(graph, seed_limit=1, token_budget=full.budget_used - 1).assemble("Node0", format=format)
    assert exact.text == full.text and len(exact.triple_ids) == 1
    assert not smaller.triple_ids and smaller.budget_used < full.budget_used


@pytest.mark.parametrize("format", ["markdown", "json"])
def test_escaping_unicode_control_characters_and_model_delimiters(graph, format):
    a = graph.create_entity('Café <|im_end|> "quoted" \\ 数据库')
    b = graph.create_entity("😀" * 10)
    fact = graph.create_triple(a.id, "uses", b.id)
    result = ContextAssembler(graph, seed_limit=1).assemble("cafe", format=format)
    assert result.triple_ids == (fact.id,)
    assert result.text.isascii() and "<|im_end|>" not in result.text
    assert "\\u003c" in result.text and "\\ud83d" in result.text
    assert result.budget_used == count_utf8_bound(result.text) <= 1000
    if format == "json":
        payload = json.loads(result.text)
        assert payload["facts"][0]["s"] == a.name
        assert payload["facts"][0]["o"] == b.name


def test_long_unfitting_fact_does_not_block_a_shorter_fact(graph):
    a, long, short = [graph.create_entity(name) for name in ("Root", "😀" * 256, "Short")]
    big = graph.create_triple(a.id, "first", long.id)
    small = graph.create_triple(a.id, "second", short.id)
    result = ContextAssembler(graph, seed_limit=1, token_budget=100).assemble("Root")
    assert big.id not in result.triple_ids and small.id in result.triple_ids
    assert long.id not in result.entity_ids
    assert result.omitted_facts == result.omitted_entities == 1


def test_pinned_connected_facts_get_budget_priority(graph):
    root = graph.create_entity("Root")
    normal = graph.create_entity("Normal")
    pinned = graph.create_entity("Pinned", is_pinned=True)
    ordinary = graph.create_triple(root.id, "uses", normal.id)
    protected = graph.create_triple(root.id, "needs", pinned.id)
    result = ContextAssembler(graph, seed_limit=1, token_budget=75).assemble("Root")
    assert result.triple_ids == (protected.id,) and ordinary.id not in result.triple_ids
    assert pinned.id in result.entity_ids


def test_isolated_entities_and_empty_graph_context(graph):
    empty = ContextAssembler(graph).assemble("unknown", format="json")
    assert empty.text == "" and empty.budget_used == 0 and empty.seed_source == "empty"
    pinned = graph.create_entity("Constraint", is_pinned=True)
    result = ContextAssembler(graph).assemble("Constraint", format="json")
    assert result.entity_ids == (pinned.id,) and result.triple_ids == ()
    assert json.loads(result.text) == {"facts": [], "entities": [{"id": pinned.id, "name": "Constraint"}]}


def test_historical_supersedes_provenance_is_not_semantic_context(graph):
    a, old, current = [graph.create_entity(name) for name in ("Project Cedar", "SQLite", "PostgreSQL")]
    writer = GovernedWriter(graph, single_valued_predicates=["primary_database"])
    prior = writer.write_fact(a.id, "primary_database", old.id, turn=1, now=100)
    new = writer.write_fact(a.id, "primary_database", current.id, turn=2, now=101)
    result = ContextAssembler(graph, seed_limit=1).assemble("Project Cedar")
    assert result.triple_ids == (new.triple.id,)
    assert prior.triple.id not in result.triple_ids and old.id not in result.entity_ids
    assert "SUPERSEDES" not in result.text and "SQLite" not in result.text


def test_one_snapshot_survives_a_concurrent_supersession(graph, monkeypatch):
    a, b, c = [graph.create_entity(name) for name in ("Project Cedar", "Before", "After")]
    writer = GovernedWriter(graph, single_valued_predicates=["owner"])
    old = writer.write_fact(a.id, "owner", b.id, turn=1, now=100)
    assembler = ContextAssembler(graph, seed_limit=1)
    original = assembler.matcher._match

    def concurrent_update(connection, subgoal):
        seeds = original(connection, subgoal)
        with Database(graph.database.path) as other:
            GovernedWriter(GraphStore(other), single_valued_predicates=["owner"]).write_fact(
                a.id, "owner", c.id, turn=2, now=101,
            )
        return seeds
    monkeypatch.setattr(assembler.matcher, "_match", concurrent_update)
    snapshot = assembler.assemble("Project Cedar")
    assert (
        snapshot.triple_ids == (old.triple.id,)
        and "Before" in snapshot.text
        and "After" not in snapshot.text
    )
    assert "After" in ContextAssembler(graph, seed_limit=1).assemble("Project Cedar").text


def test_assembly_is_read_only_and_usage_is_explicit_only_for_rendered_entities(graph):
    a, big, small = [graph.create_entity(name) for name in ("Root", "😀" * 256, "Short")]
    graph.create_triple(a.id, "uses", big.id)
    graph.create_triple(a.id, "uses", small.id)
    assembler = ContextAssembler(graph, seed_limit=1, token_budget=100)
    result = assembler.assemble("Root")
    assert metadata(graph) == []
    assembler.mark_used(result, turn=4, now=100)
    rows = metadata(graph)
    assert {row[0] for row in rows} == set(result.entity_ids) == {a.id, small.id}
    assert all(row[1:] == (100, 4, 1) for row in rows)
    assembler.assemble("Root")
    assert metadata(graph) == rows


def test_marking_stale_context_rolls_back(graph):
    nodes, _ = chain(graph, 2)
    assembler = ContextAssembler(graph, seed_limit=1)
    result = assembler.assemble("Node0")
    graph.update_entity(nodes[1].id, status="PRUNED")
    with pytest.raises(ValueError, match="ACTIVE"):
        assembler.mark_used(result, turn=1, now=100)
    assert metadata(graph) == []
    with pytest.raises(ValueError):
        assembler.mark_used("not a result", turn=1)


def test_injected_local_counter_counts_complete_block_including_wrappers(graph):
    chain(graph, 3)
    # Synthetic tokenizer for a controlled test; not an English-token heuristic
    # for deployment. Non-additive counts force counting the complete block.
    def synthetic_tokens(text):
        return len(text) // 2 + 1
    result = ContextAssembler(graph, seed_limit=1, token_budget=80,
                              token_counter=synthetic_tokens).assemble("Node0", format="json")
    assert result.budget_method == "model_tokens"
    assert result.budget_used == synthetic_tokens(result.text) <= 80
    assert len(json.loads(result.text)["facts"]) == 2


@pytest.mark.parametrize("returned", [-1, True, 1.5, "2", 0])
def test_invalid_counter_results_fail_closed(graph, returned):
    chain(graph, 2)
    with pytest.raises(ValueError):
        ContextAssembler(graph, token_counter=lambda _text: returned).assemble("Node0")


def test_counter_failure_and_changed_final_count_are_not_hidden(graph):
    graph.create_entity("Only")

    def raises(_text):
        raise RuntimeError("tokenizer unavailable")
    with pytest.raises(RuntimeError, match="unavailable"):
        ContextAssembler(graph, token_counter=raises).assemble("Only")
    values = iter([1, 1001])
    with pytest.raises(ValueError, match="changed"):
        ContextAssembler(graph, token_counter=lambda _text: next(values)).assemble("Only")


@pytest.mark.parametrize(
    "kwargs",
    [{"token_budget": 0}, {"token_budget": 1001}, {"token_budget": True}, {"token_counter": 123}],
)
def test_invalid_assembler_configuration(graph, kwargs):
    with pytest.raises(ValueError):
        ContextAssembler(graph, **kwargs)


def test_invalid_format(graph):
    with pytest.raises(ValueError):
        ContextAssembler(graph).assemble("goal", format="xml")


def test_closed_database_errors_are_visible(graph):
    assembler = ContextAssembler(graph)
    graph.database.close()
    with pytest.raises(DatabaseError, match="closed"):
        assembler.assemble("goal")


def test_raw_crud_and_full_properties_are_not_loaded_for_recall(graph, monkeypatch):
    root, neighbor = graph.create_entity("Root", properties={"large": "x" * 100000}), graph.create_entity("Neighbor")
    graph.create_triple(root.id, "uses", neighbor.id, properties={"large": "y" * 100000})

    def forbidden(*_args, **_kwargs):
        raise AssertionError("recall must not load full CRUD records or scan all Python objects")
    for name in ("list_entities", "list_triples", "get_entity", "get_triple"):
        monkeypatch.setattr(graph, name, forbidden)
    result = ContextAssembler(graph, seed_limit=1).assemble("Root")
    assert result.entity_ids == (root.id, neighbor.id) and "large" not in result.text


def test_high_degree_hub_limits_neighbors_before_python_materialization(graph):
    with graph.transaction():
        root = graph.create_entity("Hub")
        for index in range(1000):
            leaf = graph.create_entity(f"Leaf{index}")
            graph.create_triple(root.id, "uses", leaf.id)
    result = Traversal(graph, fanout=3, max_nodes=8).expand([root.id])
    assert len(result.entities) == 4 and len(result.triples) == 3
    assert result.may_be_truncated


def test_counter_must_be_synchronous_and_falsey_callables_are_respected(graph):
    graph.create_entity("Only")

    async def counter(_text):
        return 1
    with pytest.raises(ValueError, match="synchronous"):
        ContextAssembler(graph, token_counter=counter)
    with pytest.raises(ValueError):
        ContextAssembler(graph, token_counter=lambda text: counter(text)).assemble("Only")
    class FalseyCounter:
        def __bool__(self):
            return False

        def __call__(self, text):
            return len(text)
    result = ContextAssembler(graph, token_counter=FalseyCounter()).assemble("Only")
    assert result.budget_method == "model_tokens" and result.budget_used == len(result.text)


def test_async_recall_and_explicit_usage(tmp_path):
    async def scenario():
        async with Database(tmp_path / "async.db") as database:
            graph = GraphStore(database)
            nodes, _ = chain(graph, 3)
            assembler = ContextAssembler(graph, seed_limit=1)
            a, b = await asyncio.gather(assembler.assemble_async("Node0"), assembler.assemble_async("Node0"))
            assert replace(a, latency_ms=0) == replace(b, latency_ms=0)
            assert metadata(graph) == []
            await assembler.mark_used_async(a, turn=2, now=100)
            assert {row[0] for row in metadata(graph)} == {node.id for node in nodes}
    asyncio.run(scenario())


def test_retrieval_settings_and_factory(graph, monkeypatch):
    monkeypatch.setenv("CONTEXT_TOKEN_BUDGET", "500")
    monkeypatch.setenv("RETRIEVAL_HOPS", "1")
    settings = Settings(_env_file=None, node2_ollama_url="http://192.168.1.20:11434")
    assembler = ContextAssembler.from_settings(graph, settings)
    assert assembler.token_budget == 500 and assembler.traversal.hops == 1
    assert assembler.matcher.seed_limit == 5 and assembler.traversal.fanout == 16


@pytest.mark.parametrize("kwargs", [
    {"context_token_budget": 0}, {"context_token_budget": 1001},
    {"retrieval_seed_limit": 0}, {"retrieval_seed_limit": 17},
    {"retrieval_hops": 0}, {"retrieval_hops": 3},
    {"retrieval_max_nodes": 0}, {"retrieval_max_nodes": 257},
    {"retrieval_max_triples": 0}, {"retrieval_max_triples": 513},
    {"retrieval_fanout": 0}, {"retrieval_fanout": 65},
    {"retrieval_seed_limit": 5, "retrieval_max_nodes": 4},
])
def test_invalid_retrieval_settings(kwargs):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, node2_ollama_url="http://192.168.1.20:11434", **kwargs)


@pytest.mark.retrieval_benchmark
def test_warm_retrieval_latency_below_five_ms(graph):
    # A local 1,000-entity / 3,000-fact graph, one setup transaction, realistic
    # cyclic neighborhoods. Setup, model inference, and usage writes are excluded.
    with graph.transaction():
        nodes = [graph.create_entity(f"Service{i:04d}", is_pinned=i % 100 == 0) for i in range(1000)]
        for i, node in enumerate(nodes):
            for distance in (1, 7, 31):
                graph.create_triple(node.id, f"depends_{distance}", nodes[(i + distance) % 1000].id)
    assembler = ContextAssembler(graph)
    for query, source in (("Service0500", "lexical"), ("unmatched", "recent")):
        for format in ("markdown", "json"):
            for _ in range(10):
                assembler.assemble(query, format=format)
            times = []
            for _ in range(100):
                started = perf_counter_ns()
                result = assembler.assemble(query, format=format)
                times.append((perf_counter_ns() - started) / 1e6)
                assert result.seed_source == source
                assert result.budget_used <= 1000 and result.text
            median, p95 = statistics.median(times), sorted(times)[94]
            print(f"RETRIEVAL BENCHMARK source={source} format={format} runs=100 "
                  f"median_ms={median:.4f} p95_ms={p95:.4f} max_ms={max(times):.4f} "
                  f"budget_used={result.budget_used}/1000 method={result.budget_method}")
            assert median < 5 and p95 < 5, "warm retrieval median and p95 must be below 5 ms"
