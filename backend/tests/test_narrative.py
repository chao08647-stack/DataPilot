import pytest

from insight.narrative import narrative_issues
from insight.providers import ModelError
from insight.workflow import Workflow

CATALOG = {"metrics": [{"expression": "gross_sales-discounts-refunds"}]}
RETENTION_CATALOG = {"metrics": [{
    "id": "account_retention",
    "description": "关键功能分组仅用注册日为day0的前7日[day0,day7)的automation_saved。",
}]}


@pytest.mark.parametrize("bad_window", ["注册前7日", "注册前 7 天", "注册前七日"])
def test_retention_early_window_guard_keeps_post_signup_direction(bad_window):
    source = {"summary": f"按{bad_window}关键功能使用分组。"}
    issues = narrative_issues(source, RETENTION_CATALOG)
    assert len(issues) == 1 and "analysis.summary" in issues[0]
    assert "注册后的前7日[day0,day7)" in issues[0]
    assert source["summary"] == f"按{bad_window}关键功能使用分组。"


def test_retention_early_window_guard_checks_nested_sections_without_financial_metric():
    source = {"summary": "注册后的前7日关键功能使用分组。",
              "findings": [{"detail": "按注册前7日automation_saved分组"}],
              "recommendations": ["固定注册前7日关键功能分组，避免使用注册后行为。"]}
    issues = narrative_issues(source, RETENTION_CATALOG)
    assert len(issues) == 2
    assert "findings[0].detail" in issues[0]
    assert "recommendations[0]" in issues[1]


@pytest.mark.parametrize("text", [
    "关键功能分组为注册后的前7日[day0,day7)。",
    "关键功能分组不是注册前7日，而是注册后前7日。",
    "关键功能分组为注册后前7日，并非注册前7日。",
    "关键功能分组为注册后前7天，该窗口不等于注册前7天。",
    "关键功能分组并不等于注册前7天。",
    "automation_saved窗口不同于注册前7日。",
    "关键功能窗口不能解读为注册前7日。",
    "关键功能窗口不能被解释为‘注册前7天’。",
    "关键功能窗口不应理解为注册前七日。",
    "关键功能窗口不得表述为注册前7天。",
    "单独观察注册前7日的营销触点，不涉及留存功能分组。",
])
def test_retention_early_window_guard_accepts_correct_or_unrelated_statements(text):
    assert not narrative_issues({"summary": text}, RETENTION_CATALOG)


def test_retention_negation_only_applies_to_its_immediate_window_not_later_bad_claim():
    text="关键功能窗口不是其他范围，而是注册前7天。"
    assert narrative_issues({"summary":text},RETENTION_CATALOG)
    text="关键功能窗口不等于注册前7天；但我们实际按注册前7天分组。"
    assert narrative_issues({"summary":text},RETENTION_CATALOG)


def test_retention_early_window_guard_requires_explicit_published_semantics():
    source = {"summary": "注册前7日关键功能使用分组。"}
    assert not narrative_issues(source, CATALOG)
    assert not narrative_issues(source, {"metrics": [{"id": "account_retention"}]})
    assert not narrative_issues(source, {"metrics": [{
        "id": "other_metric", "description": RETENTION_CATALOG["metrics"][0]["description"],
    }]})


def test_narrative_formula_checks_every_section_without_changing_text():
    source = {"summary": "正确结果", "limitations": ["贡献利润=成交额−折扣−成本"],
              "findings": [{"detail": "标价成交额−折扣−成本"}]}
    issues = narrative_issues(source, CATALOG)
    assert len(issues) == 1 and "limitations[0]" in issues[0]
    assert source["limitations"][0] == "贡献利润=成交额−折扣−成本"
    assert not narrative_issues(source, {"metrics": []})


@pytest.mark.asyncio
async def test_deterministic_formula_check_precedes_model_approval_and_keeps_one_revision():
    class Subject:
        def scene(self, state):
            return CATALOG

        def emit(self, *args):
            pass

        async def ask(self, *args):
            raise AssertionError("Invalid formula must not be sent for approval")

    state = {"analysis": {"summary": "成交额-折扣-成本"}, "charts": []}
    result = await Workflow.final_review(Subject(), state)
    assert result["review"]["action"] == "repair"
    with pytest.raises(ModelError, match="确定性"):
        await Workflow.final_review(Subject(), {**state, "delivery_revisions": 1})
