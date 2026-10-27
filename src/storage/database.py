"""Bounded SQLite connections, nested transactions, and queued asynchronous writes."""

import asyncio
from contextlib import contextmanager, nullcontext
import inspect
from pathlib import Path
from queue import Empty, LifoQueue
import sqlite3
from threading import Condition, RLock, local
from typing import Callable, Iterator, TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    from src.config import Settings

Result = TypeVar("Result")
SCHEMA_VERSION = 2
SCHEMA_PATH = Path(__file__).with_name("schema.sql")


class DatabaseError(RuntimeError):
    """The local database cannot satisfy the required storage configuration."""


class PoolTimeoutError(DatabaseError):
    """No pooled connection became available within the configured wait budget."""


class Database:
    """Own a small pool of exclusively leased connections to one local file.

    Connections may move between threads but are never shared concurrently.
    Writers are serialized within this instance; BEGIN IMMEDIATE and SQLite's
    busy timeout handle other instances/processes. Nested transactions use
    savepoints. Read transactions prohibit writes and cannot be promoted.
    Do not manually commit, close, or change PRAGMAs on managed connections.
    """

    def __init__(self, path: str | Path, *, pool_size: int = 4, busy_timeout_ms: int = 5000) -> None:
        if sqlite3.sqlite_version_info < (3, 38, 0):
            raise DatabaseError("SQLite 3.38+ is required for STRICT tables and JSON property checks")
        if str(path).strip() in ("", ":memory:") or str(path).startswith("file:"):
            raise ValueError("WAL storage requires a local filesystem database path")
        if type(pool_size) is not int or not 1 <= pool_size <= 16:
            raise ValueError("pool_size must be an integer in the range 1-16")
        if type(busy_timeout_ms) is not int or not 1 <= busy_timeout_ms <= 60_000:
            raise ValueError("busy_timeout_ms must be an integer in the range 1-60000")
        self.path = Path(path).expanduser().resolve()
        self.pool_size = pool_size
        self.busy_timeout_ms = busy_timeout_ms
        self._pool: LifoQueue[sqlite3.Connection] = LifoQueue(maxsize=pool_size)
        self._condition = Condition()
        self._writer_lock = RLock()
        self._local = local()
        self._active = 0
        self._closing = False
        self._closed = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._async_gate: asyncio.Lock | None = None
        self._queue: asyncio.Queue | None = None
        self._worker: asyncio.Task | None = None
        self._async_closing = False
        self._shutdown_enqueued = False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connections: list[sqlite3.Connection] = []
        try:
            first = self._open_connection()
            connections.append(first)
            self._initialize(first)
            for _ in range(pool_size - 1):
                connections.append(self._open_connection())
        except BaseException:
            for connection in connections:
                connection.close()
            raise
        for connection in connections:
            self._pool.put_nowait(connection)

    @classmethod
    def from_settings(cls, settings: "Settings | None" = None) -> "Database":
        if settings is None:
            from src.config import get_settings
            settings = get_settings()
        return cls(settings.database_path, pool_size=settings.sqlite_pool_size,
                   busy_timeout_ms=settings.sqlite_busy_timeout_ms)

    def _open_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path, timeout=self.busy_timeout_ms / 1000,
            isolation_level=None, check_same_thread=False,
        )
        try:
            connection.row_factory = sqlite3.Row
            connection.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
            mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if mode.lower() != "wal":
                raise DatabaseError(f"WAL is required; SQLite selected {mode!r}")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA synchronous=FULL")
        except BaseException:
            connection.close()
            raise
        return connection

    def _initialize(self, connection: sqlite3.Connection) -> None:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, SCHEMA_VERSION):
            raise DatabaseError(f"Unsupported schema version {version}; expected {SCHEMA_VERSION}")
        schema = SCHEMA_PATH.read_text(encoding="utf-8")
        try:
            connection.executescript(
                "BEGIN IMMEDIATE;\n" + schema
                + f"\nPRAGMA user_version={SCHEMA_VERSION};\nCOMMIT;"
            )
        except BaseException as exc:
            if connection.in_transaction:
                connection.rollback()
            if isinstance(exc, sqlite3.OperationalError) and "no such module: fts5" in str(exc):
                raise DatabaseError("This Python SQLite build requires FTS5 support") from exc
            raise

    @contextmanager
    def _checkout(self) -> Iterator[sqlite3.Connection]:
        with self._condition:
            if self._closed or self._closing:
                raise DatabaseError("Database is closed or closing")
            self._active += 1
        connection = None
        try:
            try:
                connection = self._pool.get(timeout=self.busy_timeout_ms / 1000)
            except Empty as exc:
                raise PoolTimeoutError("Timed out waiting for a pooled connection") from exc
            yield connection
        finally:
            if connection is not None:
                self._pool.put_nowait(connection)
            with self._condition:
                self._active -= 1
                self._condition.notify_all()

    @contextmanager
    def transaction(self, *, write: bool = True) -> Iterator[sqlite3.Connection]:
        """Commit on success and roll back on any exception, including cancellation."""
        current = getattr(self._local, "connection", None)
        if current is not None:
            if write and not self._local.write:
                raise DatabaseError("Cannot promote a read transaction to a write transaction")
            self._local.savepoint += 1
            name = f"nested_{self._local.savepoint}"
            current.execute(f"SAVEPOINT {name}")
            try:
                yield current
                current.execute(f"RELEASE SAVEPOINT {name}")
            except BaseException:
                current.execute(f"ROLLBACK TO SAVEPOINT {name}")
                current.execute(f"RELEASE SAVEPOINT {name}")
                raise
            return
        with self._writer_lock if write else nullcontext():
            with self._checkout() as connection:
                self._local.connection = connection
                self._local.write = write
                self._local.savepoint = 0
                try:
                    if not write:
                        connection.execute("PRAGMA query_only=ON")
                    connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                    yield connection
                    connection.commit()
                except BaseException:
                    if connection.in_transaction:
                        connection.rollback()
                    raise
                finally:
                    del self._local.connection
                    del self._local.write
                    del self._local.savepoint
                    if not write:
                        connection.execute("PRAGMA query_only=OFF")

    def connection(self):
        """Borrow a connection in a read-only snapshot transaction."""
        return self.transaction(write=False)

    def _execute_write(self, operation: Callable[[sqlite3.Connection], Result]) -> Result:
        with self.transaction() as connection:
            result = operation(connection)
            if inspect.isawaitable(result):
                if inspect.iscoroutine(result):
                    result.close()
                raise TypeError("Queued write callbacks must be synchronous")
            return result

    async def _write_worker(self) -> None:
        while True:
            job = await self._queue.get()
            try:
                if job is None:
                    return
                operation, future = job
                if future.cancelled():
                    continue
                try:
                    result = await asyncio.to_thread(self._execute_write, operation)
                except BaseException as exc:
                    if isinstance(exc, asyncio.CancelledError) and asyncio.current_task().cancelling():
                        raise
                    if not future.done():
                        future.set_exception(exc)
                else:
                    if not future.done():
                        future.set_result(result)
            finally:
                self._queue.task_done()

    async def write_async(self, operation: Callable[[sqlite3.Connection], Result]) -> Result:
        """Queue a synchronous callback; execute and commit it off the event loop.

        Cancelled queued jobs are skipped. A started thread transaction finishes
        atomically even if its caller cancels. Use aclose() to drain the queue.
        One Database instance's async API belongs to one event loop.
        """
        if not callable(operation):
            raise TypeError("operation must be callable")
        loop = asyncio.get_running_loop()
        with self._condition:
            if self._closed or self._closing or self._async_closing:
                raise DatabaseError("Database is closed or closing")
            if self._loop is None:
                self._loop = loop
                self._async_gate = asyncio.Lock()
                self._queue = asyncio.Queue(maxsize=self.pool_size * 4)
                self._worker = loop.create_task(self._write_worker())
            elif self._loop is not loop:
                raise DatabaseError("Async writes must use the database's original event loop")
        async with self._async_gate:
            if self._async_closing:
                raise DatabaseError("Database is closed or closing")
            future = loop.create_future()
            await self._queue.put((operation, future))
        return await future

    def close(self) -> None:
        """Wait for active leases and close all connections; calls are idempotent."""
        if getattr(self._local, "connection", None) is not None:
            raise DatabaseError("Cannot close the database inside a transaction")
        with self._condition:
            if self._worker is not None and not self._worker.done():
                raise DatabaseError("Use await database.aclose() after asynchronous writes")
            self._closing = True
            self._condition.wait_for(lambda: self._active == 0)
            if self._closed:
                return
            while not self._pool.empty():
                self._pool.get_nowait().close()
            self._closed = True

    async def aclose(self) -> None:
        if self._loop is not None and self._loop is not asyncio.get_running_loop():
            raise DatabaseError("Async close must use the database's original event loop")
        if self._worker is not None:
            async with self._async_gate:
                self._async_closing = True
                if not self._shutdown_enqueued:
                    await self._queue.put(None)
                    self._shutdown_enqueued = True
            # A cancelled shutdown caller must not cancel the draining worker.
            await asyncio.shield(self._worker)
        await asyncio.to_thread(self.close)

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    async def __aenter__(self) -> "Database":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
