"""Small, strict data contracts shared by extraction callers and parsers."""

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_MESSAGE_CHARS = 2000
MAX_TRIPLES = 16
MAX_LABEL_CHARS = 256


def normalize_label(value: str) -> str:
    """Preserve display casing while normalizing whitespace as storage does."""
    value.encode("utf-8")
    if any(ord(char) < 32 and not char.isspace() for char in value):
        raise ValueError("labels must not contain non-whitespace control characters")
    value = " ".join(value.split())
    if not value:
        raise ValueError("labels must contain non-whitespace text")
    return value


class _StrictModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", allow_inf_nan=False, validate_default=True)


class SemanticTriple(_StrictModel):
    """One affirmative fact; endpoints retain the source message's wording."""

    subject: str = Field(min_length=1, max_length=MAX_LABEL_CHARS)
    predicate: str = Field(min_length=1, max_length=MAX_LABEL_CHARS)
    object: str = Field(min_length=1, max_length=MAX_LABEL_CHARS)
    confidence: float = Field(default=1.0, ge=0, le=1)

    @field_validator("subject", "predicate", "object")
    @classmethod
    def labels_are_nonblank(cls, value: str) -> str:
        return normalize_label(value)

    @field_validator("predicate")
    @classmethod
    def canonical_predicate(cls, value: str) -> str:
        value = value.casefold()
        if len(value) > MAX_LABEL_CHARS:
            raise ValueError("normalized predicate exceeds the label character limit")
        return value


class ExtractedGraph(_StrictModel):
    """A required triples array; an explicit empty array is a valid no-fact result."""

    triples: list[SemanticTriple] = Field(max_length=MAX_TRIPLES)

    @model_validator(mode="after")
    def remove_duplicate_facts(self) -> "ExtractedGraph":
        seen: set[tuple[str, str, str]] = set()
        unique: list[SemanticTriple] = []
        for triple in self.triples:
            key = (triple.subject.casefold(), triple.predicate, triple.object.casefold())
            if key not in seen:
                seen.add(key)
                unique.append(triple)
        self.triples = unique
        return self


class AgentMessage(_StrictModel):
    """One bounded raw message; content is preserved, including formatting."""

    agent_id: str = Field(min_length=1, max_length=64)
    content: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)
    turn: int = Field(default=0, ge=0, le=1_000_000)

    @field_validator("agent_id")
    @classmethod
    def agent_is_nonblank(cls, value: str) -> str:
        return normalize_label(value)

    @field_validator("content")
    @classmethod
    def content_is_nonblank(cls, value: str) -> str:
        value.encode("utf-8")
        if not value.strip():
            raise ValueError("content must contain non-whitespace text")
        if "\x00" in value:
            raise ValueError("content must not contain NUL characters")
        return value
