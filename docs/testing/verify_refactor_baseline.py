"""Check published evidence and reproduce both analyses without model requests.

Run from the repository root:
    python3 docs/testing/verify_refactor_baseline.py --check-source
Omit --check-source after an approved source refactor.
"""

import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
BASELINE = ROOT / 'docs/testing/refactor_baseline.json'


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def check_file(relative_path, expected_hash):
    path = (ROOT / relative_path).resolve()
    if not path.is_relative_to(ROOT) or not path.is_file():
        raise ValueError(f'Missing or invalid baseline file: {relative_path}')
    actual_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual_hash != expected_hash:
        raise ValueError(f'Baseline checksum mismatch: {relative_path}')


def verify(check_source=False):
    baseline = read_json(BASELINE)
    if baseline['version'] != 1:
        raise ValueError('Unsupported refactor baseline version')

    if check_source:
        for name, expected_hash in baseline['source_sha256'].items():
            check_file(name, expected_hash)
        count = len(baseline['source_sha256'])
        print(f'SOURCE BASELINE OK: {count} files match the pre-refactor reference')

    for name, expected_hash in baseline['evidence_sha256'].items():
        check_file(name, expected_hash)
    for name in baseline['publication_manifests']:
        manifest = ROOT / name
        for artifact, expected_hash in read_json(manifest).items():
            relative_path = (manifest.parent / artifact).relative_to(ROOT)
            check_file(relative_path, expected_hash)
    count = len(baseline['evidence_sha256'])
    manifests = len(baseline['publication_manifests'])
    print(f'PUBLISHED EVIDENCE OK: {count} unchanged files; {manifests} publication manifests')

    # Import the working implementation, so this remains useful after refactoring.
    sys.path.insert(0, str(ROOT))
    from experiments.analyze_results import analyze_bundle as analyze_original
    from experiments.integrated_analysis import analyze_bundle as analyze_integrated

    analyzers = {'original': analyze_original, 'integrated': analyze_integrated}
    for study in baseline['completed_studies']:
        result = analyzers[study['name']](ROOT / study['bundle'])
        # Compare the entire JSON report, including costs, curves and unknowns.
        actual = json.loads(json.dumps(result))
        expected = read_json(ROOT / study['summary'])
        if actual != expected:
            raise ValueError(f"Recomputed analysis differs: {study['name']}")
        turns = sum(item['turns'] for item in actual['conditions'].values())
        if turns != study['turns']:
            raise ValueError(f"Unexpected completed turn count: {study['name']}")
        print(f"{study['name'].upper()} ANALYSIS OK: {turns} turns; full summary unchanged")
    print('REFACTOR BASELINE OK: no inference requests or evidence writes')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-source', action='store_true',
                        help='Also require the original source/configuration/test hashes')
    args = parser.parse_args()
    try:
        verify(check_source=args.check_source)
    except (OSError, ValueError, KeyError, TypeError, ImportError) as error:
        print(f'BASELINE ERROR: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
