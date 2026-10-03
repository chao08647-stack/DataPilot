"""Deterministic normal, anomaly and insufficient-evidence tests; no models."""
from copy import deepcopy

import pytest

from insight.analytics import run_analysis


def query(rows):
    return [{"id": "input", "columns": list(rows[0]), "rows": rows, "truncated": False}]


PROFIT = [dict(period=p, gross_sales=100, discounts=10, refunds=5, cogs=40,
               fulfillment=5, payment_fees=1, marketing=9) for p in ("2025-09", "2025-11")]
REFUND = [dict(group="carrier", orders=100, refunded_orders=10, late_orders=20, late_refunded_orders=2)]
FUNNEL = [dict(period=p, group="organic", registered=100, activated=80, trial_started=60, paid=40)
          for p in ("2025-09", "2025-11")]
MRR = [dict(period=p, account_id=1, mrr=100, first_started="2024-01-01")
       for p in ("2025-09-30", "2025-11-30")]
STORES = [dict(period=p, store_id=1, open_days=30, visitors=1000, transactions=100, sales=10000)
          for p in ("2025-09", "2025-11")]
STOCK = [dict(store_id=1, product_id=1, opening_stock=10, receipts=5, sold_units=3,
              transfer_in=2, transfer_out=1, adjustments=-1, closing_stock=12)]
CASES = [("profit_bridge", PROFIT, {}), ("refund_diagnosis", REFUND, {}),
         ("conversion_funnel", FUNNEL, {}), ("mrr_bridge", MRR, {"complete_snapshot": True}),
         ("same_store_decomposition", STORES, {}), ("inventory_reconciliation", STOCK, {})]


@pytest.mark.parametrize("tool,rows,config", CASES)
def test_normal_diagnostic_is_reconciled(tool, rows, config):
    result = run_analysis(tool, query(rows), config)
    assert result["status"] == "ok"
    assert result["reconciliation"]["balanced"]
    assert result["facts"] and result["series"] and result["limitations"]
    assert all(fact["evidence_ids"] == ["input"] for fact in result["facts"])


@pytest.mark.parametrize("tool,rows,config", CASES)
@pytest.mark.parametrize("missing", ["empty", "truncated", "null"])
def test_missing_evidence_never_becomes_zero_or_diagnosis(tool, rows, config, missing):
    inputs = query(deepcopy(rows))
    if missing == "empty":
        inputs[0]["rows"] = []
    elif missing == "truncated":
        inputs[0]["truncated"] = True
    else:
        inputs[0]["rows"][0] = {key: None for key in rows[0]}
    result = run_analysis(tool, inputs, config)
    assert result["status"] == "insufficient_data"
    assert result["facts"] == [] and result["series"] == []
    assert "chart" not in result


def test_profit_anomaly_is_additive_not_causal():
    rows = deepcopy(PROFIT)
    rows[1].update(discounts=20, cogs=50)
    result = run_analysis("profit_bridge", query(rows))
    assert sum(row["delta"] for row in result["series"]) == -20
    assert result["reconciliation"]["residual"] == 0


def test_refund_anomaly_and_missing_comparison():
    row = {**REFUND[0], "refunded_orders": 28, "late_refunded_orders": 20}
    result = run_analysis("refund_diagnosis", query([row]))
    assert result["series"][0]["gap_pp"] == 90
    row.update(late_orders=100, late_refunded_orders=28)
    result = run_analysis("refund_diagnosis", query([row]))
    assert result["series"][0]["gap_pp"] is None


def test_funnel_source_mix_and_within_source_reconcile():
    rows = [dict(period=p, group=g, registered=n, activated=n, trial_started=n, paid=k)
            for p, g, n, k in [("2025-09", "A", 80, 40), ("2025-09", "B", 20, 4),
                              ("2025-11", "A", 20, 8), ("2025-11", "B", 80, 8)]]
    result = run_analysis("conversion_funnel", query(rows))
    facts = {item["name"]: item["value"] for item in result["facts"]}
    assert facts["付费转化率变化"] == -28
    assert facts["来源结构变化贡献"] == -18
    assert facts["来源内转化变化贡献"] == -10
    assert result["reconciliation"]["conversion_residual_pp"] == 0


def test_funnel_joint_dimensions_are_preserved_in_method_not_pure_channel_attribution():
    rows = [dict(period=p, group=c + '|' + size, channel=c, company_size=size,
                 registered=20, activated=15, trial_started=10, paid=paid)
            for p, paid in [('2025-Q3', 8), ('2025-Q4', 5)]
            for c in ['organic', 'paid'] for size in ['small', 'large']]
    result = run_analysis('conversion_funnel', query(rows))
    assert result['status'] == 'ok'
    assert result['grouping_dimensions'] == ['channel', 'company_size']
    assert '渠道×企业规模' in result['method']
    assert '不是纯渠道归因' in result['method']
    assert result['reconciliation']['conversion_residual_pp'] == 0


def test_funnel_dimension_decline_uses_complete_weighted_groups_and_preserves_columns():
    rows = [dict(period=period, channel=channel, company_size=size, group=channel+'|'+size,
                 registered=n, activated=n, trial_started=n, paid=paid)
            for period, channel, size, n, paid in [
                ('2025-Q3','A','small',90,54), ('2025-Q3','A','large',10,6),
                ('2025-Q3','B','small',90,45), ('2025-Q3','B','large',10,5),
                ('2025-Q4','A','small',20,10), ('2025-Q4','A','large',80,30),
                ('2025-Q4','B','small',90,40), ('2025-Q4','B','large',10,5),
            ]]
    result = run_analysis('conversion_funnel', query(list(reversed(rows))))
    table = next(t['rows'] for t in result['tables'] if t['id']=='funnel_channel')
    assert len(table)==4
    current={row['channel']:row for row in table if row['period']=='2025-Q4'}
    assert current['A']['paid_pct']==40
    assert current['A']['paid_pct_delta_pp']==-20
    assert current['B']['paid_pct_delta_pp']==-5
    assert current['A']['paid_pct_decline_rank']==1
    assert current['B']['paid_pct_decline_rank']==2
    assert current['A']['registered']==100 and current['A']['paid']==40
    assert current['A']['payment_loss']==60
    assert all(row['paid_pct_delta_pp'] is None and row['paid_pct_decline_rank'] is None
               for row in table if row['period']=='2025-Q3')
    fact=next(f for f in result['facts'] if f.get('dimension')=='channel')
    assert fact['groups']==['A'] and fact['value']==-20 and fact['evidence_ids']==['input']
    # Enterprise-size comparisons independently aggregate the same full input,
    # not the minimum of per-channel subgroup rates.
    size_table=next(t['rows'] for t in result['tables'] if t['id']=='funnel_company_size')
    for row in size_table:
        if row['period']!='2025-Q4':
            continue
        before=[r for r in rows if r['period']=='2025-Q3' and r['company_size']==row['company_size']]
        after=[r for r in rows if r['period']=='2025-Q4' and r['company_size']==row['company_size']]
        expected=100*(sum(r['paid'] for r in after)/sum(r['registered'] for r in after)
                      -sum(r['paid'] for r in before)/sum(r['registered'] for r in before))
        assert row['paid_pct_delta_pp']==pytest.approx(expected,abs=1e-6)


def test_funnel_decline_ranking_keeps_ties_and_does_not_rank_non_declines_or_missing_denominators():
    rows=[dict(period=p, channel=c, registered=n, activated=n, trial_started=n, paid=k)
          for c, bn, bk, cn, ck in [('A',10,5,10,3), ('B',20,10,20,6),
                                    ('C',10,5,10,4), ('up',10,2,10,3),
                                    ('flat',10,4,10,4), ('missing',0,0,10,4)]
          for p,n,k in [('2025-Q3',bn,bk),('2025-Q4',cn,ck)]]
    result=run_analysis('conversion_funnel',query(rows))
    table=next(t['rows'] for t in result['tables'] if t['id']=='funnel_channel')
    current={r['channel']:r for r in table if r['period']=='2025-Q4'}
    assert {key:current[key]['paid_pct_decline_rank'] for key in current}=={
        'A':1,'B':1,'C':2,'flat':None,'missing':None,'up':None}
    assert current['missing']['paid_pct_delta_pp'] is None
    assert current['flat']['paid_pct_delta_pp']==0 and current['up']['paid_pct_delta_pp']==10
    fact=next(f for f in result['facts'] if f.get('dimension')=='channel')
    assert fact['groups']==['A','B'] and fact['value']==-20


def test_funnel_no_decline_does_not_invent_largest_declining_group():
    rows=[dict(period=p,channel='A',registered=3,activated=3,trial_started=3,paid=k)
          for p,k in [('2025-Q3',1),('2025-Q4',2)]]
    result=run_analysis('conversion_funnel',query(rows))
    table=next(t['rows'] for t in result['tables'] if t['id']=='funnel_channel')
    assert table[1]['paid_pct_delta_pp']==33.333333
    assert table[1]['paid_pct_decline_rank'] is None
    assert not any(f.get('dimension')=='channel' for f in result['facts'])


def test_mrr_all_five_components_and_fixed_start_cohort():
    rows = [dict(period="2025-09-30", account_id=i, mrr=100, first_started="2024-01-01") for i in [1, 2, 3]]
    rows += [dict(period="2025-11-30", account_id=i, mrr=value, first_started=start)
             for i, value, start in [(1, 150, "2024-01-01"), (2, 50, "2024-01-01"),
                                     (4, 80, "2025-10-01"), (5, 60, "2024-01-01")]]
    result = run_analysis("mrr_bridge", query(rows), {"complete_snapshot": True})
    assert {r["component"]: r["delta"] for r in result["series"]} == {"新增": 80, "扩张": 50, "收缩": -50, "流失": -100, "恢复": 60}
    assert result["reconciliation"]["closing"] == 340
    assert run_analysis("mrr_bridge", query(rows))["status"] == "insufficient_data"


def test_same_store_excludes_new_store_and_reconciles_interactions():
    rows = deepcopy(STORES)
    rows[1].update(visitors=800, transactions=60, sales=5400)
    rows.append(dict(period="2025-11", store_id=2, open_days=30, visitors=500, transactions=100, sales=9000))
    result = run_analysis("same_store_decomposition", query(rows))
    assert result["reconciliation"]["excluded_stores"] == ["2"]
    assert sum(r["delta"] for r in result["series"]) == pytest.approx(-4600, abs=1e-5)


def test_inventory_residual_cannot_cancel_between_objects():
    rows = [{**STOCK[0], "closing_stock": 14}, {**STOCK[0], "store_id": 2, "closing_stock": 10}]
    result = run_analysis("inventory_reconciliation", query(rows))
    assert not result["reconciliation"]["balanced"]
    assert result["reconciliation"]["absolute_residual"] == 4


def test_inventory_period_selection_and_duplicate_objects():
    rows = [{**STOCK[0], "period": "2025-10"}, {**STOCK[0], "period": "2025-11", "closing_stock": 15}]
    result = run_analysis("inventory_reconciliation", query(rows), {"current_period": "2025-10"})
    assert result["reconciliation"]["balanced"]
    assert run_analysis("inventory_reconciliation", query(STOCK * 2))["status"] == "insufficient_data"


def test_nonfinite_numeric_evidence_is_rejected():
    rows = deepcopy(PROFIT)
    rows[0]["cogs"] = float("nan")
    assert run_analysis("profit_bridge", query(rows))["status"] == "insufficient_data"
