"""One/two-hop undirected discovery with bounded recursive SQL expansion."""

from dataclasses import dataclass
import json
import sqlite3

from src.retrieval.seed_matcher import MAX_LABEL_CHARS, _limit
from src.storage.graph_store import GraphStore, _id


@dataclass(frozen=True)
class RetrievedEntity:
    id: int
    name: str
    depth: int
    is_pinned: bool


@dataclass(frozen=True)
class RetrievedTriple:
    id: int
    subject_id: int
    predicate: str
    object_id: int
    confidence: float


@dataclass(frozen=True)
class Neighborhood:
    entities: tuple[RetrievedEntity, ...]
    triples: tuple[RetrievedTriple, ...]
    may_be_truncated: bool


class Traversal:
    """Discover both edge directions, preserving stored fact direction.

    Per-direction fan-out and recursive row caps prevent hubs and cycles from
    expanding without bound. Only active facts with active endpoints and labels
    within the extraction schema's 256-character ceiling are traversable.
    Python receives a bounded result; no whole-graph adjacency structure is built.
    """

    def __init__(self, graph: GraphStore, *, hops: int = 2, max_nodes: int = 64,
                 max_triples: int = 128, fanout: int = 16) -> None:
        self.graph = graph
        self.hops = _limit(hops, "hops", 2)
        self.max_nodes = _limit(max_nodes, "max_nodes", 256)
        self.max_triples = _limit(max_triples, "max_triples", 512)
        self.fanout = _limit(fanout, "fanout", 64)

    def _seeds(self, entity_ids) -> tuple[int, ...]:
        if not isinstance(entity_ids, (list, tuple, set, frozenset)) or len(entity_ids) > 16:
            raise ValueError("seed IDs must be a collection of at most 16 IDs")
        ids = tuple(sorted({_id(value) for value in entity_ids}))
        if len(ids) > self.max_nodes:
            raise ValueError("max_nodes must accommodate every supplied seed")
        return ids

    def expand(self, entity_ids) -> Neighborhood:
        ids = self._seeds(entity_ids)
        with self.graph.database.connection() as connection:
            return self._expand(connection, ids)

    def _expand(self, connection: sqlite3.Connection, ids: tuple[int, ...]) -> Neighborhood:
        if not ids:
            return Neighborhood((), (), False)
        parameters = {f"seed{i}": value for i, value in enumerate(ids)}
        parameters.update(hops=self.hops, nodes=self.max_nodes, fanout=self.fanout,
                          labels=MAX_LABEL_CHARS, walk_limit=self.max_nodes * (self.hops + 1))
        placeholders = ",".join(f":seed{i}" for i in range(len(ids)))
        # Correlated, indexed LIMITs bound each direction BEFORE populating the
        # recursive queue. UNION deduplicates (node,depth); depth <= 2 prevents
        # cycles from recursing indefinitely. ORDER BY depth makes this BFS.
        adjacency = """
            SELECT id FROM (
                SELECT t.id FROM triples t JOIN entities other ON other.id=t.object_id
                WHERE t.subject_id=w.id AND t.status='ACTIVE' AND other.status='ACTIVE'
                  AND instr(other.name,char(0))=0 AND length(other.name)<=:labels AND instr(t.predicate,char(0))=0 AND length(t.predicate)<=:labels
                ORDER BY t.id LIMIT :fanout
            ) UNION SELECT id FROM (
                SELECT t.id FROM triples t JOIN entities other ON other.id=t.subject_id
                WHERE t.object_id=w.id AND t.status='ACTIVE' AND other.status='ACTIVE'
                  AND instr(other.name,char(0))=0 AND length(other.name)<=:labels AND instr(t.predicate,char(0))=0 AND length(t.predicate)<=:labels
                ORDER BY t.id LIMIT :fanout
            )
        """
        rows = connection.execute(f"""
            WITH RECURSIVE walk(id,depth) AS (
                SELECT id,0 FROM entities WHERE id IN ({placeholders})
                    AND status='ACTIVE' AND instr(name,char(0))=0 AND length(name)<=:labels
                UNION
                SELECT CASE WHEN t.subject_id=w.id THEN t.object_id ELSE t.subject_id END,w.depth+1
                FROM walk w JOIN triples t ON t.id IN ({adjacency})
                WHERE w.depth<:hops ORDER BY 2,1 LIMIT :walk_limit
            ), nearest AS (
                SELECT id,min(depth) AS depth FROM walk GROUP BY id ORDER BY depth,id LIMIT :nodes
            )
            SELECT e.id,e.name,e.is_pinned,n.depth,(SELECT COUNT(*) FROM walk) AS walk_rows,
                CASE WHEN n.depth<:hops THEN (
                    EXISTS(SELECT 1 FROM triples t JOIN entities o ON o.id=t.object_id
                        WHERE t.subject_id=e.id AND t.status='ACTIVE' AND o.status='ACTIVE'
                          AND instr(o.name,char(0))=0 AND length(o.name)<=:labels AND instr(t.predicate,char(0))=0 AND length(t.predicate)<=:labels
                        ORDER BY t.id LIMIT 1 OFFSET :fanout)
                    OR EXISTS(SELECT 1 FROM triples t JOIN entities s ON s.id=t.subject_id
                        WHERE t.object_id=e.id AND t.status='ACTIVE' AND s.status='ACTIVE'
                          AND instr(s.name,char(0))=0 AND length(s.name)<=:labels AND instr(t.predicate,char(0))=0 AND length(t.predicate)<=:labels
                        ORDER BY t.id LIMIT 1 OFFSET :fanout)
                ) ELSE 0 END AS fanout_limited
            FROM nearest n JOIN entities e ON e.id=n.id ORDER BY n.depth,e.id
        """, parameters).fetchall()
        if not rows:
            return Neighborhood((), (), False)
        entities = tuple(RetrievedEntity(row["id"], row["name"], row["depth"], bool(row["is_pinned"])) for row in rows)
        # Return only edges whose endpoints both survived the bounded BFS. JSON1
        # carries the small node/depth set without SQLite variable-limit issues.
        nodes = json.dumps([[item.id, item.depth] for item in entities], separators=(",", ":"))
        facts = connection.execute("""
            WITH selected AS MATERIALIZED (
                SELECT value->>0 AS id,value->>1 AS depth FROM json_each(?)
            )
            SELECT t.id,t.subject_id,t.predicate,t.object_id,t.confidence
            FROM selected a JOIN triples t ON t.subject_id=a.id AND t.status='ACTIVE'
            JOIN selected b ON b.id=t.object_id
            JOIN entities s ON s.id=t.subject_id JOIN entities o ON o.id=t.object_id
            WHERE instr(t.predicate,char(0))=0 AND length(t.predicate)<=?
            ORDER BY (s.is_pinned OR o.is_pinned) DESC,min(a.depth,b.depth),max(a.depth,b.depth),t.id
            LIMIT ?
        """, (nodes, MAX_LABEL_CHARS, self.max_triples + 1)).fetchall()
        limited = (len(entities) == self.max_nodes or rows[0]["walk_rows"] >= parameters["walk_limit"]
                   or any(row["fanout_limited"] for row in rows) or len(facts) > self.max_triples)
        return Neighborhood(entities, tuple(RetrievedTriple(**dict(row)) for row in facts[:self.max_triples]), limited)
