"""Assemble complete graph facts under a strict, explicitly counted budget."""

import asyncio
from dataclasses import dataclass
import json
import inspect
from time import perf_counter
from typing import Callable, Literal

from src.governance.decay import record_access
from src.retrieval.seed_matcher import SeedMatcher, validate_limit, normalize_subgoal
from src.retrieval.traversal import Traversal
from src.storage.graph_store import GraphStore

ContextFormat = Literal["markdown", "json"]


def count_utf8_bound(text: str) -> int:
    """Conservative text-token ceiling for byte-level BPE; not a measured count.

    One regular byte-level BPE token consumes at least one input byte. All text
    emitted here is ASCII-escaped, so no Unicode normalization is required.
    Chat templates, role prompts, and task text are outside this block's budget.
    """
    return len(text.encode("utf-8"))


@dataclass(frozen=True)
class ContextResult:
    text: str
    format: ContextFormat
    budget_used: int
    budget_limit: int
    budget_method: Literal["utf8_upper_bound", "model_tokens"]
    entity_ids: tuple[int, ...]
    triple_ids: tuple[int, ...]
    seed_ids: tuple[int, ...]
    seed_source: str
    omitted_facts: int
    omitted_entities: int
    neighborhood_limited: bool
    latency_ms: float


def quote_label(value: str) -> str:
    # Escape angle brackets as well as controls/Unicode, so a raw model control
    # token or Markdown/HTML delimiter in a label remains literal data.
    """Escape a graph label so controls, markup and Unicode remain literal data."""
    return json.dumps(value, ensure_ascii=True).replace("<", "\\u003c").replace(">", "\\u003e")


class ContextAssembler:
    """Read one snapshot, select whole facts, and never silently cut a label.

    Optional token_counter must be a correct LOCAL model-specific text counter;
    word/character heuristics are not valid replacements. The dependency-free
    default is a conservative byte-level BPE ceiling, distinct from telemetry.
    """

    def __init__(self, graph: GraphStore, *, token_budget: int = 1000, seed_limit: int = 5,
                 hops: int = 2, max_nodes: int = 64, max_triples: int = 128, fanout: int = 16,
                 token_counter: Callable[[str], int] | None = None) -> None:
        self.graph = graph
        self.token_budget = validate_limit(token_budget, "token_budget", 1000)
        self.matcher = SeedMatcher(graph, seed_limit=seed_limit)
        self.traversal = Traversal(graph, hops=hops, max_nodes=max_nodes, max_triples=max_triples, fanout=fanout)
        if seed_limit > max_nodes:
            raise ValueError("max_nodes must accommodate seed_limit")
        if token_counter is not None and (not callable(token_counter) or inspect.iscoroutinefunction(token_counter)):
            raise ValueError("token_counter must be a synchronous callable")
        self._counter = token_counter if token_counter is not None else count_utf8_bound
        self._method = "model_tokens" if token_counter is not None else "utf8_upper_bound"

    @classmethod
    def from_settings(cls, graph: GraphStore, settings, *, token_counter=None) -> "ContextAssembler":
        return cls(graph, token_budget=settings.context_token_budget, seed_limit=settings.retrieval_seed_limit,
                   hops=settings.retrieval_hops, max_nodes=settings.retrieval_max_nodes,
                   max_triples=settings.retrieval_max_triples, fanout=settings.retrieval_fanout,
                   token_counter=token_counter)

    def _count(self, text: str) -> int:
        count = self._counter(text)
        if inspect.iscoroutine(count):
            count.close()
        if type(count) is not int or count < 0 or (text and count == 0):
            raise ValueError("token_counter must return a positive integer for nonempty text")
        return count

    def assemble(self, subgoal: str, *, format: ContextFormat = "markdown") -> ContextResult:
        started = perf_counter()
        subgoal = normalize_subgoal(subgoal)
        if format not in ("markdown", "json"):
            raise ValueError("format must be 'markdown' or 'json'")
        with self.graph.database.connection() as connection:
            seeds = self.matcher._match(connection, subgoal)
            graph = self.traversal._expand(connection, self.traversal._seeds(seeds.entity_ids))
        by_id = {entity.id: entity for entity in graph.entities}
        selected_facts, selected_entities = [], []
        fact_text, entity_text = [], []
        covered = set()
        quoted_names = {key: quote_label(entity.name) for key, entity in by_id.items()}
        quoted_predicates = {triple.predicate: quote_label(triple.predicate) for triple in graph.triples}

        def fact_line(t):
            subject, predicate, obj = quoted_names[t.subject_id], quoted_predicates[t.predicate], quoted_names[t.object_id]
            if format == "json":
                return f'{{"id":{t.id},"s":{subject},"p":{predicate},"o":{obj}}}'
            return f"- [t{t.id}] {subject} -- {predicate} --> {obj}"

        def entity_line(e):
            if format == "json":
                return f'{{"id":{e.id},"name":{quoted_names[e.id]}}}'
            return f"- [e{e.id}] entity: {quoted_names[e.id]}"

        def render() -> str:
            if format == "json":
                return '{"facts":[' + ",".join(fact_text) + '],"entities":[' + ",".join(entity_text) + "]}"
            return "## Graph memory\n" + "\n".join([*fact_text, *entity_text])

        # Pinned isolated entities precede ordinary facts. Connected pinned facts
        # already sort first in SQL. A skipped large fact never prevents a later
        # short fact from fitting. Complete labels and valid JSON are preserved.
        incident = {value for t in graph.triples for value in (t.subject_id, t.object_id)}
        pinned_isolated = [e for e in graph.entities if e.is_pinned and e.id not in incident]
        for item in [*pinned_isolated, *graph.triples]:
            is_fact = hasattr(item, "predicate")
            target = selected_facts if is_fact else selected_entities
            pieces = fact_text if is_fact else entity_text
            target.append(item)
            pieces.append(fact_line(item) if is_fact else entity_line(item))
            if self._count(render()) > self.token_budget:
                target.pop()
                pieces.pop()
            elif is_fact:
                covered.update((item.subject_id, item.object_id))
            else:
                covered.add(item.id)
        for item in sorted(graph.entities, key=lambda e: (not e.is_pinned, e.depth, e.id)):
            if item.id in covered:
                continue
            selected_entities.append(item)
            entity_text.append(entity_line(item))
            if self._count(render()) > self.token_budget:
                selected_entities.pop()
                entity_text.pop()
            else:
                covered.add(item.id)
        # No match or no fitting item yields an empty string, not an invalid
        # sliced JSON object or a wrapper that alone exceeds the budget.
        text = render() if selected_facts or selected_entities else ""
        count = self._count(text) if text else 0
        if count > self.token_budget:
            raise ValueError("token_counter changed during assembly")
        return ContextResult(
            text, format, count, self.token_budget, self._method, tuple(sorted(covered)),
            tuple(t.id for t in selected_facts), seeds.entity_ids, seeds.source,
            len(graph.triples) - len(selected_facts), len(graph.entities) - len(covered),
            graph.may_be_truncated, (perf_counter() - started) * 1000,
        )

    async def assemble_async(self, subgoal: str, *, format: ContextFormat = "markdown") -> ContextResult:
        return await asyncio.to_thread(self.assemble, subgoal, format=format)

    def mark_used(self, result: ContextResult, *, turn: int, now: float | None = None) -> None:
        """Call only after the returned context is actually supplied to an agent."""
        if not isinstance(result, ContextResult):
            raise ValueError("result must be a ContextResult")
        record_access(self.graph, result.entity_ids, turn=turn, now=now)

    async def mark_used_async(self, result: ContextResult, *, turn: int, now: float | None = None) -> None:
        await self.graph.write_async(lambda _graph: self.mark_used(result, turn=turn, now=now))
