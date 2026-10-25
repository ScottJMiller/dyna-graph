"""Conservative JSON cleanup; never invent fields or complete truncated graphs."""

from dataclasses import dataclass
import json
import re

from pydantic import ValidationError

from src.models.schemas import ExtractedGraph

MAX_RESPONSE_CHARS = 65_536
MAX_JSON_DEPTH = 32
_FENCE = re.compile(r"\A```(?:json)?[ \t]*\r?\n(.*?)\r?\n?```\Z", re.IGNORECASE | re.DOTALL)
_UNICODE_ESCAPE = re.compile(r"[0-9a-fA-F]{4}\Z")


class GraphParseError(ValueError):
    """A response failed syntax, schema, resource, or grounding validation."""

    def __init__(self, kind: str, detail: str) -> None:
        self.kind = kind
        self.detail = detail
        super().__init__(f"{kind}: {detail}")


@dataclass(frozen=True)
class ParsedGraph:
    graph: ExtractedGraph
    repairs: tuple[str, ...]


def _object_pairs(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise GraphParseError("syntax", "duplicate JSON keys are ambiguous")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise GraphParseError("syntax", "non-finite JSON numbers are prohibited")


def _loads(text: str) -> object:
    try:
        return json.loads(text, object_pairs_hook=_object_pairs, parse_constant=_reject_constant)
    except (json.JSONDecodeError, GraphParseError):
        raise
    except (ValueError, RecursionError):
        raise GraphParseError("limits", "JSON decoder numeric or nesting limit exceeded") from None


def _unwrap(text: str, repairs: list[str]) -> str:
    candidate = text.strip()
    if candidate.startswith("\ufeff"):
        candidate = candidate[1:].strip()
        repairs.append("leading_bom")
    fenced = _FENCE.fullmatch(candidate)
    if fenced:
        candidate = fenced.group(1).strip()
        repairs.append("markdown_fence")
    if candidate.startswith("{") and candidate.endswith("}"):
        return candidate
    first, last = candidate.find("{"), candidate.rfind("}")
    if first < 0 or last < first:
        raise GraphParseError("syntax", "expected one complete JSON object")
    surrounding = candidate[:first] + candidate[last + 1:]
    if any(char in surrounding for char in "{}[]"):
        raise GraphParseError("syntax", "ambiguous wrapper or incomplete JSON object")
    repairs.append("surrounding_text")
    return candidate[first:last + 1]


def _repair_strings(text: str, repairs: list[str]) -> str:
    """Escape only string-local defects; leave structural errors for rejection.

    Interior quotes followed by ordinary text can be escaped. Quotes adjacent
    to a colon, comma, or closing delimiter remain unchanged to avoid guessing.
    """
    output: list[str] = []
    in_string = False
    depth = 0
    index = 0
    while index < len(text):
        char = text[index]
        if not in_string:
            if char in "{[":
                depth += 1
                if depth > MAX_JSON_DEPTH:
                    raise GraphParseError("limits", "JSON nesting limit exceeded")
            elif char in "}]":
                depth -= 1
            if char == '"':
                in_string = True
            output.append(char)
            index += 1
            continue
        if char == "\\":
            following = text[index + 1:index + 2]
            if following and following in '"\\/bfnrt':
                output.extend((char, following))
                index += 2
                continue
            if following == "u" and _UNICODE_ESCAPE.fullmatch(text[index + 2:index + 6]):
                output.append(text[index:index + 6])
                index += 6
                continue
            output.append("\\\\")
            repairs.append("invalid_backslash")
        elif ord(char) < 32:
            output.append(json.dumps(char)[1:-1])
            repairs.append("control_character")
        elif char == '"':
            lookahead = index + 1
            while lookahead < len(text) and text[lookahead].isspace():
                lookahead += 1
            following = text[lookahead:lookahead + 1]
            delimiters = ":,}]"  # Structural adjacency remains authoritative.
            if not following or following in delimiters:
                in_string = False
                output.append(char)
            else:
                output.append('\\"')
                repairs.append("interior_quote")
        else:
            output.append(char)
        index += 1
    return "".join(output)


def parse_graph_response(text: str) -> ParsedGraph:
    """Return validated triples and a record of any syntax cleanup performed."""
    if not isinstance(text, str) or not text.strip():
        raise GraphParseError("syntax", "response must be a nonempty string")
    if len(text) > MAX_RESPONSE_CHARS:
        raise GraphParseError("limits", "response character limit exceeded")
    repairs: list[str] = []
    candidate = _unwrap(text, repairs)
    # Check depth even for valid JSON, without trusting recursive deserialization.
    _repair_strings(candidate, [])
    try:
        data = _loads(candidate)
    except json.JSONDecodeError:
        repair_notes: list[str] = []
        repaired = _repair_strings(candidate, repair_notes)
        if not repair_notes:
            raise GraphParseError("syntax", "invalid or truncated JSON") from None
        try:
            data = _loads(repaired)
        except (json.JSONDecodeError, RecursionError):
            raise GraphParseError("syntax", "JSON remains invalid after string cleanup") from None
        repairs.extend(repair_notes)
    except RecursionError:
        raise GraphParseError("limits", "JSON nesting limit exceeded") from None
    try:
        graph = ExtractedGraph.model_validate(data)
    except ValidationError as exc:
        issues = [f"{'.'.join(map(str, issue['loc'])) or 'graph'}: {issue['type']}"
                  for issue in exc.errors(include_input=False, include_url=False)[:5]]
        raise GraphParseError("schema", "; ".join(issues)) from None
    return ParsedGraph(graph, tuple(dict.fromkeys(repairs)))
