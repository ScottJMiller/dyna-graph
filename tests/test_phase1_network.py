"""Offline failure-path tests and opt-in real two-node health/inference tests."""

import asyncio
import json
import os

import httpx
import pytest
from pydantic import ValidationError
from rich.console import Console

from src.config import Settings
from src.network.ollama_client import OllamaClient, OllamaError, OllamaUnavailableError

console = Console(color_system=None)


@pytest.fixture(autouse=True)
def isolated_unit_environment(monkeypatch, request):
    # Unit tests never inherit workstation .env values or shell configuration.
    if "lan" not in request.keywords:
        names = {name.upper() for name in Settings.model_fields}
        for field in Settings.model_fields.values():
            alias = field.validation_alias
            if alias is not None:
                names.update(name.upper() for name in alias.choices)
        for name in list(os.environ):
            if name.upper() in names:
                monkeypatch.delenv(name)


@pytest.fixture
def settings():
    return Settings(_env_file=None, node2_ollama_url="http://192.168.1.20:11434")


def generated(model="qwen2.5:7b"):
    return {"model": model, "response": "OK", "done": True,
            "total_duration": 25_000_000, "eval_count": 1}


@pytest.mark.parametrize("url", [
    "http://localhost:11434", "http://127.0.0.1:11434/",
    "http://10.0.0.10:11434", "http://172.16.0.10:11434",
    "http://[::1]:11434", "http://[fd00::1]:11434",
])
def test_local_node_addresses(settings, url):
    configured = Settings(_env_file=None, node1_ollama_url=url,
                          node2_ollama_url=settings.node_url("node2"))
    assert configured.node_url("node1") == str(configured.node1_ollama_url).rstrip("/")


@pytest.mark.parametrize("url", [
    "http://192.168.1.X:11434", "http://8.8.8.8:11434",
    "http://example.com:11434", "http://0.0.0.0:11434",
    "http://169.254.1.2:11434", "http://192.0.2.1:11434",
    "http://224.0.0.1:11434", "http://[::]:11434",
    "http://[2001:4860:4860::8888]:11434", "ftp://192.168.1.20:11434",
    "http://user:secret@192.168.1.20:11434", "http://192.168.1.20:11434/api",
    "http://192.168.1.20:11434?key=value", "http://192.168.1.20:11434#fragment",
    "http://192.168.1.20:0", "http://192.168.1.20:65536",
    "http://localhost:11434", "http://127.0.0.1:11434", "http://[::1]:11434",
])
def test_invalid_worker_addresses(url):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, node2_ollama_url=url)


def test_missing_worker_address():
    with pytest.raises(ValidationError, match="node2_ollama_url"):
        Settings(_env_file=None)


def test_duplicate_node_hosts():
    with pytest.raises(ValidationError, match="different host addresses"):
        Settings(_env_file=None, node1_ollama_url="http://192.168.1.20:11434",
                 node2_ollama_url="http://192.168.1.20:11435")


@pytest.mark.parametrize("host", ["example.com", "8.8.8.8", "0.0.0.0", "http://192.168.1.10"])
def test_invalid_coordinator(settings, host):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, coordinator_host=host, node2_ollama_url=settings.node_url("node2"))


@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan"), 1801])
def test_invalid_timeout(settings, value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, node2_ollama_url=settings.node_url("node2"),
                 ollama_inference_timeout_seconds=value)


@pytest.mark.parametrize("model", ["", "   ", "bad model", "model:cloud", "model:7b-cloud"])
def test_invalid_or_cloud_model(settings, model):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, node2_ollama_url=settings.node_url("node2"), node1_ollama_model=model)


def test_dotenv_and_environment_precedence(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("NODE2_OLLAMA_URL=http://192.168.1.20:11434\nOLLAMA_HEALTH_TIMEOUT_SECONDS=12\n", encoding="utf-8")
    assert Settings(_env_file=env_file).ollama_health_timeout_seconds == 12
    monkeypatch.setenv("NODE2_OLLAMA_URL", "http://10.0.0.20:11434")
    configured = Settings(_env_file=env_file)
    assert configured.node_url("node2") == "http://10.0.0.20:11434"


def test_root_dotenv_path_is_independent_of_working_directory(tmp_path, monkeypatch):
    from src.config import ROOT
    monkeypatch.chdir(tmp_path)
    assert Settings.model_config["env_file"] == ROOT / ".env"
    assert ROOT.joinpath(".env.example").is_file()


@pytest.mark.parametrize("alias, node", [
    ("NODE1_MODEL", "node1"), ("NODE1_OLLAMA_MODEL", "node1"),
    ("NODE2_MODEL", "node2"), ("NODE2_OLLAMA_MODEL", "node2"),
])
def test_model_environment_aliases(monkeypatch, alias, node):
    monkeypatch.setenv(alias, "custom:local")
    configured = Settings(_env_file=None, node2_ollama_url="http://10.0.60.181:11434")
    assert configured.node_model(node) == "custom:local"


@pytest.mark.parametrize("alias", ["COORDINATOR_HOST", "NODE1_HOST_IP"])
def test_coordinator_environment_aliases(monkeypatch, alias):
    monkeypatch.setenv(alias, "10.0.10.82")
    configured = Settings(_env_file=None, node2_ollama_url="http://10.0.60.181:11434")
    assert configured.coordinator_host == "10.0.10.82"


def test_existing_cluster_dotenv_names(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "NODE1_OLLAMA_URL=http://localhost:11434\nNODE1_MODEL=qwen2.5:7b\n"
        "NODE1_HOST_IP=10.0.10.82\nNODE2_OLLAMA_URL=http://10.0.60.181:11434\n"
        "NODE2_MODEL=llama3.2:3b\nDATABASE_PATH=data/dynagraph.db\n"
        "SQLITE_WAL_MODE=True\nPRUNING_THRESHOLD=0.2\n", encoding="utf-8",
    )
    configured = Settings(_env_file=env_file)
    assert configured.coordinator_host == "10.0.10.82"
    assert configured.node_model("node1") == "qwen2.5:7b"
    assert configured.node_model("node2") == "llama3.2:3b"
    assert configured.node_url("node2") == "http://10.0.60.181:11434"


@pytest.mark.parametrize("env_alias, file_alias", [
    ("NODE1_MODEL", "NODE1_OLLAMA_MODEL"), ("NODE1_OLLAMA_MODEL", "NODE1_MODEL"),
])
def test_environment_precedence_across_aliases(tmp_path, monkeypatch, env_alias, file_alias):
    env_file = tmp_path / ".env"
    env_file.write_text(f"NODE2_OLLAMA_URL=http://10.0.60.181:11434\n{file_alias}=file:local\n", encoding="utf-8")
    monkeypatch.setenv(env_alias, "environment:local")
    assert Settings(_env_file=env_file).node_model("node1") == "environment:local"


def test_relative_database_path_survives_directory_rename(settings, tmp_path, monkeypatch):
    from src.config import ROOT
    monkeypatch.chdir(tmp_path)
    assert settings.database_path == ROOT / "data/dynagraph.db"
    assert not (tmp_path / "data").exists()


def test_absolute_database_path_creates_no_database(tmp_path):
    path = tmp_path / "state" / "dynagraph.db"
    configured = Settings(_env_file=None, node2_ollama_url="http://10.0.60.181:11434", database_path=path)
    assert configured.database_path == path
    assert not path.parent.exists()


@pytest.mark.parametrize("path", ["", "   ", "https://example.com/db"])
def test_invalid_database_path(path):
    with pytest.raises(ValidationError, match="DATABASE_PATH"):
        Settings(_env_file=None, node2_ollama_url="http://10.0.60.181:11434", database_path=path)


def test_database_path_cannot_be_existing_directory(tmp_path):
    with pytest.raises(ValidationError, match="directory"):
        Settings(_env_file=None, node2_ollama_url="http://10.0.60.181:11434", database_path=tmp_path)


@pytest.mark.parametrize("node", ["node1", "node2"])
@pytest.mark.parametrize("threads", [0, -1, 65])
def test_invalid_node_thread_budget(node, threads):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, node2_ollama_url="http://10.0.60.181:11434", **{f"{node}_ollama_num_threads": threads})


def test_example_settings_load_generic_lan_configuration():
    from src.config import ROOT
    configured = Settings(_env_file=ROOT / ".env.example")
    assert configured.coordinator_host == "192.168.1.10"
    assert configured.node_url("node1") == "http://localhost:11434"
    assert configured.node_url("node2") == "http://192.168.1.20:11434"
    assert configured.node_model("node1") == "qwen2.5:7b"
    assert configured.node_model("node2") == "llama3.2:3b"
    assert configured.node_threads("node1") == 4
    assert configured.node_threads("node2") == 4


def test_ping_and_client_cleanup(settings):
    async def scenario():
        def handler(request):
            assert request.method == "GET"
            assert request.url.path == "/api/version"
            assert request.extensions["timeout"]["connect"] == 5
            return httpx.Response(200, json={"version": "0.11.0"})
        async with OllamaClient(settings, transport=httpx.MockTransport(handler)) as client:
            result = await client.ping("node2")
            assert result.node == "node2"
            assert result.version == "0.11.0"
            assert result.latency_ms >= 0
        assert client._http.is_closed
    asyncio.run(scenario())


@pytest.mark.parametrize("models", [[], [
    {"name": "llama3.2:3b", "size": 2019393189, "details": {"quantization_level": "Q4_K_M"}},
]])
def test_model_inventory(settings, models):
    def handler(request):
        assert request.method == "GET"
        assert request.url.path == "/api/tags"
        return httpx.Response(200, json={"models": models})
    async def scenario():
        async with OllamaClient(settings, transport=httpx.MockTransport(handler)) as client:
            inventory = await client.list_models("node2")
            assert inventory.node == "node2"
            assert inventory.latency_ms >= 0
            assert len(inventory.models) == len(models)
            if models:
                assert inventory.models[0].name == "llama3.2:3b"
                assert inventory.models[0].quantization_level == "Q4_K_M"
                assert inventory.models[0].size_bytes == 2019393189
    asyncio.run(scenario())


@pytest.mark.parametrize("payload", [
    b"not JSON", b"[]", b"{}", b'{"models":"invalid"}',
    b'{"models":[{"name":123}]}', b'{"models":[{"name":""}]}',
    b'{"models":[{"name":"llama3.2:3b","size":-1}]}',
    b'{"models":[{"name":"llama3.2:3b","details":{"quantization_level":42}}]}',
])
def test_malformed_model_inventory(settings, payload):
    async def scenario():
        async with OllamaClient(settings, transport=httpx.MockTransport(lambda _: httpx.Response(200, content=payload))) as client:
            with pytest.raises(OllamaError, match="protocol"):
                await client.list_models("node2")
    asyncio.run(scenario())


def test_generate_request_and_metrics(settings):
    async def scenario():
        def handler(request):
            assert request.method == "POST"
            assert request.url.path == "/api/generate"
            payload = json.loads(request.content)
            assert payload["stream"] is False
            assert payload["model"] == settings.node_model("node1")
            assert payload["options"] == {"num_predict": 16, "num_ctx": 2048, "num_thread": 8, "temperature": 0}
            assert request.extensions["timeout"]["read"] == 300
            return httpx.Response(200, json=generated())
        async with OllamaClient(settings, transport=httpx.MockTransport(handler)) as client:
            result = await client.generate("Reply OK", max_tokens=16)
            assert result.response == "OK"
            assert result.server_duration_ms == 25
            assert result.generated_tokens == 1
            assert result.latency_ms >= result.node_latency_ms >= 0
            assert not result.used_fallback
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["connection", "timeout", "http503", "http408", "http429"])
@pytest.mark.parametrize("preferred", ["node1", "node2"])
def test_fallback_uses_other_nodes_model(settings, failure, preferred):
    configured = Settings(_env_file=None, node2_ollama_url=settings.node_url("node2"), node2_ollama_model="worker:local")
    other = "node2" if preferred == "node1" else "node1"
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if str(request.url).startswith(configured.node_url(preferred)):
            if failure == "connection":
                raise httpx.ConnectError("connection refused", request=request)
            if failure == "timeout":
                raise httpx.ReadTimeout("read timed out", request=request)
            return httpx.Response(int(failure[4:]), json={"error": "unavailable"})
        assert json.loads(request.content)["model"] == configured.node_model(other)
        assert json.loads(request.content)["options"]["num_thread"] == configured.node_threads(other)
        return httpx.Response(200, json=generated(configured.node_model(other)))

    async def scenario():
        async with OllamaClient(configured, transport=httpx.MockTransport(handler)) as client:
            result = await client.generate("Reply OK", node=preferred)
            assert result.node == other
            assert result.used_fallback
            assert len(result.failed_attempts) == 1
            assert result.failed_attempts[0].node == preferred
            assert result.failed_attempts[0].latency_ms >= 0
            assert len(calls) == 2
    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["ping", "generate"])
def test_direct_node_failure_never_falls_back(settings, operation):
    calls = []
    def handler(request):
        calls.append(request)
        raise httpx.ConnectError("unreachable", request=request)
    async def scenario():
        async with OllamaClient(settings, transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(OllamaError) as caught:
                if operation == "ping":
                    await client.ping("node2")
                else:
                    await client.generate("Reply OK", node="node2", allow_fallback=False)
            assert caught.value.node == "node2"
            assert caught.value.kind == "transport"
            assert len(calls) == 1
    asyncio.run(scenario())


def test_both_nodes_unavailable(settings):
    def handler(request):
        raise httpx.ConnectError("unreachable", request=request)
    async def scenario():
        async with OllamaClient(settings, transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(OllamaUnavailableError) as caught:
                await client.generate("Reply OK")
            assert [error.node for error in caught.value.errors] == ["node1", "node2"]
            assert all(error.retryable for error in caught.value.errors)
    asyncio.run(scenario())


@pytest.mark.parametrize("status", [302, 400, 401, 404])
def test_http_errors_and_redirects_do_not_fall_back(settings, status):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, headers={"Location": "https://example.com"}, json={"error": "request rejected"})
    async def scenario():
        async with OllamaClient(settings, transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(OllamaError) as caught:
                await client.generate("Reply OK")
            assert caught.value.status_code == status
            assert not caught.value.retryable
            assert len(calls) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("payload", [
    b"not JSON", b"[]", b'{"version":""}', b'{"version":123}',
    b'{"error":"model failure"}', b'{}',
])
def test_malformed_health_responses(settings, payload):
    async def scenario():
        async with OllamaClient(settings, transport=httpx.MockTransport(lambda _: httpx.Response(200, content=payload))) as client:
            with pytest.raises(OllamaError, match="protocol"):
                await client.ping("node1")
    asyncio.run(scenario())


@pytest.mark.parametrize("changes", [
    {"done": False}, {"done": "true"}, {"response": "   "},
    {"total_duration": -1}, {"eval_count": "1"}, {"model": ""},
])
def test_malformed_generation_responses(settings, changes):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=generated() | changes)
    async def scenario():
        async with OllamaClient(settings, transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(OllamaError, match="protocol"):
                await client.generate("Reply OK")
            assert len(calls) == 1
    asyncio.run(scenario())


def test_total_wall_clock_timeout(settings):
    configured = Settings(_env_file=None, node2_ollama_url=settings.node_url("node2"), ollama_health_timeout_seconds=0.01)
    async def handler(request):
        await asyncio.sleep(0.1)
        return httpx.Response(200, json={"version": "0.11.0"})
    async def scenario():
        async with OllamaClient(configured, transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(OllamaError) as caught:
                await client.ping("node1")
            assert caught.value.kind == "timeout"
            assert caught.value.retryable
    asyncio.run(scenario())


@pytest.mark.parametrize("kwargs", [
    {"prompt": ""}, {"prompt": "   "}, {"prompt": "OK", "max_tokens": 0},
    {"prompt": "OK", "max_tokens": 257}, {"prompt": "OK", "max_tokens": True},
    {"prompt": "OK", "node": "node3"},
])
def test_invalid_input_makes_no_request(settings, kwargs):
    def handler(request):
        pytest.fail("invalid input must not make an HTTP request")
    async def scenario():
        async with OllamaClient(settings, transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(ValueError):
                await client.generate(**kwargs)
    asyncio.run(scenario())


def test_proxy_environment_is_ignored(settings, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://8.8.8.8:8080")
    monkeypatch.setenv("HTTPS_PROXY", "http://8.8.8.8:8080")
    monkeypatch.setenv("ALL_PROXY", "http://8.8.8.8:8080")
    async def scenario():
        async with OllamaClient(settings) as client:
            assert client._http.trust_env is False
            assert not client._http.follow_redirects
            assert not client._http._mounts
    asyncio.run(scenario())


@pytest.mark.lan
@pytest.mark.parametrize("node", ["node1", "node2"])
def test_lan_ping(node):
    async def scenario():
        async with OllamaClient(Settings()) as client:
            result = await client.ping(node)
            assert result.node == node
            assert result.version
            assert result.latency_ms >= 0
            console.print(f"HEALTH {node} url={result.url} version={result.version} latency_ms={result.latency_ms:.2f}")
    asyncio.run(scenario())


@pytest.mark.lan
@pytest.mark.parametrize("node", ["node1", "node2"])
def test_lan_installed_model(node):
    configured = Settings()
    async def scenario():
        async with OllamaClient(configured) as client:
            inventory = await client.list_models(node)
            models = {model.name: model for model in inventory.models}
            name = configured.node_model(node)
            assert name in models, f"{node}: configured model {name!r} missing; installed: {list(models)}"
            assert models[name].quantization_level == "Q4_K_M"
            console.print(f"MODEL {node} name={name} quantization={models[name].quantization_level} latency_ms={inventory.latency_ms:.2f}")
    asyncio.run(scenario())


@pytest.mark.lan
@pytest.mark.parametrize("node", ["node1", "node2"])
def test_lan_inference(node):
    async def scenario():
        async with OllamaClient(Settings()) as client:
            result = await client.generate("Reply with the single word OK.", node=node, allow_fallback=False, max_tokens=16)
            assert result.node == node
            assert result.response.strip()
            assert not result.used_fallback
            assert result.latency_ms >= result.node_latency_ms >= 0
            console.print(f"INFERENCE {node} model={result.model} latency_ms={result.latency_ms:.2f} response={result.response!r}")
    asyncio.run(scenario())


@pytest.mark.lan
@pytest.mark.parametrize("failed_node", ["node1", "node2"])
def test_lan_fallback_with_simulated_outage(failed_node):
    # Inject only the preferred node's outage; the surviving node runs real inference.
    configured = Settings()
    survivor = "node2" if failed_node == "node1" else "node1"
    async def scenario():
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as real:
            async def handler(request):
                if str(request.url).startswith(configured.node_url(failed_node) + "/"):
                    raise httpx.ConnectError("controlled node outage", request=request)
                return await real.send(request)
            async with OllamaClient(configured, transport=httpx.MockTransport(handler)) as client:
                result = await client.generate("Reply with the single word OK.", node=failed_node, max_tokens=16)
                assert result.node == survivor
                assert result.used_fallback
                assert result.response.strip()
                assert len(result.failed_attempts) == 1
                assert result.failed_attempts[0].node == failed_node
                console.print(f"FALLBACK {failed_node} -> {survivor} latency_ms={result.latency_ms:.2f} response={result.response!r}")
    asyncio.run(scenario())
