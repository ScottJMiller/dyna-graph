"""Versioned synthetic observations and questions, with an independent oracle."""

from dataclasses import asdict, dataclass
import hashlib
import json

SCENARIO_VERSION = "database-updates-v2"
# Each entry is (new subject, new database, queried subject). No oracle is sent
# to a model: only the observation, question and answer-format instruction.
SCHEDULE = (
    ("Cedar", "SQLite", "Cedar"), ("Aurora", "Redis", "Aurora"),
    ("Birch", "MariaDB", "Cedar"), ("Dune", "Neo4j", "Birch"),
    ("Cedar", "PostgreSQL", "Cedar"), ("Aurora", "Valkey", "Aurora"),
    ("Elm", "MongoDB", "Cedar"), ("Birch", "DuckDB", "Birch"),
    ("Fir", "Cassandra", "Aurora"), (None, None, "Cedar"),
    ("Cedar", "SQLite", "Cedar"), ("Dune", "ArangoDB", "Birch"),
    ("Grove", "Firebird", "Cedar"), ("Aurora", "Redis", "Aurora"),
    (None, None, "Birch"), ("Cedar", "DuckDB", "Cedar"),
    ("Elm", "CouchDB", "Aurora"), ("Birch", "PostgreSQL", "Birch"),
    (None, None, "Cedar"), (None, None, "Aurora"),
)


@dataclass(frozen=True)
class ScenarioTurn:
    step: int
    observation: str
    subject: str
    expected: str
    update_probe: bool
    delayed_probe: bool

    @property
    def task(self):
        observation = self.observation or "No new observation."
        return (f"Observation: {observation}\nWhich database does {self.subject} use now? "
                "In content, write one short sentence: start with the project name, "
                "then the word uses, then the actual database name. Use the most recent "
                "supported value. Do not add explanations or output placeholder text.")


def scenario(turns=20) -> tuple[ScenarioTurn, ...]:
    if type(turns) is not int or not 15 <= turns <= 20:
        raise ValueError("the scenario requires 15-20 turns")
    current, changed, result = {}, set(), []
    for step, (name, database, query) in enumerate(SCHEDULE[:turns], 1):
        if name is not None:
            if name in current and current[name] != database:
                changed.add(name)
            current[name] = database
        subject = f"Project {query}"
        result.append(ScenarioTurn(step, "" if name is None else f"Project {name} uses {database}.",
            subject, f"{subject} uses {current[query]}.", query in changed,
            name != query))
    return tuple(result)


def scenario_records(turns=20):
    """Retain actual rendered inputs, as well as the independently derived oracle."""
    return [asdict(t) | {"task": t.task} for t in scenario(turns)]


def scenario_hash(turns=20):
    return hashlib.sha256(json.dumps({"version": SCENARIO_VERSION, "turns": scenario_records(turns)},
        sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def score(answer: str, expected: str) -> bool:
    """Strict answer-contract match; this is not a general truth evaluator."""
    normalize = lambda s: " ".join(s.strip().rstrip(".").split()).casefold()
    return normalize(answer) == normalize(expected)
