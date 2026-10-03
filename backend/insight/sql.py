"""Read-only, bounded DuckDB queries. SQL and chart numbers share one evidence source."""

import datetime as dt
import math
import re
import threading
import time
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import duckdb
import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import traverse_scope

from insight.query_results import MAX_RESULT_ROWS, bounded_rows, cursor_rows


class SQLRejected(ValueError):
    pass


def _set_operation_scopes(scope):
    # SQLGlot 30.19 renamed union_scopes to set_operation_scopes.
    branches = getattr(scope, "set_operation_scopes", None)
    if branches is None:
        branches = getattr(scope, "union_scopes", None)
    if not isinstance(branches, (list, tuple)) or len(branches) != 2:
        raise SQLRejected("SQL 集合运算作用域无法安全解析。")
    return branches


_SAFE_FUNCTIONS = {
    # SQLGlot normalizes vendor names (strftime -> TIME_TO_STR, date_trunc -> TIMESTAMP_TRUNC).
    "AND", "OR", "CASE", "IF", "CAST", "TRY_CAST", "COALESCE", "NULLIF", "EXISTS",
    "COUNT", "SUM", "AVG", "MIN", "MAX", "ROUND", "ABS", "CEIL", "FLOOR",
    "POWER", "SQRT", "MOD", "GREATEST", "LEAST", "SIGN", "LOG", "LN", "EXP",
    "ROW_NUMBER", "RANK", "DENSE_RANK", "PERCENT_RANK", "CUME_DIST", "NTILE",
    "LAG", "LEAD", "FIRST_VALUE", "LAST_VALUE", "NTH_VALUE",
    "YEAR", "MONTH", "DAY", "HOUR", "MINUTE", "SECOND", "EXTRACT",
    "DATE", "DATE_TRUNC", "TIMESTAMP_TRUNC", "TIME_TO_STR", "STR_TO_TIME",
    "DATE_ADD", "DATE_SUB", "DATE_DIFF", "TIMESTAMP_ADD", "TIMESTAMP_SUB",
    "TIMESTAMP_DIFF", "TS_OR_DS_TO_DATE", "LAST_DAY", "CURRENT_DATE", "CURRENT_TIMESTAMP",
    "LOWER", "UPPER", "LENGTH", "TRIM", "LTRIM", "RTRIM", "SUBSTRING",
    "CONCAT", "CONCAT_WS", "REPLACE", "LEFT", "RIGHT", "SPLIT_PART",
    "BOOL_AND", "BOOL_OR", "COUNT_IF", "MEDIAN", "QUANTILE_CONT", "PERCENTILE_CONT",
    "STDDEV", "STDDEV_POP", "STDDEV_SAMP", "VARIANCE", "VAR_POP", "VAR_SAMP",
    "APPROX_DISTINCT", "ARG_MAX", "ARG_MIN", "ARRAY_AGG", "GROUP_CONCAT",
}
_SAFE_ANONYMOUS = {
    "strftime", "strptime", "try_strptime", "date_trunc", "date_part", "date_diff", "datediff",
    "make_date", "last_day", "epoch", "year", "month", "day", "round", "approx_count_distinct",
    "quantile_cont", "median", "arg_max", "arg_min", "count_if", "bool_and", "bool_or",
}


def _conjuncts(expression):
    if isinstance(expression, exp.Paren):
        return _conjuncts(expression.this)
    if isinstance(expression, exp.And):
        return _conjuncts(expression.left) + _conjuncts(expression.right)
    return [expression]


def _validate_scopes(tree, scenario, approved, *, dialect="duckdb", schemas=None, allow_subqueries=False):
    """Only approved physical keys may justify a JOIN, through any CTE depth.

    Derived passthrough columns retain provenance; aggregate/calculated values do
    not become join keys just because a model gives them a plausible alias.
    Legacy entry points retain their v1 policy. Enterprise connectors explicitly
    allow bounded scalar aggregates, approved IN relations and correlated EXISTS.
    """
    table_records = {}
    for table in scenario["tables"]:
        name = table["name"]
        if table.get("schema") and "." not in name:
            name = f"{table['schema']}.{name}"
        if name.lower() in table_records:
            raise SQLRejected("表名存在不明确的大小写或命名冲突。")
        table_records[name.lower()] = (name, table)
    schema = {name: [c.lower() for c in record[1]["columns"]] for name, record in table_records.items()}
    allowed_schemas = {value.lower() for value in schemas or []}

    def canonical(name):
        key = name.lower()
        if key in schema:
            return key
        candidates = [n for n in schema if n.split(".")[-1] == key]
        return candidates[0] if len(candidates) == 1 else key

    approved = {canonical(name) for name in approved}
    relations = set()
    composite = {}
    temporal = []
    for relation in scenario["relations"]:
        if relation.get("type", relation.get("kind")) == "temporal":
            temporal.append(relation)
            continue
        edge = ((canonical(relation["left_table"]), relation["left_column"].lower()),
                (canonical(relation["right_table"]), relation["right_column"].lower()))
        relations.update((edge, edge[::-1]))
        extra = [((canonical(relation["left_table"]), key["left_column"].lower()),
                  (canonical(relation["right_table"]), key["right_column"].lower())) for key in relation.get("additional_keys", [])]
        composite[edge] = {edge, *extra}
        composite[edge[::-1]] = {edge[::-1], *(pair[::-1] for pair in extra)}
    outputs = {}
    physical_names = {}

    def source_columns(source):
        if isinstance(source, exp.Table):
            table_name = physical_names[id(source)]
            return {name: {(table_name, name)} for name in schema[table_name]}
        return outputs.get(id(source), {})

    def column_origins(column, selected):
        if not isinstance(column, exp.Column) or column.is_star:
            return set()
        if column.table:
            source = selected.get(column.table.lower())
            return source_columns(source).get(column.name.lower(), set()) if source is not None else set()
        matches = [source_columns(source)[column.name.lower()] for source in selected.values()
                   if column.name.lower() in source_columns(source)]
        return matches[0] if len(matches) == 1 else set()

    def pair_proofs(condition, selected):
        proofs = {}
        for equality in _conjuncts(condition):
            if isinstance(equality, exp.EQ) and isinstance(equality.left, exp.Column) and isinstance(equality.right, exp.Column):
                a, b = equality.left, equality.right
                if not a.table or not b.table or a.table.lower() == b.table.lower():
                    continue
                left, right = column_origins(a, selected), column_origins(b, selected)
                proof = proofs.setdefault(frozenset((a.table.lower(), b.table.lower())), set())
                proof.update((x, y) for x in left for y in right)
                proof.update((y, x) for x in left for y in right)
        return proofs

    def valid_temporal(condition, selected, alias_pair):
        def unwrap(value):
            while isinstance(value, exp.Paren):
                value = value.this
            return value

        clauses = _conjuncts(condition)
        proofs = pair_proofs(condition, selected).get(frozenset(alias_pair), set())
        for relation in temporal:
            left_table, right_table = canonical(relation["left_table"]), canonical(relation["right_table"])
            point = (left_table, relation["left_column"].lower())
            start = (right_table, relation["right_column"].lower())
            end = (right_table, relation.get("end_column", "").lower())
            if not end[1]:
                continue
            extra = {((left_table, key["left_column"].lower()), (right_table, key["right_column"].lower())) for key in relation.get("additional_keys", [])}
            if not extra.issubset(proofs):
                continue
            for point_alias, range_alias in (alias_pair, tuple(reversed(alias_pair))):
                def is_column(value, origin, alias):
                    return isinstance(value, exp.Column) and value.table.lower() == alias and column_origins(value, selected) == {origin}

                def compare(value, bound, operator, reverse):
                    value = unwrap(value)
                    return ((isinstance(value, operator) and is_column(value.left, point, point_alias) and is_column(value.right, bound, range_alias))
                            or (isinstance(value, reverse) and is_column(value.right, point, point_alias) and is_column(value.left, bound, range_alias)))

                def upper(value):
                    op, reverse = (exp.LTE, exp.GTE) if relation.get("end_inclusive", False) else (exp.LT, exp.GT)
                    if compare(value, end, op, reverse):
                        return True
                    value = unwrap(value)
                    if not relation.get("allow_open_end", True) or not isinstance(value, exp.Or):
                        return False
                    for comparison, null_test in ((value.left, value.right), (value.right, value.left)):
                        null_test = unwrap(null_test)
                        if (compare(comparison, end, op, reverse) and isinstance(null_test, exp.Is)
                                and is_column(null_test.this, end, range_alias) and isinstance(null_test.expression, exp.Null)):
                            return True
                    return False

                if any(compare(c, start, exp.GTE, exp.LTE) for c in clauses) and any(upper(c) for c in clauses):
                    return True
        return False

    def approved_pair(condition, selected, a_alias, b_alias):
        proof = pair_proofs(condition, selected).get(frozenset((a_alias, b_alias)), set())
        for equality in _conjuncts(condition):
            if not isinstance(equality, exp.EQ) or not isinstance(equality.left, exp.Column) or not isinstance(equality.right, exp.Column):
                continue
            a, b = equality.left, equality.right
            if frozenset((a.table.lower(), b.table.lower())) != frozenset((a_alias, b_alias)):
                continue
            left, right = column_origins(a, selected), column_origins(b, selected)
            if left and right and all((x, y) in relations and composite[(x, y)].issubset(proof) for x in left for y in right):
                return True
        return valid_temporal(condition, selected, (a_alias, b_alias))

    try:
        scopes = list(traverse_scope(tree))
        # Resolve physical identifiers before checking expressions in child scopes.
        # Explicit qualification eliminates reliance on writable search_path schemas.
        for scope in scopes:
            for _, source in scope.selected_sources.values():
                if not isinstance(source, exp.Table):
                    continue
                if not isinstance(source.this, exp.Identifier) or source.catalog:
                    raise SQLRejected("查询引用了未批准的表、外部资源或表函数。")
                if source.db and (not schemas or source.db.lower() not in allowed_schemas):
                    raise SQLRejected("查询引用了未批准的 schema。")
                if source.db.lower() in {"pg_catalog", "information_schema", "mysql", "sys", "performance_schema"} or source.name.lower().startswith(("pg_", "sqlite_", "duckdb_")):
                    raise SQLRejected("禁止查询系统表。")
                supplied = f"{source.db}.{source.name}" if source.db else source.name
                candidates = [key for key in schema if ((key == supplied.lower()
                    or ("." not in key and key == source.name.lower() and source.db.lower() == (schemas or [""])[0].lower()))
                    if source.db else key.split(".")[-1] == source.name.lower())]
                if len(candidates) != 1 or candidates[0] not in approved:
                    raise SQLRejected("查询引用了未批准或不明确的表。")
                key = candidates[0]
                physical_names[id(source)] = key
                actual = table_records[key][0].split(".")
                if len(actual) > 2:
                    raise SQLRejected("不允许跨数据库查询。")
                target_schema = actual[0] if len(actual) == 2 else (next(iter(schemas)) if schemas else "")
                if target_schema and target_schema.lower() not in allowed_schemas:
                    raise SQLRejected("发布模型超出数据源的 schema 范围。")
                if dialect != "duckdb" or source.db:
                    if target_schema:
                        source.set("db", exp.to_identifier(target_schema, quoted=True))
                    source.set("this", exp.to_identifier(actual[-1], quoted=True))
        for scope in scopes:
            if scope.scope_type.name == "SUBQUERY" and not allow_subqueries:
                raise SQLRejected("首版不允许标量或谓词子查询；请改用批准的显式关联或分开查询。")
            selected = {alias.lower(): source for alias, (_, source) in scope.selected_sources.items()}
            for source in selected.values():
                if isinstance(source, exp.Table):
                    if id(source) not in physical_names:
                        raise SQLRejected("查询引用了未批准的表、外部资源或表函数。")
                elif not hasattr(source, "scope_type"):
                    raise SQLRejected("不支持此数据来源。")
            for table in scope.tables:
                if not isinstance(table.this, exp.Identifier):
                    raise SQLRejected("禁止表函数、文件读取及外部访问。")
            if scope.is_udtf:
                raise SQLRejected("禁止表函数、文件读取及外部访问。")

            from_clause = scope.expression.args.get("from_")
            prior = {from_clause.this.alias_or_name.lower()} if from_clause is not None else set()
            for join in scope.expression.args.get("joins", []):
                condition = join.args.get("on")
                if condition is None or join.args.get("method") == "NATURAL" or join.args.get("kind") == "CROSS":
                    raise SQLRejected("关联必须使用显式 ON 条件，禁止隐式/CROSS/NATURAL JOIN。")
                target = join.this.alias_or_name.lower()
                valid = any(approved_pair(condition, selected, target, previous) for previous in prior)
                if not valid:
                    raise SQLRejected("多表关联不在批准的关联路径内：" + condition.sql(dialect=dialect)[:250]
                                      + "。关联关系可双向使用；CTE必须透传正式原始键，计算日期/同名月份不是关联键；复合关系需全部键。请先按正式外键聚合再关联，或拆成独立查询。")
                prior.add(target)

            projected = {}
            if isinstance(scope.expression, exp.SetOperation):
                branches = [outputs.get(id(branch), {}) for branch in _set_operation_scopes(scope)]
                if branches:
                    for index, name in enumerate(branches[0]):
                        positions = [list(branch.values()) for branch in branches]
                        projected[name] = (set().union(*(p[index] for p in positions))
                                           if all(len(p) > index and p[index] for p in positions) else set())
            else:
                for projection in scope.expression.selects:
                    if projection.is_star:
                        if isinstance(projection, exp.Column) and projection.table:
                            source = selected.get(projection.table.lower())
                            projected.update(source_columns(source) if source is not None else {})
                        else:
                            for source in selected.values():
                                for key, origins in source_columns(source).items():
                                    # Ambiguous star output cannot serve as an approved key.
                                    projected[key] = set() if key in projected else origins
                    else:
                        expression = projection.this if isinstance(projection, exp.Alias) else projection
                        projected[projection.alias_or_name.lower()] = column_origins(expression, selected)
            if scope.outer_columns:
                projected = {name.lower(): origins for name, origins in zip(scope.outer_columns, projected.values())}
            outputs[id(scope)] = projected

        if allow_subqueries:
            for scope in scopes:
                # Correlation can be hidden in a derived table or CTE nested
                # inside EXISTS. Check every scope, not just direct SUBQUERY.
                selected = {alias.lower(): source for alias, (_, source) in scope.selected_sources.items()}
                outer, parent = {}, scope.parent
                while parent is not None:
                    for alias, (_, source) in parent.selected_sources.items():
                        if alias.lower() not in selected:
                            outer.setdefault(alias.lower(), source)
                    parent = parent.parent
                combined = {**outer, **selected}
                correlated = set()
                for column in scope.columns:
                    if column.find_ancestor(exp.Select) is not scope.expression:
                        continue
                    if column.table and column.table.lower() not in selected:
                        if column.table.lower() not in outer:
                            raise SQLRejected("子查询包含未知外部表别名。")
                        correlated.add(column.table.lower())
                    elif not column.table and not column_origins(column, selected) and column_origins(column, outer):
                        raise SQLRejected("相关子查询必须显式标注外部列的表别名。")
                where = scope.expression.args.get("where")
                if correlated and (where is None or not all(any(approved_pair(where.this, combined, local, remote) for local in selected) for remote in correlated)):
                    raise SQLRejected("相关子查询必须包含完整批准的关联条件。")
                if scope.scope_type.name != "SUBQUERY":
                    continue
                owner = scope.expression.parent
                if isinstance(owner, exp.Subquery):
                    owner = owner.parent
                if isinstance(owner, exp.In):
                    origins = list(outputs[id(scope)].values())
                    outer_origins = column_origins(owner.this, outer)
                    if (len(origins) != 1 or not origins[0] or not outer_origins
                            or not all(a == b or ((a, b) in relations and len(composite[(a, b)]) == 1) for a in outer_origins for b in origins[0])):
                        raise SQLRejected("IN 子查询必须匹配批准的单字段关系；复合关系请使用 EXISTS。")
                elif not isinstance(owner, exp.Exists):
                    projections = scope.expression.selects
                    limit = scope.expression.args.get("limit")
                    bounded = limit is not None and isinstance(limit.expression, exp.Literal) and limit.expression.this == "1"
                    aggregate = len(projections) == 1 and any(projections[0].find_all(exp.AggFunc)) and not scope.expression.args.get("group") and not list(projections[0].find_all(exp.Window))
                    if len(projections) != 1 or not (bounded or aggregate):
                        raise SQLRejected("标量子查询需要单个聚合结果或明确 LIMIT 1。")
    except sqlglot.errors.OptimizeError:
        raise SQLRejected("表别名重复或查询作用域无法安全解析。") from None


def validate_sql(sql: str, scenario: dict, allowed_tables: list[str], *, dialect="duckdb", schemas=None, allow_subqueries=False) -> str:
    dialect = "postgres" if dialect in {"postgresql", "postgres"} else dialect
    if dialect not in {"duckdb", "postgres", "mysql"}:
        raise SQLRejected("不支持此 SQL 方言。")
    if not isinstance(sql, str) or not sql.strip() or len(sql) > 30000:
        raise SQLRejected("SQL 长度超限。")
    try:
        trees = sqlglot.parse(sql, read=dialect)
    except sqlglot.errors.ParseError:
        raise SQLRejected("SQL 语法解析失败。") from None
    if len(trees) != 1 or not isinstance(trees[0], exp.Query):
        raise SQLRejected("只允许一条只读 SELECT 查询。")
    tree = trees[0]
    if any(isinstance(n, (exp.Insert, exp.Update, exp.Delete, exp.Create, exp.Drop, exp.Command, exp.Into, exp.Lock)) for n in tree.walk()):
        raise SQLRejected("禁止写入、管理命令和 SELECT INTO。")
    if any(with_clause.args.get("recursive") for with_clause in tree.find_all(exp.With)):
        raise SQLRejected("不允许递归 CTE。")
    approved = {name.lower() for name in allowed_tables}
    if any(isinstance(node, exp.Dot) and isinstance(node.expression, exp.Func) for node in tree.walk()):
        raise SQLRejected("禁止 schema 限定的自定义函数。")
    if any(isinstance(node, (exp.PropertyEQ, exp.SessionParameter)) for node in tree.walk()):
        raise SQLRejected("禁止会话变量或赋值。")
    if any(isinstance(node, exp.Parameter) for node in tree.walk()):
        raise SQLRejected("请使用 ? 或命名占位符绑定参数，禁止会话变量。")
    if any(isinstance(node, exp.DataType) and node.this.name == "USERDEFINED" for node in tree.walk()):
        raise SQLRejected("禁止转换为自定义类型。")
    for function in tree.find_all(exp.Func):
        if isinstance(function, exp.Anonymous):
            valid = function.name.lower() in _SAFE_ANONYMOUS
        else:
            valid = function.sql_name() in _SAFE_FUNCTIONS
        if not valid:
            name = function.name if isinstance(function, exp.Anonymous) else function.sql_name()
            raise SQLRejected(f"函数不在允许清单中：{name[:60]}")
    if any(isinstance(n, exp.Star) and (n.args.get("replace") or n.args.get("rename")) for n in tree.walk()):
        raise SQLRejected("不支持 SELECT * REPLACE/RENAME。")
    _validate_scopes(tree, scenario, approved, dialect=dialect, schemas=schemas, allow_subqueries=allow_subqueries)
    for select in tree.find_all(exp.Select):
        select.set("hint", None)
    # MySQL executable comments must never survive normalization.
    return tree.sql(dialect=dialect, comments=False)


def bind_parameters(sql, dialect, parameters):
    """Render placeholders for a driver without interpolating values or literals.

    The portable API uses ? positional parameters. Named placeholders follow the
    parser's dialect ($name for DuckDB, :name/pyformat for PostgreSQL, :name for
    MySQL). MySQL callers must not supply raw DB-API %s placeholders to the parser.
    """
    tree = sqlglot.parse_one(sql, read=dialect)
    placeholders = list(tree.find_all(exp.Placeholder))
    if not placeholders:
        if parameters:
            raise ValueError("Parameters supplied but SQL has no placeholders")
        return sql, None
    if parameters is None:
        raise ValueError("SQL placeholders require bound parameters")
    named = isinstance(parameters, dict)
    if not named and (not isinstance(parameters, (tuple, list))):
        raise ValueError("Parameters must be a dict, list or tuple")
    if named and any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(key)) for key in parameters):
        raise ValueError("Invalid parameter name")
    marker_prefix = f"__insight_bind_{uuid4().hex}_"
    markers = {}
    for i, placeholder in enumerate(placeholders):
        name = str(placeholder.this.name if isinstance(placeholder.this, exp.Identifier) else placeholder.this or "")
        if bool(name) != named:
            raise ValueError("Do not mix named and positional parameters")
        if named and name not in parameters:
            raise ValueError("Missing named parameter")
        marker = f"{marker_prefix}{i}__"
        markers[marker] = name
        placeholder.replace(exp.Var(this=marker))
    rendered = tree.sql(dialect=dialect, comments=False)
    occurrence = re.findall(re.escape(marker_prefix) + r"\d+__", rendered)
    if not named and len(occurrence) != len(parameters):
        raise ValueError("SQL parameter count mismatch")
    values = tuple(parameters[name] for marker in occurrence if (name := markers[marker])) if named else tuple(parameters)
    if dialect != "duckdb":
        rendered = rendered.replace("%", "%%")
    for marker in occurrence:
        rendered = rendered.replace(marker, "?" if dialect == "duckdb" else "%s")
    return rendered, values


def error_category(message: str) -> str:
    text = message.lower()
    if "timeout" in text or "interrupt" in text or "超时" in text:
        return "timeout"
    if any(word in text for word in ("禁止", "未批准", "批准的关联", "只读", "不允许", "外部资源")):
        return "policy"
    if "group by" in text or "aggregate" in text:
        return "aggregation"
    if "function" in text or "syntax" in text or "parser" in text or "语法" in text or "函数" in text:
        return "dialect"
    if "conversion" in text or "cast" in text or "类型" in text:
        return "type"
    if "column" in text or "table" in text or "binder" in text:
        return "schema"
    return "execution"


def json_value(value):
    if isinstance(value, (dt.date, dt.datetime, dt.time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return json_value(float(value))
    if isinstance(value, (dt.timedelta, UUID)):
        return str(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (list, tuple)):
        return [json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): json_value(item) for key, item in value.items()}
    if isinstance(value, bytes):
        return value.hex()
    return value


def execute_query(path: Path, sql: str, scenario: dict, allowed_tables: list[str], query_id="q1", *, timeout=10, limit=MAX_RESULT_ROWS,
                  parameters=None, schemas=None, allow_subqueries=False, cancel_event=None, register_cancel=None) -> dict:
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_RESULT_ROWS:
        raise ValueError("Result limit must be between 1 and 20000")
    if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0 or timeout > 60:
        raise ValueError("Timeout must be positive and at most 60 seconds")
    validated = validate_sql(sql, scenario, allowed_tables, schemas=schemas, allow_subqueries=allow_subqueries)
    bound_sql, bound_parameters = bind_parameters(validated, "duckdb", parameters)
    started = time.perf_counter()
    connection = duckdb.connect(str(path), read_only=True, config={"enable_external_access": False, "threads": 2, "memory_limit": "256MB"})
    finished, expired = threading.Event(), threading.Event()
    if register_cancel is not None:
        register_cancel(connection.interrupt)

    def interrupt_until_finished():
        expired.set()
        # A single interrupt between DESCRIBE and SELECT could otherwise be lost.
        while not finished.is_set():
            connection.interrupt()
            finished.wait(0.01)

    def wait_and_interrupt():
        deadline = time.monotonic() + timeout
        while not finished.wait(0.01):
            if (cancel_event is not None and cancel_event.is_set()) or time.monotonic() >= deadline:
                interrupt_until_finished()
                return

    timer = threading.Thread(target=wait_and_interrupt, daemon=True)
    try:
        timer.start()
        if cancel_event is not None and cancel_event.is_set():
            raise TimeoutError("SQL execution cancelled")
        columns = connection.execute(f"DESCRIBE {bound_sql}", bound_parameters).fetchall()
        names = [item[0] for item in columns]
        if len(names) != len({name.lower() for name in names}):
            raise SQLRejected("结果列名重复，请为每个字段设置唯一别名。")
        if expired.is_set():
            raise TimeoutError("SQL execution timeout")
        result = connection.execute(f'SELECT * FROM ({bound_sql}) AS "__insight_preview" LIMIT {limit + 1}', bound_parameters)
        table = bounded_rows(names, cursor_rows(result), limit=limit, normalize=json_value)
        if expired.is_set():
            raise TimeoutError("SQL execution timeout")
        return {"id": query_id, "sql": validated, "columns": names,
                **table,
                "elapsed_ms": round((time.perf_counter() - started) * 1000),
                "queried_at": dt.datetime.now(dt.UTC).isoformat()}
    finally:
        finished.set()
        timer.join()  # Do not race a callback calling interrupt() with close().
        connection.close()


def evidence_statistics(queries: list[dict]) -> list[dict]:
    """Never ask the model to calculate percentages or infer an implicit time ordering."""
    profiles = []
    for q in queries:
        stats = {"query_id": q["id"], "row_count": len(q["rows"]), "truncated": q["truncated"], "columns": {}}
        if q.get("truncated"):
            stats["limitation"] = "结果已截断；不计算总体汇总、趋势或比例。"
            profiles.append(stats)
            continue
        for i, name in enumerate(q["columns"]):
            values = [r[i] for r in q["rows"]]
            numbers = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
            if numbers:
                column = {"min": min(numbers), "max": max(numbers), "sum": round(sum(numbers), 6)}
                column["top_rows"] = [{"row_index": index, "value": value} for index, value in
                                      sorted(((i, v) for i, v in enumerate(values) if isinstance(v, (int, float))), key=lambda item: item[1], reverse=True)[:5]]
                if len(values) > 1 and len(values) == len(numbers):
                    column.update(first=numbers[0], last=numbers[-1], delta=round(numbers[-1] - numbers[0], 6),
                                  change_pct=round((numbers[-1] - numbers[0]) / abs(numbers[0]) * 100, 4) if numbers[0] else None,
                                  order_note="first/last 是查询行顺序，只有按时间升序时才能解释为趋势")
                    column["adjacent_changes"] = [{"from_row": i-1, "to_row": i, "delta": round(numbers[i]-numbers[i-1], 6),
                        "change_pct": round((numbers[i]-numbers[i-1])/abs(numbers[i-1])*100, 4) if numbers[i-1] else None}
                        for i in range(1, min(len(numbers), 80))]
                stats["columns"][name] = column
        profiles.append(stats)
    return profiles


# Backwards-compatible exports; rendering policy is owned by the chart module.
from insight.charts import apply_chart_units, validate_charts  # noqa: E402,F401
