"""Explicit opt-in for tests that require the physical LAN nodes."""

import pytest


def pytest_addoption(parser):
    parser.addoption("--run-storage-benchmark", action="store_true", help="Measure committed storage inserts against the sub-millisecond target")
    parser.addoption("--run-retrieval-benchmark", action="store_true", help="Measure warm graph-context assembly against the 5 ms target")
    parser.addoption("--run-lan", action="store_true", help="Run real two-node Ollama tests")
    parser.addoption(
        "--lan-node", choices=("both", "node1", "node2"), default="both",
        help="Limit real requests to one node for partial setup checks; approval requires both",
    )


def pytest_collection_modifyitems(config, items):
    run_lan = config.getoption("--run-lan")
    selected = config.getoption("--lan-node")
    for item in items:
        if "storage_benchmark" in item.keywords and not config.getoption("--run-storage-benchmark"):
            item.add_marker(pytest.mark.skip(reason="pass --run-storage-benchmark to verify the storage-host latency target"))
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
            item.add_marker(pytest.mark.skip(reason="the role-cycle test requires both nodes; partial selection is insufficient"))
            continue
        node = item.callspec.params.get("node")
        if node is None:
            failed = item.callspec.params["failed_node"]
            node = "node2" if failed == "node1" else "node1"
        if node != selected:
            item.add_marker(pytest.mark.skip(reason=f"partial check selects only {selected}; full approval requires both"))
