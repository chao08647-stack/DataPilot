"""Set-operation scope API compatibility and join-provenance regression tests."""

from types import SimpleNamespace

import pytest

from insight.sql import SQLRejected, _set_operation_scopes, validate_sql


@pytest.mark.parametrize("attribute", ["set_operation_scopes", "union_scopes"])
@pytest.mark.parametrize("container", [list, tuple])
def test_set_operation_scopes_accepts_new_and_legacy_api(attribute, container):
    branches = container([object(), object()])
    scope = SimpleNamespace(**{attribute: branches})

    assert _set_operation_scopes(scope) == branches


def test_set_operation_scopes_falls_back_when_new_api_is_none():
    branches = [object(), object()]
    scope = SimpleNamespace(set_operation_scopes=None, union_scopes=branches)

    assert _set_operation_scopes(scope) == branches


def test_set_operation_scopes_does_not_eagerly_access_legacy_api():
    branches = [object(), object()]

    class NewScope:
        set_operation_scopes = branches

        @property
        def union_scopes(self):
            raise AssertionError("The legacy API must not be read when the new API exists")

    assert _set_operation_scopes(NewScope()) == branches


@pytest.mark.parametrize("attributes", [{}, {"set_operation_scopes": None, "union_scopes": None}])
def test_set_operation_scopes_rejects_missing_branches(attributes):
    with pytest.raises(SQLRejected):
        _set_operation_scopes(SimpleNamespace(**attributes))


@pytest.mark.parametrize("attribute", ["set_operation_scopes", "union_scopes"])
@pytest.mark.parametrize(
    "branches",
    [[], [object()], [object(), object(), object()], "ab", {"left": 1, "right": 2}, {1, 2}, 0, False],
    ids=["empty", "one-branch", "three-branches", "string", "mapping", "set", "integer", "boolean"],
)
def test_set_operation_scopes_rejects_invalid_branch_shape(attribute, branches):
    with pytest.raises(SQLRejected):
        _set_operation_scopes(SimpleNamespace(**{attribute: branches}))


def test_set_operation_scopes_does_not_replace_empty_new_api_with_legacy_branches():
    scope = SimpleNamespace(set_operation_scopes=[], union_scopes=[object(), object()])

    with pytest.raises(SQLRejected):
        _set_operation_scopes(scope)


@pytest.fixture
def relation_metadata():
    return {
        "tables": [
            {"name": "orders", "columns": {"order_id": "INTEGER", "amount": "DOUBLE"}},
            {"name": "refunds", "columns": {"order_id": "INTEGER", "amount": "DOUBLE"}},
        ],
        "relations": [
            {
                "left_table": "orders",
                "left_column": "order_id",
                "right_table": "refunds",
                "right_column": "order_id",
            },
        ],
    }


def set_operation_join(operator, left="order_id", right="order_id"):
    return (
        f"WITH combined AS (SELECT {left} AS join_key FROM orders "
        f"{operator} SELECT {right} AS join_key FROM orders), "
        "passthrough AS (SELECT join_key FROM combined) "
        "SELECT p.join_key, r.amount FROM passthrough p "
        "JOIN refunds r ON p.join_key = r.order_id"
    )


@pytest.mark.parametrize("operator", ["UNION", "UNION ALL", "INTERSECT", "EXCEPT"])
def test_set_operation_cte_preserves_approved_join_keys(operator, relation_metadata):
    validated = validate_sql(
        set_operation_join(operator), relation_metadata, ["orders", "refunds"]
    )

    assert operator in validated
    assert "p.join_key = r.order_id" in validated


@pytest.mark.parametrize("operator", ["UNION", "UNION ALL", "INTERSECT", "EXCEPT"])
@pytest.mark.parametrize("invalid_key", ["amount", "order_id + 100"], ids=["unapproved-column", "computed-key"])
@pytest.mark.parametrize("branch", ["left", "right"])
def test_set_operation_rejects_unapproved_key_in_either_branch(
    operator, invalid_key, branch, relation_metadata
):
    query = set_operation_join(operator, **{branch: invalid_key})

    with pytest.raises(SQLRejected, match="批准的关联路径"):
        validate_sql(query, relation_metadata, ["orders", "refunds"])
