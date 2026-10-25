"""Transactional entity/triple CRUD and literal FTS5 prefix searches."""

from contextlib import contextmanager
from dataclasses import dataclass
import json
import math
import re
import sqlite3
from typing import Callable, Iterator, TypeVar

from src.storage.database import Database

Result = TypeVar("Result")
ENTITY_STATUSES = frozenset(("ACTIVE", "PRUNED"))
TRIPLE_STATUSES = frozenset(("ACTIVE", "SUPERSEDED", "PRUNED"))


@dataclass(frozen=True)
class Entity:
    id: int
    name: str
    canonical_name: str
    entity_type: str
    description: str
    properties: dict
    is_pinned: bool
    status: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class Triple:
    id: int
    subject_id: int
    predicate: str
    object_id: int
    properties: dict
    confidence: float
    status: str
    created_at: str
    updated_at: str


def _text(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a nonempty string")
    return " ".join(value.split())


def _id(value: int) -> int:
    if type(value) is not int or value < 1:
        raise ValueError("IDs must be positive integers")
    return value


def _properties(value: dict | None) -> str:
    if value is None:
        return "{}"
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError("properties must be a JSON object with string keys")
    try:
        return json.dumps(value, allow_nan=False, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("properties must contain finite, JSON-serializable values") from exc


def _status(value: str, allowed: frozenset[str]) -> str:
    value = _text(value, "status").upper()
    if value not in allowed:
        raise ValueError(f"status must be one of {sorted(allowed)}")
    return value


def _confidence(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError("confidence must be a finite number in the range 0-1")
    return float(value)


def _page(limit: int, offset: int = 0) -> None:
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("limit must be an integer in the range 1-1000")
    if type(offset) is not int or offset < 0:
        raise ValueError("offset must be a nonnegative integer")


def _entity(row: sqlite3.Row) -> Entity:
    data = dict(row)
    data["properties"] = json.loads(data["properties"])
    data["is_pinned"] = bool(data["is_pinned"])
    return Entity(**data)


def _triple(row: sqlite3.Row) -> Triple:
    data = dict(row)
    data["properties"] = json.loads(data["properties"])
    return Triple(**data)


class GraphStore:
    """Use a caller-owned Database; never close its connection pool implicitly.

    Names are unique after casefolding and whitespace normalization. Properties
    replace the complete JSON object on update. Physical entity deletion cascades
    to incident triples; governance/soft-pruning algorithms are separate phases.
    """

    def __init__(self, database: Database) -> None:
        self.database = database

    @contextmanager
    def transaction(self) -> Iterator["GraphStore"]:
        """Group any number of CRUD calls in one atomic write transaction."""
        with self.database.transaction():
            yield self

    async def write_async(self, operation: Callable[["GraphStore"], Result]) -> Result:
        """Queue an atomic group of synchronous CRUD calls off the event loop."""
        return await self.database.write_async(lambda _connection: operation(self))

    def _insert(self, table: str, fields: dict, convert: Callable) -> Entity | Triple:
        # Table/column names below are internal constants; user values are bound.
        columns = ", ".join(fields)
        placeholders = ", ".join("?" for _ in fields)
        with self.database.transaction() as connection:
            cursor = connection.execute(
                f"INSERT INTO {table} ({columns}) VALUES ({placeholders}) RETURNING *", tuple(fields.values()),
            )
            # Consume and close RETURNING before committing the transaction.
            row = cursor.fetchall()[0]
            cursor.close()
            return convert(row)

    def _get(self, table: str, row_id: int, convert: Callable) -> Entity | Triple | None:
        with self.database.connection() as connection:
            row = connection.execute(f"SELECT * FROM {table} WHERE id=?", (_id(row_id),)).fetchone()
            return None if row is None else convert(row)

    def _update(self, table: str, row_id: int, fields: dict, convert: Callable) -> Entity | Triple | None:
        _id(row_id)
        if not fields:
            return self._get(table, row_id, convert)
        assignments = ", ".join(f"{column}=?" for column in fields)
        with self.database.transaction() as connection:
            cursor = connection.execute(
                f"UPDATE {table} SET {assignments}, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id=?",
                (*fields.values(), row_id),
            )
            updated = cursor.rowcount
            cursor.close()
            if not updated:
                return None
            return convert(connection.execute(f"SELECT * FROM {table} WHERE id=?", (row_id,)).fetchone())

    def _delete(self, table: str, row_id: int) -> bool:
        with self.database.transaction() as connection:
            cursor = connection.execute(f"DELETE FROM {table} WHERE id=?", (_id(row_id),))
            deleted = cursor.rowcount > 0
            cursor.close()
            return deleted

    def create_entity(
        self, name: str, *, entity_type: str = "concept", description: str = "",
        properties: dict | None = None, is_pinned: bool = False, status: str = "ACTIVE",
    ) -> Entity:
        name = _text(name, "name")
        if not isinstance(description, str):
            raise ValueError("description must be a string")
        if type(is_pinned) is not bool:
            raise ValueError("is_pinned must be a boolean")
        return self._insert("entities", {
            "name": name, "canonical_name": name.casefold(),
            "entity_type": _text(entity_type, "entity_type").casefold(),
            "description": description, "properties": _properties(properties),
            "is_pinned": int(is_pinned), "status": _status(status, ENTITY_STATUSES),
        }, _entity)

    def get_entity(self, entity_id: int) -> Entity | None:
        return self._get("entities", entity_id, _entity)

    def find_entity(self, name: str) -> Entity | None:
        canonical = _text(name, "name").casefold()
        with self.database.connection() as connection:
            row = connection.execute("SELECT * FROM entities WHERE canonical_name=?", (canonical,)).fetchone()
            return None if row is None else _entity(row)

    def get_or_create_entity(self, name: str, **attributes) -> Entity:
        """Atomically find/create a canonical name; existing attributes are preserved."""
        with self.database.transaction():
            existing = self.find_entity(name)
            return existing if existing is not None else self.create_entity(name, **attributes)

    def update_entity(
        self, entity_id: int, *, name: str | None = None, entity_type: str | None = None,
        description: str | None = None, properties: dict | None = None,
        is_pinned: bool | None = None, status: str | None = None,
    ) -> Entity | None:
        fields = {}
        if name is not None:
            fields["name"] = _text(name, "name")
            fields["canonical_name"] = fields["name"].casefold()
        if entity_type is not None:
            fields["entity_type"] = _text(entity_type, "entity_type").casefold()
        if description is not None:
            if not isinstance(description, str):
                raise ValueError("description must be a string")
            fields["description"] = description
        if properties is not None:
            fields["properties"] = _properties(properties)
        if is_pinned is not None:
            if type(is_pinned) is not bool:
                raise ValueError("is_pinned must be a boolean")
            fields["is_pinned"] = int(is_pinned)
        if status is not None:
            fields["status"] = _status(status, ENTITY_STATUSES)
        return self._update("entities", entity_id, fields, _entity)

    def delete_entity(self, entity_id: int) -> bool:
        return self._delete("entities", entity_id)

    def list_entities(self, *, status: str | None = None, limit: int = 100, offset: int = 0) -> list[Entity]:
        _page(limit, offset)
        clause = ""
        arguments = []
        if status is not None:
            clause = " WHERE status=?"
            arguments.append(_status(status, ENTITY_STATUSES))
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM entities" + clause + " ORDER BY id LIMIT ? OFFSET ?", (*arguments, limit, offset),
            ).fetchall()
            return [_entity(row) for row in rows]

    def create_triple(
        self, subject_id: int, predicate: str, object_id: int, *,
        properties: dict | None = None, confidence: float = 1.0, status: str = "ACTIVE",
    ) -> Triple:
        return self._insert("triples", {
            "subject_id": _id(subject_id), "predicate": _text(predicate, "predicate").casefold(),
            "object_id": _id(object_id), "properties": _properties(properties),
            "confidence": _confidence(confidence), "status": _status(status, TRIPLE_STATUSES),
        }, _triple)

    def get_triple(self, triple_id: int) -> Triple | None:
        return self._get("triples", triple_id, _triple)

    def update_triple(
        self, triple_id: int, *, subject_id: int | None = None, predicate: str | None = None,
        object_id: int | None = None, properties: dict | None = None,
        confidence: float | None = None, status: str | None = None,
    ) -> Triple | None:
        fields = {}
        if subject_id is not None:
            fields["subject_id"] = _id(subject_id)
        if predicate is not None:
            fields["predicate"] = _text(predicate, "predicate").casefold()
        if object_id is not None:
            fields["object_id"] = _id(object_id)
        if properties is not None:
            fields["properties"] = _properties(properties)
        if confidence is not None:
            fields["confidence"] = _confidence(confidence)
        if status is not None:
            fields["status"] = _status(status, TRIPLE_STATUSES)
        return self._update("triples", triple_id, fields, _triple)

    def delete_triple(self, triple_id: int) -> bool:
        return self._delete("triples", triple_id)

    def list_triples(
        self, *, subject_id: int | None = None, predicate: str | None = None,
        object_id: int | None = None, status: str | None = None,
        limit: int = 100, offset: int = 0,
    ) -> list[Triple]:
        _page(limit, offset)
        filters = {}
        if subject_id is not None:
            filters["subject_id"] = _id(subject_id)
        if object_id is not None:
            filters["object_id"] = _id(object_id)
        if predicate is not None:
            filters["predicate"] = _text(predicate, "predicate").casefold()
        if status is not None:
            filters["status"] = _status(status, TRIPLE_STATUSES)
        clause = " WHERE " + " AND ".join(f"{column}=?" for column in filters) if filters else ""
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM triples" + clause + " ORDER BY id LIMIT ? OFFSET ?",
                (*filters.values(), limit, offset),
            ).fetchall()
            return [_triple(row) for row in rows]

    def search_prefix(self, query: str, *, limit: int = 20) -> list[Entity]:
        """Match all literal word prefixes in active entity names/descriptions."""
        _page(limit)
        query = _text(query, "query")
        words = re.findall(r"[^\W_]+", query, flags=re.UNICODE)
        if not words:
            return []
        match = " AND ".join(f'"{word}"*' for word in words)
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT e.* FROM fts_entities JOIN entities e ON e.id=fts_entities.rowid "
                "WHERE fts_entities MATCH ? AND e.status='ACTIVE' "
                "ORDER BY bm25(fts_entities), e.id LIMIT ?", (match, limit),
            ).fetchall()
            return [_entity(row) for row in rows]
