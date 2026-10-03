import pytest

from insight.investigation import InvestigationNodes
from insight.models import Investigation
from insight.providers import ModelError


class Subject:
    def __init__(self, chosen=None):
        self.chosen = chosen or []
        self.payload = None

    def scene(self, state):
        return {'metrics': [{'id': 'inventory'}], 'diagnostics': [
            {'id': 'inventory-asof', 'tool': 'inventory_asof'},
            {'id': 'inventory-flow', 'tool': 'inventory_reconciliation'}]}

    async def ask(self, state, schema, node, instructions, extra):
        self.payload = extra
        return Investigation(steps=[{'objective': '核查到货日期', 'metric_ids': ['inventory'], 'grain': '采购单'}],
                             diagnostic_ids=self.chosen)

    def emit(self, *args):
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize('status,expected', [('ok', ['inventory-flow']), ('insufficient_data', ['inventory-asof', 'inventory-flow'])])
async def test_supplement_excludes_only_successfully_completed_tools(status, expected):
    subject = Subject()
    state = {'discovery': {}, 'supplementary_count': 1,
             'calculations': [{'tool': 'inventory_asof', 'status': status}]}
    result = await InvestigationNodes.investigate(subject, state)
    assert [d['id'] for d in subject.payload['available_diagnostics']] == expected
    assert result['investigation']['diagnostic_ids'] == []
    assert '独立只读SQL' in subject.payload['supplement_rule']


@pytest.mark.asyncio
async def test_repeat_of_completed_tool_is_rejected_not_reexecuted():
    subject = Subject(['inventory-asof'])
    with pytest.raises(ModelError, match='未批准'):
        await InvestigationNodes.investigate(subject, {'discovery': {}, 'supplementary_count': 1,
            'calculations': [{'tool': 'inventory_asof', 'status': 'ok'}]})
