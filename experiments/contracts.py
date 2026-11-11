"""Versioned benchmark records and bounded, local artifact serialization."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from experiments.scenario import scenario_hash, SCENARIO_VERSION
from src.telemetry import TurnMetrics


class Strict(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid", allow_inf_nan=False,
                              revalidate_instances="always")


class BenchmarkPlan(Strict):
    version: int = Field(default=1, ge=1, le=1)
    scenario_version: Literal[SCENARIO_VERSION] = SCENARIO_VERSION
    turns: int = Field(default=20, ge=15, le=20)
    repetitions: int = Field(default=2, ge=1, le=10)
    memory_budget: int = Field(default=1000, ge=128, le=1000)
    max_tokens: int = Field(default=128, ge=64, le=256)
    warmups_per_node: int = Field(default=1, ge=0, le=3)
    history_policy: Literal["oldest_whole_turns_first"] = "oldest_whole_turns_first"

    @property
    def scenario_sha256(self):
        return scenario_hash(self.turns)

    def order(self):
        return tuple((rep, condition) for rep in range(1, self.repetitions + 1)
                     for condition in (("A", "B") if rep % 2 else ("B", "A")))


class BenchmarkRow(Strict):
    version: int = Field(default=1, ge=1, le=1)
    source: Literal["measured", "synthetic"]
    bundle_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    condition: Literal["A", "B"]
    repetition: int = Field(ge=1, le=10)
    position: int = Field(ge=1, le=20)
    scenario_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    observation: str = Field(max_length=2000)
    subject: str = Field(min_length=1, max_length=256)
    expected: str = Field(min_length=1, max_length=2000)
    answer: str = Field(min_length=1, max_length=2000)
    correct: bool
    update_probe: bool
    delayed_probe: bool
    context_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    history_full_bytes: int = Field(ge=0)
    history_dropped_turns: int = Field(ge=0)
    observation_calls: int = Field(ge=0)
    observation_prompt_tokens: int | None = Field(ge=0)
    observation_generated_tokens: int | None = Field(ge=0)
    observation_latency_ms: float = Field(ge=0)
    total_prompt_tokens: int | None = Field(ge=0)
    total_generated_tokens: int | None = Field(ge=0)
    total_inference_ms: float = Field(ge=0)
    wall_ms: float = Field(ge=0)
    telemetry: TurnMetrics

    @model_validator(mode="after")
    def totals(self):
        for total, a, b in ((self.total_prompt_tokens, self.telemetry.prompt_tokens, self.observation_prompt_tokens),
                            (self.total_generated_tokens, self.telemetry.generated_tokens, self.observation_generated_tokens)):
            if total != (None if a is None or b is None else a + b):
                raise ValueError("inconsistent total usage")
        if abs(self.total_inference_ms - self.telemetry.generation_latency_ms - self.observation_latency_ms) > 0.000001:
            raise ValueError("inconsistent inference timing")
        if self.condition == "A" and (self.observation_calls or self.telemetry.extraction_calls or
                self.telemetry.active_nodes or self.telemetry.active_edges):
            raise ValueError("linear baseline must not use extraction or graph memory")
        return self


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1_048_576), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path, data):
    """Replace a manifest atomically; model/experiment execution is never implicit."""
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(data, stream, allow_nan=False, ensure_ascii=True, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def load_json(path):
    def constant(_value):
        raise ValueError("nonfinite JSON")

    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result
    return json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=pairs,
                      parse_constant=constant)
