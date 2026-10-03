"""Narrow deterministic wording checks; not a general semantic correctness proof."""
import re


def narrative_issues(analysis, catalog):
    # Only apply this check when the published financial formula explicitly defines
    # gross_sales minus discounts. No correction is inferred from a question ID.
    has_gross_formula = any("gross_sales" in str(m.get("expression", "")) and "discounts" in str(m.get("expression", ""))
                            for m in catalog.get("metrics", []))
    # Apply the early-window guard only to the published account-retention
    # definition that explicitly groups on post-signup days [0, 7).
    has_early_retention = any(
        m.get("id") == "account_retention"
        and "[day0,day7)" in re.sub(r"\s+", "", str(m.get("description", "")))
        and ("automation_saved" in str(m.get("description", ""))
             or "early_key_feature" in str(m.get("description", "")))
        for m in catalog.get("metrics", [])
    )
    if not has_gross_formula and not has_early_retention:
        return []
    issues = []

    def visit(value, path):
        if isinstance(value, dict):
            for key, item in value.items():
                visit(item, f"{path}.{key}")
        elif isinstance(value, list):
            for index, item in enumerate(value):
                visit(item, f"{path}[{index}]")
        elif isinstance(value, str):
            if has_gross_formula and re.search(r"(?<!标价)(?<!折前)成交额\s*[−－-]\s*折扣", value):
                issues.append(f"{path} 将已扣折扣的成交额再次扣折扣。请改为正式卡中的‘标价成交额−折扣’，或使用已扣折扣成交额且不再扣折扣；其他数字保持原证据。")
            if has_early_retention and re.search(r"关键功能|automation_saved|early_key_feature", value):
                for match in re.finditer(r"注册\s*前\s*[7七]\s*[日天]", value):
                    # A correct contrast ('不是注册前7日') is not a claim that
                    # the grouping window precedes signup.
                    if re.search(
                        r"(?:不是|并非|而非|非|(?:并)?不等于|不同于|"
                        r"(?:不能|不可|不应|不得)(?:被)?(?:解读|理解|解释|表述|等同|称|写)(?:为|成))"
                        r"\s*[\"'“‘]?\s*$", value[:match.start()]
                    ):
                        continue
                    issues.append(f"{path} 将关键功能分组误写成注册之前。正式卡为注册后的前7日[day0,day7)，不是注册前7日；应避免使用分组窗口之后的未来行为，而不是排除所有注册后行为。其他数字保持原证据。")
                    break

    visit(analysis, "analysis")
    return issues
