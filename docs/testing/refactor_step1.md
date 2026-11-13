# Refactor Step 1: baseline verification

This is Step 1 of the post-experiment readability refactor, not a repeat of the
original network implementation phase. It establishes the reference for later
behavior-preserving changes. No application or experiment code was changed.
See the [scope and review sequence](../refactoring.md).

## 1. Setup

From the repository root, use the existing environment:

```bash
source .venv/bin/activate
python3 -m pip check
```

Expected: `No broken requirements found.` No dependency installation, model
requests, configuration edits or database migrations are needed.

## 2. Dedicated evidence and source checks

Run the read-only verifier:

```bash
python3 docs/testing/verify_refactor_baseline.py --check-source
```

Expected output:

```text
SOURCE BASELINE OK: 57 files match the pre-refactor reference
PUBLISHED EVIDENCE OK: 115 unchanged files; 6 publication manifests
ORIGINAL ANALYSIS OK: 80 turns; full summary unchanged
INTEGRATED ANALYSIS OK: 960 turns; full summary unchanged
REFACTOR BASELINE OK: no inference requests or evidence writes
```

The script checks the full original and integrated JSON summaries, including
costs and missing values, using the working analyzers. It checks checksums for
all published results, including the pruning probe and excluded attempts. It
does not rerun the probe, contact Ollama, initialize databases or export figures.
Failures print `BASELINE ERROR: ...` and exit nonzero. Stop and investigate any
mismatch; do not regenerate baseline hashes or modify historical evidence.

After an approved source refactor in a later step, run the same command without
`--check-source`. The four remaining output lines must still pass. The original
57 source hashes remain a reference even when maintenance intentionally changes
those files. A newly added source file does not appear in the old inventory;
this is a fixed baseline, not a complete future source-integrity scanner.

## 3. Full offline regression

```bash
python3 -m pytest -q
```

Expected: **848 passed, 18 skipped**; elapsed time varies. The skipped checks are
16 opt-in LAN cases and two storage/retrieval performance cases. Do not add
`--run-lan`, `--run-storage-benchmark` or `--run-retrieval-benchmark` for this step.
The ordinary test suite uses temporary databases and synthetic HTTP responses;
it does not run a new experiment.

## 4. Optional diagram checks

Node.js is used only for optional development checks of the standalone diagram.
Viewing the HTML needs a browser, and the Python application does not need Node.
If Node.js is already available:

```bash
node docs/testing/architecture-routing.mjs
```

Expected: **7 tests pass, 0 failures**. No npm packages or CI/CD setup is required.

## 5. Review the change

```bash
git diff --check
git status --short
git diff -- README.md
git check-ignore .env .verification/ data/experiments/
```

Expected: no whitespace errors; changes are limited to the README link and
new baseline/refactoring documentation and verification files. The three private
or generated paths remain ignored. Before staging, new documentation appears as
untracked files and is not included in `git diff`; review it directly.
There must be no modifications to `src/`, `experiments/`, `tests/`, requirements,
configuration examples, published result folders or the architecture diagram.

## Acceptance record

Verified on November 12, 2026 against starting revision
`97d0d64dbbb4b14d6a1a2897db3f6b3a60930491`:

- Dependencies: no broken requirements.
- Offline regression: **848 passed, 18 skipped in 88.73 seconds**.
- Diagram geometry: **7 passed, 0 failures**.
- Dedicated verifier: 57 source/configuration/test files, 115 published evidence
  files, six publication manifests, and both full analysis summaries matched.
- A deliberately incorrect checksum was rejected in an isolated verifier check;
  no real evidence file was altered to perform that check.
- Whitespace checks passed. Tracked files, private configuration and existing
  runtime data were unchanged by baseline test execution. Only the documentation
  and standalone verification files described above were subsequently added.

The baseline protects the recorded evidence and analysis behavior; it does not
prove that inference will reproduce the same answers or that future refactors
will preserve model latency. No new inference was performed.

Suggested commit message:

```text
docs: establish refactor baseline and preserve experiment evidence

Record the pre-refactor source reference and published artifact hashes.
Add read-only verification for historical evidence and both complete analyses.
Document the staged readability refactor and offline acceptance protocol.
```

Stop after this step and review the baseline before Step 2.
