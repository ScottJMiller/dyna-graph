"""Atomic latest-assertion-wins policy for explicitly single-valued predicates."""

from dataclasses import dataclass
import time

from src.governance.decay import validate_count, validate_number, record_access
from src.storage.graph_store import GraphStore, Triple, validate_confidence, validate_id, serialize_properties, normalize_text


@dataclass(frozen=True)
class SupersessionEdge:
    id: int
    newer_triple_id: int
    older_triple_id: int
    relation: str
    asserted_at: float
    turn: int


@dataclass(frozen=True)
class WriteResult:
    triple: Triple
    superseded_ids: tuple[int, ...]
    edges: tuple[SupersessionEdge, ...]


class GovernedWriter:
    """Use this entry point for policy enforcement; raw CRUD remains policy-free.

    No predicates are single-valued by default. Fact-row IDs are distinct from
    entity IDs: explicit SUPERSEDES edges live in their own FK-checked table.
    Reasserting a historical SPO reuses its row; event IDs/time/turn preserve the
    order of reversals, so the provenance table is an event log, not a DAG.
    """

    def __init__(self, graph: GraphStore, *, single_valued_predicates=()) -> None:
        if not isinstance(single_valued_predicates, (list, tuple, set, frozenset)):
            raise ValueError("single_valued_predicates must be a collection")
        self.graph = graph
        self.single_valued_predicates = frozenset(
            normalize_text(predicate, "predicate").casefold() for predicate in single_valued_predicates
        )

    @classmethod
    def from_settings(cls, graph: GraphStore, settings) -> "GovernedWriter":
        return cls(graph, single_valued_predicates=settings.single_valued_predicates)

    def write_fact(self, subject_id: int, predicate: str, object_id: int, *, turn: int,
                   now: float | None = None, confidence: float = 1.0, properties: dict | None = None) -> WriteResult:
        subject_id, object_id = validate_id(subject_id), validate_id(object_id)
        predicate = normalize_text(predicate, "predicate").casefold()
        validate_count(turn, "turn")
        now = time.time() if now is None else validate_number(now, "now")
        validate_confidence(confidence)
        serialize_properties(properties)
        with self.graph.database.transaction() as connection:
            for entity_id in {subject_id, object_id}:
                entity = self.graph.get_entity(entity_id)
                if entity is None:
                    raise ValueError("facts require existing subject and object entities")
                if entity.status == "PRUNED":
                    self.graph.update_entity(entity_id, status="ACTIVE")
            existing = connection.execute(
                "SELECT id FROM triples WHERE subject_id=? AND predicate=? AND object_id=?",
                (subject_id, predicate, object_id),
            ).fetchone()
            stale = []
            if predicate in self.single_valued_predicates:
                stale = connection.execute(
                    "SELECT id FROM triples WHERE subject_id=? AND predicate=? AND object_id<>? "
                    "AND status='ACTIVE' ORDER BY id", (subject_id, predicate, object_id),
                ).fetchall()
            for row in stale:
                self.graph.update_triple(row["id"], status="SUPERSEDED")
            if existing is None:
                fact = self.graph.create_triple(subject_id, predicate, object_id,
                                               confidence=confidence, properties=properties)
            else:
                fact = self.graph.update_triple(existing["id"], status="ACTIVE", confidence=confidence,
                                               properties=properties)
            edges = []
            for row in stale:
                cursor = connection.execute(
                    "INSERT INTO supersession_edges(newer_triple_id,older_triple_id,asserted_at,turn) "
                    "VALUES(?,?,?,?) RETURNING *", (fact.id, row["id"], now, turn),
                )
                edges.append(SupersessionEdge(**dict(cursor.fetchall()[0])))
                cursor.close()
            record_access(self.graph, (subject_id, object_id), turn=turn, now=now)
            return WriteResult(fact, tuple(row["id"] for row in stale), tuple(edges))

    async def write_fact_async(self, *args, **kwargs) -> WriteResult:
        return await self.graph.write_async(lambda _graph: self.write_fact(*args, **kwargs))

    def list_supersessions(self, *, after_id: int = 0, limit: int = 100) -> list[SupersessionEdge]:
        validate_count(after_id, "after_id")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("limit must be an integer in the range 1-1000")
        with self.graph.database.connection() as connection:
            return [SupersessionEdge(**dict(row)) for row in connection.execute(
                "SELECT * FROM supersession_edges WHERE id>? ORDER BY id LIMIT ?", (after_id, limit),
            )]
