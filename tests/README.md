# Automated tests

The test files are named by subsystem. They check software behavior and protect
against regressions; they do not rerun the completed measured experiments.

| File | Coverage |
| --- | --- |
| [test_network.py](test_network.py) | Settings, local Ollama HTTP behavior, failure handling and opt-in LAN checks. |
| [test_storage.py](test_storage.py) | SQLite schema, transactions, graph CRUD, search and optional insert timing. |
| [test_extraction.py](test_extraction.py) | Structured response parsing, validation, retries and optional model checks. |
| [test_governance.py](test_governance.py) | Supersession, retention scoring, pins and soft-pruning. |
| [test_retrieval.py](test_retrieval.py) | Active graph neighborhoods, context limits and optional retrieval timing. |
| [test_orchestration.py](test_orchestration.py) | Role execution, atomic checkpoints, cancellation and recovery. |
| [test_telemetry.py](test_telemetry.py) | Measurements, terminal controls and CSV export/recovery. |
| [test_benchmark.py](test_benchmark.py) | Original A/B harness, scoring, evidence validation, analysis and figures using synthetic HTTP. |
| [test_integrated.py](test_integrated.py) | Integrated four-condition protocol, failures, governance traces, accounting and figures using synthetic HTTP. |
| [conftest.py](conftest.py) | Shared environment isolation, default settings and explicit hardware-test options. |
| [fixtures/](fixtures/) | Saved malformed model replies used by failure regressions. |

## Run offline

From the repository root:

```bash
source .venv/bin/activate
python3 -m pytest --collect-only -q
python3 -m pytest -q
```

Expected: **869 tests collected**; **851 passed, 18 skipped** in the ordinary
run. Individual test names, parameter cases, assertions and opt-in behavior are
unchanged by the filename cleanup. Collection order follows the new filenames.
No real model requests occur during the ordinary offline suite.

Examples of focused offline checks:

```bash
python3 -m pytest tests/test_network.py -q
python3 -m pytest tests/test_benchmark.py tests/test_integrated.py -q
```

Expected: **114 passed, 8 skipped** for networking and **183 passed** across the
two experiment suites. The full offline run is sufficient for rename acceptance;
focused commands are alternatives for investigation, not required repetitions.

Real-node checks require `--run-lan`; timing checks require
`--run-storage-benchmark` or `--run-retrieval-benchmark`. See the existing
[verification guides](../docs/testing/phase1.md) for setup and detailed commands.
Do not enable hardware options solely to verify this rename.

## Historical paths and preserved evidence

The phase prefix was removed from each test filename: a historical name in the
form `test_phaseN_component.py` now corresponds to `test_component.py`.
Current documentation, commands and architecture links use the subsystem names.
Checksum-protected reports/manifests under `docs/results/` and the original
[pre-refactor inventory](../docs/testing/refactor_baseline.json) retain the paths
recorded when that evidence was produced. Those are historical provenance, not
current test-file names. Apply the naming rule when following a test command in
an archived report; do not rewrite measured artifact hashes to rename history.

Verify that publication evidence and both complete analyses remain unchanged:

```bash
python3 docs/testing/verify_refactor_baseline.py
node docs/testing/architecture-routing.mjs
git diff --check
```

Expected: **115 unchanged evidence files, six publication manifests**, identical
80-turn and 960-turn analyses, **8 atlas checks passed**, and no whitespace errors.
The atlas check is optional and needs Node.js only; the Python suite has no Node.js
dependency. No application behavior or research measurement changes in this cleanup.
