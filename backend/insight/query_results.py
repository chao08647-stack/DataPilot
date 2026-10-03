"""One bounded result contract for connectors, persistence and saved-result views."""
from __future__ import annotations

import json
from copy import deepcopy

MAX_RESULT_ROWS = 20_000
MAX_RESULT_BYTES = 10 * 1024 * 1024
PREVIEW_ROWS = 50


def json_size(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8"))


def bounded_rows(columns, rows, *, limit=MAX_RESULT_ROWS, byte_limit=MAX_RESULT_BYTES, normalize=None):
    """Consume only the bounded prefix; `rows` may be a streaming cursor iterator."""
    if type(limit) is not int or not 1 <= limit <= MAX_RESULT_ROWS:
        raise ValueError("Result limit must be between 1 and 20000")
    if type(byte_limit) is not int or not 1 <= byte_limit <= MAX_RESULT_BYTES:
        raise ValueError("Result byte limit must be between 1 and 10 MiB")
    size = json_size({"columns": columns, "rows": []})
    if size > byte_limit:
        raise ValueError("Result column metadata exceeds byte limit")
    result, reason = [], None
    for raw in rows:
        if len(result) >= limit:
            reason = "row_limit"
            break
        row = [normalize(v) for v in raw] if normalize else list(raw)
        if len(row) != len(columns):
            raise ValueError("Result row does not match its columns")
        addition = json_size(row) + bool(result)
        if size + addition > byte_limit:
            reason = "byte_limit"
            break
        result.append(row)
        size += addition
    return {"rows": result, "truncated": reason is not None, "truncation_reason": reason,
            "total_rows": len(result), "result_bytes": size}


def cursor_rows(cursor, *, batch_size=128):
    while batch := cursor.fetchmany(batch_size):
        yield from batch


def prepare_result(query):
    if not isinstance(query, dict) or not isinstance(query.get("id"), str) or not query["id"]:
        raise ValueError("Query result requires an ID")
    columns = query.get("columns")
    if not isinstance(columns, list) or any(not isinstance(c, str) for c in columns) or len(columns) != len(set(columns)):
        raise ValueError("Query result requires unique string column names")
    result = deepcopy(query)
    result.pop("result_ref", None)
    result.pop("preview_truncated", None)
    bounded = bounded_rows(columns, result.get("rows", []))
    bounded["truncated"] = bool(query.get("truncated") or bounded["truncated"])
    bounded["truncation_reason"] = query.get("truncation_reason") or bounded["truncation_reason"]
    if bounded["truncated"] and not bounded["truncation_reason"]:
        bounded["truncation_reason"] = "source_limit"
    result.update(bounded)
    return result


def preview_result(run_id, query):
    return {**deepcopy(query), "rows": deepcopy(query["rows"][:PREVIEW_ROWS]),
            "result_ref": {"run_id": run_id, "query_id": query["id"]},
            "preview_truncated": len(query["rows"]) > PREVIEW_ROWS, "full_result_available": True}


def page_result(query, *, offset=0, limit=50, sort_by=None, descending=False):
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= MAX_RESULT_ROWS:
        raise ValueError("Invalid saved-result pagination")
    if type(descending) is not bool:
        raise ValueError("descending must be a boolean")
    columns, rows = query["columns"], query["rows"]
    if sort_by is not None:
        if sort_by not in columns:
            raise ValueError("Sort column is not in the saved result")
        index = columns.index(sort_by)

        def sort_key(row):
            value = row[index]
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return (0, value)
            if isinstance(value, str):
                return (1, value)
            return (2, json.dumps(value, ensure_ascii=False, sort_keys=True))

        # NULLS LAST for both directions, stable within ties; never a SQL expression.
        populated = [r for r in rows if r[index] is not None]
        rows = sorted(populated, key=sort_key, reverse=descending) + [r for r in rows if r[index] is None]
    return {"columns": columns, "rows": rows[offset:offset + limit], "total": len(rows),
            "offset": offset, "limit": limit, "truncated": bool(query.get("truncated")),
            "result_bytes": query.get("result_bytes"), "truncation_reason": query.get("truncation_reason"),
            "legacy": bool(query.get("legacy")), "full_result_available": not query.get("legacy", False)}
