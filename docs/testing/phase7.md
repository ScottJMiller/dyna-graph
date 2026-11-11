# Phase 7: Telemetry and terminal dashboard verification

This protocol records Phase 7 acceptance. Current aggregate counts and the
synthetic-only experiment implementation checks are in [Phase 8](phase8.md).

Phase 7 adds measured turn usage, graph/RAM snapshots, a live Rich terminal
view, and local CSV export. Everything runs on the coordinator using the existing
dependencies. No hosted analytics, cloud API, additional service, model download,
or paid dependency is required. Phase 8's controlled experiment remains planned.

## 1. Setup and prerequisites

From the repository root:

```bash
source .venv/bin/activate
command -v python3
python3 --version
python3 -m pip check
mkdir -p .verification/phase7
```

Expect the virtual environment's `python3`, Python 3.11 or later, and
`No broken requirements found.` Requirements and configuration keys are unchanged.
Keep the working `.env`; do not replace it with the illustrative `.env.example`.
Schema version remains **3**: telemetry uses the existing turn JSON column.

## 2. Dedicated automated tests

Run without Ollama or network access:

```bash
python3 -m pytest tests/test_phase7_telemetry.py -v -s
```

Expected: **82 passed, 1 skipped**. The skipped check requires both real nodes.
Coverage includes measured/missing/invalid counters; role and extraction JSON
corrections; fallback costs; active graph snapshots; rollback; export after
restart; old checkpoints; CSV quoting, flush, validation and idempotence;
bounded display history; live export visibility; pause/quit/cancellation;
export failure recovery; CSV ownership checks before inference; repeated task
launches; commits while the display is paused; CLI argument validation; and a
synthetic offline demo.

A focused diagnostic run:

```bash
python3 -m pytest tests/test_phase7_telemetry.py -k 'committed_metrics or rollback_together or reopen_exports or logs_while_execution' -v -s
```

Expected: **6 passed, 77 deselected**, with these diagnostic lines:

```text
TELEMETRY OK: prompt_tokens=300 active_nodes=2 active_edges=1
TELEMETRY OK: prompt_tokens=420 active_nodes=2 active_edges=1
TELEMETRY OK: prompt_tokens=300 active_nodes=2 active_edges=1
ROLLBACK OK: no metric row or graph changes escaped the failed checkpoint
CSV RECOVERY OK: 3 unique turns; no repeated inference
DASHBOARD OK: 3 committed rows exported; final status COMPLETED
```

These values come from deterministic HTTP fixtures. They are not model or
experiment performance measurements. Corrective generations count toward usage;
unavailable token counters are never replaced with the retrieval byte bound.

For the CSV reuse and pause regression checks:

```bash
python3 -m pytest tests/test_phase7_telemetry.py -k 'reused_csv or repeated_tasks or commits_while_display_paused' -v -s
```

Expected: **5 passed, 78 deselected**. The checks prove that repeated tasks use
separate default CSV files, a mismatched explicit CSV is rejected before model
requests, and committed turns keep logging while the display is paused.

## 3. Offline terminal and CSV smoke check

```bash
python3 -m src.telemetry.dashboard --demo --plain --csv .verification/phase7/demo.csv
```

Expect a `SIMULATED DEMO` panel showing three turns, 300 prompt tokens per turn,
1,400 ms generation latency, graph size `2/1`, and 32.0 MiB synthetic Python RSS.
It ends with:

```text
SIMULATED DEMO OK: 3 turns; no model requests or database writes
CSV: .verification/phase7/demo.csv
```

Verify the actual CSV with a standalone script:

```bash
python3 - <<'PY'
import csv
from pathlib import Path
from src.telemetry import CSVLogger

path = Path(".verification/phase7/demo.csv")
with path.open(encoding="utf-8", newline="") as stream:
    reader = csv.DictReader(stream)
    assert tuple(reader.fieldnames) == CSVLogger.columns
    rows = list(reader)
assert len(rows) == 3
assert [int(row["step"]) for row in rows] == [1, 2, 3]
assert all(row["run_id"] == "demo" for row in rows)
assert all(int(row["prompt_tokens"]) == 300 for row in rows)
assert all((int(row["active_nodes"]), int(row["active_edges"])) == (2, 1) for row in rows)
print("CSV INTEGRITY OK: 3 unique turns; schema and measured columns valid")
PY
```

Expected:

```text
CSV INTEGRITY OK: 3 unique turns; schema and measured columns valid
```

Rerun both commands: the CSV still contains exactly three rows, with one header.
For an animated preview, omit `--plain`. The demo finishes automatically; its
synthetic values must not be used as research results. Real task controls are
verified by the dedicated suite and the optional interactive check below.

## 4. Direct two-node live acceptance

Both configured Q4_K_M models must be installed and reachable. This test uses a
temporary graph, checks both model inventories, runs one planner turn on Node 1
and extraction on Node 2, and exports the resulting checkpoint. **Fallback is
disabled**, so both actual routes must work:

```bash
python3 -m pytest tests/test_phase7_telemetry.py --run-lan -m lan -v -s
```

Expected: **1 passed, 82 deselected**, with:

```text
LIVE TELEMETRY OK: role=node1 extraction=node2 prompt_tokens=<positive integer> generation_ms=<positive number> CSV_rows=1
```

Development acceptance completed in about **45 seconds**, with 427 prompt tokens
across the role and extraction calls. Those are one test-run observations;
model versions, loading, hardware, output length, and retries affect results.
This checks telemetry transport and export, not the experiment's hypothesis.

To run all dedicated tests, including live acceptance:

```bash
python3 -m pytest tests/test_phase7_telemetry.py --run-lan -q
```

Expected: **83 passed**. Selecting only one node with `--lan-node` skips the
two-node check and is insufficient for full acceptance.

## 5. Optional real interactive workflow

This demonstration writes to an isolated database under `.verification/`, using
the configured local models. It does not modify the main graph:

```bash
DATABASE_PATH=.verification/phase7/dashboard.db python3 -m src.telemetry.dashboard --task "Repeat this fact exactly: Project Cedar uses SQLite." --subgoal "Project Cedar" --turns 3
```

Expect a printed `RUN_ID`, CSV path, live stage changes (`RECALL`, `GENERATE`,
`EXTRACT`, `COMMIT`), current role/preferred node, and rows as turns commit. Short
stages may finish between refreshes. The default route is planner/node1 →
researcher/node2 → verifier/node1; extraction defaults to Node 2 every turn.
Actual fallback node information appears in committed rows.

Each `--task` starts a **new run**, even with identical task text and the same
database. Omitting `--csv` gives each run its own ignored
`data/telemetry/<run_id>.csv`; the command above can be repeated safely. Use
`--resume RUN_ID` to continue an existing run instead of starting another.

If you previously used a shared path such as `.verification/phase7/dashboard.csv`,
keep it: it belongs to the run identified by its `run_id` column. A new run must
use a different file. CSV ownership is now checked before a new workflow is
created or inference starts. A mismatch reports the owning run and suggests
`--resume`, another path, or omitting `--csv`. It does not overwrite old data.
Pressing **p** only pauses display updates; it does not change run identity.

Press **p** to pause the display: `DISPLAY PAUSED` appears, while model execution
and CSV logging continue. Press **p** again to resume. Press **q** or **Ctrl+C**
to cancel execution; accepted SQLite writes finish before cleanup. The terminal
returns to its previous input mode. An interrupted run prints:

```text
STOPPED: committed turns are retained; resume with the printed RUN_ID.
```

The stop exits with code 130. Finishing normally prints:

```text
WORKFLOW OK: COMPLETED turns=3
```

Use the **same database** and replace `RUN_ID` with the printed identifier:

```bash
DATABASE_PATH=.verification/phase7/dashboard.db python3 -m src.telemetry.dashboard --resume RUN_ID
```

Resume continues the next uncommitted step; completed turns are not regenerated.
For a stale `RUNNING` checkpoint, first ensure its former process has stopped,
then add `--recover-interrupted`. See [Phase 6 recovery](phase6.md) for ownership
and configuration compatibility. Completed runs can be displayed/exported again.

`--plain` disables intermediate display refresh, retaining automatic CSV export
and the final snapshot. Non-terminal output also renders only the final view.
Terminal controls require a POSIX TTY; Ctrl+C remains the general stop control.

## 6. CSV recovery and programmatic use

Every new `StateMachine` turn automatically stores metrics with its graph update.
No dashboard is required to collect them. Export an existing run without model
requests, using the database in which it was created:

```bash
DATABASE_PATH=.verification/phase7/dashboard.db python3 -m src.telemetry.dashboard --resume RUN_ID --export-only --csv .verification/phase7/RUN_ID-recovered.csv
```

Expected:

```text
EXPORT OK: appended=<number of committed turns> older_without_telemetry=0
```

Repeating this export prints `appended=0`. The default CSV path, when `--csv`
is omitted, is ignored `data/telemetry/<run_id>.csv`. Use one file per run and
one writer per file. Append flushes each row to the OS; it does not fsync each
CSV row. Durable SQLite checkpoints are authoritative after power loss.
An incomplete, conflicting, reordered, or incompatible CSV is rejected rather
than silently repaired. Export to a **new path** to rebuild it from checkpoints.
Older Phase 6 turns have no recorded telemetry and increase
`older_without_telemetry`; historical measurements are not synthesized.

The programmatic API, inside an open workflow/database context:

```python
from src.telemetry import CSVLogger, TelemetryProfiler

history = await machine.history(run_id)
metrics = TelemetryProfiler.from_checkpoint(history[-1])
with CSVLogger("data/telemetry/example-run.csv") as logger:
    appended, skipped = await logger.export_run(machine, run_id)
```

Export uses pages of 100 checkpoints, retains at most 1,000 per-run CSV row
identities, and validates replay without requesting inference. Display history
is capped at 12 turns. The CLI polls workflow state twice per second; graph
counts and RSS are sampled once per committed turn, not every refresh.
No prompt or task content is exported; agent labels and run identifiers remain.

## 7. Measurement contract and limits

The CSV column order is `CSVLogger.columns`; its version is `schema_version=1`.

| Columns | Meaning |
| --- | --- |
| `schema_version`, `run_id`, `step`, `governance_turn` | Format version, run identity, local step, and database-wide committed governance turn. |
| `agent_id`, `node` | Logical role and its final successful inference node. Extraction routing stays in the detailed checkpoint. |
| `observed_at` | Unix seconds sampled during the accepted graph update. |
| `turn_work_ms` | Elapsed wall time from before the run claim to the graph/RSS sample; includes queue wait, recall, generation, extraction and governance, but excludes final checkpoint insertion/SQLite COMMIT and CSV/render time. |
| `agent_calls`, `extraction_calls` | Successful HTTP generation results, including JSON corrections and role tool rounds. They may contain model output later rejected and corrected. |
| `fallback_attempts` | Failed transport/HTTP requests retained before successful fallback responses in this committed turn. |
| `agent_prompt_tokens`, `extraction_prompt_tokens` | Sums of measured Ollama `prompt_eval_count` values in the indicated stage; blank if any response in that stage lacks the counter. |
| `prompt_tokens` | Role plus extraction measured counts; blank if either subtotal is unknown. Includes framing as counted by Ollama; does not use retrieval bytes. |
| `generated_tokens` | Sum of measured `eval_count` for all returned role/extraction generations; blank if any is missing. |
| `agent_latency_ms`, `extraction_latency_ms`, `generation_latency_ms` | Client wall-time sums for returned generations in each stage and both stages combined; include loading, HTTP overhead, and failed requests preceding successful fallback. |
| `context_budget_used`, `context_budget_limit`, `context_budget_method` | Retrieval-block budget and counting method, distinct from whole-prompt measured tokens. |
| `active_nodes`, `active_edges` | Active entities and active semantic triples with active endpoints, sampled after writes/pruning inside the same commit transaction. Historical/superseded triples and supersession provenance are excluded. |
| `process_rss_bytes`, `rss_source` | Current coordinator Python resident bytes from Linux `/proc/self/statm`; blank/`unavailable` where unsupported. Excludes both Ollama servers, remote RAM, and total cluster consumption. |

Only committed turns produce rows. Failed/uncommitted work is excluded, and token
usage of failed fallback requests is unknown even when their latency is retained.
These rows therefore do not measure all attempted work across failed runs or
full cluster resource consumption. The Phase 8 harness must state comparison
boundaries and conditions; Phase 7 does not establish token savings or accuracy.

## 8. Full regression and documentation checks

```bash
python3 -m pytest -q
```

Expected: **668 passed, 18 skipped**. The skips are 16 explicit LAN tests and two
opt-in local benchmarks. To include both established storage/retrieval benchmarks
on the repository's filesystem:

```bash
python3 -m pytest --run-storage-benchmark --run-retrieval-benchmark --basetemp .verification/phase7/regression -q
```

Expected: **670 passed, 16 skipped**. The storage test requires median committed
insert time below 1 ms; retrieval requires median/p95 below 5 ms. `--basetemp`
is a disposable test directory: pytest replaces it; use the exact dedicated path.

The atlas now documents all 65 components and 120 connections. If Node.js is
available, verify its connectors still use distinct perimeter ports and do not
overlap or pass through component interiors:

```bash
node --test docs/testing/architecture-routing.mjs
git diff --check
git check-ignore .env .verification/ data/telemetry/
git status --short
```

Expected: **7 geometry tests pass**, no whitespace errors, and all three private
runtime paths are ignored. Review staged changes; `.env`, databases, CSV output,
the implementation guide, and local notes must not be tracked. No Phase 8 source
files should be added.

## 9. Commit message

```text
feat(telemetry): add checkpoint-backed profiling and live terminal dashboard

- retain measured Ollama prompt usage alongside inference timing
- atomically record turn metrics, active graph size and coordinator Python RSS
- add bounded Rich monitoring with pause, safe stop and resume controls
- export validated per-run CSV with idempotent checkpoint recovery
- verify live two-node usage, CSV integrity and workflow regression behavior
- update generic setup, architecture atlas and Phase 7 verification protocol
```

Commit and push after reviewing and verifying the change. Phase 8 requires
separate approval.
