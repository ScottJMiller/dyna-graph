"""Four bounded retention factors with explicit time and turn inputs."""

from dataclasses import dataclass
from datetime import datetime
import math
import sqlite3
import time

from src.storage.graph_store import GraphStore, validate_id


def validate_number(value: float, name: str, *, positive: bool = False) -> float:
    """Validate a finite nonnegative number, optionally requiring positivity."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    if value < 0 or (positive and value == 0):
        raise ValueError(f"{name} must be {'positive' if positive else 'nonnegative'}")
    return float(value)


def validate_count(value: int, name: str) -> int:
    """Require a nonnegative integer counter, excluding booleans."""
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")
    return value


@dataclass(frozen=True)
class MemoryUsage:
    last_access_at: float
    last_access_turn: int = 0
    access_count: int = 0

    def __post_init__(self) -> None:
        validate_number(self.last_access_at, "last_access_at")
        validate_count(self.last_access_turn, "last_access_turn")
        validate_count(self.access_count, "access_count")


@dataclass(frozen=True)
class DecayScore:
    recency: float
    frequency: float
    centrality: float
    turn_decay: float
    retention: float


@dataclass(frozen=True)
class DecayPolicy:
    recency_half_life_seconds: float = 86400.0
    turn_half_life: float = 10.0
    frequency_scale: float = 4.0
    centrality_scale: float = 4.0
    weights: tuple[float, float, float, float] = (0.35, 0.20, 0.15, 0.30)

    def __post_init__(self) -> None:
        for name in ("recency_half_life_seconds", "turn_half_life", "frequency_scale", "centrality_scale"):
            validate_number(getattr(self, name), name, positive=True)
        if not isinstance(self.weights, tuple) or len(self.weights) != 4:
            raise ValueError("weights must be a tuple of four nonnegative numbers")
        for weight in self.weights:
            validate_number(weight, "weight")
        if not math.isclose(sum(self.weights), 1.0, rel_tol=0, abs_tol=1e-12):
            raise ValueError("weights must sum to 1")

    @classmethod
    def from_settings(cls, settings) -> "DecayPolicy":
        return cls(recency_half_life_seconds=settings.decay_recency_half_life_seconds,
                   turn_half_life=settings.decay_turn_half_life)

    def score(self, usage: MemoryUsage, *, degree: int, now: float, current_turn: int) -> DecayScore:
        validate_number(now, "now")
        validate_count(current_turn, "current_turn")
        validate_count(degree, "degree")
        if now < usage.last_access_at or current_turn < usage.last_access_turn:
            raise ValueError("time and turn must not precede the last recorded access")
        recency = 2.0 ** (-(now - usage.last_access_at) / self.recency_half_life_seconds)
        frequency = recency * usage.access_count / (usage.access_count + self.frequency_scale)
        centrality = min(1.0, degree / self.centrality_scale)
        turn_decay = 2.0 ** (-(current_turn - usage.last_access_turn) / self.turn_half_life)
        factors = (recency, frequency, centrality, turn_decay)
        return DecayScore(*factors, retention=min(1.0, sum(w * f for w, f in zip(self.weights, factors))))


# Count active incident fact edges, once for self-loops. Inactive endpoints and
# provenance edges do not contribute to semantic centrality or leaf eligibility.
DEGREE_SQL = """
    SELECT COUNT(*) FROM triples t
    JOIN entities s ON s.id=t.subject_id AND s.status='ACTIVE'
    JOIN entities o ON o.id=t.object_id AND o.status='ACTIVE'
    WHERE t.status='ACTIVE' AND (t.subject_id=e.id OR t.object_id=e.id)
"""


def usage_from_row(row: sqlite3.Row) -> MemoryUsage:
    """Read usage metadata, falling back to creation time for older entities."""
    if row["last_access_at"] is None:
        # Existing Phase 2 entities need no destructive rewrite or eager backfill.
        return MemoryUsage(datetime.fromisoformat(row["created_at"]).timestamp())
    return MemoryUsage(row["last_access_at"], row["last_access_turn"], row["access_count"])


def record_access(graph: GraphStore, entity_ids, *, turn: int, now: float | None = None) -> None:
    """Record one use per distinct ACTIVE entity, atomically; never record a scan.

    Callers decide when actual use occurs. At most 1,000 input IDs are accepted.
    Explicit timestamps are Unix seconds; per-entity time and turn are monotonic.
    """
    validate_count(turn, "turn")
    now = time.time() if now is None else validate_number(now, "now")
    if not isinstance(entity_ids, (list, tuple, set, frozenset)) or len(entity_ids) > 1000:
        raise ValueError("entity_ids must be a collection of at most 1000 IDs")
    ids = sorted({validate_id(entity_id) for entity_id in entity_ids})
    with graph.database.transaction() as connection:
        for entity_id in ids:
            row = connection.execute(
                "SELECT e.status, m.* FROM entities e LEFT JOIN entity_memory m ON m.entity_id=e.id WHERE e.id=?",
                (entity_id,),
            ).fetchone()
            if row is None or row["status"] != "ACTIVE":
                raise ValueError("access requires an existing ACTIVE entity")
            if row["last_access_at"] is not None and (now < row["last_access_at"] or turn < row["last_access_turn"]):
                raise ValueError("time and turn must not precede the last recorded access")
            connection.execute(
                "INSERT INTO entity_memory(entity_id,last_access_at,last_access_turn,access_count) VALUES(?,?,?,1) "
                "ON CONFLICT(entity_id) DO UPDATE SET last_access_at=excluded.last_access_at, "
                "last_access_turn=excluded.last_access_turn, access_count=entity_memory.access_count+1",
                (entity_id, now, turn),
            )


def score_entity(graph: GraphStore, entity_id: int, *, current_turn: int,
                 now: float | None = None, policy: DecayPolicy | None = None) -> DecayScore:
    """Read one consistent snapshot without incrementing frequency."""
    now = time.time() if now is None else now
    with graph.database.connection() as connection:
        row = connection.execute(
            f"SELECT e.created_at, m.*, ({DEGREE_SQL}) AS degree FROM entities e "
            "LEFT JOIN entity_memory m ON m.entity_id=e.id WHERE e.id=? AND e.status='ACTIVE'", (validate_id(entity_id),),
        ).fetchone()
        if row is None:
            raise ValueError("scoring requires an existing ACTIVE entity")
        return (policy or DecayPolicy()).score(usage_from_row(row), degree=row["degree"], now=now, current_turn=current_turn)
