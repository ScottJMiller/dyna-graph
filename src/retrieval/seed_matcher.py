"""Bounded literal FTS5 terms with deterministic recent-active fallback."""

from dataclasses import dataclass
import re
import sqlite3
from typing import Literal

from src.storage.graph_store import GraphStore

MAX_SUBGOAL_CHARS = 2000
MAX_TERMS = 16
MAX_LABEL_CHARS = 256
STOP_WORDS = frozenset("a an and are as at be by do does for from how i in is it of on or that the this to was what when where which who why with".split())


def _limit(value: int, name: str, maximum: int) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"{name} must be an integer in the range 1-{maximum}")
    return value


def _subgoal(value: str) -> str:
    if not isinstance(value, str) or len(value) > MAX_SUBGOAL_CHARS:
        raise ValueError(f"subgoal must be a string of at most {MAX_SUBGOAL_CHARS} characters")
    if "\x00" in value or any(0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise ValueError("subgoal must contain valid Unicode without NUL")
    return " ".join(value.split())


@dataclass(frozen=True)
class SeedMatch:
    entity_ids: tuple[int, ...]
    terms: tuple[str, ...]
    source: Literal["lexical", "recent", "empty"]


class SeedMatcher:
    def __init__(self, graph: GraphStore, *, seed_limit: int = 5) -> None:
        self.graph = graph
        self.seed_limit = _limit(seed_limit, "seed_limit", 16)

    def match(self, subgoal: str) -> SeedMatch:
        subgoal = _subgoal(subgoal)
        with self.graph.database.connection() as connection:
            return self._match(connection, subgoal)

    def _match(self, connection: sqlite3.Connection, subgoal: str) -> SeedMatch:
        terms = tuple(dict.fromkeys(
            word for word in re.findall(r"[^\W_]+", subgoal.casefold()) if word not in STOP_WORDS
        ))[:MAX_TERMS]
        if terms:
            # Only Unicode letters/digits enter quoted literal prefix terms.
            # Natural-language goals use OR; requiring every question word
            # would make useful entities disappear from normal task queries.
            expression = " OR ".join(f'"{term}"*' for term in terms)
            rows = connection.execute(
                "SELECT e.id FROM fts_entities JOIN entities e ON e.id=fts_entities.rowid "
                "WHERE fts_entities MATCH ? AND e.status='ACTIVE' AND instr(e.name,char(0))=0 AND length(e.name)<=? "
                "ORDER BY (e.canonical_name=?) DESC,bm25(fts_entities),e.id LIMIT ?",
                (expression, MAX_LABEL_CHARS, subgoal.casefold(), self.seed_limit),
            ).fetchall()
            if rows:
                return SeedMatch(tuple(row[0] for row in rows), terms, "lexical")
        rows = connection.execute(
            "SELECT id FROM entities WHERE status='ACTIVE' AND instr(name,char(0))=0 AND length(name)<=? "
            "ORDER BY updated_at DESC,id DESC LIMIT ?", (MAX_LABEL_CHARS, self.seed_limit),
        ).fetchall()
        return SeedMatch(tuple(row[0] for row in rows), terms, "recent" if rows else "empty")
