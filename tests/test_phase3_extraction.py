"""Synthetic parser/pipeline failures and explicit opt-in two-node extraction."""

import asyncio
import json

import httpx
import pytest
from pydantic import ValidationError

from src.config import Settings
from src.extraction.extractor import ExtractionError, Extractor
from src.extraction.parser import GraphParseError, MAX_JSON_DEPTH, MAX_RESPONSE_CHARS, parse_graph_response
from src.models.schemas import AgentMessage, ExtractedGraph, MAX_MESSAGE_CHARS, MAX_TRIPLES, SemanticTriple
from src.network.ollama_client import OllamaClient, OllamaError, OllamaUnavailableError

FACT = {"subject": "Project Cedar", "predicate": "uses", "object": "SQLite"}
GRAPH = json.dumps({"triples": [FACT]})
MESSAGE = "## Notes\n- Project Cedar uses SQLite.\n```text\nStatus: ready\n```"


def generation(request, response=GRAPH, **fields):
    """Return a synthetic Ollama generation with explicit extraction counters."""
    return httpx.Response(200, json={
        "model": json.loads(request.content)["model"], "response": response,
        "done": True, "done_reason": "stop", "total_duration": 5_000_000,
        "eval_count": 32, **fields,
    })


def message(content=MESSAGE):
    """Build the source message used by parser and grounding checks."""
    return AgentMessage(agent_id="researcher", content=content, turn=2)


def test_schema_normalization_deduplication_and_round_trip():
    graph = ExtractedGraph.model_validate({"triples": [
        {"subject": " Project   Cedar ", "predicate": " USES ", "object": "SQLite", "confidence": 0.7},
        {"subject": "project cedar", "predicate": "uses", "object": "sqlite", "confidence": 0.9},
    ]})
    assert len(graph.triples) == 1
    assert graph.triples[0].subject == "Project Cedar"
    assert graph.triples[0].predicate == "uses"
    assert graph.triples[0].confidence == 0.7
    assert ExtractedGraph.model_validate_json(graph.model_dump_json()) == graph


@pytest.mark.parametrize("change", [
    {"subject": " "}, {"predicate": "\t"}, {"object": ""},
    {"subject": 123}, {"object": False}, {"predicate": None},
    {"confidence": -0.1}, {"confidence": 1.1}, {"confidence": True},
    {"confidence": "0.5"}, {"confidence": float("nan")}, {"confidence": float("inf")},
    {"subject": "x" * 257}, {"object": "bad\x00label"}, {"object": "\ud800"},
    {"unexpected": "field"}, {"predicate": "İ" * 256},
])
def test_invalid_triple_schema(change):
    with pytest.raises(ValidationError):
        SemanticTriple.model_validate(FACT | change)


@pytest.mark.parametrize("payload", [{}, {"triples": None}, {"triples": "[]"},
    {"triples": [], "entities": []}, {"triples": [FACT] * (MAX_TRIPLES + 1)}])
def test_invalid_graph_schema(payload):
    with pytest.raises(ValidationError):
        ExtractedGraph.model_validate(payload)


def test_explicit_empty_graph_and_default_confidence():
    assert parse_graph_response('{"triples":[]}').graph.triples == []
    assert parse_graph_response(GRAPH).graph.triples[0].confidence == 1.0


@pytest.mark.parametrize("change", [
    {"agent_id": " "}, {"agent_id": 42}, {"agent_id": "a" * 65},
    {"content": "\n\t"}, {"content": False}, {"content": "x" * (MAX_MESSAGE_CHARS + 1)},
    {"content": "bad\x00text"}, {"content": "\udfff"},
    {"turn": -1}, {"turn": True}, {"turn": "1"}, {"turn": 1_000_001},
    {"extra": "field"},
])
def test_invalid_agent_message(change):
    with pytest.raises(ValidationError):
        AgentMessage.model_validate({"agent_id": "researcher", "content": MESSAGE} | change)


def test_message_preserves_raw_formatting():
    raw = "  ## Notes\n\tProject Cedar uses SQLite.\n"
    assert message(raw).content == raw


@pytest.mark.parametrize("wrapper, repair", [
    ("{}", ()), ("  {} \n", ()),
    ("```json\n{}\n```", ("markdown_fence",)),
    ("```JSON\r\n{}\r\n```", ("markdown_fence",)),
    ("```\n{}\n```", ("markdown_fence",)),
    ("Here is the graph:\n{}\nEnd of output.", ("surrounding_text",)),
    ("\ufeff{}", ("leading_bom",)),
])
def test_parser_wrappers(wrapper, repair):
    parsed = parse_graph_response(wrapper.format(GRAPH))
    assert parsed.graph.triples[0].subject == FACT["subject"]
    assert parsed.repairs == repair


@pytest.mark.parametrize("raw, expected, repair", [
    ('{"triples":[{"subject":"Project\nCedar","predicate":"uses","object":"SQLite"}]}',
     "Project Cedar", "control_character"),
    ('{"triples":[{"subject":"Project\tCedar","predicate":"uses","object":"SQLite"}]}',
     "Project Cedar", "control_character"),
    (r'{"triples":[{"subject":"C:\work","predicate":"uses","object":"SQLite"}]}',
     r"C:\work", "invalid_backslash"),
    (r'{"triples":[{"subject":"code\u12X4","predicate":"uses","object":"SQLite"}]}',
     r"code\u12X4", "invalid_backslash"),
    ('{"triples":[{"subject":"Project "Cedar"","predicate":"uses","object":"SQLite"}]}',
     'Project "Cedar"', "interior_quote"),
])
def test_parser_repairs_only_string_local_defects(raw, expected, repair):
    parsed = parse_graph_response(raw)
    assert parsed.graph.triples[0].subject == expected
    assert parsed.repairs == (repair,)


def test_parser_preserves_valid_escapes_unicode_and_structural_characters_in_strings():
    label = 'C:\\work\\"Cedar"\nnaïve {nodes} [edges] 😀'
    raw = json.dumps({"triples": [FACT | {"subject": label}]}, ensure_ascii=False)
    parsed = parse_graph_response(raw)
    assert parsed.repairs == ()
    assert parsed.graph.triples[0].subject == " ".join(label.split())


@pytest.mark.parametrize("raw", [
    "", " \n", "not JSON", "{}", "[]", "[" + GRAPH + "]", GRAPH + GRAPH,
    GRAPH[:-1], '{"triples":[',
    '{"triples":[],"triples":[]}',
    '{"triples":[{"subject":"A","subject":"B","predicate":"uses","object":"SQLite"}]}',
    '{"triples":[{"subject":"A","predicate":"uses","object":"B","confidence":NaN}]}',
    '{"triples":[{"subject":"A","predicate":"uses","object":"B","confidence":Infinity}]}',
    '{"triples":[{"subject":"A","predicate":"uses","object":"B","confidence":1e999}]}',
    '{"triples":[{"subject":"A" "predicate":"uses","object":"B"}]}',
    "{'triples': []}", '{"triples":[],}', '{"triples":true}',
])
def test_parser_rejects_ambiguous_truncated_or_invalid_graphs(raw):
    with pytest.raises(GraphParseError):
        parse_graph_response(raw)


@pytest.mark.parametrize("raw", [
    " " * MAX_RESPONSE_CHARS + GRAPH,
    '{"triples":' + "[" * MAX_JSON_DEPTH + "0" + "]" * MAX_JSON_DEPTH + "}",
])
def test_parser_resource_bounds(raw):
    with pytest.raises(GraphParseError) as caught:
        parse_graph_response(raw)
    assert caught.value.kind == "limits"
    # The decoder's integer limit must also become a controlled parse failure.
    giant_number = "9" * 5000
    with pytest.raises(GraphParseError):
        parse_graph_response(
            '{"triples":[{"subject":"A","predicate":"uses","object":"B","confidence":' + giant_number + '}]}',
        )


@pytest.mark.parametrize("node", ["node1", "node2"])
@pytest.mark.parametrize("raw", [GRAPH, "```json\n" + GRAPH + "\n```", "Notes:\n" + GRAPH])
def test_synthetic_small_model_extraction(settings, node, raw):
    calls = []

    def handler(request):
        calls.append(request)
        payload = json.loads(request.content)
        assert request.url.path == "/api/generate"
        assert payload["format"] == "json"
        assert payload["stream"] is False
        assert payload["model"] == settings.node_model(node)
        assert payload["options"] == {"num_predict": 512, "num_ctx": 2048,
                                     "num_thread": settings.node_threads(node), "temperature": 0}
        assert "Example 1:" in payload["system"] and "Example 2:" in payload["system"]
        assert json.loads(payload["prompt"].split("\n", 1)[1])["message"] == MESSAGE
        return generation(request, raw)

    async def scenario():
        async with OllamaClient(settings, transport=httpx.MockTransport(handler)) as client:
            result = await Extractor(client).extract(message(), node=node, allow_fallback=False)
            assert result.graph.triples[0].model_dump() == FACT | {"confidence": 1.0}
            assert result.generation.node == node
            assert result.generation.server_duration_ms == 5
            assert result.latency_ms >= result.generation.latency_ms >= 0
            assert result.corrective_retries == 0
            assert len(calls) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("bad, kind", [
    ('{"triples":[', "syntax"), ('{"triples":[{"subject":42}]}', "schema"),
    (json.dumps({"triples": [FACT | {"subject": "Project Aurora"}]}), "grounding"),
])
def test_corrective_retry_is_bounded_and_source_preserving(settings, bad, kind):
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return generation(request, bad)
        payload = json.loads(request.content)
        assert "Previous output failed validation:" in payload["prompt"]
        assert json.loads(payload["prompt"].split("\n", 2)[1])["message"] == MESSAGE
        return generation(request)

    async def scenario():
        async with OllamaClient(settings, transport=httpx.MockTransport(handler)) as client:
            result = await Extractor(client).extract(message())
            assert result.corrective_retries == 1
            assert result.validation_errors[0].kind == kind
            assert len(result.attempts) == len(calls) == 2
            assert all(attempt.node == "node2" for attempt in result.attempts)
    asyncio.run(scenario())


@pytest.mark.parametrize("max_retries", [0, 1])
def test_exhausted_retries_raise_instead_of_returning_empty_graph(settings, max_retries):
    calls = []

    def handler(request):
        calls.append(request)
        return generation(request, '{"triples":[{"subject":null}]}')

    async def scenario():
        async with OllamaClient(settings, transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(ExtractionError) as caught:
                await Extractor(client, max_retries=max_retries).extract(message())
            assert len(caught.value.attempts) == len(caught.value.errors) == len(calls) == max_retries + 1
            assert caught.value.errors[-1].kind == "schema"
    asyncio.run(scenario())


def test_truncated_generation_is_rejected_even_if_json_is_valid(settings):
    async def scenario():
        async with OllamaClient(settings, transport=httpx.MockTransport(
            lambda request: generation(request, done_reason="length"),
        )) as client:
            with pytest.raises(ExtractionError) as caught:
                await Extractor(client, max_retries=0).extract(message())
            assert caught.value.errors[0].kind == "truncated"
    asyncio.run(scenario())


def test_empty_graph_is_valid_without_retry(settings):
    async def scenario():
        async with OllamaClient(settings, transport=httpx.MockTransport(
            lambda request: generation(request, '{"triples":[]}'),
        )) as client:
            result = await Extractor(client).extract(message("Hello!"))
            assert not result.graph.triples
            assert len(result.attempts) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("endpoint", ["subject", "object"])
def test_ungrounded_entities_and_word_substrings_are_rejected(settings, endpoint):
    invalid = FACT | {endpoint: "SQL" if endpoint == "object" else "Cede"}

    async def scenario():
        async with OllamaClient(settings, transport=httpx.MockTransport(
            lambda request: generation(request, json.dumps({"triples": [invalid]})),
        )) as client:
            with pytest.raises(ExtractionError) as caught:
                await Extractor(client, max_retries=0).extract(message())
            assert caught.value.errors[0].kind == "grounding"
    asyncio.run(scenario())


def test_grounding_handles_unicode_case_whitespace_and_punctuation(settings):
    fact = {"subject": "CAFÉ Cedar", "predicate": "uses", "object": "C++"}

    async def scenario():
        async with OllamaClient(settings, transport=httpx.MockTransport(
            lambda request: generation(request, json.dumps({"triples": [fact]})),
        )) as client:
            result = await Extractor(client).extract(message("Café   Cedar uses C++."))
            assert result.graph.triples[0].object == "C++"
    asyncio.run(scenario())


def test_fallback_then_correction_uses_responding_node(settings):
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.host == settings.node2_ollama_url.host:
            raise httpx.ConnectError("controlled outage", request=request)
        if len(calls) == 2:
            return generation(request, '{"triples":[{"subject":42}]}')
        payload = json.loads(request.content)
        assert payload["format"] == "json"
        assert payload["model"] == settings.node_model("node1")
        assert payload["options"]["num_thread"] == settings.node_threads("node1")
        return generation(request)

    async def scenario():
        async with OllamaClient(settings, transport=httpx.MockTransport(handler)) as client:
            result = await Extractor(client).extract(message())
            assert len(calls) == 3
            assert result.attempts[0].used_fallback
            assert result.generation.node == "node1"
            assert result.corrective_retries == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("status", [400, 404, 503])
def test_http_errors_remain_visible_without_schema_retry(settings, status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={"error": "controlled failure"})

    async def scenario():
        async with OllamaClient(settings, transport=httpx.MockTransport(handler)) as client:
            expected = OllamaUnavailableError if status == 503 else OllamaError
            with pytest.raises(expected):
                await Extractor(client).extract(message())
            assert len(calls) == (2 if status == 503 else 1)
    asyncio.run(scenario())


def test_cancellation_does_not_trigger_fallback_or_schema_retry(settings):
    calls = []

    async def handler(request):
        calls.append(request)
        raise asyncio.CancelledError

    async def scenario():
        async with OllamaClient(settings, transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(asyncio.CancelledError):
                await Extractor(client).extract(message())
            assert len(calls) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("kwargs", [
    {"max_retries": -1}, {"max_retries": 2}, {"max_retries": True},
    {"max_tokens": 0}, {"max_tokens": 1025}, {"max_tokens": True},
])
def test_invalid_extractor_limits(settings, kwargs):
    async def scenario():
        async with OllamaClient(settings) as client:
            with pytest.raises(ValueError):
                Extractor(client, **kwargs)
    asyncio.run(scenario())


@pytest.mark.parametrize("kwargs", [
    {"prompt": ""}, {"prompt": "OK", "system": ""},
    {"prompt": "OK", "max_tokens": 0}, {"prompt": "OK", "max_tokens": 1025},
    {"prompt": "OK", "max_tokens": True}, {"prompt": "OK", "node": "node3"},
])
def test_invalid_json_request_makes_no_http_call(settings, kwargs):
    def handler(request):
        pytest.fail("invalid input must not send a request")

    async def scenario():
        async with OllamaClient(settings, transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(ValueError):
                await client.generate_json(**({"system": "JSON only"} | kwargs))
    asyncio.run(scenario())


def test_mutated_or_wrong_message_is_rejected_before_network(settings):
    def handler(request):
        pytest.fail("invalid input must not send a request")

    async def scenario():
        async with OllamaClient(settings, transport=httpx.MockTransport(handler)) as client:
            extractor = Extractor(client)
            with pytest.raises(TypeError):
                await extractor.extract(MESSAGE)
            changed = message()
            changed.content = "x" * (MAX_MESSAGE_CHARS + 1)
            with pytest.raises(ValidationError):
                await extractor.extract(changed)
    asyncio.run(scenario())


# Opt-in live LAN acceptance (skipped by default)

@pytest.mark.lan
@pytest.mark.parametrize("node", ["node1", "node2"])
@pytest.mark.parametrize("content, expected", [
    (MESSAGE, {("project cedar", "uses", "sqlite")}),
    ("**Status**\n- Avery maintains Project Cedar.\n- Project Cedar uses SQLite.",
     {("avery", "maintains", "project cedar"), ("project cedar", "uses", "sqlite")}),
    ("Hello! Which database should we choose?", set()),
])
def test_lan_extract_messy_messages(node, content, expected):
    async def scenario():
        async with OllamaClient(Settings()) as client:
            result = await Extractor(client).extract(message(content), node=node, allow_fallback=False)
            actual = {(t.subject.casefold(), t.predicate, t.object.casefold()) for t in result.graph.triples}
            assert actual == expected, result.graph.model_dump_json()
            assert result.generation.node == node
            assert not any(attempt.used_fallback for attempt in result.attempts)
            print(f"EXTRACTION {node} model={result.generation.model} triples={len(actual)} "
                  f"retries={result.corrective_retries} repairs={result.repairs} latency_ms={result.latency_ms:.2f}")
    asyncio.run(scenario())
