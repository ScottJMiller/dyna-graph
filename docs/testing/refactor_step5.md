# Refactor Step 5: readable tests and shared offline setup

This step changed test setup and presentation, not application behavior, and
retained the original filenames. A later naming cleanup removed their phase
prefixes; commands below use current subsystem filenames. Recorded acceptance
counts describe Step 5; see the [test guide](../../tests/README.md) for current
counts. No production module, experiment implementation, measured evidence,
scenario, dependency or model configuration changes were included in Step 5.

## Changes to review

- `tests/conftest.py` owns the common offline environment isolation and default
  settings fixture. Offline cases do not inherit workstation settings or legacy
  aliases. LAN-marked cases retain their real configured environment. Tests for
  environment precedence still set their own values after setup runs.
- Three identical default settings fixtures and seven repeated environment
  setup blocks are consolidated. Policy-specific settings and temporary database
  setup stay local to the phases that need them. All offline phases now use the
  same isolation, including persistence/governance cases that previously had no
  module-level environment fixture.
- Protocol-specific HTTP guards and fake servers remain in their own modules.
  Their fault injection and synthetic answers are not combined into a general
  simulation framework. Default settings still explicitly disable `.env` loading;
  individual tests can intentionally supply a temporary environment file.
- Compressed one-line suites, semicolon-separated statements, long records and
  long assertions are expanded. Comments and embedded subprocess-script strings
  remain intact. Fixture/helper docstrings and section comments explain the
  setup, failure regressions, measurement checks and opt-in LAN cases.
- Short synthetic fact variables have descriptive names. Test names, decorator
  parameters, assertion expressions, fixtures' generation data, pytest options,
  skip markers and expected values are retained.

The structural review checks **857 existing assertion statements** and the
unchanged test bodies across all nine phase files, allowing the documented
fixture relocation, helper-variable renaming and docstrings. Collection is
compared separately to ensure all **867 test IDs remain in the same order**.
The baseline inventory is not regenerated to describe the edited test files.

## 1. Setup and preserved research evidence

From the repository root:

```bash
source .venv/bin/activate
python3 -m pip check
python3 docs/testing/verify_refactor_baseline.py
```

Expected:

```text
No broken requirements found.
PUBLISHED EVIDENCE OK: 115 unchanged files; 6 publication manifests
ORIGINAL ANALYSIS OK: 80 turns; full summary unchanged
INTEGRATED ANALYSIS OK: 960 turns; full summary unchanged
REFACTOR BASELINE OK: no inference requests or evidence writes
```

Omit `--check-source`. Tests intentionally differ from the pre-refactor source
reference. The published evidence and both full saved JSON analyses must still
match. This verifier does not call either model or export experiment outputs.

## 2. Confirm collection and default offline execution

```bash
python3 -m pytest --collect-only -q
```

Expected final summary: **867 tests collected**; elapsed time varies. The same
test function names and parameterized cases remain available for selection.
Collection does not perform model requests or run performance measurements.

Then run the full offline suite:

```bash
python3 -m pytest -q
```

Expected: **849 passed, 18 skipped**. There are no new or removed tests. The
skipped checks remain the 16 opt-in LAN cases and two storage/retrieval timing
cases. The full suite exercises the shared fixtures and retained network,
storage, extraction, governance, retrieval, orchestration, telemetry and
experiment regressions. Do not enable live LAN or timing checks for Step 5.

## 3. Read and run a focused category

The ordinary offline run above is sufficient for acceptance. These commands
are examples for learning or investigating a particular failure, not extra
required repetitions after a successful full run:

```bash
python3 -m pytest tests/test_orchestration.py tests/test_telemetry.py -q
python3 -m pytest tests/test_benchmark.py tests/test_integrated.py -q
```

Expected, respectively: **158 passed, 2 skipped** and **181 passed**.
The first group covers durable workflow/cancellation/telemetry; the second uses
synthetic HTTP for experiment execution, failures and evidence validation.

`tests/conftest.py` documents the explicit test options. Existing LAN tests require
`--run-lan`, with `--lan-node` providing partial network checks only. Local
performance checks require `--run-storage-benchmark` or
`--run-retrieval-benchmark`. These flags and their skip behavior are unchanged;
they are not part of this step's acceptance run.

## 4. Review scope

```bash
git diff --check
git status --short
git diff --stat
```

Expected: no whitespace errors. Review the nine phase test files,
`tests/conftest.py`, updated refactoring notes and this new protocol. Existing
failure fixtures, runtime modules, experiment implementations, database SQL,
requirements, baseline inventory and published results remain unchanged.
Private `.env` and runtime data remain untouched.

The architecture HTML is unchanged in Step 5. Its redundant Implemented selector
and related status UI remain scheduled for removal in Step 6, alongside the final
README/navigation cleanup. No Node.js test is required for this unchanged atlas.

## Acceptance record

Verified on November 12, 2026:

- Full offline suite: **849 passed, 18 skipped in 88.45 seconds**.
- Collection: **867 identical test IDs in the same order**.
- Structural comparison: all **857 assertion statements**, existing test bodies,
  decorator/parameter values and pytest gates retained. The documented fixture
  relocation, helper-variable renaming and docstrings are the only semantic
  source differences allowed by this comparison.
- An isolated fixture probe confirmed offline aliases are removed and
  LAN-marked cases retain their configured environment; probe changes were
  restored before the process exited.
- Both complete historical JSON analyses matched. All 115 evidence files and
  six publication manifests passed their original checksums.
- Live source, existing configuration and runtime data were unchanged by
  isolated verification. Only reviewed Step 5 test/documentation files were
  subsequently applied.

The common fixture intentionally standardizes offline environment isolation while preserving
the live-LAN exception. Verification does not establish model accuracy or latency;
no new model-driven experiment is run.

See the [refactoring scope](../refactoring.md). Stop for review before Step 6.

Suggested commit message:

```text
test: simplify phase checks and share offline setup

Consolidate environment isolation and default settings in conftest.py.
Expand compressed tests and document fixtures, synthetic helpers and failure categories.
Preserve all test IDs, assertions, opt-in gates and published evidence.
Add the Step 5 verification protocol.
```
