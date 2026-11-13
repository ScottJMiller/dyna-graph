"""Shared support for the original and integrated local experiments.

These helpers describe roles, inference costs, model inventory and provenance.
Importing this module does not load settings, open a database or contact Ollama.
Scenario execution and scoring stay in their respective study modules.
"""

from dataclasses import asdict
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import re
from time import perf_counter

from experiments.contracts import sha256
from src.agents import PlannerAgent, ResearchAgent, VerificationAgent
from src.agents.base_agent import encode
from src.extraction import Extractor, ExtractionError
from src.telemetry import TurnMetrics
from src.telemetry.profiler import process_rss


class _ResearchRole(ResearchAgent):
    """Use the same research-role framing for both memory representations."""

    @property
    def role_prompt(self):
        # Identical condition-neutral framing for both history representations.
        """Keep the role instructions independent of the condition being compared."""
        return super().role_prompt.replace("graph memory", "memory")


def make_agents(client):
    """Create the planner, researcher and verifier in the fixed execution order."""
    return (PlannerAgent(client), _ResearchRole(client), VerificationAgent(client))


class RecordedExtractor(Extractor):
    """Attach exact failed source text for diagnostics; validation is unchanged."""

    async def extract(self, message, **kwargs):
        """Retain the failed extraction source while preserving normal validation."""
        try:
            return await super().extract(message, **kwargs)
        except ExtractionError as exc:
            exc.source_message = message.model_dump(mode="json")
            raise


def failure_record(exc, *, manifest, condition, repetition, step, stage, task):
    """Serialize available failure evidence without exposing endpoint addresses."""
    errors = getattr(exc, "errors", ())
    if callable(errors):
        errors = errors()
    errors = errors if isinstance(errors, (list, tuple)) else ()
    details = [{"kind": e.get("type", "validation"), "detail": e.get("msg", "validation failed")}
               if isinstance(e, dict) else {"kind": getattr(e, "kind", type(e).__name__),
                   "detail": getattr(e, "detail", "validation failed")} for e in errors]
    return {"version": 1, "bundle_id": manifest["bundle_id"], "source": manifest["source"],
        "condition": condition, "repetition": repetition, "step": step,
        "stage": stage, "task": task, "error_type": type(exc).__name__,
        "source_message": getattr(exc, "source_message", None),
        "validation_errors": details,
        "http_failure": ({"node":exc.node,"kind":exc.kind,"status_code":exc.status_code,
            "latency_ms":exc.latency_ms,"server_error":exc.server_error} if hasattr(exc,'server_error') else None),
        "generation_attempts": [{"node": g.node, "model": g.model, "response": g.response,
            "prompt_tokens": g.prompt_tokens, "generated_tokens": g.generated_tokens,
            "latency_ms": g.latency_ms, "done_reason": g.done_reason,
            "error": g.error, "http_status": g.http_status}
            for g in getattr(exc, "attempts", ())]}


def linear_context(history, budget):
    """Retain newest complete episodes; never truncate a message or JSON value."""
    full = encode(history) if history else ""
    kept, dropped = list(history), 0
    while kept and len(encode(kept).encode()) > budget:
        kept.pop(0)
        dropped += 1
    return (encode(kept) if kept else ""), len(full.encode()), dropped


def sum_attempt_field(generations, field):
    """Sum one attempt field, returning unknown when any counter is unavailable."""
    values = [getattr(g, field) for g in generations]
    return None if any(v is None for v in values) else sum(values)


def add_costs(a, b):
    """Combine two costs without replacing an unknown value with zero."""
    return None if a is None or b is None else a + b


def validate_routes(generations, client, node):
    """Require every attempt to use the planned node and model without fallback."""
    if any(g.node != node or g.model != client.settings.node_model(node) or g.failed_attempts
           for g in generations):
        raise ValueError("experiment routing/model differs from the fixed condition plan")


def baseline_metrics(run_id, item, result, context, budget, started):
    """Build history-only turn metrics with no extraction or graph usage."""
    generations = result.generations
    prompt = sum_attempt_field(generations, "prompt_tokens")
    latency = sum(g.latency_ms for g in generations)
    rss, source = process_rss()
    import time
    return TurnMetrics(run_id=run_id, step=item.step, governance_turn=item.step,
        agent_id=result.message.agent_id, node=result.generation.node, observed_at=time.time(),
        turn_work_ms=(perf_counter() - started) * 1000, agent_calls=len(generations), extraction_calls=0,
        fallback_attempts=sum(len(g.failed_attempts) for g in generations),
        agent_prompt_tokens=prompt, extraction_prompt_tokens=0, prompt_tokens=prompt,
        generated_tokens=sum_attempt_field(generations, "generated_tokens"), agent_latency_ms=latency,
        extraction_latency_ms=0.0, generation_latency_ms=latency, context_budget_used=len(context.encode()),
        context_budget_limit=budget, context_budget_method="utf8_upper_bound", active_nodes=0,
        active_edges=0, process_rss_bytes=rss, rss_source=source)


def package_versions():
    """Record installed analysis/runtime package versions without installing anything."""
    result = {}
    for name in ("pydantic", "pydantic-settings", "httpx", "rich", "matplotlib", "numpy"):
        try:
            result[name] = version(name)
        except PackageNotFoundError:
            result[name] = None
    return result


def source_provenance():
    """Hash the current requirements, implementation and experiment source files."""
    root = Path(__file__).resolve().parents[1]
    paths = [root / "requirements.txt", *(root / "src").rglob("*.py"),
             *(root / "src").rglob("*.sql"), *(root / "experiments").glob("*.py")]
    return {str(p.relative_to(root)): sha256(p) for p in sorted(paths)}


async def model_inventory(client, *, synthetic):
    """Check both configured local models and return their installed metadata."""
    result = {}
    for node in ("node1", "node2"):
        health = await client.ping(node)
        installed = await client.list_models(node)
        matches = [m for m in installed.models if m.name == client.settings.node_model(node)]
        if len(matches) != 1 or matches[0].quantization_level != "Q4_K_M":
            raise ValueError(f"{node} must have its configured local Q4_K_M model installed")
        if not synthetic and not re.fullmatch(r"[a-fA-F0-9]{64}", matches[0].digest or ""):
            raise ValueError(f"{node} must report its installed model digest")
        result[node] = {"ollama_version": health.version, **asdict(matches[0]),
                        "threads": client.settings.node_threads(node)}
    return result
