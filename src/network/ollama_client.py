"""Asynchronous local Ollama health checks and bounded inference with failover."""

import asyncio
from dataclasses import dataclass
from time import perf_counter
from typing import TypeVar

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.config import NodeName, Settings

Payload = TypeVar("Payload", bound=BaseModel)


class _VersionPayload(BaseModel):
    model_config = ConfigDict(strict=True, str_strip_whitespace=True)
    version: str = Field(min_length=1)


class _ModelDetails(BaseModel):
    model_config = ConfigDict(strict=True)
    quantization_level: str | None = None


class _ModelEntry(BaseModel):
    model_config = ConfigDict(strict=True, str_strip_whitespace=True)
    name: str = Field(min_length=1)
    size: int | None = Field(default=None, ge=0)
    details: _ModelDetails = Field(default_factory=_ModelDetails)
    digest: str | None = None


class _TagsPayload(BaseModel):
    model_config = ConfigDict(strict=True)
    models: list[_ModelEntry]


class _GenerationPayload(BaseModel):
    model_config = ConfigDict(strict=True, str_strip_whitespace=True)
    model: str = Field(min_length=1)
    response: str = Field(min_length=1)
    done: bool
    total_duration: int | None = Field(default=None, ge=0)
    eval_count: int | None = Field(default=None, ge=0)
    prompt_eval_count: int | None = Field(default=None, ge=0)
    done_reason: str | None = None

    @field_validator("done")
    @classmethod
    def generation_finished(cls, value: bool) -> bool:
        if not value:
            raise ValueError("non-streaming generation must be complete")
        return value


@dataclass(frozen=True)
class HealthResult:
    node: NodeName
    url: str
    version: str
    latency_ms: float


@dataclass(frozen=True)
class InstalledModel:
    name: str
    quantization_level: str | None
    size_bytes: int | None
    digest: str | None = None


@dataclass(frozen=True)
class ModelInventory:
    node: NodeName
    url: str
    models: tuple[InstalledModel, ...]
    latency_ms: float


class OllamaError(RuntimeError):
    """A node-specific failure, including the failed attempt's elapsed time."""

    def __init__(
        self, node: NodeName, url: str, kind: str, detail: str,
        latency_ms: float, *, retryable: bool = False, status_code: int | None = None,
    ) -> None:
        self.node = node
        self.url = url
        self.kind = kind
        self.latency_ms = latency_ms
        self.retryable = retryable
        self.status_code = status_code
        super().__init__(f"{node} {url}: {kind}: {detail} ({latency_ms:.1f} ms)")


class OllamaUnavailableError(RuntimeError):
    """Both configured nodes failed; retain each cause for troubleshooting."""

    def __init__(self, errors: tuple[OllamaError, ...]) -> None:
        self.errors = errors
        super().__init__("All attempted Ollama nodes failed: " + "; ".join(map(str, errors)))


@dataclass(frozen=True)
class GenerationResult:
    node: NodeName
    model: str
    response: str
    latency_ms: float
    node_latency_ms: float
    server_duration_ms: float | None
    generated_tokens: int | None
    failed_attempts: tuple[OllamaError, ...]
    done_reason: str | None = None
    prompt_tokens: int | None = None

    @property
    def used_fallback(self) -> bool:
        return bool(self.failed_attempts)


class OllamaClient:
    """Own one reusable HTTP connection pool; use as an async context manager.

    Health checks always target the requested node. Inference may try the other
    node once after a transport failure, timeout, or HTTP 5xx/408/429 response.
    Invalid payloads and other HTTP 4xx errors remain visible without failover.
    """

    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.settings = settings
        self._http = httpx.AsyncClient(
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=2),
        )

    async def __aenter__(self) -> "OllamaClient":
        await self._http.__aenter__()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self._http.__aexit__(*args)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(
        self, node: NodeName, endpoint: str, schema: type[Payload],
        *, payload: dict | None = None, timeout_seconds: float,
    ) -> tuple[Payload, float]:
        url = self.settings.node_url(node) + endpoint
        started = perf_counter()

        def error(
            kind: str, detail: str, *, retryable: bool = False, status_code: int | None = None,
        ) -> OllamaError:
            return OllamaError(
                node, url, kind, detail, (perf_counter() - started) * 1000,
                retryable=retryable, status_code=status_code,
            )

        try:
            # HTTPX budgets socket inactivity; asyncio also caps total wall time.
            async with asyncio.timeout(timeout_seconds):
                response = await self._http.request(
                    "GET" if payload is None else "POST", url, json=payload,
                    timeout=httpx.Timeout(
                        timeout_seconds,
                        connect=min(self.settings.ollama_connect_timeout_seconds, timeout_seconds),
                    ),
                )
                response.raise_for_status()
                data = response.json()
                if isinstance(data, dict) and "error" in data:
                    raise error("protocol", str(data["error"])[:300])
                parsed = schema.model_validate(data)
        except (httpx.TimeoutException, TimeoutError) as exc:
            raise error(
                "timeout", f"request timed out (total budget {timeout_seconds:g}s; "
                f"connect budget {min(self.settings.ollama_connect_timeout_seconds, timeout_seconds):g}s)",
                retryable=True,
            ) from exc
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            detail = exc.response.text[:300] or exc.response.reason_phrase
            raise error("http", f"HTTP {status}: {detail}", status_code=status,
                        retryable=status >= 500 or status in (408, 429)) from exc
        except httpx.RequestError as exc:
            raise error("transport", str(exc), retryable=True) from exc
        except ValueError as exc:
            raise error("protocol", "invalid Ollama JSON response") from exc
        return parsed, (perf_counter() - started) * 1000

    async def ping(self, node: NodeName) -> HealthResult:
        """Time an HTTP /api/version round trip; this is not an ICMP ping."""
        parsed, latency = await self._request(
            node, "/api/version", _VersionPayload,
            timeout_seconds=self.settings.ollama_health_timeout_seconds,
        )
        return HealthResult(node, self.settings.node_url(node), parsed.version, latency)

    async def list_models(self, node: NodeName) -> ModelInventory:
        """Read locally installed model tags and quantization without downloading."""
        parsed, latency = await self._request(
            node, "/api/tags", _TagsPayload,
            timeout_seconds=self.settings.ollama_health_timeout_seconds,
        )
        models = tuple(
            InstalledModel(model.name, model.details.quantization_level, model.size, model.digest)
            for model in parsed.models
        )
        return ModelInventory(node, self.settings.node_url(node), models, latency)

    async def generate(
        self, prompt: str, *, node: NodeName = "node1", allow_fallback: bool = True,
        max_tokens: int = 32,
    ) -> GenerationResult:
        """Generate a short response using each attempted node's configured model."""
        if type(max_tokens) is not int or not 1 <= max_tokens <= 256:
            raise ValueError("max_tokens must be an integer in the range 1-256")
        return await self._generate(prompt, node=node, allow_fallback=allow_fallback, max_tokens=max_tokens)

    async def generate_json(
        self, prompt: str, *, system: str, node: NodeName = "node2",
        allow_fallback: bool = True, max_tokens: int = 512,
    ) -> GenerationResult:
        """Request non-streaming JSON with a bounded extraction output budget.

        JSON mode constrains syntax; the caller must validate the returned schema.
        This method leaves the short-response limits of generate() unchanged.
        """
        if not isinstance(system, str) or not system.strip():
            raise ValueError("system must be a nonempty string")
        if type(max_tokens) is not int or not 1 <= max_tokens <= 1024:
            raise ValueError("max_tokens must be an integer in the range 1-1024")
        return await self._generate(
            prompt, node=node, allow_fallback=allow_fallback, max_tokens=max_tokens,
            system=system,
        )

    async def _generate(
        self, prompt: str, *, node: NodeName, allow_fallback: bool, max_tokens: int,
        system: str | None = None,
    ) -> GenerationResult:
        self.settings.node_url(node)
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt must be a nonempty string")
        nodes = [node]
        if allow_fallback:
            nodes.append("node2" if node == "node1" else "node1")
        started = perf_counter()
        failures: list[OllamaError] = []
        for target in nodes:
            try:
                parsed, latency = await self._request(
                    target, "/api/generate", _GenerationPayload,
                    timeout_seconds=self.settings.ollama_inference_timeout_seconds,
                    payload={
                        "model": self.settings.node_model(target), "prompt": prompt,
                        "stream": False, "keep_alive": "5m",
                        **({"format": "json", "system": system} if system is not None else {}),
                        "options": {"num_predict": max_tokens, "num_ctx": 2048,
                                    "num_thread": self.settings.node_threads(target), "temperature": 0},
                    },
                )
            except OllamaError as exc:
                if not exc.retryable or not allow_fallback:
                    raise
                failures.append(exc)
                continue
            return GenerationResult(
                node=target, model=parsed.model, response=parsed.response,
                latency_ms=(perf_counter() - started) * 1000, node_latency_ms=latency,
                server_duration_ms=None if parsed.total_duration is None else parsed.total_duration / 1_000_000,
                generated_tokens=parsed.eval_count, failed_attempts=tuple(failures),
                done_reason=parsed.done_reason,
                prompt_tokens=parsed.prompt_eval_count,
            )
        raise OllamaUnavailableError(tuple(failures))
