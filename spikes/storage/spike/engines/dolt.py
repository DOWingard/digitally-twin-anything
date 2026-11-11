"""Dolt adapter: the graph is one table per object type and one per link type."""

from __future__ import annotations

import pyarrow as pa
import pymysql

from ..config import Config
from ..fixture import ChangeSet
from ..log import Log
from ..ontology import Ontology, to_sql_ddl
from .base import REJECTION_CASES, Engine

log = Log("engine.dolt")

_DATABASE = "spike"
_DELETE_CHUNK = 1000


class DoltEngine(Engine):
    name = "dolt"
    _service = "dolt"
    _data_path = "/var/lib/dolt"

    def __init__(self, config: Config, ontology: Ontology, connect_database: bool = False) -> None:
        self._config = config
        self._ontology = ontology
        self._conn = self._connect(_DATABASE if connect_database else None)
        self._upsert_sql = {}
        for object_type in ontology.types:
            names = [f.name for f in object_type.fields]
            columns = ", ".join(f"`{n}`" for n in names)
            placeholders = ", ".join(["%s"] * len(names))
            updates = ", ".join(f"`{n}`=VALUES(`{n}`)" for n in names if n != "uid")
            self._upsert_sql[object_type.name] = (
                f"INSERT INTO `{object_type.table}` ({columns}) VALUES ({placeholders}) "
                f"ON DUPLICATE KEY UPDATE {updates}"
            )

    def _connect(self, database: str | None) -> pymysql.connections.Connection:
        try:
            return pymysql.connect(
                host=self._config.host,
                port=self._config.dolt_port,
                user="root",
                password=self._config.dolt_password,
                database=database,
                autocommit=True,
                connect_timeout=15,
                read_timeout=900,
                write_timeout=900,
            )
        except pymysql.MySQLError as exc:
            log.error("connection to dolt failed", exc=exc, port=self._config.dolt_port)
            raise

    # -- lifecycle ----------------------------------------------------------

    def reset(self) -> None:
        with self._conn.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS `{_DATABASE}`")
            cur.execute(f"CREATE DATABASE `{_DATABASE}`")
            cur.execute(f"USE `{_DATABASE}`")
        log.info("database recreated", database=_DATABASE)

    def install_schema(self) -> list[str]:
        statements = to_sql_ddl(self._ontology)
        with self._conn.cursor() as cur:
            for statement in statements:
                log.debug("creating table", statement=statement)
                cur.execute(statement)
            cur.execute("CALL DOLT_COMMIT('-A', '-m', 'schema')")
        log.info("schema installed", tables=len(statements))
        return []

    def clone(self) -> "DoltEngine":
        return DoltEngine(self._config, self._ontology, connect_database=True)

    def close(self) -> None:
        try:
            self._conn.close()
        except pymysql.MySQLError as exc:
            log.warn("closing connection failed", error=repr(exc))

    # -- writes -------------------------------------------------------------

    def _write(self, conn: pymysql.connections.Connection, changes: ChangeSet, message: str) -> str:
        try:
            conn.begin()
            with conn.cursor() as cur:
                for object_type in self._ontology.types:
                    rows = changes.upserts.get(object_type.name)
                    if rows:
                        names = [f.name for f in object_type.fields]
                        cur.executemany(
                            self._upsert_sql[object_type.name],
                            [tuple(row[n] for n in names) for row in rows],
                        )
                # Referencing types are deleted before the types they reference.
                for object_type in reversed(self._ontology.types):
                    uids = changes.deletes.get(object_type.name) or []
                    for start in range(0, len(uids), _DELETE_CHUNK):
                        chunk = uids[start : start + _DELETE_CHUNK]
                        marks = ", ".join(["%s"] * len(chunk))
                        cur.execute(
                            f"DELETE FROM `{object_type.table}` WHERE `uid` IN ({marks})", chunk
                        )
            conn.commit()
            with conn.cursor() as cur:
                cur.execute("CALL DOLT_COMMIT('-A', '--allow-empty', '-m', %s)", (message,))
                return cur.fetchone()[0]
        except pymysql.MySQLError as exc:
            log.error("commit failed, rolling back", exc=exc, message_text=message)
            conn.rollback()
            raise

    def commit(self, changes: ChangeSet, message: str, branch: str | None = None) -> str:
        if branch is None:
            return self._write(self._conn, changes, message)
        # A connection to `database/branch` writes to that branch only.
        conn = self._connect(f"{_DATABASE}/{branch}")
        try:
            return self._write(conn, changes, message)
        finally:
            conn.close()

    # -- reads --------------------------------------------------------------

    @staticmethod
    def _as_of(at: str | None, branch: str | None) -> tuple[str, tuple]:
        ref = at or branch
        return (" AS OF %s", (ref,)) if ref else ("", ())

    def get_object(
        self, type_name: str, uid: str, at: str | None = None, branch: str | None = None
    ) -> dict | None:
        object_type = self._ontology.type(type_name)
        names = [f.name for f in object_type.fields]
        clause, params = self._as_of(at, branch)
        columns = ", ".join(f"`{n}`" for n in names)
        with self._conn.cursor() as cur:
            cur.execute(
                f"SELECT {columns} FROM `{object_type.table}`{clause} WHERE `uid` = %s",
                (*params, uid),
            )
            row = cur.fetchone()
        return dict(zip(names, row)) if row else None

    def node_table(self, type_name: str, at: str | None = None) -> pa.Table:
        object_type = self._ontology.type(type_name)
        names = [f.name for f in object_type.fields]
        clause, params = self._as_of(at, None)
        columns = ", ".join(f"`{n}`" for n in names)
        with self._conn.cursor() as cur:
            cur.execute(f"SELECT {columns} FROM `{object_type.table}`{clause}", params)
            rows = cur.fetchall()
        if not rows:
            return pa.table({n: [] for n in names})
        return pa.table(dict(zip(names, (list(column) for column in zip(*rows)))))

    def property_history(self, type_name: str, uid: str, prop: str) -> list[tuple[str, object]]:
        table = self._ontology.type(type_name).table
        with self._conn.cursor() as cur:
            cur.execute(
                f"SELECT `to_commit`, `to_{prop}` FROM `dolt_diff_{table}` "
                "WHERE `to_uid` = %s AND `to_commit` <> 'WORKING'",
                (uid,),
            )
            return [(commit, value) for commit, value in cur.fetchall()]

    def diff_count(self, before: str, after: str, type_name: str) -> int:
        table = self._ontology.type(type_name).table
        with self._conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM DOLT_DIFF(%s, %s, %s)", (before, after, table))
            return int(cur.fetchone()[0])

    def neighbors(self, uid: str, depth: int, at: str | None = None) -> set[str]:
        clause, params = self._as_of(at, None)
        query = (
            "WITH RECURSIVE hop (uid, d) AS ("
            " SELECT CAST(%s AS CHAR(64)), 0"
            " UNION"
            f" SELECT f.`dst`, hop.d + 1 FROM hop JOIN `feeds`{clause} AS f ON f.`src` = hop.uid"
            " WHERE hop.d < %s"
            ") SELECT DISTINCT uid FROM hop WHERE d > 0 AND uid <> %s"
        )
        with self._conn.cursor() as cur:
            cur.execute(query, (uid, *params, depth, uid))
            return {row[0] for row in cur.fetchall()}

    # -- branches -----------------------------------------------------------

    def branch_create(self, name: str, at: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute("CALL DOLT_BRANCH(%s, %s)", (name, at))

    def branch_delete(self, name: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute("CALL DOLT_BRANCH('-D', %s)", (name,))

    # -- constraints --------------------------------------------------------

    def rejections(self) -> dict[str, bool]:
        valid = ("reject-probe", "probe", 10.0, 1.0, "ok", None)
        with self._conn.cursor() as cur:
            cur.execute("SELECT `uid` FROM `site` LIMIT 1")
            site = cur.fetchone()[0]
        cases = {
            "bad_enum": ("reject-probe", "probe", 10.0, 1.0, "exploded", site),
            "missing_required": ("reject-probe", None, 10.0, 1.0, "ok", site),
            "dangling_ref": ("reject-probe", "probe", 10.0, 1.0, "ok", "no-such-site"),
            "wrong_type": ("reject-probe", "probe", "not-a-number", 1.0, "ok", site),
            "below_minimum": ("reject-probe", "probe", -5.0, 1.0, "ok", site),
        }
        assert set(cases) == set(REJECTION_CASES) and len(valid) == 6
        sql = (
            "INSERT INTO `asset` (`uid`, `name`, `capacity`, `load`, `status`, `site`) "
            "VALUES (%s, %s, %s, %s, %s, %s)"
        )
        outcome = {}
        for case, row in cases.items():
            self._conn.begin()
            try:
                with self._conn.cursor() as cur:
                    cur.execute(sql, row)
                outcome[case] = False
                log.warn("invalid write was accepted", case=case)
            except pymysql.MySQLError as exc:
                outcome[case] = True
                log.debug("invalid write refused", case=case, reason=str(exc))
            finally:
                self._conn.rollback()
        return outcome
