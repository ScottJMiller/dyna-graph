# Refactor Step 4: readable orchestration and storage ownership

This step reorganizes the controller into named operations while preserving its
existing behavior. Storage executable statements are unchanged; its new
docstrings explain who owns connections, how queued writes finish, and when the
caller must close the pool. No experiment is rerun.

## Changes to review

`StateMachine.step()` now shows the sequence directly:

1. Validate inputs and identify the next role.
2. Claim the READY checkpoint with an owner token.
3. Recall bounded context.
4. Execute the selected agent using the existing failure policy.
5. Extract the response and apply the existing synchronous acceptance policy.
6. Commit memory, governance, telemetry and the durable turn together.
7. Record a failure only if the uncommitted turn still belongs to this owner.

The extracted methods are `_claim_turn`, `_recall_context`, `_execute_agent`,
`_extract_response`, `_select_facts`, `_commit_turn` and `_record_turn_failure`.
They are internal controller operations, not a new public extension framework.
All classes/functions in this module have purpose/constraint docstrings.

The SQL bodies for claim, commit and failure recording are preserved. The
per-controller lock, owner-token compare-and-update, `_settled` cancellation
shielding, stage assignments and retry/failure policies remain in place.
Accepted fact writes, usage updates, pruning, telemetry and the turn checkpoint
remain inside one transaction. Models and CSV output remain outside it.

`Database` owns the connection pool and serializes queued writes. `GraphStore`
and `StateMachine` borrow that database; they never close it. The application
closes its own client and database. Use the asynchronous database context manager
or `aclose()` after queued writes so accepted work drains before pool shutdown.
No schema, storage queue implementation, connection policy or migration changes.

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

Do not add `--check-source` or regenerate the original baseline hashes. Source
changes are intentional; saved evidence and complete analysis summaries must
remain unchanged.

## 2. Dedicated controller and telemetry tests

```bash
python3 -m pytest tests/test_phase6_orchestration.py tests/test_phase7_telemetry.py -q
```

Expected: **158 passed, 2 skipped**. The two skipped cases require an explicit
live two-node opt-in. Existing automated checks cover:

- Restarting at the next uncommitted role without replaying committed work.
- Cancellation during generation and during an accepted commit.
- Exactly one durable turn after a cancelled commit finishes.
- Competing owners, a losing claim and guarded recovery.
- SQL failure rollback of graph updates, usage, provenance and telemetry.
- Fact acceptance policies, invalid extraction, empty graphs and role routing.
- Real versus governance clocks and committed telemetry/CSV consistency.

The existing tests already exercise these behaviors; no test is added merely
to assert that a particular helper exists or to duplicate the new method layout.

## 3. Full offline regression

For complete acceptance, run:

```bash
python3 -m pytest -q
```

Expected: **849 passed, 18 skipped**. This includes the controller/telemetry tests
above and all storage, extraction, governance, retrieval and experiment checks;
running both commands is unnecessary for the same unchanged checkout. The
skipped checks are 16 live LAN cases and two optional performance cases. Do not
enable LAN checks or launch a model-driven experiment during this refactor.

## 4. Plan-only entry points and scope

```bash
python3 -m experiments.benchmark_harness
python3 -m experiments.integrated_harness
git diff --check
git status --short
```

Expected first lines of the two harness commands:

```text
PLAN ONLY: 4 condition runs; 80 scored turns; no model requests or experiment files
PLAN ONLY: 16 condition runs; 960 scored turns; no model requests or experiment files
```

Their full JSON plan output remains unchanged. Whitespace checks produce no
errors. Step 4 changes only `src/orchestration/state_machine.py`, docstrings in
`src/storage/database.py`, the refactoring notes and this protocol. Previously
approved working-tree changes remain intact. There are no additional changes to
experiments, tests, SQL, dependency pins, baseline inventory, published evidence,
private configuration or runtime data.

The architecture HTML is unchanged by Step 4. Before the refactor is complete,
Step 6 will remove redundant controls, including the Implemented selector, and
review related status labels/filter code. That cleanup has its own verification;
Node.js geometry checks need not be repeated for this unchanged diagram.

## Acceptance record

Verified on November 12, 2026:

- Full offline regression: **849 passed, 18 skipped in 92.97 seconds**.
- Both full historical JSON summaries matched, and all 115 published evidence
  files and six publication manifests passed their original checksums.
- Both harnesses produced byte-identical plan-only stdout compared with the
  pre-Step-4 working tree.
- Structural comparison confirmed unchanged claim, commit and failure SQL
  bodies, unchanged database executable code, and unchanged other workflow
  methods apart from added docstrings.
- Existing working-tree changes, private configuration and runtime data were
  unchanged by isolated verification. Only the reviewed Step 4 files were
  subsequently applied.

Structural review compares the
three extracted transaction bodies with their originals and checks that storage
executable code, compatibility hashing, startup and resume/recovery logic are
unchanged apart from docstrings. This is maintenance verification, not evidence
of identical future model answers or measured latency. No new inference occurs.

See the [refactoring scope](../refactoring.md). Stop for review before Step 5.

Suggested commit message:

```text
refactor: clarify workflow stages and document storage ownership

Extract claim, recall, generation, extraction and commit operations from step().
Preserve atomic checkpoints, owner guards and cancellation settlement.
Document caller-owned storage lifetimes and the Step 4 verification protocol.
Record the final architecture control cleanup for Step 6.
```
