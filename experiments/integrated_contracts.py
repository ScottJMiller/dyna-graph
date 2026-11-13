"""Protocol-4 contracts with explicit rejected-reply outcomes."""

from typing import Literal

from pydantic import Field, model_validator

from experiments.contracts import Strict
from experiments.integrated_scenario import VERSION, fingerprint
from src.models import SemanticTriple, AgentMessage
from src.telemetry import TurnMetrics

CONDITIONS = {
    'A': {'name': 'Bounded linear history', 'single_valued_predicates': (), 'prune': False},
    'B': {'name': 'Graph without supersession or pruning', 'single_valued_predicates': (), 'prune': False},
    'C': {'name': 'Graph with supersession', 'single_valued_predicates': ('uses',), 'prune': False},
    'D': {'name': 'Graph with supersession and pruning', 'single_valued_predicates': ('uses',), 'prune': True},
}
ORDERS = ('ABDC', 'BCAD', 'CDBA', 'DACB')
GENERATION_PROTOCOL = dict(
    context_tokens=2048,
    temperature=0,
    keep_alive='5m',
    extractor_max_tokens=512,
    extractor_max_retries=1,
)
REPLY_ERROR_POLICY = dict(
    version=1,
    classification='bounded_invalid_agent_json_after_correction',
    score='incorrect_and_format_invalid',
    answer=None,
    response_extraction='skipped',
    graph_policy='mark_supplied_memory_used_then_normal_pruning_and_checkpoint',
    baseline_history='retain_task_with_null_answer',
    costs='include_all_generation_attempts',
    other_errors='abort_cohort',
    maximum_attempt_chars=16384,
)
INFERENCE_ERROR_POLICY = dict(
    version=1,
    prediction_abort='exact_HTTP_500_token_repeat_limit',
    retry='existing_correction_budget_same_node',
    missing_response_and_counters=None,
    extraction_failure='record_empty_graph_after_exhausted_validation',
    answer_scoring='independent_of_extraction_success',
    other_http_and_transport_errors='abort_cohort',
)
PRIMARY_OUTCOMES = [
    'delayed_answer_accuracy_by_variant',
    'full_pipeline_prompt_tokens_by_variant',
    'single_value_conflicts',
    'active_graph_size_and_missing_required_support',
]
HYPOTHESES = {
    'supersession': 'Compare C minus B for active single-value conflicts and answer accuracy.',
    'pruning': 'Compare D minus C for active graph reduction and delayed accuracy; preservation may fail.',
}


class IntegratedPlan(Strict):
    """Fixed protocol parameters and counterbalanced condition order."""
    version: Literal[4] = 4
    scenario_version: Literal[VERSION] = VERSION
    turns: int = Field(default=60, ge=60, le=80)
    variants: int = Field(default=4, ge=1, le=8)
    memory_budget: int = Field(default=1000, ge=128, le=1000)
    max_tokens: int = Field(default=128, ge=64, le=256)
    warmups_per_node: int = Field(default=1, ge=0, le=3)
    hours_per_turn: Literal[2.0] = 2.0
    pruning_threshold: Literal[0.15] = 0.15
    recency_half_life_seconds: Literal[86400.0] = 86400.0
    turn_half_life: Literal[10.0] = 10.0

    def order(self):
        """Return condition runs in the declared counterbalanced workload order."""
        return tuple(
            (variant, condition)
            for variant in range(1, self.variants + 1)
            for condition in ORDERS[(variant - 1) % 4]
        )

    @property
    def row_count(self):
        """Return the number of scheduled role turns across all conditions."""
        return 4 * self.variants * self.turns

    def scenario_hashes(self):
        """Identify the rendered inputs and oracle for every workload variant."""
        return {str(variant): fingerprint(self.turns, variant) for variant in range(1, self.variants + 1)}


class FactState(Strict):
    """One historical fact with endpoint activity used by replay validation."""
    id: int = Field(ge=1)
    subject: str = Field(min_length=1, max_length=256)
    predicate: str = Field(min_length=1, max_length=256)
    object: str = Field(min_length=1, max_length=256)
    status: Literal['ACTIVE', 'SUPERSEDED', 'PRUNED']
    subject_active: bool
    object_active: bool

    @property
    def live(self):
        """Return whether the triple and both endpoints are currently active."""
        return self.status == 'ACTIVE' and self.subject_active and self.object_active


class MemoryState(Strict):
    """Question-specific fact history and graph usage at one turn boundary."""
    target_facts: tuple[FactState, ...] = Field(max_length=256)
    active_nodes: int = Field(ge=0)
    active_edges: int = Field(ge=0)
    single_value_conflicts: int = Field(ge=0)
    subject_pinned: bool
    last_access_at: float | None = Field(ge=0)
    last_access_turn: int | None = Field(ge=0)


class Change(Strict):
    """One observation or response stage transition in the saved fact catalog."""
    stage: Literal['observation', 'response']
    before: Literal['ACTIVE', 'SUPERSEDED', 'PRUNED'] | None
    fact: FactState


class InferenceAttempt(Strict):
    """Returned generation or known server abort, including measured costs."""
    node: Literal['node1', 'node2']
    model: str = Field(min_length=1, max_length=256)
    response: str | None = Field(max_length=16384)
    prompt_tokens: int | None = Field(ge=0)
    generated_tokens: int | None = Field(ge=0)
    latency_ms: float = Field(ge=0)
    done_reason: str | None = Field(max_length=256)
    error: Literal['token_repeat_limit'] | None = None
    http_status: Literal[500] | None = None

    @model_validator(mode='after')
    def aborted_or_returned(self):
        """Keep aborted prediction text and counters null rather than fabricate values."""
        if self.error:
            if (
                self.http_status != 500
                or any((v is not None for v in (self.response, self.prompt_tokens, self.generated_tokens, self.done_reason)))
            ):
                raise ValueError('prediction abort must preserve unavailable text/counters as null')
        elif self.response is None or self.http_status is not None:
            raise ValueError('returned generation needs text and no HTTP error')
        return self


class ReplyAttempt(InferenceAttempt):
    """A role attempt that must fail the same parser used during execution."""

    @model_validator(mode='after')
    def rejected_by_original_parser(self):
        """Reject a failure record whose attempt is actually a valid role reply."""
        from src.agents.base_agent import parse_reply
        try:
            parse_reply(self)
        except (ValueError, RecursionError):
            return self
        raise ValueError('reply failure contains a valid accepted reply')


class ReplyFailure(Strict):
    """Bounded role failure with all attempts retained for scoring and costs."""
    kind: Literal['invalid_or_truncated_json']
    attempts: tuple[ReplyAttempt, ...] = Field(min_length=1, max_length=2)


class ExtractionIssue(Strict):
    """Validation classification and detail for one unsuccessful extraction."""
    kind: str = Field(min_length=1, max_length=64)
    detail: str = Field(min_length=1, max_length=2000)


class ExtractionFailure(Strict):
    """Source message, issues and attempts from an exhausted extraction."""
    kind: Literal['extraction_failed']
    source_message: AgentMessage
    errors: tuple[ExtractionIssue, ...] = Field(min_length=1, max_length=2)
    attempts: tuple[InferenceAttempt, ...] = Field(min_length=1, max_length=2)

    @model_validator(mode='after')
    def all_attempts_failed(self):
        """Require every saved issue to match reparsing of its source and attempt."""
        from src.extraction.extractor import parse_extraction, GraphParseError
        if len(self.errors) != len(self.attempts):
            raise ValueError('extraction issue count differs')
        for attempt, error in zip(self.attempts, self.errors):
            try:
                parse_extraction(attempt, self.source_message)
            except GraphParseError as exc:
                if (exc.kind, exc.detail) != (error.kind, error.detail):
                    raise ValueError('extraction validation issue differs')
                continue
            raise ValueError('extraction failure contains accepted facts')
        return self


class IntegratedRow(Strict):
    """One complete scheduled outcome and its memory, inference and cost evidence."""
    version: Literal[4] = 4
    source: Literal['measured', 'synthetic']
    bundle_id: str = Field(pattern='^[a-f0-9]{32}$')
    condition: Literal['A', 'B', 'C', 'D']
    variant: int = Field(ge=1, le=8)
    position: int = Field(ge=1, le=32)
    step: int = Field(ge=1, le=80)
    scenario_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    task: str = Field(max_length=500)
    observation: str = Field(max_length=2000)
    answer: str | None = Field(min_length=1, max_length=2000)
    reply_failure: ReplyFailure | None = None
    observation_failure: ExtractionFailure | None = None
    response_failure: ExtractionFailure | None = None
    observation_attempts: tuple[InferenceAttempt, ...] = Field(default=(), max_length=2)
    agent_attempts: tuple[InferenceAttempt, ...] = Field(min_length=1, max_length=2)
    response_attempts: tuple[InferenceAttempt, ...] = Field(default=(), max_length=2)
    format_valid: bool
    correct: bool
    context: str = Field(max_length=1000)
    context_triple_ids: tuple[int, ...] = Field(max_length=128)
    retrieved_facts: tuple[FactState, ...] = Field(max_length=128)
    logical_time: float = Field(ge=0)
    memory_before: MemoryState
    memory_after: MemoryState
    changes: tuple[Change, ...] = Field(max_length=512)
    pruned_entity_ids: tuple[int, ...] = Field(max_length=100)
    observation_facts: tuple[SemanticTriple, ...] = Field(max_length=16)
    response_facts: tuple[SemanticTriple, ...] = Field(max_length=16)
    observation_calls: int = Field(ge=0, le=2)
    observation_prompt_tokens: int | None = Field(ge=0)
    observation_generated_tokens: int | None = Field(ge=0)
    observation_latency_ms: float = Field(ge=0)
    total_prompt_tokens: int | None = Field(ge=0)
    total_generated_tokens: int | None = Field(ge=0)
    total_inference_ms: float = Field(ge=0)
    wall_ms: float = Field(ge=0)
    history_full_bytes: int = Field(ge=0)
    history_dropped_turns: int = Field(ge=0)
    telemetry: TurnMetrics

    @model_validator(mode='after')
    def consistent(self):
        """Validate each evidence stage in order before accepting a turn record."""
        self._validate_attempt_costs()
        self._validate_role_reply()
        self._validate_extractions()
        self._validate_totals()
        self._validate_reply_outcome()
        return self

    def _validate_attempt_costs(self):
        """Check attempt counts and costs, preserving unavailable counters as unknown."""

        def total(attempts, field):
            """Sum costs only when every attempt supplies the requested counter."""
            values = [getattr(a, field) for a in attempts]
            return None if any((v is None for v in values)) else sum(values)
        if (
            len(self.observation_attempts) != self.observation_calls
            or len(self.agent_attempts) != self.telemetry.agent_calls
            or len(self.response_attempts) != self.telemetry.extraction_calls
        ):
            raise ValueError('attempt evidence differs from recorded call counts')
        checks = [
            (self.observation_attempts, self.observation_prompt_tokens, self.observation_generated_tokens, self.observation_latency_ms),
            (self.agent_attempts, self.telemetry.agent_prompt_tokens, None, self.telemetry.agent_latency_ms),
            (self.response_attempts, self.telemetry.extraction_prompt_tokens, None, self.telemetry.extraction_latency_ms),
        ]
        for attempts, prompts, generated, latency in checks:
            if total(attempts, 'prompt_tokens') != prompts or abs(total(attempts, 'latency_ms') - latency) > 1e-06:
                raise ValueError('attempt costs differ')
        if (
            total(self.observation_attempts, 'generated_tokens') != self.observation_generated_tokens
            or total(self.agent_attempts + self.response_attempts, 'generated_tokens') != self.telemetry.generated_tokens
        ):
            raise ValueError('generated token evidence differs')

    def _validate_role_reply(self):
        """Reparse the accepted or rejected role attempts without repairing answers."""
        from src.agents.base_agent import parse_reply
        if self.reply_failure is None:
            parsed = parse_reply(self.agent_attempts[-1])
            if parsed.content != self.answer or parsed.tool_calls:
                raise ValueError('answer differs from accepted role reply')
            for previous in self.agent_attempts[:-1]:
                try:
                    parse_reply(previous)
                except (ValueError, RecursionError):
                    continue
                raise ValueError('role correction follows a valid reply')
        elif self.reply_failure.model_dump()['attempts'] != tuple((a.model_dump() for a in self.agent_attempts)):
            raise ValueError('rejected role attempts differ')

    def _validate_extractions(self):
        """Reparse extraction attempts and require the recorded facts or failure."""
        from src.extraction.extractor import parse_extraction, GraphParseError
        for attempts, failure, facts, source in [
            (self.observation_attempts, self.observation_failure, self.observation_facts, self.observation),
            (self.response_attempts, self.response_failure, self.response_facts, self.answer),
        ]:
            if failure is not None:
                if (
                    facts
                    or failure.source_message.content != source
                    or failure.source_message.turn != self.step
                    or failure.source_message.agent_id != self.telemetry.agent_id
                ):
                    raise ValueError('failed extraction has facts or a different source')
                if failure.model_dump()['attempts'] != tuple((a.model_dump() for a in attempts)):
                    raise ValueError('failed extraction attempts differ')
            elif attempts:
                message = AgentMessage(agent_id=self.telemetry.agent_id, content=source, turn=self.step)
                parsed = parse_extraction(attempts[-1], message)
                if tuple(parsed.graph.triples) != facts:
                    raise ValueError('accepted extraction differs from evidence')
                for previous in attempts[:-1]:
                    try:
                        parse_extraction(previous, message)
                    except GraphParseError:
                        continue
                    raise ValueError('extraction correction follows a valid graph')

    def _validate_totals(self):
        """Check pipeline totals, elapsed inference and the supplied memory byte count."""
        for total, a, b in [
            (self.total_prompt_tokens, self.telemetry.prompt_tokens, self.observation_prompt_tokens),
            (self.total_generated_tokens, self.telemetry.generated_tokens, self.observation_generated_tokens),
        ]:
            if total != (None if a is None or b is None else a + b):
                raise ValueError('inconsistent pipeline usage')
        if (
            abs(self.total_inference_ms - self.telemetry.generation_latency_ms - self.observation_latency_ms) > 1e-06
        ):
            raise ValueError('inconsistent inference duration')
        if len(self.context.encode()) != self.telemetry.context_budget_used:
            raise ValueError('context byte count mismatch')
        if self.correct and (not self.format_valid):
            raise ValueError('invalid answer cannot be correct')

    def _validate_reply_outcome(self):
        """Require rejected replies to have no accepted answer or response facts."""
        if self.reply_failure is None:
            if self.answer is None:
                raise ValueError('an accepted reply must contain an answer')
        else:
            if (
                self.answer is not None
                or self.correct
                or self.format_valid
                or self.response_facts
                or self.telemetry.extraction_calls
                or self.response_failure
            ):
                raise ValueError('a rejected reply has no accepted answer or response extraction')
            attempts = self.reply_failure.attempts

            def total(field):
                """Sum costs only when every attempt supplies the requested counter."""
                values = [getattr(a, field) for a in attempts]
                return None if any((v is None for v in values)) else sum(values)
            if (
                self.telemetry.agent_calls != len(attempts)
                or self.telemetry.agent_prompt_tokens != total('prompt_tokens')
                or self.telemetry.generated_tokens != total('generated_tokens')
                or abs(self.telemetry.agent_latency_ms - sum((a.latency_ms for a in attempts))) > 1e-06
            ):
                raise ValueError('rejected reply cost differs from actual generation attempts')
        if (
            self.condition == 'A'
            and (
                self.observation_calls
                or self.telemetry.extraction_calls
                or self.changes
                or self.context_triple_ids
                or self.memory_before.active_nodes
                or self.memory_after.active_nodes
            )
        ):
            raise ValueError('linear baseline must not use graph memory')


def project(row, event):
    """One deterministic CSV/analysis projection, independent of model execution."""
    from experiments.integrated_scenario import role_at
    known = bool(event.expected)

    def contains(facts, value):
        """Check live support for one expected subject, predicate and value."""
        return any(
            fact.live
            and (fact.subject.casefold(), fact.predicate, fact.object.casefold())
            == (event.subject.casefold(), event.predicate, value.casefold())
            for fact in facts
        )

    def in_observation(value):
        """Check literal support in the current scripted observation."""
        statement = f'{event.subject} {event.predicate} {value}.'
        return statement.casefold() in row.observation.casefold()

    def in_history(value):
        """Check literal support in the supplied linear-history window."""
        statement = f'{event.subject} {event.predicate} {value}.'
        return statement.casefold() in row.context.casefold()

    available = None
    if known and row.condition != 'A':
        available = all(contains(row.memory_before.target_facts, value) for value in event.expected)
    if not known:
        supplied = None
    elif row.condition == 'A':
        supplied = all(in_history(value) for value in event.expected)
    else:
        supplied = all(contains(row.retrieved_facts, value) for value in event.expected)
    in_input = None if not known else all(in_observation(value) for value in event.expected)
    combined = None if not known else all(
        in_observation(value)
        or (in_history(value) if row.condition == 'A' else contains(row.retrieved_facts, value))
        for value in event.expected
    )
    missing_states = set()
    if known and row.condition != 'A':
        for value in event.expected:
            matches = [
                fact for fact in row.memory_before.target_facts
                if fact.object.casefold() == value.casefold()
            ]
            if not any(fact.live for fact in matches):
                if not matches:
                    missing_states.add('absent')
                elif any(fact.status == 'PRUNED' or not fact.subject_active or not fact.object_active
                         for fact in matches):
                    missing_states.add('pruned')
                else:
                    missing_states.add('superseded')
    cross_node = any(role_at(source_step)[1] != row.telemetry.node for source_step in event.source_steps)
    return dict(
        condition=row.condition,
        variant=row.variant,
        step=row.step,
        category=event.category,
        lag=event.recall_gap,
        source_age=None if not event.source_steps else row.step - min(event.source_steps),
        cross_node=cross_node,
        correct=row.correct,
        format_valid=row.format_valid,
        reply_failed=row.reply_failure is not None,
        observation_failed=row.observation_failure is not None,
        response_extraction_failed=row.response_failure is not None,
        prediction_aborts=sum((attempt.error is not None for attempt in row.observation_attempts + row.agent_attempts + row.response_attempts)),
        support_available=available,
        support_supplied=supplied,
        support_in_input=in_input,
        total_support_supplied=combined,
        missing_support_states='|'.join(sorted(missing_states)),
        logical_time=row.logical_time,
        observed_at=row.telemetry.observed_at,
        memory_bytes=row.telemetry.context_budget_used,
        memory_allowance=row.telemetry.context_budget_limit,
        role_prompt_tokens=row.telemetry.agent_prompt_tokens,
        total_prompt_tokens=row.total_prompt_tokens,
        total_generated_tokens=row.total_generated_tokens,
        wall_ms=row.wall_ms,
        pruned_triples=sum((change.fact.status == 'PRUNED' and change.before != 'PRUNED' for change in row.changes)),
        superseded_triples=sum((change.fact.status == 'SUPERSEDED' and change.before != 'SUPERSEDED' for change in row.changes)),
        active_nodes=row.memory_after.active_nodes,
        active_edges=row.memory_after.active_edges,
        single_value_conflicts=row.memory_after.single_value_conflicts,
        history_dropped_turns=row.history_dropped_turns,
        history_full_bytes=row.history_full_bytes,
    )
