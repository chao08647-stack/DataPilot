"""Canonical LangGraph Store records plus a rebuildable hybrid search projection.

The Store is intentionally NOT used as the SQL hybrid search engine. PostgreSQL
FTS / pgvector return IDs; every hit is rehydrated and scoped against the Store.
"""
from __future__ import annotations

import asyncio
import json
import math
import re
from copy import deepcopy
from datetime import UTC, datetime
from hashlib import sha256

import jieba
from psycopg.types.json import Jsonb

KINDS = ("business", "sql_experience", "preference")
VECTOR_DIM = 1024


def _expired(record):
    value = record.get("metadata", {}).get("expires_at")
    if value is None:
        return False
    try:
        expires = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        return expires <= datetime.now(UTC)
    except (ValueError, TypeError, AttributeError):
        return True  # Malformed expiry is not safe to treat as unbounded memory.


def _tokens(text):
    return [word.lower() for word in jieba.lcut(text) if re.fullmatch(r"[\w\u4e00-\u9fff]+", word) and word.strip()]


def _validate_vector(vector):
    if len(vector) != VECTOR_DIM or not all(isinstance(x, (int, float)) and math.isfinite(x) for x in vector) or not any(vector):
        raise ValueError("Embedding must be a finite, nonzero 1024-dimensional vector")
    return [float(x) for x in vector]


def weighted_rrf(lexical_ids, dense_ids, limit=3):
    """Rank fusion avoids comparing incomparable lexical/cosine score scales."""
    scores = {}
    for ids, weight in ((lexical_ids, 0.45), (dense_ids, 0.55)):
        for rank, document_id in enumerate(dict.fromkeys(ids), start=1):
            scores[document_id] = scores.get(document_id, 0.0) + weight / (60 + rank)
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:limit]


class MemoryService:
    def __init__(self, store, repository, embedder=None):
        self.store = store
        self.repository = repository
        self.embedder = embedder
        self._offline = bool(getattr(repository, "is_memory", False))
        self._documents = {}  # Projection ONLY for the explicitly offline test adapter.
        self.embedding_failures = 0

    @staticmethod
    def _namespace(scenario_id, kind):
        return ("insight_agents", scenario_id, kind)

    async def _get(self, scenario_id, memory_id, kind=None):
        for candidate in (kind,) if kind else KINDS:
            item = await self.store.aget(self._namespace(scenario_id, candidate), memory_id)
            if item is not None:
                return deepcopy(item.value)
        return None

    async def _put(self, record):
        await self.store.aput(self._namespace(record["scenario_id"], record["kind"]), record["id"], deepcopy(record), index=False)

    async def list(self, scenario_id, kind=None):
        if kind is not None and kind not in KINDS:
            raise ValueError("Unknown memory kind")
        prefix = ("insight_agents", scenario_id) + ((kind,) if kind else ())
        records, offset = [], 0
        while True:
            page = await self.store.asearch(prefix, limit=100, offset=offset)
            for item in page:
                value = deepcopy(item.value)
                if value.get("scenario_id") == scenario_id and not value.get("metadata", {}).get("deleted"):
                    records.append(value)
            if len(page) < 100:
                break
            offset += len(page)
        return sorted(records, key=lambda value: (value["kind"], value["id"]))

    async def seed(self, scenario):
        from insight.scenarios import validate_repair_seed

        count = 0
        for kind, source in (("business", "business_knowledge"), ("sql_experience", "repair_experiences"), ("preference", "preferences")):
            for seed in scenario.get(source, []):
                memory_id = f"{kind}:{seed['id']}"
                existing = await self._get(scenario["id"], memory_id, kind)
                prior = existing
                if existing and str(existing.get("metadata", {}).get("schema_version")) != str(scenario["schema_version"]):
                    if existing.get("metadata", {}).get("deleted"):
                        continue  # A new schema must not revive a user's deletion.
                    memory_id += f":v:{scenario['schema_version']}"
                    existing = await self._get(scenario["id"], memory_id, kind)
                if existing is not None:
                    # Never overwrite edits, re-enable disabled records, or revive tombstones.
                    if not existing.get("metadata", {}).get("deleted"):
                        await self._project(existing)
                    continue
                if kind == "sql_experience" and not validate_repair_seed(seed):
                    raise ValueError(f"Repair fixture did not verify: {scenario['id']}/{seed['id']}")
                metadata = {"schema_version": str(scenario["schema_version"]), "dialect": scenario.get("dialect", "duckdb"), "deleted": False}
                if kind == "sql_experience":
                    metadata.update(error_category=seed["error_category"], tables=seed["tables"], verified=True)
                elif kind == "preference":
                    metadata.update(key=seed["key"], value=seed["value"])
                else:
                    metadata["tags"] = seed.get("tags", [])
                record = {
                    "id": memory_id, "scenario_id": scenario["id"], "kind": kind,
                    "title": seed["title"], "content": seed.get("content", seed.get("description", "")),
                    "metadata": metadata, "active": kind != "preference" and (prior is None or prior.get("active", False)), "origin": "builtin",
                }
                # fixture_sql, broken_sql, fixed_sql and expected NEVER enter the record.
                await self._put(record)
                await self._project(record)
                count += 1
        return {"seeded": count, "embedding_failures": self.embedding_failures}

    async def preferences(self, scenario_id, schema_version=None):
        return {record["metadata"]["key"]: record["metadata"]["value"] for record in await self.list(scenario_id, "preference")
                if record["active"] and not _expired(record) and
                (schema_version is None or str(record["metadata"].get("schema_version")) == str(schema_version))}

    async def update(self, scenario_id, memory_id, *, active=None, value=None, content=None):
        record = await self._get(scenario_id, memory_id)
        if record is None or record["metadata"].get("deleted"):
            raise KeyError(memory_id)
        if active is not None:
            record["active"] = bool(active)
        if value is not None:
            if record["kind"] != "preference":
                raise ValueError("Structured values only apply to preferences")
            if type(value) is not type(record["metadata"].get("value")):
                raise ValueError("修改偏好时必须保持原有值类型（文字、布尔值、数字或列表）。")
            record["metadata"]["value"] = value
        if content is not None:
            record["content"] = str(content)
            if record["kind"] == "sql_experience":
                # Changing a verified strategy invalidates its previous fixture assurance.
                record["metadata"]["verified"] = False
        await self._put(record)
        await self._project(record)
        return record

    async def delete(self, scenario_id, memory_id):
        record = await self._get(scenario_id, memory_id)
        if record is None:
            raise KeyError(memory_id)
        # Stable tombstone prevents the next initialization from reintroducing a seed.
        record.update(active=False, content="", title="Deleted memory")
        record["metadata"] = {"deleted": True, "schema_version": record["metadata"].get("schema_version")}
        await self._put(record)
        await self._project(record)

    async def remember_repair(self, scenario_id, schema_version, run_id, content, error_category, tables, dialect="duckdb"):
        memory_id = f"sql_experience:run:{run_id}"
        existing = await self._get(scenario_id, memory_id, "sql_experience")
        if existing is not None:
            if not existing["metadata"].get("deleted"):
                await self._project(existing)
            return existing
        record = {
            "id": memory_id, "scenario_id": scenario_id, "kind": "sql_experience",
            "title": "Verified SQL repair", "content": content, "active": True, "origin": "runtime",
            "metadata": {"schema_version": str(schema_version), "dialect": dialect, "error_category": error_category,
                         "tables": sorted(set(tables)), "verified": True, "source_run_id": run_id, "deleted": False},
        }
        # Caller owns success verification; this method is idempotent per business run.
        await self._put(record)
        await self._project(record)
        return record

    @staticmethod
    def _eligible(record, scenario_id, schema_version, kind, error_category, tables, dialect="duckdb"):
        metadata = record.get("metadata", {})
        if record.get("scenario_id") != scenario_id or record.get("kind") != kind or not record.get("active") or metadata.get("deleted") or _expired(record):
            return False
        if str(metadata.get("schema_version")) != str(schema_version) or metadata.get("dialect") != dialect:
            return False
        if kind == "sql_experience" and not metadata.get("verified"):
            return False
        if error_category is not None and metadata.get("error_category") != error_category:
            return False
        if tables is not None and not set(metadata.get("tables", [])).issubset(set(tables)):
            return False
        return True

    async def _embed(self, text):
        if self.embedder is None:
            return None
        vectors = await self.embedder.embed([text])
        if len(vectors) != 1:
            raise ValueError("Embedding count mismatch")
        return _validate_vector(vectors[0])

    def _get_projection(self, scenario_id, document_id):
        if self._offline:
            return deepcopy(self._documents.get((scenario_id, document_id)))
        with self.repository.connect() as conn:
            return conn.execute("SELECT content_hash,embedding::text AS embedding,embedding_model FROM ia_retrieval WHERE scenario_id=%s AND document_id=%s", (scenario_id, document_id)).fetchone()

    def _delete_projection(self, scenario_id, document_id):
        if self._offline:
            self._documents.pop((scenario_id, document_id), None)
        else:
            with self.repository.connect() as conn:
                conn.execute("DELETE FROM ia_retrieval WHERE scenario_id=%s AND document_id=%s", (scenario_id, document_id))

    async def _project(self, record):
        metadata = record["metadata"]
        if record["kind"] == "preference" or not record["active"] or metadata.get("deleted") or (record["kind"] == "sql_experience" and not metadata.get("verified")):
            await asyncio.to_thread(self._delete_projection, record["scenario_id"], record["id"])
            return
        text = " ".join([record["title"], record["content"], metadata.get("error_category", ""), *metadata.get("tables", []), *metadata.get("tags", [])])
        digest = sha256((text + json.dumps(metadata, sort_keys=True, ensure_ascii=False)).encode()).hexdigest()
        model = str(getattr(self.embedder, "model", "bge-m3")) if self.embedder else None
        old = await asyncio.to_thread(self._get_projection, record["scenario_id"], record["id"])
        embedding = None
        if old and old["content_hash"] == digest and old.get("embedding") is not None and (model is None or old.get("embedding_model") == model):
            embedding = json.loads(old["embedding"]) if isinstance(old["embedding"], str) else old["embedding"]
            model = old.get("embedding_model")
        elif self.embedder:
            try:
                embedding = await self._embed(text)
            except Exception:
                # Model credentials/endpoint errors must not leak via logs or public payloads.
                self.embedding_failures += 1
        document = {
            "document_id": record["id"], "scenario_id": record["scenario_id"], "kind": record["kind"],
            "title": record["title"], "content": record["content"], "tokenized": " ".join(_tokens(text)),
            "metadata_json": deepcopy(metadata), "content_hash": digest, "embedding": embedding,
            "embedding_model": model if embedding is not None else None,
        }
        await asyncio.to_thread(self._upsert_projection, document)

    def _upsert_projection(self, document):
        if self._offline:
            self._documents[(document["scenario_id"], document["document_id"])] = deepcopy(document)
            return
        with self.repository.connect() as conn:
            conn.execute("""
                INSERT INTO ia_retrieval(document_id,scenario_id,kind,title,content,tokenized,metadata_json,content_hash,embedding,embedding_model)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::vector,%s)
                ON CONFLICT(scenario_id,document_id) DO UPDATE SET kind=EXCLUDED.kind,title=EXCLUDED.title,
                    content=EXCLUDED.content,tokenized=EXCLUDED.tokenized,metadata_json=EXCLUDED.metadata_json,
                    content_hash=EXCLUDED.content_hash,embedding=EXCLUDED.embedding,
                    embedding_model=EXCLUDED.embedding_model,updated_at=now()
            """, (document["document_id"], document["scenario_id"], document["kind"], document["title"], document["content"], document["tokenized"], Jsonb(document["metadata_json"]), document["content_hash"], json.dumps(document["embedding"]) if document["embedding"] is not None else None, document["embedding_model"]))

    def _candidate_ids(self, scenario_id, schema_version, query, kind, error_category, tables, vector, limit, dialect="duckdb"):
        terms = list(dict.fromkeys(_tokens(query)))[:32]
        if self._offline:
            eligible = []
            for (sid, _), doc in self._documents.items():
                record = {"scenario_id": sid, "kind": doc["kind"], "active": True, "metadata": doc["metadata_json"]}
                if self._eligible(record, scenario_id, schema_version, kind, error_category, tables, dialect):
                    eligible.append(doc)
            lexical = [(doc["document_id"], len(set(terms) & set(doc["tokenized"].split()))) for doc in eligible]
            lexical_ids = [key for key, score in sorted(lexical, key=lambda row: (-row[1], row[0])) if score > 0][:limit]
            dense = []
            if vector is not None:
                model = str(getattr(self.embedder, "model", "bge-m3"))
                query_norm = math.sqrt(sum(x * x for x in vector))
                for doc in eligible:
                    other = doc["embedding"]
                    if other is not None and doc.get("embedding_model") == model:
                        score = sum(a * b for a, b in zip(vector, other)) / (query_norm * math.sqrt(sum(x * x for x in other)))
                        dense.append((doc["document_id"], score))
            return lexical_ids, [key for key, _ in sorted(dense, key=lambda row: (-row[1], row[0]))[:limit]]
        filters = """scenario_id=%s AND kind=%s AND metadata_json->>'schema_version'=%s
            AND metadata_json->>'dialect'=%s
            AND (%s::text IS NULL OR metadata_json->>'error_category'=%s)
            AND (%s::text[] IS NULL OR COALESCE(metadata_json->'tables','[]'::jsonb) <@ to_jsonb(%s::text[]))"""
        args = (scenario_id, kind, str(schema_version), dialect, error_category, error_category, tables, tables)
        with self.repository.connect() as conn:
            lexical_ids = []
            if terms:
                rows = conn.execute(f"""SELECT document_id FROM ia_retrieval,
                    websearch_to_tsquery('simple', %s) AS q WHERE {filters} AND search_vector @@ q
                    ORDER BY ts_rank_cd(search_vector,q) DESC,document_id LIMIT %s""", (" OR ".join(terms), *args, limit)).fetchall()
                lexical_ids = [row["document_id"] for row in rows]
            dense_ids = []
            if vector is not None:
                model = str(getattr(self.embedder, "model", "bge-m3"))
                rows = conn.execute(f"""SELECT document_id FROM ia_retrieval WHERE {filters} AND embedding IS NOT NULL AND embedding_model=%s
                    ORDER BY embedding <=> %s::vector LIMIT %s""", (*args, model, json.dumps(vector), limit)).fetchall()
                dense_ids = [row["document_id"] for row in rows]
        return lexical_ids, dense_ids

    async def search(self, scenario_id, schema_version, query, kind="business", error_category=None, tables=None, limit=3, dialect="duckdb"):
        if kind not in {"business", "sql_experience"}:
            raise ValueError("Preferences are read directly, not retrieved")
        if limit <= 0:
            return {"items": [], "mode": "offline_lexical" if self._offline else "lexical"}
        vector, mode = None, "lexical"
        if self.embedder is not None:
            try:
                vector = await self._embed(query)
                mode = "hybrid"
            except Exception:
                mode = "lexical_fallback"
                self.embedding_failures += 1
        lexical_ids, dense_ids = await asyncio.to_thread(self._candidate_ids, scenario_id, schema_version, query, kind, error_category, tables, vector, max(30, limit * 5), dialect)
        if mode == "hybrid" and not dense_ids:
            mode = "lexical_no_indexed_vectors"
        fused = weighted_rrf(lexical_ids, dense_ids, limit=max(30, limit * 5))
        items = []
        for memory_id, score in fused:
            record = await self._get(scenario_id, memory_id, kind)
            if record and self._eligible(record, scenario_id, schema_version, kind, error_category, tables, dialect):
                items.append({**record, "score": score})
            if len(items) == limit:
                break
        return {"items": items, "mode": f"offline_{mode}" if self._offline else mode}
