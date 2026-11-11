"""TerminusDB adapter: every object and every link is a schema-checked document."""

from __future__ import annotations

import functools

import pyarrow as pa
from terminusdb import Client
from terminusdb import WOQLQuery as WQ

from ..config import Config
from ..fixture import ChangeSet
from ..log import Log
from ..ontology import Ontology, to_terminus_schema
from .base import REJECTION_CASES, Engine

log = Log("engine.terminusdb")

_DATABASE = "spike"
_TEAM = "admin"
_HISTORY_PAGE = 500
_HISTORY_QUERY_CHUNK = 200
_REQUEST_TIMEOUT_S = 900


class TerminusEngine(Engine):
    name = "terminusdb"
    _service = "terminusdb"
    _data_path = "/app/terminusdb/storage"

    def __init__(self, config: Config, ontology: Ontology, connect_database: bool = False) -> None:
        self._config = config
        self._ontology = ontology
        self._client = Client(f"http://{config.host}:{config.terminus_port}/")
        # The document API writes upserts and deletes as two commits. The later
        # one is the version id; this maps the earlier one onto it.
        self._alias: dict[str, str] = {}
        self._connect(_DATABASE if connect_database else None)

    def _connect(self, database: str | None) -> None:
        try:
            self._client.connect(
                user="admin", key=self._config.terminus_password, team=_TEAM, db=database
            )
        except Exception as exc:
            log.error("connection to terminusdb failed", exc=exc, port=self._config.terminus_port)
            raise
        # The HTTP session exists only after connecting, and each connect may replace it.
        session = self._client._session
        if getattr(session, "_spike_tuned", False):
            return
        # Document responses stall about 40 ms on a reused connection (measured:
        # 50 ms kept alive, 11 ms on a fresh one), so each request opens its own.
        session.headers["Connection"] = "close"
        # The client sets no timeout; without one a slow diff would hang the run.
        session.request = functools.partial(session.request, timeout=_REQUEST_TIMEOUT_S)
        session._spike_tuned = True

    # -- lifecycle ----------------------------------------------------------

    def reset(self) -> None:
        if self._client.has_database(_DATABASE, _TEAM):
            self._client.delete_database(_DATABASE, _TEAM, force=True)
        self._client.create_database(
            _DATABASE, team=_TEAM, label=_DATABASE, description="storage benchmark"
        )
        self._connect(_DATABASE)
        self._alias.clear()
        log.info("database recreated", database=_DATABASE)

    def install_schema(self) -> list[str]:
        documents, unenforced = to_terminus_schema(self._ontology)
        self._client.insert_document(documents, graph_type="schema", commit_msg="schema")
        log.info("schema installed", classes=len(documents), unenforced=unenforced)
        return unenforced

    def clone(self) -> "TerminusEngine":
        other = TerminusEngine(self._config, self._ontology, connect_database=True)
        other._alias = self._alias
        return other

    def close(self) -> None:
        try:
            self._client.close()
        except Exception as exc:
            log.warn("closing client failed", error=repr(exc))

    # -- conversion ---------------------------------------------------------

    def _to_document(self, type_name: str, row: dict) -> dict:
        document = {"@type": type_name}
        for field in self._ontology.type(type_name).fields:
            value = row[field.name]
            document[field.name] = f"{field.target}/{value}" if field.kind == "ref" else value
        return document

    def _from_document(self, type_name: str, document: dict) -> dict:
        row = {}
        for field in self._ontology.type(type_name).fields:
            value = document.get(field.name)
            if field.kind == "ref" and isinstance(value, str):
                value = value.split("/", 1)[1]
            row[field.name] = value
        return row

    def _point_at(self, at: str | None, branch: str | None) -> None:
        self._client.branch = branch or "main"
        self._client.ref = at

    def _head(self, branch: str | None) -> str:
        if branch is None:
            return self._client.log(count=1)[0]["identifier"]
        for entry in self._client.get_all_branches():
            if entry["name"] == branch:
                return entry["head"].split("/", 1)[1]
        raise LookupError(f"branch {branch} not found")

    # -- writes -------------------------------------------------------------

    def commit(self, changes: ChangeSet, message: str, branch: str | None = None) -> str:
        self._point_at(None, branch)
        try:
            documents = [
                self._to_document(object_type.name, row)
                for object_type in self._ontology.types
                for row in changes.upserts.get(object_type.name, [])
            ]
            first = None
            if documents:
                self._client.replace_document(documents, commit_msg=message, create=True)
                first = self._head(branch)
            doomed = [
                f"{type_name}/{uid}" for type_name, uids in changes.deletes.items() for uid in uids
            ]
            if doomed:
                self._client.delete_document(doomed, commit_msg=message)
            version = self._head(branch)
            if first and first != version:
                self._alias[first] = version
            return version
        except Exception as exc:
            log.error("commit failed", exc=exc, message_text=message, branch=branch)
            raise
        finally:
            self._point_at(None, None)

    # -- reads --------------------------------------------------------------

    def get_object(
        self, type_name: str, uid: str, at: str | None = None, branch: str | None = None
    ) -> dict | None:
        self._point_at(at, branch)
        try:
            return self._from_document(type_name, self._client.get_document(f"{type_name}/{uid}"))
        except Exception as exc:
            # A missing document is an expected outcome for objects not yet created.
            log.debug("document not found", type=type_name, uid=uid, at=at, reason=str(exc)[:200])
            return None
        finally:
            self._point_at(None, None)

    def node_table(self, type_name: str, at: str | None = None) -> pa.Table:
        self._point_at(at, None)
        try:
            documents = self._client.get_documents_by_type(type_name, as_list=True)
        finally:
            self._point_at(None, None)
        fields = self._ontology.type(type_name).fields
        columns: dict[str, list] = {field.name: [] for field in fields}
        for document in documents:
            for field in fields:
                value = document.get(field.name)
                if field.kind == "ref" and isinstance(value, str):
                    value = value.split("/", 1)[1]
                columns[field.name].append(value)
        return pa.table(columns)

    def property_history(self, type_name: str, uid: str, prop: str) -> list[tuple[str, object]]:
        doc_id = f"{type_name}/{uid}"
        commits: list[str] = []
        start = 0
        while True:
            page = self._client.get_document_history(doc_id, start=start, count=_HISTORY_PAGE)
            commits.extend(entry["identifier"] for entry in page)
            if len(page) < _HISTORY_PAGE:
                break
            start += _HISTORY_PAGE
        # The history endpoint lists commits without values. One query that reads the
        # property inside each listed commit avoids a round trip per change point.
        history = []
        for start in range(0, len(commits), _HISTORY_QUERY_CHUNK):
            chunk = commits[start : start + _HISTORY_QUERY_CHUNK]
            query = WQ().woql_or(
                *[
                    WQ().woql_and(
                        WQ().using(
                            f"{_TEAM}/{_DATABASE}/local/commit/{commit}",
                            WQ().triple(doc_id, prop, "v:Value"),
                        ),
                        WQ().eq("v:Commit", WQ().string(commit)),
                    )
                    for commit in chunk
                ]
            )
            for binding in self._client.query(query)["bindings"]:
                commit = binding["Commit"]["@value"]
                history.append((self._alias.get(commit, commit), binding["Value"]["@value"]))
        return history

    def diff_count(self, before: str, after: str, type_name: str) -> int:
        prefix = f"{type_name}/"
        count = 0
        for patch in self._client.diff_version(before, after):
            doc_id = (
                patch.get("@id")
                or (patch.get("@insert") or {}).get("@id")
                or (patch.get("@delete") or {}).get("@id")
                or ""
            )
            count += doc_id.startswith(prefix)
        return count

    def neighbors(self, uid: str, depth: int, at: str | None = None) -> set[str]:
        self._point_at(at, None)
        try:
            # A link is a document, so one hop is: into the link by src, out by dst.
            query = WQ().path(f"Asset/{uid}", f"(<src,dst){{1,{depth}}}", "v:Target")
            bindings = self._client.query(query)["bindings"]
        finally:
            self._point_at(None, None)
        found = {binding["Target"].split("/", 1)[1] for binding in bindings}
        found.discard(uid)
        return found

    # -- branches -----------------------------------------------------------

    def branch_create(self, name: str, at: str) -> None:
        self._point_at(at, None)
        try:
            self._client.create_branch(name)
        finally:
            self._point_at(None, None)

    def branch_delete(self, name: str) -> None:
        self._client.delete_branch(name)

    # -- constraints --------------------------------------------------------

    def rejections(self) -> dict[str, bool]:
        site = self._client.get_documents_by_type("Site", count=1, as_list=True)[0]["@id"]
        base = {
            "@type": "Asset",
            "uid": "reject-probe",
            "name": "probe",
            "capacity": 10.0,
            "load": 1.0,
            "status": "ok",
            "site": site,
        }
        cases = {
            "bad_enum": {**base, "status": "exploded"},
            "missing_required": {k: v for k, v in base.items() if k != "name"},
            "dangling_ref": {**base, "site": "Site/no-such-site"},
            "wrong_type": {**base, "capacity": "not-a-number"},
            "below_minimum": {**base, "capacity": -5.0},
        }
        assert set(cases) == set(REJECTION_CASES)
        # Probe on a throwaway branch so an accepted invalid write never reaches the live line.
        branch = "reject-probe"
        self.branch_create(branch, self._head(None))
        outcome = {}
        try:
            self._point_at(None, branch)
            for case, document in cases.items():
                try:
                    self._client.insert_document(document, commit_msg=f"probe {case}")
                    outcome[case] = False
                    log.warn("invalid write was accepted", case=case)
                    self._client.delete_document(["Asset/reject-probe"], commit_msg="undo probe")
                except Exception as exc:
                    outcome[case] = True
                    log.debug("invalid write refused", case=case, reason=str(exc)[:200])
        finally:
            self._point_at(None, None)
            self.branch_delete(branch)
        return outcome
