# Deterministic pruning probe verification

This supplemental Phase 8 check exercises the existing governance mechanism
with simulated age. It does not start Ollama, load `.env`, modify an application
database, repeat the model-driven benchmark, or change the retention policy.
See the [results and interpretation](../results/pruning-probe-001/README.md).

## 1. Run the dedicated automated probe

From the repository root with the existing dependencies installed:

```bash
source .venv/bin/activate
python3 docs/testing/pruning_probe.py --output .verification/pruning-probe-review-001
```

Use a new output directory each time. Existing directories are rejected.
Expected console output (the final path depends on the checkout):

```text
PRUNING PROBE OK: 12 checks passed; two identical logical executions per condition
FIRST OBSERVED PRUNING: 48 simulated hours; no triple pruning through 36 hours
FINAL ACTIVE TRIPLES: off=7 on=5; on has 2 PRUNED triples
DELAYED FACT AVAILABLE: off=yes on=no; pins, frequent fact and unused cycle survive
OUTPUT: <absolute output directory>
```

Expected exit code: zero. Four fresh SQLite databases, a fixed plan, per-entity
score/state evidence, a 40-row checkpoint timeline, summary and checksummed
manifest are created under the selected directory. If a check fails, the manifest
is FAILED, the process exits nonzero, and available evidence remains for review.

The twelve checks cover deterministic repetition; identical initial fact states;
no early pruning; the first sampled pruning checkpoint; disabled-pruning
retention; frequent/pinned survival; abandoned/delayed pruning; non-leaf cycle
survival; the delayed-fact lookup miss; preservation of all original rows;
pruning exactly the eligible set; and below-threshold cycle protection.

## 2. Verify persisted evidence and repeatability

```bash
python3 - <<'PYCODE'
import hashlib, json
from pathlib import Path
saved = Path('docs/results/pruning-probe-001')
fresh = Path('.verification/pruning-probe-review-001')
for root in (saved, fresh):
    manifest = json.loads((root / 'manifest.json').read_text())
    assert manifest['status'] == 'COMPLETED'
    for name, expected in manifest['artifacts'].items():
        assert hashlib.sha256((root / name).read_bytes()).hexdigest() == expected, name
    assert all(json.loads((root / 'summary.json').read_text())['checks'].values())
assert (saved / 'summary.json').read_bytes() == (fresh / 'summary.json').read_bytes()
assert (saved / 'evidence.json').read_bytes() == (fresh / 'evidence.json').read_bytes()
assert (saved / 'timeline.csv').read_bytes() == (fresh / 'timeline.csv').read_bytes()
print('PRUNING EVIDENCE OK: checksums valid; logical results reproduce exactly')
PYCODE
```

Expected: the printed success line. SQLite audit timestamps and DB checksums can
differ across executions; those files are individually checked against their own
manifest, not compared byte-for-byte. A deliberate future policy/source change
may require a separately versioned probe and new reference results.

## 3. Existing regression and documentation checks

```bash
python3 -m pytest tests/test_governance.py -q
node docs/testing/architecture-routing.mjs
git diff --check
git status --short
```

Expected: **102 passed**, **7 geometry tests passed**, no whitespace errors, and
only the intended documentation/probe additions plus any pre-existing work.
The full pytest count is **791 passed, 18 skipped**, including the integrated
protocol extension. This supplemental
script is run explicitly and is not added to default pytest collection.

## 4. Commit message

```text
test(governance): document deterministic pruning behavior and forgetting tradeoffs

- compare pruning on and off with unchanged policy and simulated age
- record reproducible scores, eligibility, states and active-only fact probes
- expose delayed-fact loss and non-leaf protection alongside retained pins
- preserve the completed model-driven study and add offline verification
```
