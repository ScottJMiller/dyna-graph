"""Validated environment configuration for the two-node local cluster."""

from ipaddress import ip_address, ip_network
from pathlib import Path
import re
from typing import Literal

from pydantic import AliasChoices, Field, HttpUrl, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

NodeName = Literal["node1", "node2"]
ROOT = Path(__file__).resolve().parents[1]
LAN_NETWORKS = tuple(
    ip_network(network)
    for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")
)


def validate_local_host(host: str) -> str:
    """Accept localhost, loopback, RFC1918 IPv4, or IPv6 unique-local addresses."""
    host = host.lower()
    if host == "localhost":
        return host
    try:
        address = ip_address(host)
    except ValueError as exc:
        raise ValueError("use localhost or a literal LAN IP address; replace any X placeholder") from exc
    if address.is_loopback or any(address in network for network in LAN_NETWORKS):
        return host
    raise ValueError("address must be loopback or a private LAN IP; public/bind-all addresses are invalid")


class Settings(BaseSettings):
    """Load the root .env file; process environment values take precedence."""

    model_config = SettingsConfigDict(
        env_file=ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        frozen=True,
        allow_inf_nan=False,
        populate_by_name=True,
    )

    coordinator_host: str = Field(
        default="127.0.0.1", validation_alias=AliasChoices("COORDINATOR_HOST", "NODE1_HOST_IP"),
    )
    node1_ollama_url: HttpUrl = HttpUrl("http://localhost:11434")
    node2_ollama_url: HttpUrl
    node1_ollama_model: str = Field(
        default="qwen2.5:7b", validation_alias=AliasChoices("NODE1_MODEL", "NODE1_OLLAMA_MODEL"),
    )
    node2_ollama_model: str = Field(
        default="llama3.2:3b", validation_alias=AliasChoices("NODE2_MODEL", "NODE2_OLLAMA_MODEL"),
    )
    database_path: Path = Path("data/dynagraph.db")
    node1_ollama_num_threads: int = Field(default=8, ge=1, le=64)
    node2_ollama_num_threads: int = Field(default=4, ge=1, le=64)
    sqlite_wal_mode: bool = True
    sqlite_busy_timeout_ms: int = Field(default=5000, ge=1, le=60_000)
    sqlite_pool_size: int = Field(default=4, ge=1, le=16)
    ollama_connect_timeout_seconds: float = Field(default=5, gt=0, le=60)
    ollama_health_timeout_seconds: float = Field(default=10, gt=0, le=120)
    ollama_inference_timeout_seconds: float = Field(default=300, gt=0, le=1800)

    # Domain-specific single-value policy; JSON array in .env, empty by default.
    single_valued_predicates: tuple[str, ...] = ()
    pruning_threshold: float = Field(default=0.2, ge=0, le=1)
    pruning_batch_size: int = Field(default=100, ge=1, le=1000)
    pruning_interval_seconds: float = Field(default=60, gt=0, le=86400)
    decay_recency_half_life_seconds: float = Field(default=86400, gt=0, le=31536000)
    decay_turn_half_life: float = Field(default=10, gt=0, le=1_000_000)

    @field_validator("single_valued_predicates")
    @classmethod
    def normalize_single_valued_predicates(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) > 100:
            raise ValueError("at most 100 single-valued predicates are supported")
        normalized = tuple(dict.fromkeys(" ".join(item.split()).casefold() for item in value))
        if any(not item or len(item) > 256 for item in normalized):
            raise ValueError("single-valued predicates must have 1-256 characters")
        return normalized

    @field_validator("coordinator_host")
    @classmethod
    def coordinator_is_local(cls, value: str) -> str:
        return validate_local_host(value.strip())

    @field_validator("node1_ollama_url", "node2_ollama_url")
    @classmethod
    def endpoint_is_local(cls, value: HttpUrl) -> HttpUrl:
        validate_local_host((value.host or "").strip("[]"))
        if value.username is not None or value.password is not None:
            raise ValueError("Ollama URLs must not contain credentials")
        if value.path not in (None, "/") or value.query is not None or value.fragment is not None:
            raise ValueError("use a root Ollama URL without a path, query, or fragment")
        if value.port is None or value.port < 1:
            raise ValueError("URL port must be in the range 1-65535")
        return value

    @field_validator("node1_ollama_model", "node2_ollama_model")
    @classmethod
    def model_is_local(cls, value: str) -> str:
        value = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9._/-]+(?::[A-Za-z0-9._-]+)?", value):
            raise ValueError("use a nonempty local Ollama model name")
        if value.lower().endswith((":cloud", "-cloud")):
            raise ValueError("cloud models are prohibited; install a local quantized model")
        return value

    @field_validator("database_path", mode="before")
    @classmethod
    def database_path_is_nonempty(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip()
            if not value or "://" in value:
                raise ValueError("DATABASE_PATH must be a nonempty local filesystem path")
        return value

    @field_validator("sqlite_wal_mode")
    @classmethod
    def require_wal(cls, value: bool) -> bool:
        if not value:
            raise ValueError("SQLite WAL mode is required for local concurrent storage")
        return value

    @field_validator("database_path")
    @classmethod
    def resolve_database_path(cls, value: Path) -> Path:
        value = value.expanduser()
        resolved = (value if value.is_absolute() else ROOT / value).resolve()
        if resolved.is_dir():
            raise ValueError("DATABASE_PATH must identify a file, not an existing directory")
        return resolved

    @model_validator(mode="after")
    def worker_is_remote(self) -> "Settings":
        worker = (self.node2_ollama_url.host or "").strip("[]")
        if worker == "localhost" or ip_address(worker).is_loopback:
            raise ValueError("Node 2 must use its remote private LAN IP, not loopback")
        if self.node1_ollama_url.host == self.node2_ollama_url.host:
            raise ValueError("Node 1 and Node 2 must use different host addresses")
        return self

    def node_url(self, node: NodeName) -> str:
        if node not in ("node1", "node2"):
            raise ValueError("node must be 'node1' or 'node2'")
        return str(getattr(self, f"{node}_ollama_url")).rstrip("/")

    def node_model(self, node: NodeName) -> str:
        self.node_url(node)
        return getattr(self, f"{node}_ollama_model")

    def node_threads(self, node: NodeName) -> int:
        self.node_url(node)
        return getattr(self, f"{node}_ollama_num_threads")


def get_settings() -> Settings:
    """Load settings explicitly, without validation or network work at import time."""
    return Settings()
