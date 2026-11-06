"""Single-writer, per-run CSV projection of durable checkpoint telemetry."""

import csv
import io
import json
from pathlib import Path
import re

from src.telemetry.profiler import TelemetryProfiler, TurnMetrics


class CSVLogger:
    """Append and flush each committed turn; identical replay is idempotent.

    One writer owns one file for one run (at most 1,000 turns). Reopen validates
    its complete schema and rows before appending. On an interrupted/partial
    append, export to a new path from SQLite checkpoints instead of guessing.
    No prompt text, task content, addresses, or model responses enter the CSV.
    """

    columns = tuple(TurnMetrics.model_fields)

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._rows: dict[int, dict[str, str]] = {}
        self.run_id: str | None = None
        self._file = None

    @classmethod
    def _row(cls, metrics):
        # Use the CSV codec both ways so blank values and floats match on reopen.
        stream = io.StringIO(newline="")
        writer = csv.DictWriter(stream, fieldnames=cls.columns)
        writer.writeheader()
        writer.writerow(metrics.model_dump())
        stream.seek(0)
        return next(csv.DictReader(stream))

    def __enter__(self):
        if self._file is not None:
            raise RuntimeError("CSV logger is already open")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        rows = {}
        run_id = None
        if self.path.exists() and self.path.stat().st_size:
            with self.path.open("r", encoding="utf-8", newline="") as source:
                reader = csv.DictReader(source, strict=True)
                if tuple(reader.fieldnames or ()) != self.columns:
                    raise ValueError("CSV header differs from telemetry schema v1")
                # At most 1,000 rows; strict round-trip detects truncation and bad values.
                for row in reader:
                    if len(rows) >= 1000 or set(row) != set(self.columns) or any(v is None for v in row.values()):
                        raise ValueError("invalid or oversized telemetry CSV")
                    labels = {"run_id", "agent_id", "node", "context_budget_method", "rss_source"}
                    values = {k: v if k in labels else None if v == "" else json.loads(v)
                              for k, v in row.items()}
                    metrics = TurnMetrics.model_validate(values)
                    if self._row(metrics) != row:
                        raise ValueError("noncanonical telemetry CSV row")
                    if metrics.step in rows or (rows and metrics.step <= max(rows)):
                        raise ValueError("CSV steps must be unique and increasing")
                    if run_id is not None and metrics.run_id != run_id:
                        raise ValueError("CSV must contain one run")
                    run_id, rows[metrics.step] = metrics.run_id, row
            # A missing final line terminator could join two records on append.
            with self.path.open("rb") as source:
                source.seek(-1, 2)
                if source.read(1) != b"\n":
                    raise ValueError("CSV ends with an incomplete line; export to a new path")
        self._rows, self.run_id = rows, run_id
        self._file = self.path.open("a", encoding="utf-8", newline="")
        self._writer = csv.DictWriter(self._file, fieldnames=self.columns)
        try:
            if self._file.tell() == 0:
                self._writer.writeheader()
                self._file.flush()
        except BaseException:
            self.close()
            raise
        return self

    def bind_run(self, run_id: str) -> None:
        """Reject a reused output file before starting inference or exporting."""
        if self._file is None:
            raise RuntimeError("open CSV logger with a context manager")
        if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", run_id):
            raise ValueError("invalid telemetry run_id")
        if self.run_id is not None and run_id != self.run_id:
            raise ValueError(
                f"CSV must contain one run: {self.path} belongs to run {self.run_id}; "
                f"requested run {run_id}. Use --resume {self.run_id} with its original database, "
                "choose a different --csv path, or omit --csv for a separate per-run file."
            )
        self.run_id = run_id

    def append(self, metrics: TurnMetrics) -> bool:
        if self._file is None:
            raise RuntimeError("open CSV logger with a context manager")
        metrics = TurnMetrics.model_validate(metrics)
        row = self._row(metrics)
        self.bind_run(metrics.run_id)
        if metrics.step in self._rows:
            if self._rows[metrics.step] != row:
                raise ValueError("conflicting telemetry for an existing run/step")
            return False
        if self._rows and metrics.step <= max(self._rows):
            raise ValueError("CSV steps must increase")
        self._writer.writerow(row)
        self._file.flush()
        self.run_id, self._rows[metrics.step] = metrics.run_id, row
        return True

    async def export_run(self, machine, run_id: str) -> tuple[int, int]:
        """Replay durable rows in bounded pages. Returns (appended, older skipped).

        Call outside workflow execution: an export failure cannot undo a commit.
        Repeated export also validates previously written rows against SQLite.
        """
        self.bind_run(run_id)
        appended = skipped = after = 0
        while True:
            history = await machine.history(run_id, after_step=after, limit=100)
            if not history:
                return appended, skipped
            for checkpoint in history:
                metrics = TelemetryProfiler.from_checkpoint(checkpoint)
                if metrics is None:
                    skipped += 1
                else:
                    appended += self.append(metrics)
                after = checkpoint.step

    def close(self):
        if self._file is not None:
            file, self._file = self._file, None
            file.close()

    def __exit__(self, *args):
        self.close()
