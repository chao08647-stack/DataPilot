import pytest

from insight.models import Discovery, Intent
from insight.workflow import Workflow


class Subject:
    def __init__(self, response):
        self.response = response
        self.skills = type('Skills', (), {'list': lambda *a, **k: []})()
        self.events = []

    def scene(self, state):
        return {'reporting_range': {'start': '2025-01-01', 'end': '2025-12-31'},
                'date_range': {'start': '2024-01-01', 'end': '2026-03-31'},
                'metrics': [{'id': 'retention', 'tables': ['accounts']}],
                'tables': [{'name': 'accounts', 'columns': ['signup_date']}], 'relations': []}

    async def ask(self, *args):
        return self.response

    def emit(self, *args):
        self.events.append(args)


@pytest.mark.asyncio
@pytest.mark.parametrize('source,expected', [('default', '2025-01-01'), ('user', '2024'), ('context', '2024')])
async def test_default_report_scope_does_not_replace_explicit_or_context_time(source, expected):
    subject = Subject(Intent(normalized_question='留存', metric_ids=['retention'], time_source=source, time_range='2024'))
    result = await Workflow.intent(subject, {'scenario_id': 'test', 'question': '不同注册月份的留存如何？'})
    assert result['intent']['time_range'].startswith(expected)
    assert '2026' not in result['intent']['time_range']


@pytest.mark.asyncio
async def test_discovery_cannot_expand_resolved_reporting_period():
    subject = Subject(Discovery(metric_ids=['retention'], tables=['accounts'], time_range='2024至2026', rationale='定位'))
    result = await Workflow.discovery(subject, {'intent': {'metric_ids': ['retention'], 'dimensions': [], 'time_range': '2025'}})
    assert result['discovery']['time_range'] == '2025'
