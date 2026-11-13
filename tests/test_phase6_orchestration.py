"""Offline role/checkpoint failures and opt-in three-role two-node acceptance."""

import asyncio
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
from threading import Event

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError
import pytest

from src.agents import AgentError, AgentReply, BaseAgent, PlannerAgent, ResearchAgent, Tool, VerificationAgent
from src.config import Settings
from src.extraction import Extractor
from src.governance import record_access
from src.network.ollama_client import OllamaClient
from src.orchestration import Stage, StateMachine, WorkflowError
from src.storage.database import Database
from src.storage.graph_store import GraphStore

NOW = 2_000_000_000.0
CONTENT = "Project Cedar uses SQLite."
FACT = {"subject": "Project Cedar", "predicate": "uses", "object": "SQLite"}


def response(request, payload, **fields):
    """Build a synthetic generation while allowing each test to override counters."""
    body = json.loads(request.content)
    return httpx.Response(200, json={"model": body["model"], "response": payload if isinstance(payload, str) else json.dumps(payload),
        "done": True, "done_reason": "stop", "eval_count": 32, "total_duration": 1_000_000, **fields})


class Script:
    """Track role and extraction calls while injecting requested workflow outcomes."""

    def __init__(self, replies=None):
        """Configure role replies and initialize recorded calls for one workflow."""
        self.replies, self.calls, self.actor_calls = replies, [], 0
        self.extraction_error = False

    async def __call__(self, request):
        """Respond from the supplied request and configured faults, never a real model."""
        body = json.loads(request.content)
        self.calls.append((request, body))
        if body["system"].startswith("Extract explicitly"):
            if self.extraction_error:
                return response(request, {"invalid": True})
            message = json.JSONDecoder().raw_decode(body["prompt"].split("\n", 1)[1])[0]["message"]
            facts = []
            for obj in ("SQLite", "PostgreSQL"):
                if f"Project Cedar uses {obj}." in message:
                    facts.append(FACT | {"object": obj})
            return response(request, {"triples": facts})
        index = self.actor_calls
        self.actor_calls += 1
        payload = {"content": CONTENT, "next_subgoal": "Project Cedar", "complete": False}
        if self.replies is not None:
            payload = self.replies[index] if isinstance(self.replies, list) else self.replies(index, body)
        return response(request, payload)


async def resources(tmp_path, settings, script, callback):
    """Run a test callback while its mock client and temporary database remain open."""
    async with OllamaClient(settings, transport=httpx.MockTransport(script)) as client:
        async with Database(tmp_path / "workflow.db") as database:
            graph = GraphStore(database)
            machine = StateMachine(client, graph, clock=lambda: NOW)
            return await callback(client, graph, machine)


# Role execution, checkpoints and governed memory

def test_default_cycle_runs_recall_extract_write_and_durable_checkpoints(tmp_path, settings):
    script = Script()

    async def scenario(client, graph, machine):
        start = await machine.start(CONTENT, run_id="cycle")
        assert start.status == "READY" and start.next_step == 0
        first = await machine.step(start.id)
        assert first.context.text == "" and first.checkpoint.governance_turn == 1
        assert first.agent.message.agent_id == "planner" and first.extraction.generation.node == "node2"
        done = await machine.resume(start.id)
        assert (done.status, done.next_step, done.stop_reason) == ("COMPLETED", 3, "turn_budget")
        history = await machine.history(start.id)
        assert [t.agent_id for t in history] == ["planner", "researcher", "verifier"]
        assert [t.node for t in history] == ["node1", "node2", "node1"]
        assert [t.governance_turn for t in history] == [1, 2, 3]
        assert all(t.result["fact_ids"] == [1] for t in history)
        assert history[1].result["context"]["triple_ids"] == [1]
        assert len(graph.list_entities()) == 2 and len(graph.list_triples(status="ACTIVE")) == 1
        assert len(script.calls) == 6 and machine.stage == Stage.IDLE
        assert (await machine.resume(start.id)).next_step == 3 and len(script.calls) == 6
        assert len(await machine.history(start.id, after_step=1, limit=1)) == 1
        print("CYCLE OK: planner=node1 researcher=node2 verifier=node1 checkpoints=3")
    asyncio.run(resources(tmp_path, settings, script, scenario))


def test_reopen_resumes_next_role_without_replaying_committed_step(tmp_path, settings):
    script = Script()

    async def first(client, graph, machine):
        checkpoint = await machine.start(CONTENT, run_id="resume")
        await machine.step(checkpoint.id)
    asyncio.run(resources(tmp_path, settings, script, first))

    async def second(client, graph, machine):
        done = await machine.resume("resume")
        assert done.next_step == 3
        assert [row.agent_id for row in await machine.history("resume")] == ["planner", "researcher", "verifier"]
        assert len(graph.list_triples()) == 1
        assert len(script.calls) == 6
        print("RESUME OK: committed step retained; next role resumed")
    asyncio.run(resources(tmp_path, settings, script, second))


@pytest.mark.parametrize("stop_on_verified,expected", [(True, 3), (False, 5)])
def test_only_verifier_can_end_a_run_early(tmp_path, settings, stop_on_verified, expected):
    script = Script(lambda _i, _body: {"content": CONTENT, "complete": True})

    async def scenario(client, graph, machine):
        done = await machine.run(CONTENT, max_turns=5, stop_on_verified=stop_on_verified)
        assert done.next_step == expected
        assert done.stop_reason == ("verified" if stop_on_verified else "turn_budget")
        history = await machine.history(done.id)
        assert not history[0].result["complete"] and not history[1].result["complete"]
        assert history[2].result["complete"]
    asyncio.run(resources(tmp_path, settings, script, scenario))


def test_global_governance_turns_survive_new_runs_and_external_usage(tmp_path, settings):
    async def scenario(client, graph, machine):
        item = graph.create_entity("Seed")
        record_access(graph, [item.id], turn=50, now=NOW)
        first = await machine.run(CONTENT, max_turns=1)
        second = await machine.run(CONTENT, max_turns=1)
        a, = await machine.history(first.id)
        b, = await machine.history(second.id)
        assert (a.message.turn, b.message.turn) == (1, 1)
        assert (a.governance_turn, b.governance_turn) == (51, 52)
    asyncio.run(resources(tmp_path, settings, Script(), scenario))


def test_contradictory_update_supersedes_before_next_role_recall(tmp_path, settings):
    script = Script(
        [
            {"content": CONTENT, "next_subgoal": "Project Cedar"},
            {"content": "Project Cedar uses PostgreSQL.", "next_subgoal": "Project Cedar"},
            {"content": "Project Cedar uses PostgreSQL."},
        ],
    )
    configured = settings.model_copy(update={"single_valued_predicates": ("uses",)})

    async def scenario(client, graph, machine):
        done = await machine.run(CONTENT)
        history = await machine.history(done.id)
        assert history[1].result["superseded_ids"] == [1]
        assert "PostgreSQL" in history[2].result["context"]["text"]
        assert "SQLite" not in history[2].result["context"]["text"]
        assert len(graph.list_triples(status="ACTIVE")) == len(graph.list_triples(status="SUPERSEDED")) == 1
        assert len(machine.writer.list_supersessions()) == 1
    asyncio.run(resources(tmp_path, configured, script, scenario))


def test_fact_filter_can_reject_without_fabricating_or_mutating_facts(tmp_path, settings):
    async def scenario(client, graph, machine):
        machine.accept_fact = lambda fact, message: False
        done = await machine.run(CONTENT, max_turns=1)
        assert graph.list_entities() == [] and graph.list_triples() == []
        assert (await machine.history(done.id))[0].result["fact_ids"] == []

        def mutate(fact, message):
            fact.object = "Invented"
            message.content = "invented"
            return True
        machine.accept_fact = mutate
        await machine.run(CONTENT, max_turns=1)
        assert graph.find_entity("Invented") is None and graph.find_entity("SQLite") is not None
    asyncio.run(resources(tmp_path, settings, Script(), scenario))


def test_failed_extraction_does_not_write_partial_memory_and_can_resume(tmp_path, settings):
    script = Script()
    script.extraction_error = True

    async def scenario(client, graph, machine):
        checkpoint = await machine.start(CONTENT, run_id="retry")
        with pytest.raises(Exception, match="extraction failed"):
            await machine.step(checkpoint.id)
        state = await machine.get_checkpoint(checkpoint.id)
        assert state.status == "FAILED" and state.next_step == 0 and state.error.startswith("EXTRACT")
        assert graph.list_entities() == [] and await machine.history(checkpoint.id) == ()
        script.extraction_error = False
        assert (await machine.resume(checkpoint.id)).next_step == 3
    asyncio.run(resources(tmp_path, settings, script, scenario))


# Rollback, cancellation, ownership and recovery

def test_checkpoint_sql_failure_rolls_back_graph_usage_and_provenance(tmp_path, settings):
    async def scenario(client, graph, machine):
        with graph.database.transaction() as connection:
            connection.execute("CREATE TRIGGER fail_checkpoint BEFORE INSERT ON workflow_turns "
                               "BEGIN SELECT RAISE(ABORT,'injected checkpoint failure'); END")
        checkpoint = await machine.start(CONTENT, max_turns=1)
        with pytest.raises(sqlite3.IntegrityError, match="injected"):
            await machine.step(checkpoint.id)
        assert graph.list_entities() == [] and graph.list_triples() == []
        with graph.database.connection() as connection:
            assert connection.execute("SELECT count(*) FROM entity_memory").fetchone()[0] == 0
        assert (await machine.get_checkpoint(checkpoint.id)).next_step == 0
        with graph.database.transaction() as connection:
            connection.execute("DROP TRIGGER fail_checkpoint")
        assert (await machine.resume(checkpoint.id)).status == "COMPLETED"
        print("ROLLBACK OK: graph, usage and turn checkpoint commit together")
    asyncio.run(resources(tmp_path, settings, Script(), scenario))


def test_cancellation_during_generation_leaves_resumable_checkpoint(tmp_path, settings):
    entered = asyncio.Event()
    base = Script()

    async def blocking(request):
        entered.set()
        await asyncio.Event().wait()

    async def scenario():
        async with OllamaClient(settings, transport=httpx.MockTransport(blocking)) as client:
            async with Database(tmp_path / "cancel.db") as db:
                machine = StateMachine(client, GraphStore(db), clock=lambda: NOW)
                checkpoint = await machine.start(CONTENT, max_turns=1)
                task = asyncio.create_task(machine.step(checkpoint.id))
                await asyncio.wait_for(entered.wait(), 3)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                state = await machine.get_checkpoint(checkpoint.id)
                assert state.status == "CANCELLED" and state.next_step == 0
                assert await machine.history(checkpoint.id) == ()
                client._http._transport = httpx.MockTransport(base)
                assert (await machine.resume(checkpoint.id)).next_step == 1
    asyncio.run(scenario())


def test_cancelled_commit_waits_then_preserves_exactly_one_turn(tmp_path, settings, monkeypatch):
    """An accepted write must finish once before cancellation escapes to the caller."""
    entered, release = Event(), Event()

    async def scenario(client, graph, machine):
        checkpoint = await machine.start(CONTENT, max_turns=1)
        original = machine.writer.write_fact

        def slow(*args, **kwargs):
            entered.set()
            if not release.wait(3):
                raise RuntimeError("test failed to release commit")
            return original(*args, **kwargs)
        monkeypatch.setattr(machine.writer, "write_fact", slow)
        task = asyncio.create_task(machine.step(checkpoint.id))
        try:
            for _ in range(100):
                if entered.is_set():
                    break
                await asyncio.sleep(0.01)
            assert entered.is_set()
            task.cancel()
            await asyncio.sleep(0.01)
            assert not task.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (await machine.get_checkpoint(checkpoint.id)).status == "COMPLETED"
        assert len(await machine.history(checkpoint.id)) == 1
        assert (await machine.resume(checkpoint.id)).next_step == 1
    asyncio.run(resources(tmp_path, settings, Script(), scenario))


def test_interrupted_running_recovery_is_explicit_and_config_must_match(tmp_path, settings):
    async def scenario(client, graph, machine):
        checkpoint = await machine.start(CONTENT, max_turns=1)
        with graph.database.transaction() as connection:
            connection.execute(
                "UPDATE workflow_runs SET status='RUNNING',owner_token='old' WHERE id=?",
                (checkpoint.id,),
            )
        with pytest.raises(WorkflowError, match="RUNNING"):
            await machine.resume(checkpoint.id)
        changed = StateMachine(client, graph, agents=[ResearchAgent(client)])
        with pytest.raises(WorkflowError, match="configuration"):
            await changed.resume(checkpoint.id, recover_interrupted=True)
        assert (await machine.resume(checkpoint.id, recover_interrupted=True)).next_step == 1
    asyncio.run(resources(tmp_path, settings, Script(), scenario))


def test_competing_controller_cannot_fail_or_steal_running_owner(tmp_path, settings):
    entered, release = asyncio.Event(), asyncio.Event()
    scripted = Script()

    async def blocking(request):
        if not json.loads(request.content)["system"].startswith("Extract explicitly"):
            entered.set()
            await release.wait()
        return await scripted(request)

    async def scenario(client, graph, machine):
        checkpoint = await machine.start(CONTENT, max_turns=1)
        other = StateMachine(client, graph, clock=lambda: NOW)
        task = asyncio.create_task(machine.step(checkpoint.id))
        await asyncio.wait_for(entered.wait(), 3)
        try:
            with pytest.raises(WorkflowError, match="RUNNING"):
                await other.step(checkpoint.id)
            assert (await machine.get_checkpoint(checkpoint.id)).status == "RUNNING"
        finally:
            release.set()
        await task
    asyncio.run(resources(tmp_path, settings, blocking, scenario))


def test_simultaneous_claim_loser_cannot_change_winners_checkpoint(tmp_path, settings, monkeypatch):
    """A failed competing claim must never release or overwrite the winning owner."""
    entered, release, both = asyncio.Event(), asyncio.Event(), asyncio.Event()
    script = Script()

    async def blocking(request):
        if not json.loads(request.content)["system"].startswith("Extract explicitly"):
            entered.set()
            await release.wait()
        return await script(request)

    async def scenario(client, graph, machine):
        checkpoint = await machine.start(CONTENT, max_turns=1)
        other = StateMachine(client, graph, clock=lambda: NOW)
        count = 0
        for worker in (machine, other):
            original = worker.get_checkpoint

            async def synchronized(run_id, read=original):
                nonlocal count
                state = await read(run_id)
                if state.status == "READY":
                    count += 1
                    if count == 2:
                        both.set()
                    await both.wait()
                return state
            monkeypatch.setattr(worker, "get_checkpoint", synchronized)
        tasks = [asyncio.create_task(worker.step(checkpoint.id)) for worker in (machine, other)]
        await asyncio.wait_for(entered.wait(), 3)
        try:
            for _ in range(100):
                if any(task.done() for task in tasks):
                    break
                await asyncio.sleep(.01)
            with graph.database.connection() as connection:
                assert connection.execute("SELECT status FROM workflow_runs").fetchone()[0] == "RUNNING"
        finally:
            release.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        assert sum(isinstance(result, WorkflowError) for result in results) == 1
        assert len(await machine.history(checkpoint.id)) == 1
        assert (await machine.get_checkpoint(checkpoint.id)).status == "COMPLETED"
    asyncio.run(resources(tmp_path, settings, blocking, scenario))


def test_inference_never_holds_an_open_write_transaction(tmp_path, settings):
    script = Script()

    async def probe(request):
        # An independent connection can acquire the write reservation during
        # every model request, including extraction, because no SQL spans HTTP.
        with sqlite3.connect(tmp_path / "workflow.db", timeout=.1) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.rollback()
        return await script(request)

    async def scenario(client, graph, machine):
        assert (await machine.run(CONTENT, max_turns=1)).next_step == 1
    asyncio.run(resources(tmp_path, settings, probe, scenario))


def test_empty_extracted_graph_is_a_successful_checkpoint(tmp_path, settings):
    async def scenario(client, graph, machine):
        done = await machine.run("Say hello", max_turns=1)
        assert graph.list_entities() == []
        turn, = await machine.history(done.id)
        assert turn.result["extracted_facts"] == [] and turn.result["fact_ids"] == []
    asyncio.run(resources(tmp_path, settings, Script([{"content": "Hello!"}]), scenario))


def test_invalid_fact_filter_result_is_atomic(tmp_path, settings):
    async def scenario(client, graph, machine):
        machine.accept_fact = lambda *_args: 1
        checkpoint = await machine.start(CONTENT, max_turns=1)
        with pytest.raises(ValueError, match="boolean"):
            await machine.step(checkpoint.id)
        assert graph.list_entities() == [] and await machine.history(checkpoint.id) == ()
    asyncio.run(resources(tmp_path, settings, Script(), scenario))


def test_agent_http_fallback_records_actual_node(tmp_path, settings):
    script = Script()

    async def handler(request):
        body = json.loads(request.content)
        if request.url.host == "localhost" and not body["system"].startswith("Extract explicitly"):
            return httpx.Response(503, json={"error": "controlled offline node"})
        return await script(request)

    async def scenario(client, graph, machine):
        done = await machine.run(CONTENT, max_turns=1)
        turn, = await machine.history(done.id)
        assert turn.node == "node2" and turn.agent_id == "planner"
        assert turn.result["agent_generations"][0]["used_fallback"]
    asyncio.run(resources(tmp_path, settings, handler, scenario))


def test_length_stop_requires_correction_before_accepting_content(tmp_path, settings):
    count = 0

    async def handler(request):
        nonlocal count
        count += 1
        return response(request, {"content": CONTENT}, done_reason="length" if count == 1 else "stop")

    async def scenario(client, graph, machine):
        result = await PlannerAgent(client).execute("Check", "Cedar", "", turn=1)
        assert len(result.generations) == 2
    asyncio.run(resources(tmp_path, settings, handler, scenario))


# Input validation and compatibility

@pytest.mark.parametrize("arguments", [{"after_step": -1}, {"limit": 0}, {"limit": 1001}, {"limit": True}])
def test_invalid_checkpoint_history_page(tmp_path, settings, arguments):
    async def scenario(client, graph, machine):
        checkpoint = await machine.start(CONTENT)
        with pytest.raises(ValueError):
            await machine.history(checkpoint.id, **arguments)
    asyncio.run(resources(tmp_path, settings, Script(), scenario))


def test_unknown_runs_and_duplicate_start_ids_are_visible(tmp_path, settings):
    async def scenario(client, graph, machine):
        with pytest.raises(WorkflowError, match="unknown"):
            await machine.get_checkpoint("missing")
        await machine.start(CONTENT, run_id="unique")
        with pytest.raises(sqlite3.IntegrityError):
            await machine.start(CONTENT, run_id="unique")
        assert (await machine.get_checkpoint("unique")).next_step == 0
    asyncio.run(resources(tmp_path, settings, Script(), scenario))


def test_failed_v2_upgrade_rolls_back_version_and_existing_graph(tmp_path, monkeypatch):
    import src.storage.database as module
    path, bad = tmp_path / "old.db", tmp_path / "bad.sql"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE sentinel(value TEXT)")
        connection.execute("INSERT INTO sentinel VALUES('preserved')")
        connection.execute("PRAGMA user_version=2")
    bad.write_text("CREATE TABLE partial(id INTEGER); INVALID SQL;", encoding="utf-8")
    monkeypatch.setattr(module, "SCHEMA_PATH", bad)
    with pytest.raises(sqlite3.OperationalError):
        Database(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert connection.execute("SELECT value FROM sentinel").fetchone()[0] == 'preserved'
        assert not connection.execute("SELECT name FROM sqlite_master WHERE name='partial'").fetchall()


class LookupArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(max_length=32)


def test_registered_tool_validates_executes_and_returns_to_same_role(tmp_path, settings):
    seen = []
    tool = Tool(
        "lookup",
        "Read a supplied local label",
        LookupArgs,
        lambda name: seen.append(name) or {"value": "SQLite"},
    )
    script = Script([{"tool_calls": [{"name": "lookup", "arguments": {"name": "database"}}]}, {"content": CONTENT}])

    async def scenario(client, graph, machine):
        agent = PlannerAgent(client, tools=[tool])
        result = await agent.execute("Check the local label.", "database", "", turn=1)
        assert seen == ["database"] and result.message.content == CONTENT
        assert len(result.generations) == 2 and len(result.tools) == 1
        assert 'TOOL_RESULT' in script.calls[1][1]["prompt"]
        assert result.tools[0].result_json == '{"value":"SQLite"}'
    asyncio.run(resources(tmp_path, settings, script, scenario))


@pytest.mark.parametrize("calls", [
    [{"name": "shell", "arguments": {}}],
    [{"name": "lookup", "arguments": {"name": 1}}],
    [{"name": "lookup", "arguments": {"name": "ok", "extra": True}}],
    [{"name": "lookup", "arguments": {"name": "ok"}}, {"name": "shell", "arguments": {}}],
])
def test_invalid_tool_batches_execute_nothing(tmp_path, settings, calls):
    seen = []
    tool = Tool("lookup", "Read a label", LookupArgs, lambda name: seen.append(name))

    async def scenario(client, graph, machine):
        with pytest.raises(AgentError, match="tool"):
            await PlannerAgent(client, tools=[tool]).execute("Check", "label", "", turn=1)
        assert seen == []
    asyncio.run(resources(tmp_path, settings, Script([{"tool_calls": calls}]), scenario))


@pytest.mark.parametrize("failure", ["rounds", "oversized", "timeout", "handler"])
def test_tool_limits_and_failures_are_visible_without_reexecution(tmp_path, settings, failure):
    async def handler(name):
        if failure == "timeout":
            await asyncio.sleep(1)
        if failure == "handler":
            raise RuntimeError("failed")
        return "x" * (300 if failure == "oversized" else 1)
    tool = Tool("lookup", "Read a label", LookupArgs, handler)
    request = {"tool_calls": [{"name": "lookup", "arguments": {"name": "ok"}}]}

    async def scenario(client, graph, machine):
        with pytest.raises(AgentError, match="tool"):
            await PlannerAgent(client, tools=[tool], tool_timeout_seconds=.01).execute("Check", "label", "", turn=1)
    asyncio.run(resources(tmp_path, settings, Script([request, request]), scenario))


@pytest.mark.parametrize("payload", [
    {}, {"content": " "}, {"content": 1}, {"content": "x" * 2001},
    {"content": "bad\x00text"}, {"content": "\ud800"}, {"content": "ok", "extra": 1},
    {"content": "ok", "complete": "true"}, {"content": "ok", "next_subgoal": " "},
    {"content": "ok", "tool_calls": [{"name": "lookup"}]},
])
def test_invalid_agent_reply_schema(payload):
    with pytest.raises((ValidationError, ValueError)):
        AgentReply.model_validate(payload)


def test_agent_correction_is_bounded_and_duplicate_keys_rejected(tmp_path, settings):
    script = Script(['{"content":"bad","content":"ambiguous"}', {"content": CONTENT}])

    async def scenario(client, graph, machine):
        result = await PlannerAgent(client).execute("Check", "Cedar", "", turn=1)
        assert len(result.generations) == 2 and result.message.content == CONTENT
        assert "Return a complete JSON reply" in script.calls[1][1]["prompt"]
    asyncio.run(resources(tmp_path, settings, script, scenario))


@pytest.mark.parametrize("raw", ['not JSON', '{"content":', '{"content":"ok","extra":true}', 'x' * 16385])
def test_agent_reply_retry_exhaustion_is_explicit(tmp_path, settings, raw):
    async def scenario(client, graph, machine):
        with pytest.raises(AgentError) as caught:
            await PlannerAgent(client).execute("Check", "Cedar", "", turn=1)
        assert caught.value.kind == "reply" and len(caught.value.attempts) == 2
    asyncio.run(resources(tmp_path, settings, Script([raw, raw]), scenario))


@pytest.mark.parametrize("kwargs", [
    {"max_tokens": 31}, {"max_tokens": 513}, {"max_tokens": True},
    {"max_retries": -1}, {"max_retries": 2}, {"max_tool_rounds": 3},
    {"tool_timeout_seconds": 0}, {"tool_timeout_seconds": float('nan')}, {"tools": [1]},
])
def test_invalid_agent_configuration(tmp_path, settings, kwargs):
    async def scenario(client, graph, machine):
        with pytest.raises(ValueError):
            PlannerAgent(client, **kwargs)
    asyncio.run(resources(tmp_path, settings, Script(), scenario))


@pytest.mark.parametrize("kwargs", [
    {"task": ""}, {"task": "x" * 501}, {"initial_subgoal": "x" * 257},
    {"max_turns": 0}, {"max_turns": 1001}, {"max_turns": True},
    {"stop_on_verified": 1}, {"run_id": "../bad"},
])
def test_invalid_workflow_start_makes_no_http_call(tmp_path, settings, kwargs):
    script = Script()

    async def scenario(client, graph, machine):
        with pytest.raises(ValueError):
            await machine.start(**({"task": CONTENT} | kwargs))
        assert script.calls == []
    asyncio.run(resources(tmp_path, settings, script, scenario))


def test_full_prompt_and_output_reservations_shrink_memory_and_fail_closed(tmp_path, settings):
    script = Script()

    async def scenario(client, graph, machine):
        agent = PlannerAgent(client)
        assert agent.context_capacity("Check", "Cedar") > 0
        with pytest.raises(AgentError, match="prompt_budget"):
            await agent.execute("😀" * 400, "goal", "", turn=1)
        with pytest.raises(AgentError):
            await agent.execute("Check", "goal", "x" * 1001, turn=1)
        with pytest.raises(AgentError):
            await agent.execute("Check", "goal", "数据库", turn=1)
        assert script.calls == []
        done = await machine.run(CONTENT, max_turns=1)
        for _request, body in script.calls:
            if body["system"].startswith("Extract explicitly"):
                continue
            assert len((body["system"] + body["prompt"]).encode()) + 128 + body["options"]["num_predict"] <= 2048
        assert (await machine.history(done.id))[0].result["context"]["budget_limit"] <= 1000
    asyncio.run(resources(tmp_path, settings, script, scenario))


def test_abstract_contract_and_custom_role_registration(tmp_path, settings):
    async def scenario(client, graph, machine):
        with pytest.raises(TypeError):
            BaseAgent(client, agent_id="base", node="node1")
        class LocalRole(BaseAgent):
            @property
            def role_prompt(self):
                return "State the supplied current facts."
        role = LocalRole(client, agent_id="specialist", node="node2")
        custom = StateMachine(client, graph, agents=[role], clock=lambda: NOW)
        done = await custom.run(CONTENT, max_turns=2)
        assert [t.agent_id for t in await custom.history(done.id)] == ["specialist", "specialist"]
        for agents in ([], [role, role], [object()]):
            with pytest.raises(ValueError):
                StateMachine(client, graph, agents=agents)
    asyncio.run(resources(tmp_path, settings, Script(), scenario))


@pytest.mark.parametrize("kwargs", [
    {"agent_max_tokens": 0}, {"agent_max_tokens": 513}, {"agent_max_retries": 2},
    {"agent_max_tool_rounds": 3}, {"agent_tool_timeout_seconds": 0},
    {"orchestration_max_turns": 0}, {"orchestration_max_turns": 1001},
])
def test_invalid_orchestration_settings(kwargs):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, node2_ollama_url="http://192.168.1.20:11434", **kwargs)


def test_v2_upgrade_preserves_graph_and_adds_atomic_workflow_tables(tmp_path):
    path = tmp_path / "v2.db"
    schema = (Path(__file__).parents[1] / "src/storage/schema.sql").read_text().split('-- Durable role-cycle')[0]
    with sqlite3.connect(path) as connection:
        connection.executescript(schema)
        connection.execute("INSERT INTO entities(name,canonical_name,is_pinned) VALUES('Legacy','legacy',1)")
        connection.execute("PRAGMA user_version=2")
    with Database(path) as database:
        graph = GraphStore(database)
        assert graph.get_entity(1).is_pinned and graph.search_prefix("leg")
        with database.connection() as connection:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
            assert connection.execute("SELECT count(*) FROM workflow_runs").fetchone()[0] == 0
            assert connection.execute("PRAGMA integrity_check").fetchone()[0] == 'ok'
            assert not connection.execute("PRAGMA foreign_key_check").fetchall()


# Opt-in live LAN acceptance (skipped by default)

@pytest.mark.lan
@pytest.mark.both_nodes
def test_lan_three_agent_cycle_across_both_nodes(tmp_path):
    async def scenario():
        configured = Settings().model_copy(update={"orchestration_allow_fallback": False})
        async with OllamaClient(configured) as client:
            for node in ("node1", "node2"):
                inventory = await client.list_models(node)
                assert (
                    any(m.name == configured.node_model(node) and m.quantization_level == "Q4_K_M" for m in inventory.models)
                )
            async with Database(tmp_path / "live-cycle.db") as database:
                machine = StateMachine(client, GraphStore(database))
                checkpoint = await machine.run(
                    'Project Cedar uses SQLite. In content, repeat exactly "Project Cedar uses SQLite." for each role.',
                    initial_subgoal="Confirm Project Cedar uses SQLite.", max_turns=3)
                history = await machine.history(checkpoint.id)
                assert [t.agent_id for t in history] == ["planner", "researcher", "verifier"]
                assert [t.node for t in history] == ["node1", "node2", "node1"]
                assert checkpoint.status == "COMPLETED" and checkpoint.next_step == 3
                for turn in history:
                    assert turn.result["fact_ids"], turn.message.content
                    assert all(g["node"] == "node2" and not g["used_fallback"] for g in turn.result["extraction_attempts"])
                    assert all(not g["used_fallback"] for g in turn.result["agent_generations"])
                    assert turn.result["context"]["budget_used"] <= 1000
                    print(f"CYCLE step={turn.step} agent={turn.agent_id} node={turn.node} "
                          f"extraction_node=node2 facts={len(turn.result['fact_ids'])} "
                          f"context_budget={turn.result['context']['budget_used']}/1000")
                triples = machine.graph.list_triples(status="ACTIVE")
                facts = {(
                    machine.graph.get_entity(t.subject_id).canonical_name,
                    t.predicate,
                    machine.graph.get_entity(t.object_id).canonical_name,
                ) for t in triples}
                assert facts == {("project cedar", "uses", "sqlite")}, facts
                assert history[1].result["context"]["triple_ids"]
                print("TWO-NODE CYCLE OK: 3 committed turns; direct node routes; active fact preserved")
    asyncio.run(scenario())
