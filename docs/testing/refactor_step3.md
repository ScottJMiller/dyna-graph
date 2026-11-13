# Refactor Step 3: explicit shared helpers

This step separates reusable experiment support from the study runners and makes
shared core functions explicit. It does not restructure the orchestration loop,
change storage behavior or rerun either model-driven experiment.

## Changes to review

- `experiments/common.py` now owns the shared role construction, recorded
  extractor, failure diagnostics, bounded history, cost accounting, fixed-route
  checks, installed-model inventory and source-provenance collection. Its classes
  and functions have docstrings. Study execution and scoring remain separate.
- Both runners import this support directly. The original CSV column order and
  `flatten_row` live beside `BenchmarkRow` in `experiments/contracts.py`.
  The analyzers no longer import either executable harness for helper access.
- Core functions retain their original owners and implementation, with explicit
  shared names and docstrings. Import/call sites are updated; no compatibility
  wrappers or broad utilities package were added.
- Existing experiment tests use the new helper imports. One new regression test
  starts a fresh interpreter and rejects analysis imports that load a runner,
  load settings, initialize storage or send HTTP requests.
- The text architecture inventory and interactive map describe the new common
  module. The map now contains **80 components and 161 connectors**.

| Owner | Explicit shared functions |
| --- | --- |
| `src/storage/graph_store.py` | `normalize_text`, `validate_id`, `serialize_properties`, `validate_confidence` |
| `src/governance/decay.py` | `validate_number`, `validate_count`, `usage_from_row` |
| `src/retrieval/seed_matcher.py` | `validate_limit`, `normalize_subgoal` |
| `src/retrieval/context_assembler.py` | `quote_label` |

Implementation-only helpers remain private. Core source statements are unchanged
apart from these helper names/imports and their docstrings. In particular, the
SQL, database schema, transaction boundaries, governance formulas, workflow
recovery and cancellation logic are unchanged. All published evidence, baseline
hashes, scenarios, prompts, scoring, retry policies and dependency pins remain
unchanged. Source-provenance collection already includes every experiment Python
module, so future runs will record the new `common.py` too; historical manifests
must not be rewritten to include it.

## 1. Setup and historical evidence

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

Omit `--check-source`: maintenance intentionally changes source files and adds a
module. Do not update the original baseline inventory to make source hashes
match the new implementation. All historical evidence and both complete JSON
summaries must continue to match.

## 2. Dedicated automated experiment checks

```bash
python3 -m pytest tests/test_phase8_benchmark.py tests/test_phase8_integrated.py -q
```

Expected: **181 passed**, no skips (65 original-study checks and 116 integrated
checks). The existing tests exercise the shared helpers through synthetic model
responses, including failure records, missing counters, fixed routes and
bounded-history replay. The new import-boundary test forbids configuration,
database and HTTP work in a fresh interpreter, then confirms that importing the
common module and both analyzers has not loaded either study runner.

For full acceptance instead, run:

```bash
python3 -m pytest -q
```

Expected: **849 passed, 18 skipped**. This includes the 181 checks above; running
both commands is unnecessary on an unchanged checkout. The extra passing test
compared with Step 2 is the import-boundary regression. The skipped checks are
16 opt-in LAN cases and two optional performance cases. Do not enable live LAN
checks or execute a model-driven study for this step.

## 3. Plan-only entry points

```bash
python3 -m experiments.benchmark_harness
python3 -m experiments.integrated_harness
```

Expected first lines, respectively:

```text
PLAN ONLY: 4 condition runs; 80 scored turns; no model requests or experiment files
PLAN ONLY: 16 condition runs; 960 scored turns; no model requests or experiment files
```

Both also print their unchanged plans as JSON. Their complete standard output
must match the pre-Step-3 implementation. Settings and runtime data are not
loaded by these plan-only commands.

## 4. Optional diagram verification

If Node.js is already available, run the dependency-free checks:

```bash
node docs/testing/architecture-routing.mjs
```

Expected: **7 passed, 0 failures**, checking all 161 connectors. No Node version
pin, npm dependencies or CI/CD were introduced. The standalone page remains
viewable directly in a browser without Node.js or a server.

## 5. Scope and whitespace

```bash
git diff --check
git status --short
git diff --stat
```

Expected: no whitespace errors. Review the common module, import/call changes,
original CSV projection relocation, two adapted experiment test files, one new
import-boundary test and the accompanying architecture/refactoring documents.
There must be no changes to database SQL, scenario files, requirements, the
baseline inventory or published evidence. Private `.env` and existing runtime
data remain untouched. Do not commit generated verification outputs.

## Acceptance record

Verified on November 12, 2026:

- Full offline regression: **849 passed, 18 skipped in 92.09 seconds**.
- Both complete historical JSON summaries matched; 115 published evidence files
  and six publication checksum manifests were unchanged.
- Both harnesses produced byte-identical plan-only stdout compared with the
  pre-Step-3 checkout.
- All seven diagram checks passed against 80 components and 161 connectors.
- Structural comparison confirmed unchanged moved-helper logic and core logic,
  allowing helper renames/import changes and added docstrings.
- The import-boundary regression also passed after extending its guard to direct
  `Settings` construction, in addition to `get_settings`.
- The live checkout, private configuration and runtime data were unchanged by
  isolated verification. Only the reviewed Step 3 files were subsequently applied.

The structural review compares the moved helper code
and renamed core functions against the prior implementation, allowing only
helper naming/import changes and added docstrings. This supports behavior
preservation alongside the runtime tests; it does not establish identical future
model answers or timings. No new inference is performed.

See the [refactoring scope](../refactoring.md). Step 4 remains a separate review.

Suggested commit message:

```text
refactor: separate shared experiment helpers from study runners

Move common roles, inference accounting and provenance into experiments/common.py.
Keep the original CSV projection with its record contract and clarify shared core APIs.
Preserve historical results and guard analysis imports against execution side effects.
Update architecture documentation and the Step 3 verification protocol.
```
