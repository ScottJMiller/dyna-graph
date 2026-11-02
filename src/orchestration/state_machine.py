"""Sequential local role cycles with atomic graph/checkpoint synchronization."""

import asyncio
from dataclasses import asdict, dataclass
from enum import StrEnum
import hashlib
import inspect
import json
import re
import time
from typing import Callable
from uuid import uuid4

from src.agents import AgentResult, BaseAgent, default_agents
from src.agents.base_agent import encode, text
from src.extraction import Extractor
from src.governance import GovernedWriter, Pruner
from src.governance.decay import _number
from src.models import AgentMessage, SemanticTriple
from src.retrieval import ContextAssembler, ContextResult
from src.storage.graph_store import GraphStore


class Stage(StrEnum):
    IDLE = "IDLE"
    RECALL = "RECALL"
    GENERATE = "GENERATE"
    EXTRACT = "EXTRACT"
    COMMIT = "COMMIT"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class WorkflowError(RuntimeError):
    """A checkpoint is absent, incompatible, busy, or cannot advance safely."""


@dataclass(frozen=True)
class RunCheckpoint:
    id: str
    task: str
    subgoal: str
    agent_ids: tuple[str, ...]
    config_hash: str
    max_turns: int
    next_step: int
    status: str
    stop_on_verified: bool
    stop_reason: str | None
    last_response: str
    error: str | None
    owner_token: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class TurnCheckpoint:
    run_id: str
    step: int
    governance_turn: int
    agent_id: str
    node: str
    message: AgentMessage
    result: dict
    created_at: str


@dataclass(frozen=True)
class TurnResult:
    checkpoint: TurnCheckpoint
    agent: AgentResult
    context: ContextResult
    extraction: object


def _run_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
        raise ValueError("run_id must have 1-64 ASCII letters, digits, underscores or hyphens")
    return value


def _run(row):
    values = dict(row)
    values["agent_ids"] = tuple(json.loads(values["agent_ids"]))
    values["stop_on_verified"] = bool(values["stop_on_verified"])
    return RunCheckpoint(**values)


def _turn(row):
    values = dict(row)
    values["message"] = AgentMessage.model_validate_json(values["message"])
    values["result"] = json.loads(values["result"])
    return TurnCheckpoint(**values)


def _generation(generation):
    return {"node": generation.node, "model": generation.model, "latency_ms": generation.latency_ms,
            "generated_tokens": generation.generated_tokens, "used_fallback": generation.used_fallback,
            "done_reason": generation.done_reason}


async def _settled(operation):
    """Drain accepted checkpoint writes before propagating cancellation."""
    task = asyncio.create_task(operation)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
        # Retrieve any exception so cancellation never leaves an unobserved task.
        if not task.cancelled():
            task.exception()
        raise


class StateMachine:
    """Caller owns the client/database. Inference is sequential, never in SQL.

    Failed/cancelled runs resume at their next uncommitted step. A RUNNING row
    requires explicit interrupted-process recovery; never recover a live owner.
    Accepted memory updates and the durable turn checkpoint commit together.
    """

    def __init__(self, client, graph: GraphStore, *, agents=None, extractor=None,
                 accept_fact: Callable[[SemanticTriple, AgentMessage], bool] | None = None,
                 clock: Callable[[], float] = time.time):
        self.client, self.graph, self.settings = client, graph, client.settings
        self.agents = tuple(default_agents(client) if agents is None else agents)
        if not 1 <= len(self.agents) <= 16 or any(not isinstance(a, BaseAgent) for a in self.agents):
            raise ValueError("register 1-16 BaseAgent instances")
        if len({a.agent_id for a in self.agents}) != len(self.agents):
            raise ValueError("agent IDs must be unique")
        if any(a.client is not client for a in self.agents):
            raise ValueError("all roles must share the caller-owned client")
        if (accept_fact is not None and (not callable(accept_fact) or inspect.iscoroutinefunction(accept_fact))) or not callable(clock):
            raise ValueError("accept_fact and clock must be callable")
        self.extractor = Extractor(client) if extractor is None else extractor
        if self.extractor.client is not client:
            raise ValueError("extractor must share the caller-owned client")
        self.accept_fact, self.clock = accept_fact, clock
        self.writer = GovernedWriter.from_settings(graph, self.settings)
        self.pruner = Pruner.from_settings(graph, self.settings)
        self.stage = Stage.IDLE
        self._lock = asyncio.Lock()
        configuration = {
            "protocol": 1, "agents": [{"id": a.agent_id, "node": a.node, "system": a.system_prompt(),
                "max_tokens": a.max_tokens, "retries": a.max_retries, "tool_rounds": a.max_tool_rounds,
                "tool_timeout": a.tool_timeout_seconds, "can_complete": a.can_complete} for a in self.agents],
            "nodes": [(self.settings.node_url(n), self.settings.node_model(n)) for n in ("node1", "node2")],
            "policy": self.settings.model_dump(mode="json", include={
                "single_valued_predicates", "context_token_budget", "retrieval_seed_limit", "retrieval_hops",
                "retrieval_max_nodes", "retrieval_max_triples", "retrieval_fanout", "pruning_threshold",
                "pruning_batch_size", "decay_recency_half_life_seconds", "decay_turn_half_life",
                "orchestration_allow_fallback", "orchestration_prune_between_turns"}),
            "accept_fact": None if accept_fact is None else f"{getattr(accept_fact, '__module__', type(accept_fact).__module__)}.{getattr(accept_fact, '__qualname__', type(accept_fact).__qualname__)}",
        }
        self.config_hash = hashlib.sha256(encode(configuration).encode()).hexdigest()

    async def get_checkpoint(self, run_id: str) -> RunCheckpoint:
        _run_id(run_id)
        def read():
            with self.graph.database.connection() as connection:
                row = connection.execute("SELECT * FROM workflow_runs WHERE id=?", (run_id,)).fetchone()
                if row is None:
                    raise WorkflowError("unknown run_id")
                return _run(row)
        return await asyncio.to_thread(read)

    async def history(self, run_id: str, *, after_step: int = 0, limit: int = 100) -> tuple[TurnCheckpoint, ...]:
        _run_id(run_id)
        if type(after_step) is not int or after_step < 0 or type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("after_step must be nonnegative and limit must be 1-1000")
        await self.get_checkpoint(run_id)
        def read():
            with self.graph.database.connection() as connection:
                return tuple(_turn(row) for row in connection.execute(
                    "SELECT * FROM workflow_turns WHERE run_id=? AND step>? ORDER BY step LIMIT ?",
                    (run_id, after_step, limit)))
        return await asyncio.to_thread(read)

    async def start(self, task: str, *, initial_subgoal: str | None = None,
                    max_turns: int | None = None, stop_on_verified: bool = False,
                    run_id: str | None = None) -> RunCheckpoint:
        task = text(task, "task", 500)
        subgoal = text(task if initial_subgoal is None else initial_subgoal, "initial_subgoal", 256)
        max_turns = self.settings.orchestration_max_turns if max_turns is None else max_turns
        if type(max_turns) is not int or not 1 <= max_turns <= 1000 or type(stop_on_verified) is not bool:
            raise ValueError("max_turns must be 1-1000 and stop_on_verified must be boolean")
        run_id = uuid4().hex if run_id is None else _run_id(run_id)
        # Validate role framing before creating a run that cannot dispatch.
        for agent in self.agents:
            agent.context_capacity(task, subgoal)
        def create(_graph):
            with self.graph.database.transaction() as connection:
                connection.execute("INSERT INTO workflow_runs(id,task,subgoal,agent_ids,config_hash,max_turns,stop_on_verified) VALUES(?,?,?,?,?,?,?)",
                    (run_id, task, subgoal, encode([a.agent_id for a in self.agents]), self.config_hash, max_turns, int(stop_on_verified)))
                return _run(connection.execute("SELECT * FROM workflow_runs WHERE id=?", (run_id,)).fetchone())
        return await _settled(self.graph.write_async(create))

    def _compatible(self, checkpoint):
        if checkpoint.config_hash != self.config_hash:
            raise WorkflowError("run configuration differs; resume with its original roles, routes and policy")

    async def step(self, run_id: str) -> TurnResult:
        async with self._lock:
            checkpoint = await self.get_checkpoint(run_id)
            self._compatible(checkpoint)
            if checkpoint.status != "READY":
                raise WorkflowError(f"run is {checkpoint.status}; resume or recover it explicitly")
            step = checkpoint.next_step + 1
            claimed = False
            owner = uuid4().hex
            try:
                def claim(_graph):
                    with self.graph.database.transaction() as connection:
                        changed = connection.execute("UPDATE workflow_runs SET status='RUNNING',error=NULL,owner_token=? "
                            "WHERE id=? AND status='READY' AND next_step=?", (owner, run_id, checkpoint.next_step)).rowcount
                        if changed != 1:
                            raise WorkflowError("run is already owned or its checkpoint changed")
                # Set before awaiting: a cancelled claim still settles durably.
                claimed = True
                await _settled(self.graph.write_async(claim))
                agent = self.agents[checkpoint.next_step % len(self.agents)]
                self.stage = Stage.RECALL
                capacity = min(self.settings.context_token_budget, agent.context_capacity(checkpoint.task, checkpoint.subgoal))
                assembler = ContextAssembler(self.graph, token_budget=capacity,
                    seed_limit=self.settings.retrieval_seed_limit, hops=self.settings.retrieval_hops,
                    max_nodes=self.settings.retrieval_max_nodes, max_triples=self.settings.retrieval_max_triples,
                    fanout=self.settings.retrieval_fanout)
                context = await assembler.assemble_async(checkpoint.subgoal)
                self.stage = Stage.GENERATE
                result = await agent.execute(checkpoint.task, checkpoint.subgoal, context.text, turn=step,
                    allow_fallback=self.settings.orchestration_allow_fallback)
                self.stage = Stage.EXTRACT
                extracted = await self.extractor.extract(result.message, node="node2",
                    allow_fallback=self.settings.orchestration_allow_fallback)
                accepted = []
                for fact in extracted.graph.triples:
                    decision = True if self.accept_fact is None else self.accept_fact(
                        SemanticTriple.model_validate(fact.model_dump()), AgentMessage.model_validate(result.message.model_dump()))
                    if inspect.iscoroutine(decision):
                        decision.close()
                    if type(decision) is not bool:
                        raise ValueError("accept_fact must return a boolean synchronously")
                    if decision:
                        accepted.append(SemanticTriple.model_validate(fact.model_dump()))
                next_subgoal = checkpoint.subgoal if result.next_subgoal is None else text(result.next_subgoal, "next_subgoal", 256)
                self.stage = Stage.COMMIT
                def commit(_graph):
                    with self.graph.database.transaction() as connection:
                        row = connection.execute("SELECT status,next_step,owner_token FROM workflow_runs WHERE id=?", (run_id,)).fetchone()
                        if row["status"] != "RUNNING" or row["next_step"] != checkpoint.next_step or row["owner_token"] != owner:
                            raise WorkflowError("checkpoint ownership changed before commit")
                        turn = connection.execute("SELECT max(COALESCE((SELECT max(last_access_turn) FROM entity_memory),0),"
                            "COALESCE((SELECT max(governance_turn) FROM workflow_turns),0))+1").fetchone()[0]
                        now = _number(self.clock(), "clock")
                        assembler.mark_used(context, turn=turn, now=now)
                        writes = []
                        for fact in accepted:
                            endpoints = []
                            for label in (fact.subject, fact.object):
                                exact = next((e for e in self.graph.search_prefix(label, limit=5)
                                              if e.canonical_name == label.casefold()), None)
                                endpoints.append(exact or self.graph.get_or_create_entity(label))
                            writes.append(self.writer.write_fact(endpoints[0].id, fact.predicate, endpoints[1].id,
                                confidence=fact.confidence, turn=turn, now=now))
                        pruning = self.pruner.run_once(current_turn=turn, now=now) if self.settings.orchestration_prune_between_turns else None
                        verified = checkpoint.stop_on_verified and result.complete
                        done = step == checkpoint.max_turns or verified
                        payload = {"context": asdict(context), "next_subgoal": next_subgoal,
                            "complete": result.complete, "fact_ids": [w.triple.id for w in writes],
                            "superseded_ids": [value for w in writes for value in w.superseded_ids],
                            "pruned_ids": [] if pruning is None else list(pruning.pruned_ids),
                            "extracted_facts": extracted.graph.model_dump(mode="json")["triples"],
                            "agent_generations": [_generation(g) for g in result.generations],
                            "extraction_attempts": [_generation(g) for g in extracted.attempts],
                            "tools": [asdict(t) for t in result.tools]}
                        connection.execute("INSERT INTO workflow_turns(run_id,step,governance_turn,agent_id,node,message,result) VALUES(?,?,?,?,?,?,?)",
                            (run_id, step, turn, agent.agent_id, result.generation.node, result.message.model_dump_json(), encode(payload)))
                        connection.execute("UPDATE workflow_runs SET next_step=?,subgoal=?,status=?,stop_reason=?,last_response=?,error=NULL,owner_token=NULL,"
                            "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",
                            (step, next_subgoal, "COMPLETED" if done else "READY", "verified" if verified else "turn_budget" if done else None,
                             result.message.content, run_id))
                        return _turn(connection.execute("SELECT * FROM workflow_turns WHERE run_id=? AND step=?", (run_id, step)).fetchone())
                saved = await _settled(self.graph.write_async(commit))
                self.stage = Stage.IDLE
                return TurnResult(saved, result, context, extracted)
            except BaseException as exc:
                cancelled = isinstance(exc, asyncio.CancelledError)
                if claimed:
                    def fail(_graph):
                        with self.graph.database.transaction() as connection:
                            connection.execute("UPDATE workflow_runs SET status=?,error=?,owner_token=NULL,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                                "WHERE id=? AND status='RUNNING' AND next_step=? AND owner_token=?",
                                ("CANCELLED" if cancelled else "FAILED", f"{self.stage}: {type(exc).__name__}"[:300], run_id, checkpoint.next_step, owner))
                    await _settled(self.graph.write_async(fail))
                self.stage = Stage.CANCELLED if cancelled else Stage.FAILED
                raise

    async def resume(self, run_id: str, *, recover_interrupted: bool = False) -> RunCheckpoint:
        if type(recover_interrupted) is not bool:
            raise ValueError("recover_interrupted must be boolean")
        checkpoint = await self.get_checkpoint(run_id)
        self._compatible(checkpoint)
        if checkpoint.status == "RUNNING" and not recover_interrupted:
            raise WorkflowError("run is RUNNING; recover only after its previous owner has stopped")
        if checkpoint.status in ("FAILED", "CANCELLED", "RUNNING"):
            def reset(_graph):
                with self.graph.database.transaction() as connection:
                    changed = connection.execute("UPDATE workflow_runs SET status='READY',error=NULL,owner_token=NULL WHERE id=? AND status=? AND next_step=?",
                        (run_id, checkpoint.status, checkpoint.next_step)).rowcount
                    if changed != 1:
                        raise WorkflowError("checkpoint changed during recovery")
            await _settled(self.graph.write_async(reset))
        while True:
            checkpoint = await self.get_checkpoint(run_id)
            if checkpoint.status == "COMPLETED":
                return checkpoint
            await self.step(run_id)

    async def run(self, task: str, **kwargs) -> RunCheckpoint:
        checkpoint = await self.start(task, **kwargs)
        try:
            return await self.resume(checkpoint.id)
        except Exception as exc:
            raise WorkflowError(f"run {checkpoint.id} failed; its checkpoint is retained") from exc
