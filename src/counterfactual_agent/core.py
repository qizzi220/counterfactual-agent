from __future__ import annotations

import functools
import hashlib
import json
import math
import os
import re
import sqlite3
import threading
import time
import uuid
from contextlib import closing, contextmanager
from collections import Counter
from pathlib import Path
from typing import Any


class LabError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


def guarded(method):
    @functools.wraps(method)
    def call(self, *args, **kwargs):
        with self._lock:
            try:
                return method(self, *args, **kwargs)
            except LabError as exc:
                return {"ok": False, "error": {"code": exc.code, "message": exc.message}}
            except (sqlite3.Error, OSError):
                return {"ok": False, "error": {"code": "storage_error", "message": "The local operation could not be completed."}}
    return call


class CounterfactualLab:
    def __init__(self, data_root: Path, state_root: Path, max_rows: int = 10000,
                 max_steps: int = 200000, timeout_seconds: float = 2.0):
        self.data_root = Path(data_root).resolve(strict=True)
        if not self.data_root.is_dir():
            raise ValueError("data_root must be an existing directory")
        self.state_root = Path(state_root).resolve()
        self.state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.snapshots = self.state_root / "snapshots"
        self.snapshots.mkdir(exist_ok=True, mode=0o700)
        self.store_path = self.state_root / "state.sqlite3"
        self.max_rows = max(1, int(max_rows))
        self.max_steps = max(1000, int(max_steps))
        self.timeout_seconds = max(0.01, float(timeout_seconds))
        self._lock = threading.RLock()
        with self._store() as con:
            con.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, question TEXT NOT NULL,
                reference_sql TEXT NOT NULL, tags TEXT NOT NULL, ordered INTEGER NOT NULL,
                base_digest TEXT NOT NULL, frozen INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS variants (
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
                name TEXT NOT NULL, partition TEXT NOT NULL, digest TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS attempts (
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
                query_sql TEXT NOT NULL, query_hash TEXT NOT NULL,
                partition TEXT NOT NULL, status TEXT NOT NULL, report TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS lessons (
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
                failing_attempt_id TEXT NOT NULL, passing_attempt_id TEXT NOT NULL,
                title TEXT NOT NULL, rule TEXT NOT NULL, tags TEXT NOT NULL,
                status TEXT NOT NULL, created_at REAL NOT NULL
            );
            """)
        os.chmod(self.store_path, 0o600)

    @contextmanager
    def _store(self):
        con = sqlite3.connect(self.store_path, timeout=10)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        con.execute("BEGIN IMMEDIATE")
        try:
            with con:
                yield con
        finally:
            con.close()

    @staticmethod
    def _text(value, field, limit=8000):
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise LabError("invalid_input", f"{field} must be a nonempty string of at most {limit} characters.")
        return value.strip()

    @staticmethod
    def _tags(tags):
        if not isinstance(tags, list) or len(tags) > 20 or any(not isinstance(t, str) or not t.strip() or len(t) > 80 for t in tags):
            raise LabError("invalid_input", "tags must contain at most twenty short strings.")
        return sorted(set(t.strip().lower() for t in tags))

    @staticmethod
    def _identifier(name):
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", name):
            raise LabError("invalid_identifier", "Use an ordinary table or column identifier.")
        return '"' + name + '"'

    @staticmethod
    def _hash_file(path):
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()

    @staticmethod
    def _sql_hash(query):
        return hashlib.sha256(query.encode("utf-8")).hexdigest()

    def _task(self, con, task_id):
        row = con.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise LabError("not_found", "Task does not exist.")
        return row

    def _snapshot(self, source, destination):
        try:
            with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as src:
                with closing(sqlite3.connect(destination)) as dst, dst:
                    deadline = time.monotonic() + max(10.0, self.timeout_seconds * 5)
                    def bounded_backup(status, remaining, total):
                        if time.monotonic() > deadline:
                            raise LabError("snapshot_timeout", "The snapshot could not finish within the copy budget.")
                    src.backup(dst, pages=128, progress=bounded_backup, sleep=0.05)
            os.chmod(destination, 0o600)
        except Exception:
            destination.unlink(missing_ok=True)
            raise

    def _query(self, path, query):
        started = time.monotonic()
        allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}
        count = 0
        limited = False
        denied = False

        def progress():
            nonlocal count, limited
            count += 1000
            limited = count > self.max_steps or time.monotonic() - started > self.timeout_seconds
            return int(limited)

        def authorize(action, arg1, arg2, database, trigger):
            nonlocal denied
            if action not in allowed or (action == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() in {"load_extension", "readfile", "writefile", "eval", "fts3_tokenizer"}):
                denied = True
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        try:
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as con:
                con.execute("PRAGMA query_only = ON")
                con.execute("PRAGMA trusted_schema = OFF")
                con.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 1_000_000)
                con.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 100_000)
                con.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, 200)
                con.set_progress_handler(progress, 1000)
                con.set_authorizer(authorize)
                cursor = con.execute(query)
                if cursor.description is None:
                    return {"status": "inconclusive", "reason": "not_a_result_query"}, None
                rows = []
                output_bytes = 0
                for row in cursor:
                    if len(rows) >= self.max_rows:
                        return {"status": "inconclusive", "reason": "row_limit"}, None
                    output_bytes += sum(len(v.encode("utf-8")) if isinstance(v, str) else len(v) if isinstance(v, bytes) else 16 for v in row)
                    if output_bytes > 4 * 1024 * 1024:
                        return {"status": "inconclusive", "reason": "output_limit"}, None
                    rows.append(row)
                if any(isinstance(v, float) and not math.isfinite(v) for row in rows for v in row):
                    return {"status": "inconclusive", "reason": "nonfinite_value"}, None
                return {"status": "complete", "row_count": len(rows), "column_count": len(cursor.description)}, rows
        except sqlite3.Error:
            reason = "query_not_allowed" if denied else "execution_limit" if limited else "query_failed"
            return {"status": "inconclusive", "reason": reason}, None

    @guarded
    def register_task(self, name, question, database_path, reference_sql, tags=None, ordered=False):
        name = self._text(name, "name", 160)
        question = self._text(question, "question")
        reference_sql = self._text(reference_sql, "reference_sql", 100000)
        tags = self._tags([] if tags is None else tags)
        if not isinstance(ordered, bool):
            raise LabError("invalid_input", "ordered must be a boolean.")
        if not isinstance(database_path, str) or Path(database_path).is_absolute():
            raise LabError("path_not_allowed", "Provide a relative SQLite path inside data_root.")
        try:
            source = (self.data_root / database_path).resolve(strict=True)
            source.relative_to(self.data_root)
        except (OSError, ValueError):
            raise LabError("path_not_allowed", "The database must be inside data_root.")
        if source.is_file() and source.stat().st_size > 64 * 1024 * 1024:
            raise LabError("snapshot_limit", "Use a SQLite snapshot no larger than 64 MiB.")
        if not source.is_file():
            raise LabError("path_not_allowed", "The database must be a regular file.")
        task_id = uuid.uuid4().hex
        destination = self.snapshots / (task_id + ".sqlite3")
        self._snapshot(source, destination)
        check, _ = self._query(destination, reference_sql)
        if check["status"] != "complete":
            destination.unlink(missing_ok=True)
            raise LabError("invalid_reference", "The reference query must finish as a bounded read-only result query.")
        with self._store() as con:
            con.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,0,?)", (
                task_id, name, question, reference_sql, json.dumps(tags), int(ordered),
                self._hash_file(destination), time.time()))
        return {"ok": True, "task_id": task_id, "comparison": "ordered" if ordered else "multiset",
                "scope": "local_snapshot", "reference_requires_semantic_review": True}

    @staticmethod
    def _values(mapping, allow_empty=False):
        if not isinstance(mapping, dict) or (not mapping and not allow_empty) or len(mapping) > 100:
            raise LabError("invalid_mutation", "A nonempty mapping of columns to scalar values is required.")
        for value in mapping.values():
            if value is not None and type(value) not in {str, int, float}:
                raise LabError("invalid_mutation", "Mutation values must be strings, numbers or null.")
            if type(value) is int and not -(2 ** 63) <= value < 2 ** 63:
                raise LabError("invalid_mutation", "Integer values must fit SQLite signed 64-bit storage.")
            if isinstance(value, str) and len(value) > 100000:
                raise LabError("invalid_mutation", "Mutation values exceed the size limit.")
            if isinstance(value, float) and not math.isfinite(value):
                raise LabError("invalid_mutation", "Mutation numbers must be finite.")
        return mapping

    def _apply_operation(self, con, operation):
        if not isinstance(operation, dict) or operation.get("kind") not in {"insert", "update", "delete"}:
            raise LabError("invalid_mutation", "Each operation must be insert, update or delete.")
        kind = operation["kind"]
        allowed_keys = {"kind", "table", "values"} if kind == "insert" else {"kind", "table", "where", "values"} if kind == "update" else {"kind", "table", "where"}
        if set(operation) - allowed_keys:
            raise LabError("invalid_mutation", "The operation contains unsupported fields.")
        table = self._identifier(operation.get("table"))
        kind_row = con.execute("SELECT type FROM sqlite_master WHERE name = ?", (operation.get("table"),)).fetchone()
        if kind_row is None or kind_row[0] != "table" or operation["table"].lower().startswith("sqlite_"):
            raise LabError("invalid_mutation", "Mutations require an ordinary application table.")
        if kind == "insert":
            values = self._values(operation.get("values"))
            names = ", ".join(self._identifier(k) for k in values)
            placeholders = ", ".join("?" for _ in values)
            con.execute(f"INSERT INTO {table} ({names}) VALUES ({placeholders})", list(values.values()))
            return
        where = self._values(operation.get("where"))
        predicates = " AND ".join(self._identifier(k) + (" IS NULL" if v is None else " = ?") for k, v in where.items())
        args = [v for v in where.values() if v is not None]
        if kind == "delete":
            con.execute(f"DELETE FROM {table} WHERE {predicates}", args)
        else:
            values = self._values(operation.get("values"))
            changes = ", ".join(self._identifier(k) + " = ?" for k in values)
            con.execute(f"UPDATE {table} SET {changes} WHERE {predicates}", list(values.values()) + args)

    @guarded
    def add_variant(self, task_id, name, operations, partition="practice"):
        name = self._text(name, "name", 160)
        if partition not in {"practice", "holdout"}:
            raise LabError("invalid_input", "partition must be practice or holdout.")
        if not isinstance(operations, list) or not 1 <= len(operations) <= 100:
            raise LabError("invalid_mutation", "Provide between one and one hundred operations.")
        with self._store() as store:
            task = self._task(store, task_id)
            if task["frozen"]:
                raise LabError("suite_frozen", "Variants cannot change after evaluation starts. Create a new task version.")
            source = self.snapshots / (task_id + ".sqlite3")
            if self._hash_file(source) != task["base_digest"]:
                raise LabError("snapshot_changed", "The stored snapshot changed outside the lab.")
            variant_id = uuid.uuid4().hex
            destination = self.snapshots / (variant_id + ".sqlite3")
            self._snapshot(source, destination)
            try:
                with closing(sqlite3.connect(destination)) as con, con:
                    con.execute("PRAGMA foreign_keys = ON")
                    con.execute("PRAGMA trusted_schema = OFF")
                    deadline = time.monotonic() + self.timeout_seconds
                    con.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
                    con.execute("BEGIN")
                    for operation in operations:
                        self._apply_operation(con, operation)
                    if con.execute("PRAGMA foreign_key_check").fetchone():
                        raise LabError("constraint_violation", "The variant violates database constraints.")
                if destination.stat().st_size > 64 * 1024 * 1024:
                    raise LabError("snapshot_limit", "The variant exceeds the 64 MiB snapshot limit.")
                check, _ = self._query(destination, task["reference_sql"])
                if check["status"] != "complete":
                    raise LabError("invalid_variant", "The reference does not finish on this variant.")
                store.execute("INSERT INTO variants VALUES (?,?,?,?,?)", (variant_id, task_id, name, partition, self._hash_file(destination)))
            except sqlite3.Error:
                destination.unlink(missing_ok=True)
                raise LabError("constraint_violation", "The variant could not be applied while preserving the schema.")
            except Exception:
                destination.unlink(missing_ok=True)
                raise
        return {"ok": True, "variant_id": variant_id, "partition": partition,
                "business_constraints_require_review": True}

    @guarded
    def evaluate(self, task_id, candidate_sql, partition="practice"):
        candidate_sql = self._text(candidate_sql, "candidate_sql", 100000)
        if partition not in {"practice", "holdout"}:
            raise LabError("invalid_input", "partition must be practice or holdout.")
        with self._store() as con:
            task = self._task(con, task_id)
            variants = con.execute("SELECT * FROM variants WHERE task_id = ? AND partition = ? ORDER BY id", (task_id, partition)).fetchall()
            if not variants:
                raise LabError("empty_suite", "At least one variant in this partition is required.")
            cases = [(v["id"], v["digest"]) for v in variants]
            if partition == "practice":
                cases.insert(0, (task_id, task["base_digest"]))
            prior_holdout = con.execute("SELECT COUNT(*) FROM attempts WHERE task_id = ? AND partition = 'holdout'", (task_id,)).fetchone()[0]
            con.execute("UPDATE tasks SET frozen = 1 WHERE id = ?", (task_id,))
            con.commit()
            checks = []
            for case_id, digest in cases:
                path = self.snapshots / (case_id + ".sqlite3")
                if not path.is_file() or self._hash_file(path) != digest:
                    checks.append({"case_id": case_id, "status": "inconclusive", "reason": "snapshot_changed"})
                    continue
                reference_check, reference_rows = self._query(path, task["reference_sql"])
                candidate_check, candidate_rows = self._query(path, candidate_sql)
                if reference_check["status"] != "complete" or candidate_check["status"] != "complete":
                    checks.append({"case_id": case_id, "status": "inconclusive", "reference": reference_check, "candidate": candidate_check})
                    continue
                matched = (candidate_rows == reference_rows if task["ordered"] else Counter(candidate_rows) == Counter(reference_rows))
                if candidate_check["column_count"] != reference_check["column_count"]:
                    matched = False
                checks.append({"case_id": case_id, "status": "match" if matched else "different",
                               "reference_rows": reference_check["row_count"], "candidate_rows": candidate_check["row_count"]})
            status = "inconclusive" if any(c["status"] == "inconclusive" for c in checks) else "fail" if any(c["status"] == "different" for c in checks) else "pass"
            attempt_id = uuid.uuid4().hex
            report = {"ok": True, "attempt_id": attempt_id, "task_id": task_id,
                      "partition": partition, "status": status, "checks": checks,
                      "query_hash": self._sql_hash(candidate_sql),
                      "scope": "frozen_task_cases_only", "holdout_previously_exposed": bool(prior_holdout) if partition == "holdout" else None,
                      "holdout_is_independently_blinded": False}
            con.execute("INSERT INTO attempts VALUES (?,?,?,?,?,?,?,?)", (attempt_id, task_id, candidate_sql,
                report["query_hash"], partition, status, json.dumps(report), time.time()))
        return report

    def _evidence(self, con, task_id, failing_id, passing_id):
        failing = con.execute("SELECT * FROM attempts WHERE id = ? AND task_id = ?", (failing_id, task_id)).fetchone()
        passing = con.execute("SELECT * FROM attempts WHERE id = ? AND task_id = ?", (passing_id, task_id)).fetchone()
        if failing is None or passing is None or failing["partition"] != "practice" or passing["partition"] != "practice":
            raise LabError("invalid_evidence", "Use practice attempts from the same task.")
        if failing["status"] != "fail" or passing["status"] != "pass" or failing["query_hash"] == passing["query_hash"] or failing["created_at"] >= passing["created_at"]:
            raise LabError("invalid_evidence", "Evidence must show a semantic failure followed by a different passing query.")
        latest = con.execute("SELECT status FROM attempts WHERE task_id = ? AND query_hash = ? AND partition = 'practice' ORDER BY created_at DESC LIMIT 1", (task_id, passing["query_hash"])).fetchone()
        if latest is None or latest["status"] != "pass":
            raise LabError("invalid_evidence", "The corrected query must pass its latest practice evaluation.")
        return failing, passing

    @guarded
    def propose_lesson(self, task_id, failing_attempt_id, passing_attempt_id, title, rule, tags=None):
        title = self._text(title, "title", 160)
        rule = self._text(rule, "rule", 3000)
        tags = self._tags([] if tags is None else tags)
        if re.search(r"(?:github_pat_|gh[pousr]_|sk-[A-Za-z0-9]{12}|Bearer\s+\S+)", title + " " + rule):
            raise LabError("sensitive_lesson", "Remove credentials from the lesson.")
        with self._store() as con:
            self._task(con, task_id)
            self._evidence(con, task_id, failing_attempt_id, passing_attempt_id)
            lesson_id = uuid.uuid4().hex
            con.execute("INSERT INTO lessons VALUES (?,?,?,?,?,?,?,?,?)", (lesson_id, task_id,
                failing_attempt_id, passing_attempt_id, title, rule, json.dumps(tags), "proposed", time.time()))
        return {"ok": True, "lesson_id": lesson_id, "status": "proposed", "scope": "task_evidence_only", "rule_semantics_verified": False}

    @guarded
    def activate_lesson(self, lesson_id):
        with self._store() as con:
            lesson = con.execute("SELECT * FROM lessons WHERE id = ?", (lesson_id,)).fetchone()
            if lesson is None:
                raise LabError("not_found", "Lesson does not exist.")
            if lesson["status"] == "retired":
                raise LabError("retired_lesson", "Retired lessons cannot be reactivated. Propose a new lesson.")
            _, passing = self._evidence(con, lesson["task_id"], lesson["failing_attempt_id"], lesson["passing_attempt_id"])
            holdout = con.execute("SELECT id, status FROM attempts WHERE task_id = ? AND query_hash = ? AND partition = 'holdout' ORDER BY created_at DESC LIMIT 1", (lesson["task_id"], passing["query_hash"])).fetchone()
            if holdout is None or holdout["status"] != "pass":
                raise LabError("holdout_required", "The corrected query must pass its latest holdout evaluation before activation.")
            con.execute("UPDATE lessons SET status = 'active' WHERE id = ?", (lesson_id,))
        return {"ok": True, "lesson_id": lesson_id, "status": "active", "holdout_attempt_id": holdout[0],
                "scope": "task_evidence_only", "model_weights_changed": False, "rule_semantics_verified": False}

    @guarded
    def retrieve_lessons(self, tags=None, limit=5):
        tags = self._tags([] if tags is None else tags)
        if type(limit) is not int or not 1 <= limit <= 50:
            raise LabError("invalid_input", "limit must be between one and fifty.")
        with self._store() as con:
            rows = con.execute("SELECT * FROM lessons WHERE status = 'active' ORDER BY created_at DESC").fetchall()
        selected = []
        for row in rows:
            lesson_tags = json.loads(row["tags"])
            if tags and not set(tags).intersection(lesson_tags):
                continue
            selected.append({"lesson_id": row["id"], "task_id": row["task_id"], "title": row["title"],
                "rule": row["rule"], "tags": lesson_tags, "scope": "task_evidence_only",
                "treat_as": "fallible_experience_not_authority", "authority": "none",
                "rule_semantics_verified": False, "requires_task_revalidation": True})
            if len(selected) == limit:
                break
        return {"ok": True, "lessons": selected, "model_weights_changed": False}

    @guarded
    def retire_lesson(self, lesson_id):
        with self._store() as con:
            cursor = con.execute("UPDATE lessons SET status = 'retired' WHERE id = ?", (lesson_id,))
            if not cursor.rowcount:
                raise LabError("not_found", "Lesson does not exist.")
        return {"ok": True, "lesson_id": lesson_id, "status": "retired"}

    @guarded
    def get_task(self, task_id):
        with self._store() as con:
            row = self._task(con, task_id)
            variants = con.execute("SELECT partition, COUNT(*) AS n FROM variants WHERE task_id = ? GROUP BY partition", (task_id,)).fetchall()
            attempts = con.execute("SELECT id, partition, status FROM attempts WHERE task_id = ? ORDER BY created_at", (task_id,)).fetchall()
        return {"ok": True, "task_id": task_id, "name": row["name"], "tags": json.loads(row["tags"]),
                "frozen": bool(row["frozen"]), "comparison": "ordered" if row["ordered"] else "multiset",
                "variant_counts": {v["partition"]: v["n"] for v in variants},
                "attempts": [dict(a) for a in attempts], "scope": "local_snapshot"}
