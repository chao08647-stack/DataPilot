import pytest

from insight.config import PROJECT_ROOT
from insight.skills import SkillRegistry

REGISTRY = SkillRegistry(PROJECT_ROOT / "skills")
CAPABILITY_SKILLS = [s for s in REGISTRY.skills if s.get("required_capabilities")]


@pytest.mark.parametrize("skill", CAPABILITY_SKILLS, ids=lambda s: s["id"])
def test_custom_domain_uses_capabilities_without_template_or_industry_id(skill):
    required = skill["required_capabilities"]
    selected = REGISTRY.select(skill["allowed_nodes"][0], "customer-domain-001", skill["tags"], capabilities=required)
    assert skill["id"] in [s["id"] for s in selected]
    assert skill["id"] in [s["id"] for s in REGISTRY.list("customer-domain-001", capabilities=required)]
    assert len([s for s in selected if s["base"]]) == 1
    assert len([s for s in selected if not s["base"]]) <= 2


@pytest.mark.parametrize("skill", CAPABILITY_SKILLS, ids=lambda s: s["id"])
def test_capability_match_still_requires_tags_nodes_and_all_requirements(skill):
    node, tags, required = skill["allowed_nodes"][0], skill["tags"], skill["required_capabilities"]
    for available in [None, [], ["unknown-plugin-tool"], required[1:]]:
        assert skill["id"] not in [s["id"] for s in REGISTRY.select(node, "custom-domain", tags, available)]
    assert skill["id"] not in [s["id"] for s in REGISTRY.select(node, "custom-domain", [], required)]
    assert REGISTRY.select("unauthorized-node", "custom-domain", tags, required) == []
    # Explicit published capability scope cannot be bypassed by using a builtin ID.
    assert skill["id"] not in [s["id"] for s in REGISTRY.select(node, skill["scenarios"][0], tags, [])]
    assert skill["id"] in [s["id"] for s in REGISTRY.select(node, skill["scenarios"][0], tags)]


def test_capabilities_do_not_add_skills_or_tools_or_expand_context_budget():
    before = [(s["id"], list(s.get("tools", []))) for s in REGISTRY.skills]
    available = [cap for s in CAPABILITY_SKILLS for cap in s["required_capabilities"]]
    tags = [tag for s in REGISTRY.skills for tag in s["tags"]]
    for node in ["intent", "discovery", "sql", "repair", "analysis", "review", "visualization"]:
        selected = REGISTRY.select(node, "custom-domain", tags, available + ["*", "execute_shell", "unknown"])
        assert all(node in s["allowed_nodes"] for s in selected)
        assert len([s for s in selected if s["base"]]) == 1
        assert len([s for s in selected if not s["base"]]) <= 2
    assert before == [(s["id"], list(s.get("tools", []))) for s in REGISTRY.skills]
    assert len(CAPABILITY_SKILLS) == 6
    assert all("*" in s["scenarios"] for s in REGISTRY.list("custom-domain", ["*", "execute_shell"]))


def test_malformed_capability_input_cannot_be_interpreted_as_characters():
    with pytest.raises(ValueError, match="collection"):
        REGISTRY.select("analysis", "custom-domain", ["profit"], "profit_bridge")


def test_all_skills_positive_and_negative_selection():
    registry = SkillRegistry(PROJECT_ROOT / "skills")
    assert len(registry.skills) == 18
    for skill in registry.skills:
        scenario = "ecommerce" if "*" in skill["scenarios"] else skill["scenarios"][0]
        matched = False
        for node in skill["allowed_nodes"]:
            selected = registry.select(node, scenario, skill["tags"])
            assert sum(s["base"] for s in selected) <= 1
            assert sum(not s["base"] for s in selected) <= 2
            matched |= skill["id"] in [s["id"] for s in selected]
        assert matched, f"Never selected: {skill['id']}"
        assert skill["id"] not in [s["id"] for s in registry.select("unauthorized-node", scenario, skill["tags"])]
        if "*" not in skill["scenarios"]:
            wrong = next(s for s in ["ecommerce", "saas", "retail"] if s not in skill["scenarios"])
            assert skill["id"] not in [s["id"] for s in registry.select(skill["allowed_nodes"][0], wrong, skill["tags"])]
        if not skill["base"]:
            assert skill["id"] not in [s["id"] for s in registry.select(skill["allowed_nodes"][0], scenario, [])]
