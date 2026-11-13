"""Validate complete local bundles and compute descriptive paired comparisons."""

import argparse
import csv
import math
from pathlib import Path
import statistics
import sys

from experiments.contracts import (
    BenchmarkPlan, BenchmarkRow, CSV_COLUMNS, flatten_row, load_json, sha256, write_json,
)
from experiments.scenario import scenario, scenario_records, score


def _validate_manifest(path):
    """Read a completed original-study manifest and verify its frozen scenario."""
    manifest_path = path / "manifest.json"
    if manifest_path.stat().st_size > 2_000_000:
        raise ValueError("manifest exceeds size limit")
    manifest = load_json(manifest_path)
    if type(manifest.get("version")) is not int or manifest.get("version") != 1 or manifest.get("status") != "COMPLETED":
        raise ValueError("analysis requires a complete version-1 bundle; partial runs are not results")
    plan = BenchmarkPlan.model_validate(manifest["plan"])
    if manifest["scenario_sha256"] != plan.scenario_sha256 or manifest["scenario"] != scenario_records(plan.turns):
        raise ValueError("scenario or oracle differs from its versioned plan")
    return manifest, plan


def _validate_artifacts(path, manifest, plan):
    """Check required files and hashes before loading any turn records."""
    manifest_path = path / "manifest.json"
    artifacts = manifest["artifacts"]
    required = {"turns.jsonl", "metrics.csv", *(f"{c}-{r}.csv" for r, c in plan.order()),
                *(f"{c}-{r}.db" for r, c in plan.order())}
    if not required <= set(artifacts):
        raise ValueError("missing condition artifacts")
    for name, digest in artifacts.items():
        target = (path / name).resolve()
        if Path(name).is_absolute() or not target.is_relative_to(path) or target == manifest_path:
            raise ValueError("unsafe artifact path")
        if not target.is_file() or sha256(target) != digest:
            raise ValueError(f"artifact checksum mismatch: {name}")


def _read_turns(path, plan):
    """Load bounded turn records and require the full paired cohort."""
    raw_path = path / "turns.jsonl"
    if raw_path.stat().st_size > 50_000_000:
        raise ValueError("turn evidence exceeds size limit")
    rows = []
    with raw_path.open(encoding="utf-8") as raw:
        for line in raw:
            if len(line) > 100_000 or len(rows) >= 400:
                raise ValueError("too many or oversized turn records")
            rows.append(BenchmarkRow.model_validate_json(line))
    if len(rows) != 2 * plan.repetitions * plan.turns:
        raise ValueError("incomplete or duplicated paired turns")
    return rows


def _validate_run_order(manifest, plan, rows):
    """Check run identity, frozen inputs, scores, routing and memory allowance."""
    expected_runs = []
    for position, (rep, condition) in enumerate(plan.order(), 1):
        run_id = f"{manifest['bundle_id']}-{condition}-{rep}"
        expected_runs.append(dict(run_id=run_id, condition=condition, repetition=rep,
                                  position=position, completed_turns=plan.turns))
        for item in scenario(plan.turns):
            row = rows[(position - 1) * plan.turns + item.step - 1]
            if (row.bundle_id, row.source, row.condition, row.repetition, row.position,
                    row.scenario_sha256, row.telemetry.run_id, row.telemetry.step) != (
                    manifest["bundle_id"], manifest["source"], condition, rep, position,
                    plan.scenario_sha256, run_id, item.step):
                raise ValueError("turn order or identity differs from the plan")
            if (row.subject, row.observation, row.expected, row.update_probe, row.delayed_probe, row.correct) != (
                    item.subject, item.observation, item.expected, item.update_probe, item.delayed_probe, score(row.answer, item.expected)):
                raise ValueError("turn scoring or input differs from the independent oracle")
            role = ("planner", "researcher", "verifier")[(item.step - 1) % 3]
            node = "node2" if role == "researcher" else "node1"
            if row.telemetry.agent_id != role or row.telemetry.node != node or row.telemetry.fallback_attempts:
                raise ValueError("role route differs from the fixed plan")
            if row.telemetry.context_budget_limit > plan.memory_budget:
                raise ValueError("memory budget differs from the plan")
    if manifest["runs"] != expected_runs:
        raise ValueError("manifest run completeness differs from its turn evidence")


def _validate_metrics_csv(path, rows):
    """Require the CSV to match the deterministic JSONL projection exactly."""
    with (path / "metrics.csv").open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream, strict=True)
        if tuple(reader.fieldnames or ()) != CSV_COLUMNS:
            raise ValueError("benchmark CSV schema mismatch")
        csv_rows = list(reader)
    expected_csv = [{k: "" if v is None else str(v) for k, v in flatten_row(row).items()} for row in rows]
    if csv_rows != expected_csv:
        raise ValueError("CSV and JSONL evidence disagree")


def load_bundle(path):
    """Return the manifest, plan and validated turns from a completed A/B bundle.

    Reject partial or inconsistent evidence before analysis. This reader does
    not initialize databases, create output files or contact either model.
    """
    path = Path(path).expanduser().resolve()
    manifest, plan = _validate_manifest(path)
    _validate_artifacts(path, manifest, plan)
    rows = _read_turns(path, plan)
    _validate_run_order(manifest, plan, rows)
    _validate_metrics_csv(path, rows)
    return manifest, plan, tuple(rows)


def _complete_total(values):
    """Sum available counters only when the complete series is known."""
    return None if any(v is None for v in values) else sum(values)


def _stats(values):
    """Return descriptive mean, sample SD, median and nearest-rank p95."""
    return {"n": len(values), "mean": statistics.mean(values),
            "sd": statistics.stdev(values) if len(values) > 1 else None,
            "median": statistics.median(values),
            "p95": sorted(values)[math.ceil(len(values) * 0.95) - 1]}


def _ratio(a, b):
    """Return percentage savings relative to the first total, or unknown."""
    return None if a is None or b is None or a == 0 else (a - b) / a * 100


def analyze_bundle(path):
    """Validate the original A/B cohort and return descriptive paired results."""
    manifest, plan, rows = load_bundle(path)
    conditions = {}
    for condition in ("A", "B"):
        selected = [r for r in rows if r.condition == condition]
        update = [r for r in selected if r.update_probe]
        delayed = [r for r in selected if r.delayed_probe]
        conditions[condition] = {"turns": len(selected),
            "role_prompt_tokens": _complete_total([r.telemetry.agent_prompt_tokens for r in selected]),
            "total_prompt_tokens": _complete_total([r.total_prompt_tokens for r in selected]),
            "total_generated_tokens": _complete_total([r.total_generated_tokens for r in selected]),
            "role_counter_coverage": sum(r.telemetry.agent_prompt_tokens is not None for r in selected),
            "total_counter_coverage": sum(r.total_prompt_tokens is not None for r in selected),
            "wall_ms": _stats([r.wall_ms for r in selected]),
            "inference_ms": _stats([r.total_inference_ms for r in selected]),
            "correct": sum(r.correct for r in selected), "accuracy": sum(r.correct for r in selected) / len(selected),
            "update_probes": len(update), "update_accuracy": sum(r.correct for r in update) / len(update) if update else None,
            "delayed_probes": len(delayed), "delayed_accuracy": sum(r.correct for r in delayed) / len(delayed) if delayed else None,
            "history_truncated_turns": sum(r.history_dropped_turns > 0 for r in selected),
            "max_context_bytes": max(r.telemetry.context_budget_used for r in selected)}
    paired = []
    for rep in range(1, plan.repetitions + 1):
        a = [r for r in rows if r.condition == "A" and r.repetition == rep]
        b = [r for r in rows if r.condition == "B" and r.repetition == rep]
        paired.append({"repetition": rep,
            "role_prompt_savings_pct": _ratio(_complete_total([r.telemetry.agent_prompt_tokens for r in a]),
                                               _complete_total([r.telemetry.agent_prompt_tokens for r in b])),
            "total_prompt_savings_pct": _ratio(_complete_total([r.total_prompt_tokens for r in a]),
                                                _complete_total([r.total_prompt_tokens for r in b])),
            "wall_ms_delta_B_minus_A": sum(r.wall_ms for r in b) - sum(r.wall_ms for r in a),
            "accuracy_delta_B_minus_A": (sum(r.correct for r in b) - sum(r.correct for r in a)) / plan.turns})
    curves = []
    for condition in ("A", "B"):
        for step in range(1, plan.turns + 1):
            selected = [r for r in rows if r.condition == condition and r.telemetry.step == step]
            def mean_complete(values):
                """Average all repetitions only when none has an unavailable counter."""
                total = _complete_total(values)
                return None if total is None else total / len(values)
            curves.append({"condition": condition, "step": step, "n": len(selected),
                "role_prompt_tokens": mean_complete([r.telemetry.agent_prompt_tokens for r in selected]),
                "total_prompt_tokens": mean_complete([r.total_prompt_tokens for r in selected]),
                "wall_ms": statistics.mean(r.wall_ms for r in selected),
                "accuracy": statistics.mean(r.correct for r in selected),
                "context_bytes": statistics.mean(r.telemetry.context_budget_used for r in selected)})
    for condition in ("A", "B"):
        points = [r for r in curves if r["condition"] == condition]
        slope = None
        if all(p["role_prompt_tokens"] is not None for p in points):
            mx = statistics.mean(p["step"] for p in points)
            my = statistics.mean(p["role_prompt_tokens"] for p in points)
            slope = sum((p["step"] - mx) * (p["role_prompt_tokens"] - my) for p in points) / sum(
                (p["step"] - mx) ** 2 for p in points)
        conditions[condition]["observed_role_token_slope_per_turn"] = slope
    paired_summary = {}
    for name in ("role_prompt_savings_pct", "total_prompt_savings_pct", "wall_ms_delta_B_minus_A", "accuracy_delta_B_minus_A"):
        values = [p[name] for p in paired]
        complete = all(v is not None for v in values)
        paired_summary[name] = {"n_pairs": len(values), "known_pairs": sum(v is not None for v in values),
            "mean": statistics.mean(values) if complete else None,
            "sd": statistics.stdev(values) if complete and len(values) > 1 else None}
    return {"version": 1, "bundle_id": manifest["bundle_id"], "source": manifest["source"],
        "scenario_sha256": plan.scenario_sha256, "plan": plan.model_dump(),
        "conditions": conditions, "paired": paired, "paired_summary": paired_summary, "curves": curves,
        "interpretation": "Descriptive bounded-window comparison; no asymptotic proof or general accuracy claim. "
            "Missing counters stay unknown. Wall timing excludes warmup/setup/output export; RSS is Python only. "
            "Synthetic data is test evidence, never an experiment result."}


def write_analysis(bundle, output):
    """Validate first, then write JSON and CSV results to a fresh directory."""
    report = analyze_bundle(bundle)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "summary.json", report)
    for name, records in (("paired.csv", report["paired"]), ("turn_means.csv", report["curves"])):
        with (output / name).open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=tuple(records[0]))
            writer.writeheader()
            writer.writerows(records)
    return report


def main(argv=None):
    """Analyze an existing A/B bundle without launching inference."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="completed benchmark bundle")
    parser.add_argument("--output", type=Path, required=True, help="new analysis directory")
    args = parser.parse_args(argv)
    try:
        report = write_analysis(args.input, args.output)
        print(f"ANALYSIS OK: source={report['source']} paired_runs={len(report['paired'])}")
    except (OSError, ValueError, KeyError, TypeError, csv.Error) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
