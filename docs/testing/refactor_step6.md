# Refactor Step 6: documentation and architecture navigation

This final step simplifies the public entry points and the offline architecture
atlas. Runtime source, tests, pinned dependencies, configuration and published
research evidence retain their existing contents. No model experiment is rerun.

## Changes to review

The README retains exactly two main sections: experiment/framework discussion
and practical setup. The complete experiment history remains in place. A short
starting-point table separates reading evidence, checking saved analyses,
previewing synthetic telemetry and running a configured local task. Detailed
API recipes move to [the usage guide](../usage.md). Existing phase protocols
retain their detailed hardware checks; the README's offline count is corrected.

The atlas removes the Implemented selector, redundant badges, planned legend,
obsolete per-component status metadata and unused filter/CSS code. It retains
all 80 components, 161 connections, coordinates, routing and line widths. Phase
and workflow navigation, supporting files, search, details, theme selection,
zoom/pan and standalone SVG export remain available. Architecture descriptions
now describe the completed original and integrated studies.

## 1. Dependencies and preserved evidence

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

Omit `--check-source`: source intentionally differs from the pre-refactor
reference. Keep the original baseline hashes. The dedicated verifier checks
published evidence and both complete saved analysis summaries without inference
or evidence writes.

## 2. Final offline regression suite

```bash
python3 -m pytest -q
```

Expected: **849 passed, 18 skipped**. Elapsed time varies; the acceptance run
took about 92 seconds. Sixteen skipped cases require opt-in LAN access and two
require opt-in timing measurements. Do not enable those options for this review.
The existing phase scripts cover network, persistence, extraction, governance,
retrieval, orchestration, telemetry and both experiment protocols.

## 3. README commands without models

```bash
python3 -m src.telemetry.dashboard --demo --plain --csv .verification/refactor-step6/demo.csv
python3 -m experiments.benchmark_harness
```

The demo prints a synthetic three-turn dashboard, then:

```text
SIMULATED DEMO OK: 3 turns; no model requests or database writes
CSV: .verification/refactor-step6/demo.csv
```

The CSV path may be displayed as an absolute path. Demo CSV output is ignored
verification data, not measured experiment evidence.

The harness starts with:

```text
PLAN ONLY: 4 condition runs; 80 scored turns; no model requests or experiment files
```

It then prints the existing JSON plan. Do not add `--execute`. Real-model and
persistent-storage examples in the usage guide are documentation for later use,
not acceptance commands for this refactor.

## 4. Architecture and navigation

Optional geometry checks use Node.js built-in modules only; no npm installation
or CI service is needed. Viewing the atlas itself needs only a browser.

```bash
node docs/testing/architecture-routing.mjs
```

Expected: **7 tests, 7 pass, 0 fail**. All 161 connectors have unique perimeter
ports and retain orthogonal routes without card collisions or shared tracks.

Open `docs/architecture.html` directly in a browser and follow the
[interaction checklist](architecture.md#2-check-interactions). Confirm:

- No implementation-status selector, repeated status badges or planned legend.
- Initial 32 core components; Supporting files reveals all 80.
- Search for `database.py`, select a result and inspect its source link and
  connections. Search for a test file and confirm selection reveals it.
- Phase/workflow focus, Reset, keyboard/pointer zoom and pan work.
- Light/dark choice persists on reload when browser storage is available.
- Export SVG produces a readable vector image in the current theme.
- At mobile width, controls wrap and the inspector moves below the map.
- Reloading the standalone file requires no external assets or model services;
  the browser console has no JavaScript errors.

In the README, follow the usage-guide links and its six topic links. Check that
source, phase-protocol and experiment-history links still reach their targets.
The README should explain the experiment first and provide the practical steps
second. The complete chronology, reported failures and limitations remain.

## 5. Review scope

```bash
git diff --check
git status --short
git diff --stat
git check-ignore .env .verification/
```

Expected: no whitespace errors; `.env` and `.verification/` are ignored. Review
README, architecture HTML/narrative, refactoring notes, architecture verification,
the new usage guide and this protocol. No runtime, tests, requirements, evidence
or private configuration files should be part of this step's diff.

## Acceptance record

Verified on November 12, 2026:

- Final offline suite: **849 passed, 18 skipped in 91.76 seconds**.
- All **115 evidence files** and **six publication manifests** unchanged;
  both complete historical analysis summaries match.
- Offline synthetic demo and plan-only CLI produce the documented messages.
- All seven geometry checks pass for 80 components and 161 connections.
- Headless browser checks cover standalone load, simplified controls,
  core/support inventory, search/reveal, details/connections, phase/workflow
  focus, zoom/pan, persistent themes, unavailable-storage fallback, themed SVG
  serialization and mobile layout. No page JavaScript errors or HTTP requests.
- Documentation links/anchors, two-section README structure and unchanged
  experiment chronology checked before application.
- Changes were verified in an isolated copy; original private configuration,
  runtime data, source and published results were preserved during review.

This completes the six implementation steps. Final acceptance awaits your review;
no additional phase or new model experiment starts automatically.

Suggested commit message:

```text
docs: simplify README navigation and finish architecture cleanup

Move detailed API recipes into a usage guide and retain the full study history.
Remove redundant implementation-status controls, badges and atlas filter code.
Preserve offline themes, navigation, SVG export and connector geometry.
Document final offline verification and unchanged research evidence.
```
