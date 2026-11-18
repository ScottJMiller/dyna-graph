# Phase 3 Verification and Testing Protocol

Whole-suite totals in this phase protocol reflect its completion checkpoint.
See the [current workflow protocol](phase6.md) for current aggregate counts
and schema migration guidance. Dedicated phase checks remain applicable.

Verify bounded extraction from raw messages into validated semantic triples.
The pipeline uses locally installed Ollama models and the existing LAN HTTP
client. It returns a graph without initializing or modifying the database.
Run project commands from the repository root on Node 1.

## 1. Setup and prerequisites

Complete the [Phase 1 environment/LAN setup](phase1.md) and
[Phase 2 storage verification](phase2.md). Both servers must have their configured
local Q4_K_M model tags installed, with cloud features disabled. No additional
dependencies or model downloads are introduced by Phase 3.

```bash
source .venv/bin/activate
python3 --version
python3 -m pip check
python3 - <<'PY'
from src.config import get_settings
from src.models import AgentMessage, ExtractedGraph
s = get_settings()
for node in ("node1", "node2"):
    print(f"{node}: url={s.node_url(node)} model={s.node_model(node)} threads={s.node_threads(node)}")
assert ExtractedGraph.model_validate({"triples": []}).triples == []
AgentMessage(agent_id="verification", content="Project Cedar uses SQLite.")
print("EXTRACTION CONFIG AND SCHEMAS OK")
PY
```

Expected: Python 3.11+, `No broken requirements found.`, your configured node
URLs/model tags/thread budgets, and `EXTRACTION CONFIG AND SCHEMAS OK`.
The root `.env` remains private. Process environment settings take precedence.

Ollama's [generation API](https://docs.ollama.com/api/generate) accepts a JSON
format and a separate system prompt. The pipeline sends `format: "json"`,
`stream: false`, temperature 0, a 2,048-token context, and the target node's CPU
budget. JSON mode constrains output syntax; the local parser separately validates
the graph's data contract. See [structured outputs](https://docs.ollama.com/capabilities/structured-outputs).

## 2. Automated test execution

Dedicated script: `tests/test_extraction.py`.

### Offline functional tests

```bash
python3 -m pytest tests/test_extraction.py -v -s
```

Expected: **105 passed, 6 skipped**. The skipped checks require real LAN nodes.
All offline HTTP responses are simulated; no Ollama server is required.
Assertions cover:

- Strict triples/messages, bounded fields, finite confidence, and unknown-field rejection.
- Whitespace normalization, lowercase predicates, duplicate-fact removal, and JSON round trips.
- Empty graphs distinguished from missing or invalid graphs.
- Markdown/prose/BOM cleanup and string-local quote, backslash, and control-character repairs.
- Valid escaped characters and Unicode preserved; duplicate keys and ambiguous graphs rejected.
- Truncation, malformed structures, nesting/response limits, and exhausted output budgets.
- JSON-mode payloads, two examples, per-node models/thread budgets, and latency metadata.
- One corrective retry, source preservation, literal endpoint grounding, and explicit exhaustion errors.
- Fallback followed by correction on the surviving node; visible HTTP failures and cancellation.
- Invalid or mutated messages rejected before network work.

### Both-node synthetic live acceptance

```bash
python3 -m pytest tests/test_extraction.py --run-lan -m lan -v -s
```

Expected: **6 passed, 105 deselected**. Three inputs are tested on each node:

1. A Markdown/log-style message: `Project Cedar uses SQLite.` → one `uses` triple.
2. A bullet list: `Avery maintains Project Cedar.` and `Project Cedar uses SQLite.` → two triples.
3. A greeting/question with no factual assertion → an explicit empty graph.

Diagnostics resemble:

```text
EXTRACTION node1 model=<configured-model> triples=1 retries=<0-or-1> repairs=<tuple> latency_ms=<number>
EXTRACTION node2 model=<configured-model> triples=1 retries=<0-or-1> repairs=<tuple> latency_ms=<number>
EXTRACTION node1 model=<configured-model> triples=2 retries=<0-or-1> repairs=<tuple> latency_ms=<number>
EXTRACTION node2 model=<configured-model> triples=2 retries=<0-or-1> repairs=<tuple> latency_ms=<number>
EXTRACTION node1 model=<configured-model> triples=0 retries=<0-or-1> repairs=<tuple> latency_ms=<number>
EXTRACTION node2 model=<configured-model> triples=0 retries=<0-or-1> repairs=<tuple> latency_ms=<number>
6 passed, 105 deselected in <seconds>s
```

These tests disable fallback and require the exact expected facts, comparing
entity spelling without case differences. They cannot pass by silently using
the other node. Latency varies with hardware, model loading, and CPU load.

If only one server is available during setup, run a partial check:

```bash
python3 -m pytest tests/test_extraction.py --run-lan --lan-node node2 -m lan -v -s
```

Expected: **3 passed, 3 skipped, 105 deselected**. This is insufficient for
two-node acceptance.

The complete Phase 3 acceptance command is:

```bash
python3 -m pytest tests/test_extraction.py --run-lan -q
```

Expected: **111 passed, no skips or failures**.

### Regression tests

```bash
python3 -m pytest -q
python3 -m pip check
```

Expected: **301 passed, 15 skipped**, then `No broken requirements found.`
The skips are eight Phase 1 LAN checks, six Phase 3 LAN checks, and the explicit
storage benchmark. Storage tests use isolated databases. Phase 1/2 dedicated
live and benchmark commands remain in their respective guides.

## 3. Manual defensive-parser smoke check

```bash
python3 - <<'PY'
from src.extraction import GraphParseError, parse_graph_response
raw = '```json\n{"triples":[{"subject":"Project "Cedar"","predicate":"USES","object":"SQLite"}]}\n```'
result = parse_graph_response(raw)
assert result.graph.triples[0].subject == 'Project "Cedar"'
assert result.graph.triples[0].predicate == "uses"
print("PARSER REPAIR OK:", ", ".join(result.repairs))
try:
    parse_graph_response('{"triples":[')
except GraphParseError as error:
    print("INVALID GRAPH REJECTED:", error.kind)
else:
    raise AssertionError("truncated output must fail")
PY
```

Expected:

```text
PARSER REPAIR OK: markdown_fence, interior_quote
INVALID GRAPH REJECTED: syntax
```

The parser accepts one complete object, optionally surrounded by nonstructural
prose or Markdown fencing. Repairs escape raw string control characters, invalid
backslashes, and interior quotes followed by ordinary text. It does not fill
missing fields, add delimiters, complete truncated JSON, evaluate Python literals,
accept duplicate keys, or combine multiple JSON objects. Uncertain structural
errors fail validation. Successful repairs are reported in `result.repairs`.

## 4. Manual worker extraction

```bash
python3 - <<'PY'
import asyncio
from src.config import get_settings
from src.extraction import Extractor
from src.models import AgentMessage
from src.network.ollama_client import OllamaClient

async def main():
    async with OllamaClient(get_settings()) as client:
        result = await Extractor(client).extract(
            AgentMessage(agent_id="verification", content="## Notes\nProject Cedar uses SQLite.", turn=1),
            node="node2", allow_fallback=False,
        )
        facts = {(t.subject.casefold(), t.predicate, t.object.casefold()) for t in result.graph.triples}
        assert facts == {("project cedar", "uses", "sqlite")}, result.graph.model_dump_json()
        assert result.generation.node == "node2"
        print("WORKER EXTRACTION OK")
        print(f"node={result.generation.node} model={result.generation.model} triples={len(facts)} retries={result.corrective_retries}")
        print(f"latency_ms={result.latency_ms:.2f} repairs={result.repairs}")

asyncio.run(main())
PY
```

Expected: `WORKER EXTRACTION OK`, then the configured worker model, `triples=1`,
`retries=0` or `1`, and measured latency/repair metadata. This initializes no
database and persists no records.

## 5. Limits, lifecycle, and failure handling

`AgentMessage` requires an agent ID and content; turn defaults to zero.
Messages are limited to **2,000 characters** and are never silently truncated.
`ExtractedGraph` requires `triples`, including for an empty graph. Each triple
requires string subject/predicate/object; confidence defaults to 1.0 and must be
a finite number in [0, 1]. Graphs contain at most **16 triples** with **256-character
labels**. Duplicate normalized facts retain the first occurrence and confidence.

The parser permits up to 65,536 response characters and 32 levels of JSON nesting.
The default generation budget is 512 tokens; callers can set 1–1,024 tokens.
The context remains 2,048 tokens. Character limits are not exact tokenizer limits;
split long source messages before extraction. Do not increase output indefinitely
to compensate for a model that fails validation.

`Extractor` prefers Node 2. Existing transport/timeout/5xx/408/429 fallback can try
Node 1. Schema, syntax, grounding, or token-budget failure triggers at most one
corrective generation on the node that responded. Set `max_retries=0` to disable
it. Each generation can attempt at most two nodes; at default timeouts two
generations with fallback can take approximately 1,200 seconds in the worst case.
Cancelling the extraction task stops client waiting without schema retry; the
server may still be finishing an already submitted inference request.

Subject/object names must occur literally in the source after case/whitespace
normalization and word-boundary checks. This prevents copying unrelated example
entities; it does not establish factual truth, relation correctness, or complete
coverage. Aliases and paraphrased endpoints are rejected. Live acceptance asserts
the expected relationships for its controlled messages.

Successful results retain message, graph, final generation, all generation
attempts, repairs, recovered validation errors, and total latency. Exhausted validation raises `ExtractionError`
with `.errors` and `.attempts`. Transport/HTTP/protocol errors retain existing
Ollama exception types. The caller owns the client's async context and cleanup.

## 6. Failure logs and acceptance

```bash
python3 -m pytest tests/test_extraction.py --run-lan -v -s --tb=long
```

| Failure | Check |
| --- | --- |
| Connection, timeout, or HTTP failure | Re-run Phase 1 health/model checks; inspect LAN routing, service state, and memory pressure. |
| Syntax/schema error | Inspect the error kind/path; verify local model tags and complete responses. |
| `truncated` | Keep the message short; inspect token budget, model loading, and output length. |
| `grounding` | Use subject/object names present in the source; inspect invented names or paraphrases. |
| Incorrect or missing relation | Preserve the test's returned graph; review the local model's semantic extraction. |
| Invalid input | Check bounds, field types, and extra fields before sending a request. |
| Skipped LAN checks | Use `--run-lan` with both nodes selected. |

Acceptance requires **111 Phase 3 checks passing without skips**, both-node
direct extraction with the expected fact sets, and **301 passing offline
regression tests**. After initial installation, repeat live checks with WAN
access disconnected while preserving LAN routing to verify local-only operation.

## 7. Commit message

```text
feat(phase3): add structured local graph extraction

Add strict message/triple schemas and bounded defensive JSON parsing.
Request JSON-mode Ollama output with two examples and one corrective retry.
Validate source endpoints and retain node, latency, repair, and attempt metadata.
Add offline failure tests, two-node live checks, and generic verification docs.
```

Run `git diff --check` and review staged changes before committing. Keep `.env`,
database files, and local reference notes untracked.
