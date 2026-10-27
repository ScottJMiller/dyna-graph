"""Bounded, atomic soft-pruning and an explicitly started asyncio daemon."""

import asyncio
from dataclasses import dataclass
import time
from typing import Callable

from src.governance.decay import DEGREE_SQL, DecayPolicy, _count, _number, _usage
from src.storage.graph_store import GraphStore


@dataclass(frozen=True)
class PruningResult:
    scanned: int
    pruned_ids: tuple[int, ...]
    next_after_id: int


class Pruner:
    def __init__(self, graph: GraphStore, *, policy: DecayPolicy | None = None,
                 threshold: float = 0.2, batch_size: int = 100) -> None:
        _number(threshold, "threshold")
        if threshold > 1:
            raise ValueError("threshold must be in the range 0-1")
        if type(batch_size) is not int or not 1 <= batch_size <= 1000:
            raise ValueError("batch_size must be an integer in the range 1-1000")
        self.graph, self.policy = graph, policy or DecayPolicy()
        self.threshold, self.batch_size = threshold, batch_size

    @classmethod
    def from_settings(cls, graph: GraphStore, settings) -> "Pruner":
        return cls(graph, policy=DecayPolicy.from_settings(settings), threshold=settings.pruning_threshold,
                   batch_size=settings.pruning_batch_size)

    def run_once(self, *, current_turn: int, now: float | None = None, after_id: int = 0) -> PruningResult:
        """Scan at most batch_size active entities from a stable pre-prune snapshot.

        Return a cursor for the next batch (zero starts another sweep). A leaf
        has at most one active incident fact. Pins protect themselves and both
        endpoints of their active facts. No record is physically deleted.
        """
        _count(current_turn, "current_turn")
        _count(after_id, "after_id")
        now = time.time() if now is None else _number(now, "now")
        with self.graph.database.transaction() as connection:
            rows = connection.execute(
                f"SELECT e.id,e.created_at,e.is_pinned,m.*,({DEGREE_SQL}) AS degree, "
                "EXISTS(SELECT 1 FROM triples t JOIN entities p ON "
                "p.id=CASE WHEN t.subject_id=e.id THEN t.object_id ELSE t.subject_id END "
                "WHERE (t.subject_id=e.id OR t.object_id=e.id) AND t.status='ACTIVE' "
                "AND p.status='ACTIVE' AND p.is_pinned=1) AS pinned_neighbor "
                "FROM entities e LEFT JOIN entity_memory m ON m.entity_id=e.id "
                "WHERE e.status='ACTIVE' AND e.id>? ORDER BY e.id LIMIT ?",
                (after_id, self.batch_size),
            ).fetchall()
            pruned = []
            for row in rows:
                if row["is_pinned"] or row["pinned_neighbor"] or row["degree"] > 1:
                    continue
                score = self.policy.score(_usage(row), degree=row["degree"], now=now, current_turn=current_turn)
                if score.retention < self.threshold:
                    pruned.append(row["id"])
            # Apply only after every score has been calculated: one batch cannot
            # peel successively through a chain or observe its own partial work.
            for entity_id in pruned:
                self.graph.update_entity(entity_id, status="PRUNED")
                connection.execute(
                    "UPDATE triples SET status='PRUNED',updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                    "WHERE status='ACTIVE' AND (subject_id=? OR object_id=?)", (entity_id, entity_id),
                )
            cursor = rows[-1]["id"] if len(rows) == self.batch_size else 0
            return PruningResult(len(rows), tuple(pruned), cursor)

    async def run_once_async(self, **kwargs) -> PruningResult:
        return await self.graph.write_async(lambda _graph: self.run_once(**kwargs))


class PruningDaemon:
    """Caller-owned maintenance task; stop it before closing its database.

    A failed pass stops the task and is re-raised by stop(). Shutdown waits for
    the accepted write rather than cancelling it. Turn supply belongs to the
    caller; this module does not schedule agents or advance workflow turns.
    """

    def __init__(self, pruner: Pruner, turn_provider: Callable[[], int], *,
                 interval_seconds: float = 60, clock: Callable[[], float] = time.time) -> None:
        _number(interval_seconds, "interval_seconds", positive=True)
        if not callable(turn_provider) or not callable(clock):
            raise ValueError("turn_provider and clock must be callable")
        self.pruner, self.turn_provider = pruner, turn_provider
        self.interval_seconds, self.clock = interval_seconds, clock
        self.last_result: PruningResult | None = None
        self._task: asyncio.Task | None = None
        self._stop: asyncio.Event | None = None

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("stop the previous daemon task before restarting")
        loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        self._task = loop.create_task(self._run(), name="dynagraph-pruning")

    async def _run(self) -> None:
        after_id = 0
        while not self._stop.is_set():
            self.last_result = await self.pruner.run_once_async(
                current_turn=self.turn_provider(), now=self.clock(), after_id=after_id,
            )
            after_id = self.last_result.next_after_id
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_seconds)
            except TimeoutError:
                pass

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        task = self._task
        try:
            await asyncio.shield(task)
        finally:
            if task.done():
                self._task = None

    async def __aenter__(self) -> "PruningDaemon":
        self.start()
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.stop()
