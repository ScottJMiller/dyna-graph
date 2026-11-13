"""Shared offline setup and explicit opt-ins for LAN and timing checks."""

import os

import pytest

from src.config import Settings


def pytest_addoption(parser):
    """Expose explicit opt-ins; ordinary test runs stay offline."""
    parser.addoption(
        "--run-storage-benchmark",
        action="store_true",
        help="Measure committed storage inserts against the sub-millisecond target",
    )
    parser.addoption(
        "--run-retrieval-benchmark",
        action="store_true",
        help="Measure warm graph-context assembly against the 5 ms target",
    )
    parser.addoption("--run-lan", action="store_true", help="Run real two-node Ollama tests")
    parser.addoption(
        "--lan-node", choices=("both", "node1", "node2"), default="both",
        help="Limit real requests to one node for partial setup checks; approval requires both",
    )


def pytest_collection_modifyitems(config, items):
    """Skip hardware-dependent checks unless the requested opt-in allows them."""
    run_lan = config.getoption("--run-lan")
    selected = config.getoption("--lan-node")
    for item in items:
        if "storage_benchmark" in item.keywords and not config.getoption("--run-storage-benchmark"):
            item.add_marker(
                pytest.mark.skip(reason="pass --run-storage-benchmark to verify the storage-host latency target"),
            )
        if "retrieval_benchmark" in item.keywords and not config.getoption("--run-retrieval-benchmark"):
            item.add_marker(pytest.mark.skip(reason="pass --run-retrieval-benchmark to verify local retrieval latency"))
        if "lan" not in item.keywords:
            continue
        if not run_lan:
            item.add_marker(pytest.mark.skip(reason="requires physical Ollama nodes; pass --run-lan"))
            continue
        if selected == "both":
            continue
        if "both_nodes" in item.keywords:
            item.add_marker(
                pytest.mark.skip(reason="the role-cycle test requires both nodes; partial selection is insufficient"),
            )
            continue
        node = item.callspec.params.get("node")
        if node is None:
            failed = item.callspec.params["failed_node"]
            node = "node2" if failed == "node1" else "node1"
        if node != selected:
            item.add_marker(
                pytest.mark.skip(reason=f"partial check selects only {selected}; full approval requires both"),
            )


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch, request):
    """Keep offline cases independent of workstation settings and legacy aliases.

    Live LAN cases retain their configured environment. Tests that need to
    check environment precedence set their own values after this fixture runs.
    Individual Settings constructions still choose whether to read a .env file.
    """
    if "lan" in request.keywords:
        return
    setting_names = {name.upper() for name in Settings.model_fields}
    for field in Settings.model_fields.values():
        if field.validation_alias is not None:
            setting_names.update(alias.upper() for alias in field.validation_alias.choices)
    for name in list(os.environ):
        if name.upper() in setting_names:
            monkeypatch.delenv(name)


@pytest.fixture
def settings():
    """Provide default offline settings without loading the private .env file.

    Phases that need different policies define a local settings fixture or
    construct their own Settings explicitly.
    """
    return Settings(_env_file=None, node2_ollama_url="http://192.168.1.20:11434")
