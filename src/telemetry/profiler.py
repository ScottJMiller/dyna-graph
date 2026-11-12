"""Measured inference usage and graph snapshots for committed workflow turns."""

import os
from pathlib import Path
from time import perf_counter
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Count = Annotated[int, Field(ge=0)]
Milliseconds = Annotated[float, Field(ge=0)]


class TurnMetrics(BaseModel):
    """CSV schema v1. Missing Ollama counters remain unknown, never zero-filled.

    turn_work_ms ends at the in-transaction sample, before the final checkpoint
    insert and SQLite COMMIT. Generation latency includes successful responses,
    corrective generations and failed fallback requests in this committed turn.
    Failed/uncommitted turns are absent; this is not a billing/work audit log.
    An explicitly recorded reply rejection is a committed turn: its attempted
    role usage is included and its skipped response extraction has zero calls.
    """

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid", allow_inf_nan=False,
                              revalidate_instances="always")
    schema_version: int = Field(default=1, ge=1, le=1)
    run_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    step: int = Field(ge=1, le=1000)
    governance_turn: int = Field(ge=1)
    agent_id: str = Field(min_length=1, max_length=64)
    node: Literal["node1", "node2"]
    observed_at: Annotated[float, Field(ge=0)]
    turn_work_ms: Milliseconds
    agent_calls: Count
    extraction_calls: Count
    fallback_attempts: Count
    agent_prompt_tokens: Count | None
    extraction_prompt_tokens: Count | None
    prompt_tokens: Count | None
    generated_tokens: Count | None
    agent_latency_ms: Milliseconds
    extraction_latency_ms: Milliseconds
    generation_latency_ms: Milliseconds
    context_budget_used: Count
    context_budget_limit: int = Field(ge=1, le=1000)
    context_budget_method: Literal["utf8_upper_bound", "model_tokens"]
    active_nodes: Count
    active_edges: Count
    process_rss_bytes: Count | None
    rss_source: Literal["linux_proc_statm", "unavailable"]

    @model_validator(mode="after")
    def consistent(self):
        if self.context_budget_used > self.context_budget_limit:
            raise ValueError("context budget exceeded")
        counts = (self.agent_prompt_tokens, self.extraction_prompt_tokens)
        total = None if None in counts else sum(counts)
        if self.prompt_tokens != total:
            raise ValueError("prompt total must match known role and extraction counts")
        if (self.process_rss_bytes is None) != (self.rss_source == "unavailable"):
            raise ValueError("RSS value and source disagree")
        if abs(self.generation_latency_ms - self.agent_latency_ms - self.extraction_latency_ms) > 0.000001:
            raise ValueError("generation latency must equal role plus extraction latency")
        return self


def process_rss() -> tuple[int | None, str]:
    """Current coordinator Python RSS on Linux; excludes Ollama/remote memory."""
    try:
        resident_pages = int(Path("/proc/self/statm").read_text(encoding="ascii").split()[1])
        page_size = os.sysconf("SC_PAGE_SIZE")
        if resident_pages < 0 or page_size <= 0:
            raise ValueError("invalid resident memory sample")
        return resident_pages * page_size, "linux_proc_statm"
    except (OSError, ValueError, IndexError, AttributeError):
        return None, "unavailable"


def _total(generations, name):
    values = [getattr(g, name) for g in generations]
    return None if any(v is None for v in values) else sum(values)


class TelemetryProfiler:
    """Sample graph counts in the same transaction as the accepted memory update.

    No polling thread, external service, model inference, or accumulating history
    is created. SQLite's existing JSON checkpoint stores the immutable record.
    """

    def __init__(self, *, timer=perf_counter, rss_reader=process_rss):
        self.timer, self.rss_reader = timer, rss_reader

    def capture(self, connection, *, run_id, step, governance_turn, agent_id,
                node, observed_at, started, agent, extraction, context) -> TurnMetrics:
        nodes = connection.execute("SELECT count(*) FROM entities WHERE status='ACTIVE'").fetchone()[0]
        edges = connection.execute("SELECT count(*) FROM triples t JOIN entities s ON s.id=t.subject_id "
            "JOIN entities o ON o.id=t.object_id WHERE t.status='ACTIVE' AND s.status='ACTIVE' AND o.status='ACTIVE'").fetchone()[0]
        rss, source = self.rss_reader()
        generations = (*agent.generations, *extraction.attempts)
        role_ms = sum(g.latency_ms for g in agent.generations)
        extraction_ms = sum(g.latency_ms for g in extraction.attempts)
        return TurnMetrics(run_id=run_id, step=step, governance_turn=governance_turn,
            agent_id=agent_id, node=node, observed_at=observed_at,
            turn_work_ms=(self.timer() - started) * 1000,
            agent_calls=len(agent.generations), extraction_calls=len(extraction.attempts),
            fallback_attempts=sum(len(g.failed_attempts) for g in generations),
            agent_prompt_tokens=_total(agent.generations, "prompt_tokens"),
            extraction_prompt_tokens=_total(extraction.attempts, "prompt_tokens"),
            prompt_tokens=_total(generations, "prompt_tokens"),
            generated_tokens=_total(generations, "generated_tokens"),
            agent_latency_ms=role_ms, extraction_latency_ms=extraction_ms,
            generation_latency_ms=role_ms + extraction_ms,
            context_budget_used=context.budget_used, context_budget_limit=context.budget_limit,
            context_budget_method=context.budget_method, active_nodes=nodes, active_edges=edges,
            process_rss_bytes=rss, rss_source=source)

    @staticmethod
    def from_checkpoint(checkpoint) -> TurnMetrics | None:
        """Older checkpoints have no telemetry; never invent historical samples."""
        data = checkpoint.result.get("telemetry")
        if data is None:
            return None
        metrics = TurnMetrics.model_validate(data)
        if (metrics.run_id, metrics.step, metrics.governance_turn, metrics.agent_id, metrics.node) != (
                checkpoint.run_id, checkpoint.step, checkpoint.governance_turn, checkpoint.agent_id, checkpoint.node):
            raise ValueError("telemetry identity differs from its checkpoint")
        return metrics
