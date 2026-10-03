"""Application persistence; LangGraph owns its separate checkpoint/store tables.

Short synchronous operations use independent connections so transactions never span
an LLM call. The offline repository is a test double, not a production fallback.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from threading import RLock
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from insight.query_results import page_result, prepare_result, preview_result

ACTIVE = ("queued", "running", "waiting_for_input")
STATUSES = set(ACTIVE) | {"completed", "failed", "cancelled", "interrupted"}
RUN_FIELDS = {"status", "artifacts", "error", "question", "origin", "question_id", "data_version", "model_version"}
ORIGINS = {"user", "question_catalog", "legacy"}

SCHEMA = """
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS ia_threads (
    thread_id TEXT PRIMARY KEY, scenario_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    summary TEXT NOT NULL DEFAULT '', summary_upto_id BIGINT NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS ia_runs (
    run_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL REFERENCES ia_threads(thread_id),
    scenario_id TEXT NOT NULL, question TEXT NOT NULL, status TEXT NOT NULL,
    artifacts JSONB NOT NULL DEFAULT '{}', error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS ia_one_active_run_per_thread ON ia_runs(thread_id)
    WHERE status IN ('queued', 'running', 'waiting_for_input');
CREATE TABLE IF NOT EXISTS ia_messages (
    message_id BIGSERIAL PRIMARY KEY, thread_id TEXT NOT NULL REFERENCES ia_threads(thread_id),
    role TEXT NOT NULL, content TEXT NOT NULL, run_id TEXT NOT NULL REFERENCES ia_runs(run_id),
    message_key TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(thread_id, message_key)
);
CREATE TABLE IF NOT EXISTS ia_events (
    event_id BIGSERIAL PRIMARY KEY, run_id TEXT NOT NULL REFERENCES ia_runs(run_id),
    type TEXT NOT NULL, payload JSONB NOT NULL, event_key TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(), UNIQUE(run_id, event_key)
);
CREATE INDEX IF NOT EXISTS ia_events_cursor ON ia_events(run_id, event_id);
CREATE INDEX IF NOT EXISTS ia_messages_thread ON ia_messages(thread_id, message_id);
CREATE TABLE IF NOT EXISTS ia_retrieval (
    document_id TEXT NOT NULL, scenario_id TEXT NOT NULL, kind TEXT NOT NULL,
    title TEXT NOT NULL, content TEXT NOT NULL, tokenized TEXT NOT NULL,
    metadata_json JSONB NOT NULL, content_hash TEXT NOT NULL,
    embedding VECTOR(1024), embedding_model TEXT,
    search_vector TSVECTOR GENERATED ALWAYS AS (
        setweight(to_tsvector('simple', title), 'A') ||
        setweight(to_tsvector('simple', tokenized), 'B')
    ) STORED,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(scenario_id, document_id)
);
CREATE INDEX IF NOT EXISTS ia_retrieval_scope ON ia_retrieval(scenario_id, kind);
CREATE INDEX IF NOT EXISTS ia_retrieval_fts ON ia_retrieval USING GIN(search_vector);
CREATE INDEX IF NOT EXISTS ia_retrieval_meta ON ia_retrieval USING GIN(metadata_json);
CREATE INDEX IF NOT EXISTS ia_retrieval_vector ON ia_retrieval USING HNSW(embedding vector_cosine_ops);
"""

# Additive only. Existing rows are explicitly legacy; new rows always specify origin.
LIGHTBI_SCHEMA = """
ALTER TABLE ia_runs ADD COLUMN IF NOT EXISTS origin TEXT NOT NULL DEFAULT 'legacy';
ALTER TABLE ia_runs ADD COLUMN IF NOT EXISTS question_id TEXT;
ALTER TABLE ia_runs ADD COLUMN IF NOT EXISTS data_version TEXT;
ALTER TABLE ia_runs ADD COLUMN IF NOT EXISTS model_version TEXT;
ALTER TABLE ia_runs ADD COLUMN IF NOT EXISTS idempotency_key TEXT;
CREATE INDEX IF NOT EXISTS ia_runs_history ON ia_runs(scenario_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ia_runs_bootstrap ON ia_runs(idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE TABLE IF NOT EXISTS ia_query_results (
    run_id TEXT NOT NULL REFERENCES ia_runs(run_id), query_id TEXT NOT NULL,
    payload JSONB NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(run_id, query_id)
);
"""


def _json(row):
    if row is None:
        return None
    result = {key: value.isoformat() if isinstance(value, datetime) else value for key, value in row.items()}
    if "run_id" in result and "question" in result:
        result.setdefault("origin", "legacy")
        result["domain_id"] = result["scenario_id"]
        artifacts = result.get("artifacts") or {}
        for key in ("data_version", "model_version", "question_id"):
            result[key] = result.get(key) or artifacts.get(key)
    return result


def _now():
    return datetime.now(UTC).isoformat()


def _run_metadata(origin, question_id, data_version, model_version):
    if origin not in ORIGINS:
        raise ValueError("Unsupported run origin")
    if origin == "question_catalog" and not question_id:
        raise ValueError("Catalog runs require question_id")
    if any(value is not None and (not isinstance(value, str) or len(value) > 256) for value in (question_id, data_version, model_version)):
        raise ValueError("Invalid run version metadata")


def _reuse(existing, retry_failed):
    return existing and (existing["status"] in ACTIVE or (
        existing["status"] == "completed" and not existing.get("artifacts", {}).get("partial")
    ) or not retry_failed)


def _check_reservation(existing, scenario_id, origin, question_id, data_version, model_version):
    if existing and any(existing.get(key) != value for key, value in {
        "scenario_id": scenario_id, "origin": origin, "question_id": question_id,
        "data_version": data_version, "model_version": model_version,
    }.items()):
        raise ValueError("Idempotency key belongs to another domain, question or version")


def _history_item(run):
    run = _json(run)
    result = {k: v for k, v in run.items() if k not in {"artifacts", "idempotency_key"}}
    result["partial"] = bool(run.get("artifacts", {}).get("partial", run.get("partial", False)))
    result["completed_at"] = result["updated_at"] if result["status"] not in ACTIVE else None
    return result


class _HistoryResults:
    def save_query_result(self, run_id, query):
        if query.get("result_ref"):
            # Saving a checkpoint preview again must never replace its full table.
            full = self.hydrate_queries(run_id, [query])[0]
            return preview_result(run_id, full)
        result = prepare_result(query)
        if self.is_memory:
            with self._lock:
                if run_id not in self._runs:
                    raise KeyError(run_id)
                self._query_results[(run_id, result["id"])] = result
        else:
            with self.connect() as conn:
                if not conn.execute("SELECT 1 FROM ia_runs WHERE run_id=%s", (run_id,)).fetchone():
                    raise KeyError(run_id)
                conn.execute("INSERT INTO ia_query_results(run_id,query_id,payload) VALUES (%s,%s,%s) ON CONFLICT(run_id,query_id) DO UPDATE SET payload=EXCLUDED.payload,updated_at=now()", (run_id, result["id"], Jsonb(result)))
        return preview_result(run_id, result)

    def get_query_result(self, run_id, query_id):
        if self.is_memory:
            with self._lock:
                result = deepcopy(self._query_results.get((run_id, query_id)))
        else:
            with self.connect() as conn:
                row = conn.execute("SELECT payload FROM ia_query_results WHERE run_id=%s AND query_id=%s", (run_id, query_id)).fetchone()
                result = row["payload"] if row else None
        if result is not None:
            return result
        # Historical inline evidence is read-only, never silently recomputed.
        run = self.get_run(run_id)
        for query in run.get("artifacts", {}).get("queries", []):
            if query.get("id") == query_id and not query.get("result_ref") and "rows" in query:
                return {**deepcopy(query), "legacy": True, "full_result_available": False}
        raise KeyError(query_id)

    def hydrate_queries(self, run_id, queries):
        results = []
        for query in queries:
            ref = query.get("result_ref")
            if ref:
                if not isinstance(ref, dict) or ref.get("run_id") != run_id or ref.get("query_id") != query.get("id"):
                    raise ValueError("Result reference must belong to this run and query")
                full = self.get_query_result(run_id, query["id"])
                results.append({**query, **full})
            else:
                results.append(deepcopy(query))
        return results

    def query_rows(self, run_id, query_id, *, offset=0, limit=50, sort_by=None, descending=False):
        return page_result(self.get_query_result(run_id, query_id), offset=offset, limit=limit, sort_by=sort_by, descending=descending)

    def list_runs(self, domain_id=None, status=None, q=None, *, limit=50, offset=0):
        if type(limit) is not int or not 1 <= limit <= 200 or type(offset) is not int or offset < 0:
            raise ValueError("Invalid history pagination")
        if status is not None and status not in STATUSES:
            raise ValueError("Unsupported run status")
        if q is not None and (not isinstance(q, str) or len(q) > 4000):
            raise ValueError("Invalid history search")
        if self.is_memory:
            with self._lock:
                rows = [r for r in self._runs.values() if (domain_id is None or r["scenario_id"] == domain_id)
                        and (status is None or r["status"] == status) and (not q or q.casefold() in r["question"].casefold())]
                rows.sort(key=lambda r: (r["created_at"], r["run_id"]), reverse=True)
                def priority(row):
                    origin = row.get("origin", "legacy")
                    if origin == "legacy":
                        return 2
                    if origin == "user" or (origin == "question_catalog" and row["status"] == "completed"
                                            and not (row.get("artifacts") or {}).get("partial", False)):
                        return 0
                    return 1
                rows.sort(key=priority)
                items = [_history_item(deepcopy(r)) for r in rows[offset:offset + limit]]
                total = len(rows)
        else:
            where = "(%s::text IS NULL OR scenario_id=%s) AND (%s::text IS NULL OR status=%s) AND (%s::text IS NULL OR strpos(lower(question),lower(%s))>0)"
            values = (domain_id, domain_id, status, status, q, q)
            with self.connect() as conn:
                # Count and page use one snapshot even while other runs finish.
                conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                total = conn.execute(f"SELECT count(*) AS n FROM ia_runs WHERE {where}", values).fetchone()["n"]
                rows = conn.execute(f"""SELECT run_id,thread_id,scenario_id,question,status,error,created_at,updated_at,origin,question_id,
                    COALESCE(data_version,artifacts->>'data_version') AS data_version,
                    COALESCE(model_version,artifacts->>'model_version') AS model_version,
                    COALESCE((artifacts->>'partial')::boolean,false) AS partial
                    FROM ia_runs WHERE {where}
                    ORDER BY CASE WHEN origin='legacy' THEN 2
                        WHEN origin='user' OR (origin='question_catalog' AND status='completed'
                            AND NOT COALESCE((artifacts->>'partial')::boolean,false)) THEN 0 ELSE 1 END,
                        created_at DESC,run_id DESC LIMIT %s OFFSET %s""", (*values, limit, offset)).fetchall()
                items = [_history_item(r) for r in rows]
        return {"items": items, "total": total, "limit": limit, "offset": offset}


class Repository(_HistoryResults):
    is_memory = False

    def __init__(self, dsn: str):
        self.dsn = dsn

    def connect(self):
        return psycopg.connect(self.dsn, row_factory=dict_row, connect_timeout=5)

    def setup(self, *, migration=False):
        with self.connect() as conn:
            conn.execute(SCHEMA)
            installed = conn.execute("SELECT to_regclass('ia_query_results') AS name").fetchone()["name"]
            if not installed and not migration and conn.execute("SELECT count(*) AS n FROM ia_runs").fetchone()["n"]:
                raise ValueError("Existing history requires explicit backed-up Light BI migration")
            conn.execute(LIGHTBI_SCHEMA)

    def close(self):
        # Connections belong to individual operation context managers.
        pass

    def create_run(self, scenario_id, question, thread_id=None, *, origin="user", question_id=None,
                   data_version=None, model_version=None, idempotency_key=None, retry_failed=False):
        _run_metadata(origin, question_id, data_version, model_version)
        thread_id, run_id = thread_id or str(uuid4()), str(uuid4())
        try:
            with self.connect() as conn:
                if idempotency_key:
                    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (idempotency_key,))
                    existing = conn.execute("SELECT * FROM ia_runs WHERE idempotency_key=%s ORDER BY created_at DESC,run_id DESC LIMIT 1", (idempotency_key,)).fetchone()
                    _check_reservation(existing, scenario_id, origin, question_id, data_version, model_version)
                    if _reuse(existing, retry_failed):
                        return {**_json(existing), "reused": True}
                conn.execute("INSERT INTO ia_threads(thread_id,scenario_id) VALUES (%s,%s) ON CONFLICT DO NOTHING", (thread_id, scenario_id))
                thread = conn.execute("SELECT * FROM ia_threads WHERE thread_id=%s FOR UPDATE", (thread_id,)).fetchone()
                if thread["scenario_id"] != scenario_id:
                    raise ValueError("A thread cannot change scenario")
                if conn.execute("SELECT 1 FROM ia_runs WHERE thread_id=%s AND status=ANY(%s)", (thread_id, list(ACTIVE))).fetchone():
                    raise ValueError("This thread already has an unfinished run")
                row = conn.execute("INSERT INTO ia_runs(run_id,thread_id,scenario_id,question,status,origin,question_id,data_version,model_version,idempotency_key) VALUES (%s,%s,%s,%s,'queued',%s,%s,%s,%s,%s) RETURNING *", (run_id, thread_id, scenario_id, question, origin, question_id, data_version, model_version, idempotency_key)).fetchone()
                conn.execute("INSERT INTO ia_messages(thread_id,role,content,run_id,message_key) VALUES (%s,'user',%s,%s,%s)", (thread_id, question, run_id, f"question:{run_id}"))
                return _json(row)
        except psycopg.errors.UniqueViolation as exc:
            raise ValueError("This thread already has an unfinished run") from exc

    def get_run(self, run_id):
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM ia_runs WHERE run_id=%s", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return _json(row)

    def update_run(self, run_id, **fields):
        if not fields or set(fields) - RUN_FIELDS:
            raise ValueError("Unsupported run update")
        if "origin" in fields and fields["origin"] not in ORIGINS:
            raise ValueError("Unsupported run origin")
        if "status" in fields and fields["status"] not in STATUSES:
            raise ValueError("Unsupported run status")
        values = [Jsonb(value) if key == "artifacts" else value for key, value in fields.items()]
        assignments = ",".join(f"{key}=%s" for key in fields)  # keys strictly allowlisted
        with self.connect() as conn:
            row = conn.execute(f"UPDATE ia_runs SET {assignments},updated_at=now() WHERE run_id=%s RETURNING *", (*values, run_id)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return _json(row)

    def list_threads(self, scenario_id=None):
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM ia_threads WHERE (%s::text IS NULL OR scenario_id=%s) ORDER BY created_at DESC", (scenario_id, scenario_id)).fetchall()
        return [_json(row) for row in rows]

    def thread_detail(self, thread_id):
        with self.connect() as conn:
            thread = conn.execute("SELECT * FROM ia_threads WHERE thread_id=%s", (thread_id,)).fetchone()
            if not thread:
                raise KeyError(thread_id)
            runs = conn.execute("SELECT * FROM ia_runs WHERE thread_id=%s ORDER BY created_at", (thread_id,)).fetchall()
        return {"thread": _json(thread), "messages": self.messages(thread_id), "runs": [_json(row) for row in runs]}

    def add_message(self, thread_id, role, content, run_id, key=None):
        with self.connect() as conn:
            conn.execute("SELECT thread_id FROM ia_threads WHERE thread_id=%s FOR UPDATE", (thread_id,))
            run = conn.execute("SELECT thread_id FROM ia_runs WHERE run_id=%s", (run_id,)).fetchone()
            if not run or run["thread_id"] != thread_id:
                raise ValueError("Message must belong to its run's thread")
            row = conn.execute("INSERT INTO ia_messages(thread_id,role,content,run_id,message_key) VALUES (%s,%s,%s,%s,%s) ON CONFLICT(thread_id,message_key) DO NOTHING RETURNING *", (thread_id, role, content, run_id, key)).fetchone()
            if row is None:
                row = conn.execute("SELECT * FROM ia_messages WHERE thread_id=%s AND message_key=%s", (thread_id, key)).fetchone()
        return _json(row)

    def messages(self, thread_id):
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM ia_messages WHERE thread_id=%s ORDER BY message_id", (thread_id,)).fetchall()
        return [_json(row) for row in rows]

    def get_summary(self, thread_id):
        with self.connect() as conn:
            row = conn.execute("SELECT summary AS text,summary_upto_id AS upto_id FROM ia_threads WHERE thread_id=%s", (thread_id,)).fetchone()
        if not row:
            raise KeyError(thread_id)
        return row

    def save_summary(self, thread_id, text, upto_id):
        with self.connect() as conn:
            if not conn.execute("SELECT 1 FROM ia_messages WHERE thread_id=%s AND message_id=%s", (thread_id, upto_id)).fetchone():
                raise ValueError("Summary cursor must be a message in this thread")
            conn.execute("UPDATE ia_threads SET summary=%s,summary_upto_id=%s WHERE thread_id=%s AND summary_upto_id<=%s", (text, upto_id, thread_id, upto_id))

    def event(self, run_id, event_type, payload, key=None):
        with self.connect() as conn:
            # A cursor cannot skip an earlier ID whose transaction commits later.
            if not conn.execute("SELECT run_id FROM ia_runs WHERE run_id=%s FOR UPDATE", (run_id,)).fetchone():
                raise KeyError(run_id)
            row = conn.execute("INSERT INTO ia_events(run_id,type,payload,event_key) VALUES (%s,%s,%s,%s) ON CONFLICT(run_id,event_key) DO NOTHING RETURNING *", (run_id, event_type, Jsonb(payload), key)).fetchone()
            if row is None:
                row = conn.execute("SELECT * FROM ia_events WHERE run_id=%s AND event_key=%s", (run_id, key)).fetchone()
        return _json(row)

    def events(self, run_id, after=0):
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM ia_events WHERE run_id=%s AND event_id>%s ORDER BY event_id", (run_id, after)).fetchall()
        return [_json(row) for row in rows]

    def recover_interrupted(self):
        with self.connect() as conn:
            rows = conn.execute("UPDATE ia_runs SET status='interrupted',error='Service restarted during execution; submit a new task.',updated_at=now() WHERE status IN ('queued','running') RETURNING run_id").fetchall()
            for row in rows:
                conn.execute("INSERT INTO ia_events(run_id,type,payload,event_key) VALUES (%s,'run.failed',%s,%s) ON CONFLICT DO NOTHING", (row["run_id"], Jsonb({"reason": "service_restarted", "status": "interrupted"}), "interrupted"))
        return len(rows)

    def resume_run(self, run_id):
        with self.connect() as conn:
            row = conn.execute("UPDATE ia_runs SET status='running',updated_at=now() WHERE run_id=%s AND status='waiting_for_input' RETURNING *", (run_id,)).fetchone()
            if row is None:
                exists = conn.execute("SELECT 1 FROM ia_runs WHERE run_id=%s", (run_id,)).fetchone()
                if not exists:
                    raise KeyError(run_id)
                raise ValueError("Run is not waiting for user input")
        return _json(row)


class InMemoryRepository(_HistoryResults):
    """Explicit deterministic test adapter. Never selected implicitly on DB errors."""

    is_memory = True
    dsn = None

    def __init__(self):
        self._lock = RLock()
        self._runs, self._threads = {}, {}
        self._messages, self._events = [], []
        self._query_results = {}

    def setup(self, *, migration=False):
        pass

    def close(self):
        pass

    def create_run(self, scenario_id, question, thread_id=None, *, origin="user", question_id=None,
                   data_version=None, model_version=None, idempotency_key=None, retry_failed=False):
        _run_metadata(origin, question_id, data_version, model_version)
        with self._lock:
            if idempotency_key:
                existing = next((r for r in reversed(list(self._runs.values())) if r.get("idempotency_key") == idempotency_key), None)
                _check_reservation(existing, scenario_id, origin, question_id, data_version, model_version)
                if _reuse(existing, retry_failed):
                    return {**_json(deepcopy(existing)), "reused": True}
            thread_id = thread_id or str(uuid4())
            thread = self._threads.get(thread_id)
            if thread and thread["scenario_id"] != scenario_id:
                raise ValueError("A thread cannot change scenario")
            if any(r["thread_id"] == thread_id and r["status"] in ACTIVE for r in self._runs.values()):
                raise ValueError("This thread already has an unfinished run")
            self._threads.setdefault(thread_id, {"thread_id": thread_id, "scenario_id": scenario_id, "created_at": _now(), "summary": "", "summary_upto_id": 0})
            run_id, now = str(uuid4()), _now()
            self._runs[run_id] = {"run_id": run_id, "thread_id": thread_id, "scenario_id": scenario_id, "question": question, "status": "queued", "created_at": now, "updated_at": now, "artifacts": {}, "error": None,
                "origin": origin, "question_id": question_id, "data_version": data_version, "model_version": model_version, "idempotency_key": idempotency_key}
            self.add_message(thread_id, "user", question, run_id, f"question:{run_id}")
            return self.get_run(run_id)

    def get_run(self, run_id):
        with self._lock:
            return _json(deepcopy(self._runs[run_id]))

    def update_run(self, run_id, **fields):
        if not fields or set(fields) - RUN_FIELDS:
            raise ValueError("Unsupported run update")
        if "origin" in fields and fields["origin"] not in ORIGINS:
            raise ValueError("Unsupported run origin")
        if "status" in fields and fields["status"] not in STATUSES:
            raise ValueError("Unsupported run status")
        with self._lock:
            run = self._runs[run_id]
            status = fields.get("status", run["status"])
            if status in ACTIVE and any(r["run_id"] != run_id and r["thread_id"] == run["thread_id"] and r["status"] in ACTIVE for r in self._runs.values()):
                raise ValueError("This thread already has an unfinished run")
            run.update(deepcopy(fields), updated_at=_now())
            return _json(deepcopy(run))

    def list_threads(self, scenario_id=None):
        with self._lock:
            return deepcopy(sorted([t for t in self._threads.values() if scenario_id is None or t["scenario_id"] == scenario_id], key=lambda t: t["created_at"], reverse=True))

    def thread_detail(self, thread_id):
        with self._lock:
            return {"thread": deepcopy(self._threads[thread_id]), "messages": self.messages(thread_id), "runs": deepcopy([r for r in self._runs.values() if r["thread_id"] == thread_id])}

    def add_message(self, thread_id, role, content, run_id, key=None):
        with self._lock:
            if run_id not in self._runs or self._runs[run_id]["thread_id"] != thread_id:
                raise ValueError("Message must belong to its run's thread")
            if key is not None:
                existing = next((m for m in self._messages if m["thread_id"] == thread_id and m["message_key"] == key), None)
                if existing:
                    return deepcopy(existing)
            row = {"message_id": len(self._messages) + 1, "thread_id": thread_id, "role": role, "content": content, "run_id": run_id, "message_key": key, "created_at": _now()}
            self._messages.append(row)
            return deepcopy(row)

    def messages(self, thread_id):
        with self._lock:
            return deepcopy([m for m in self._messages if m["thread_id"] == thread_id])

    def get_summary(self, thread_id):
        with self._lock:
            thread = self._threads[thread_id]
            return {"text": thread["summary"], "upto_id": thread["summary_upto_id"]}

    def save_summary(self, thread_id, text, upto_id):
        with self._lock:
            if not any(m["thread_id"] == thread_id and m["message_id"] == upto_id for m in self._messages):
                raise ValueError("Summary cursor must be a message in this thread")
            thread = self._threads[thread_id]
            if upto_id >= thread["summary_upto_id"]:
                thread.update(summary=text, summary_upto_id=upto_id)

    def event(self, run_id, event_type, payload, key=None):
        with self._lock:
            if run_id not in self._runs:
                raise KeyError(run_id)
            if key is not None:
                existing = next((e for e in self._events if e["run_id"] == run_id and e["event_key"] == key), None)
                if existing:
                    return deepcopy(existing)
            row = {"event_id": len(self._events) + 1, "run_id": run_id, "type": event_type, "payload": deepcopy(payload), "event_key": key, "created_at": _now()}
            self._events.append(row)
            return deepcopy(row)

    def events(self, run_id, after=0):
        with self._lock:
            return deepcopy([e for e in self._events if e["run_id"] == run_id and e["event_id"] > after])

    def recover_interrupted(self):
        with self._lock:
            run_ids = [r["run_id"] for r in self._runs.values() if r["status"] in {"queued", "running"}]
            for run_id in run_ids:
                self.update_run(run_id, status="interrupted", error="Service restarted during execution; submit a new task.")
                self.event(run_id, "run.failed", {"reason": "service_restarted", "status": "interrupted"}, key="interrupted")
            return len(run_ids)

    def resume_run(self, run_id):
        with self._lock:
            if self._runs[run_id]["status"] != "waiting_for_input":
                raise ValueError("Run is not waiting for user input")
            return self.update_run(run_id, status="running")
