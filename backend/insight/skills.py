"""Select real prompt assets; metadata never grants tools or execution rights."""

from __future__ import annotations

from pathlib import Path

import yaml


class SkillRegistry:
    def __init__(self, root: Path):
        self.skills = []
        for path in sorted(root.glob("*/SKILL.md")):
            raw = path.read_text(encoding="utf-8-sig")
            parts = raw.split("---", 2)
            if len(parts) != 3 or parts[0].strip():
                raise ValueError(f"Invalid skill frontmatter: {path.parent.name}")
            metadata = yaml.safe_load(parts[1])
            if not all(key in metadata for key in ("id", "allowed_nodes", "scenarios", "tags", "base")):
                raise ValueError(f"Incomplete skill metadata: {path.parent.name}")
            required = metadata.get("required_capabilities", [])
            if not isinstance(required, list) or any(not isinstance(item, str) or not item.strip() for item in required):
                raise ValueError(f"Invalid skill capabilities: {path.parent.name}")
            self.skills.append({**metadata, "instructions": parts[2].strip()})
        if len({s["id"] for s in self.skills}) != len(self.skills):
            raise ValueError("Duplicate skill IDs")

    def list(self, scenario_id=None, capabilities=None):
        """Capabilities select prompt applicability, never grant executable tools.

        None preserves legacy industry routing. An explicit capability collection
        enables domain-neutral matching for skills declaring required_capabilities;
        every declared requirement is necessary, including for builtin domains.
        """
        if capabilities is not None:
            if not isinstance(capabilities, (list, tuple, set, frozenset)) or any(not isinstance(item, str) for item in capabilities):
                raise ValueError("capabilities must be a collection of string identifiers")
            capabilities = set(capabilities)
        matched = []
        for skill in self.skills:
            requirements = set(skill.get("required_capabilities", []))
            if capabilities is not None and requirements:
                applies = requirements.issubset(capabilities)
            else:
                applies = scenario_id is None or "*" in skill["scenarios"] or scenario_id in skill["scenarios"]
            if applies:
                matched.append(dict(skill))
        return matched

    def select(self, node: str, scenario_id: str, tags: list[str], capabilities=None) -> list[dict]:
        eligible = [s for s in self.list(scenario_id, capabilities) if node in s["allowed_nodes"]]
        base = [s for s in eligible if s["base"]]
        # A deterministic base is always loaded; supplementary skills require tag overlap.
        order = {"intent": "intent-analysis", "discovery": "metric-resolution", "sql": "sql-generation",
                 "repair": "sql-repair", "analysis": "trend-comparison", "review": "evidence-review",
                 "visualization": "visual-storytelling"}
        base.sort(key=lambda s: (s["id"] != order.get(node), s["id"]))
        extras = [s for s in eligible if not s["base"] and set(tags) & set(s["tags"])]
        extras.sort(key=lambda s: (-len(set(tags) & set(s["tags"])), s["id"]))
        return base[:1] + extras[:2]

    @staticmethod
    def render(skills: list[dict]) -> str:
        return "\n\n".join(f"### {s['id']}\n{s['instructions']}" for s in skills)
