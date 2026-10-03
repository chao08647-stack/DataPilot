"""Complete-result BI acceptance; all reference calculations are test-only."""
from collections import Counter, defaultdict
from copy import deepcopy
from pathlib import Path

import duckdb
import pytest

from insight.analytics import run_analysis
from insight.enterprise import validate_definition
from insight.scenarios import generate_all, load_scenarios
from insight.sql import execute_query

ROOT = Path(__file__).resolve().parents[2]
SCENES = load_scenarios(ROOT / "scenarios")
CASES = [(name, item) for name, scene in SCENES.items() for item in scene["diagnostics"]]


@pytest.fixture(scope="session")
def bi_paths():
    # Explicit versioned files are generated once, never replacing old datasets.
    return generate_all(ROOT / "scenarios", ROOT / "data")


@pytest.fixture(scope="session")
def all_bi_results(bi_paths):
    result = {}
    for name, diagnostic in CASES:
        scene = SCENES[name]
        queries = [execute_query(bi_paths[name], q["sql"], scene, [t["name"] for t in scene["tables"]],
            q["id"], timeout=30, limit=20000, allow_subqueries=True) for q in diagnostic["queries"]]
        calculated = run_analysis(diagnostic["tool"], queries, diagnostic["config"])
        result[diagnostic["id"]] = (queries, calculated)
    return result


@pytest.mark.parametrize("name,diagnostic", CASES, ids=[d["id"] for _, d in CASES])
def test_all_ten_contracts_use_complete_guarded_evidence(all_bi_results, name, diagnostic):
    validate_definition(SCENES[name])
    queries, result = all_bi_results[diagnostic["id"]]
    assert result["status"] == "ok", result
    assert result["reconciliation"]["balanced"]
    assert all(not q["truncated"] and len(q["rows"]) <= 20000 for q in queries)
    assert result["series"] and result["chart"]
    assert len(result["facts"]) <= 20
    assert len(result.get("tables", [])) <= 6


@pytest.mark.parametrize("name,diagnostic", CASES, ids=[d["id"] for _, d in CASES])
def test_every_tool_refuses_partial_primary_evidence(all_bi_results, name, diagnostic):
    queries = deepcopy(all_bi_results[diagnostic["id"]][0])
    queries[0]["truncated"] = True
    result = run_analysis(diagnostic["tool"], queries, diagnostic["config"])
    assert result["status"] == "insufficient_data"
    assert not result["facts"] and not result["series"]


@pytest.mark.parametrize("name,counts", [
    ("ecommerce", {"customers": 2000, "products": 120, "orders": 60000}),
    ("saas", {"accounts": 1000, "users": 5000}),
    ("retail", {"stores": 24, "products": 80}),
])
def test_versioned_target_sizes_and_coverage(bi_paths, name, counts):
    assert SCENES[name]["data_version"] == "light-bi-3.1"
    assert SCENES[name]["reporting_range"] == {"start": "2025-01-01", "end": "2025-12-31"}
    assert SCENES[name]["observation_end"] == "2026-03-31"
    with duckdb.connect(str(bi_paths[name]), read_only=True) as db:
        for table, count in counts.items():
            assert db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] == count
        assert str(db.execute("SELECT MAX(calendar_date) FROM business_calendar").fetchone()[0]) == "2026-03-31"


def test_twenty_independent_queries_per_domain(bi_paths):
    """20+ guarded queries/domain vs Python arithmetic over raw single-table rows.

    These reference calculations are not shipped as knowledge or template answers.
    The separate adversarial fixtures check cross-fact attribution and look-ahead.
    """
    for name in SCENES:
        expected = []
        with duckdb.connect(str(bi_paths[name]), read_only=True) as db:
            if name in {"ecommerce", "retail"}:
                table = "orders" if name == "ecommerce" else "transactions"
                day = "order_date" if name == "ecommerce" else "transaction_date"
                amount = "order_total" if name == "ecommerce" else "transaction_total"
                rows = db.execute(f"SELECT {day},{amount},status FROM {table}").fetchall()
                monthly = defaultdict(float)
                counts = Counter()
                for date, value, status in rows:
                    if date.year == 2025 and status == "completed":
                        monthly[date.month] += value
                        counts[date.month] += 1
                for month in range(1, 13):
                    where = f"year({day})=2025 AND month({day})={month} AND status='completed'"
                    expected.append((f"SELECT SUM({amount}) FROM {table} WHERE {where}", monthly[month]))
                for month in range(1, 9):
                    expected.append((f"SELECT COUNT(*) FROM {table} WHERE year({day})=2025 AND month({day})={month} AND status='completed'", counts[month]))
            else:
                rows = db.execute("SELECT signup_date,acquisition_channel,company_size FROM accounts").fetchall()
                months = Counter(day.month for day, _, _ in rows if day.year == 2025)
                channels = Counter(channel for day, channel, _ in rows if day.year == 2025)
                sizes = Counter(size for day, _, size in rows if day.year == 2025)
                for month in range(1, 13):
                    expected.append((f"SELECT COUNT(*) FROM accounts WHERE year(signup_date)=2025 AND month(signup_date)={month}", months[month]))
                for channel, count in channels.items():
                    expected.append((f"SELECT COUNT(*) FROM accounts WHERE year(signup_date)=2025 AND acquisition_channel='{channel}'", count))
                for size, count in sizes.items():
                    expected.append((f"SELECT COUNT(*) FROM accounts WHERE year(signup_date)=2025 AND company_size='{size}'", count))
                expected.append(("SELECT COUNT(*) FROM accounts WHERE year(signup_date)=2025", sum(months.values())))
            assert len(expected) >= 20
            db.close()  # The guarded executor applies a stricter connection configuration.
            for sql, reference in expected:
                actual = execute_query(bi_paths[name], sql, SCENES[name], [table["name"] for table in SCENES[name]["tables"]])
                assert actual["rows"][0][0] == pytest.approx(reference, abs=1e-6)


def test_marketing_fully_allocates_and_new_customer_months_are_not_degenerate(bi_paths, all_bi_results):
    with duckdb.connect(str(bi_paths["ecommerce"]), read_only=True) as db:
        expected = db.execute("SELECT SUM(spend) FROM marketing_spend WHERE year(spend_date)=2025").fetchone()[0]
        first = db.execute("SELECT customer_id,MIN(order_date) FROM orders WHERE status='completed' GROUP BY customer_id").fetchall()
    assert Counter(date.year for _, date in first) == {2024: 500, 2025: 1500}
    assert len({date.month for _, date in first if date.year == 2025}) == 12
    rows = all_bi_results["period-overview"][1]["series"]
    assert sum(row["marketing"] for row in rows) == pytest.approx(expected)
    assert sum(row["orders"] for row in rows) == 41248


def test_auxiliary_results_use_complete_rows_beyond_model_sample(all_bi_results):
    qs, stores = all_bi_results["store-driver-comparison"]
    assert len(qs[1]["rows"]) > 50
    assert qs[1]["id"] in stores["evidence_ids"]
    assert {table["id"] for table in stores["tables"]} == {"declining_store_categories", "declining_store_dates"}
    _, refund = all_bi_results["refund-fulfillment"]
    totals = [sum(row["refunded_amount"] for row in table["rows"]) for table in refund["tables"]]
    assert len(totals) == 3 and all(total == pytest.approx(totals[0]) for total in totals)
    _, mrr = all_bi_results["mrr-monthly-bridge"]
    bridge = next(t["rows"] for t in mrr["tables"] if t["id"] == "annual_mrr_bridge")
    assert sum(r["delta"] for r in bridge) == pytest.approx(mrr["series"][-1]["closing_mrr"]-mrr["series"][0]["opening_mrr"])
    assert len(mrr["series"]) == 12
    assert len(all_bi_results["account-retention"][1]["series"]) == 72


def test_retention_summary_is_weighted_preserves_tied_extremes_and_separates_immature():
    rows=[]
    for cohort,eligible,retained in [('2025-01',10,10),('2025-02',90,45),('2025-03',20,20),('2025-04',0,0)]:
        row={'cohort':cohort,'feature_group':'early','accounts':max(10,eligible)}
        for window in [30,60,90]:
            row.update({f'eligible_{window}':eligible,f'retained_{window}':retained})
        rows.append(row)
    query={'id':'input','columns':list(rows[0]),'rows':rows}
    result=run_analysis('account_retention',[query])
    summary=result['tables'][0]
    assert summary['id']=='retention_summary' and len(summary['rows'])==3
    for row in summary['rows']:
        assert row['eligible']==120 and row['retained']==75 and row['retention_pct']==62.5
        assert row['cohort_count']==4 and row['eligible_cohort_count']==3
        assert row['low_sample_cohort_count']==2 and row['ineligible_cohort_count']==1
        assert row['min_retention_pct']==50 and row['min_retention_cohorts']=='2025-02'
        assert row['max_retention_pct']==100 and row['max_retention_cohorts']=='2025-01, 2025-03'
    assert len(result['series'])==12
    facts={r['name']:r['value'] for r in result['facts']}
    assert facts['存在低样本窗口的注册月份×功能组数']==2
    assert run_analysis('account_retention',[{**query,'rows':rows+rows[:1]}])['status']=='insufficient_data'


def test_retention_summary_never_turns_unobserved_windows_into_zero_percent():
    row={'cohort':'2026-03','feature_group':'early','accounts':20}
    for window in [30,60,90]:
        row.update({f'eligible_{window}':0,f'retained_{window}':0})
    result=run_analysis('account_retention',[{'id':'input','columns':list(row),'rows':[row]}])
    for summary in result['tables'][0]['rows']:
        assert summary['retention_pct'] is None
        assert summary['min_retention_pct'] is None and summary['max_retention_pct'] is None
        assert summary['min_retention_cohorts']==summary['max_retention_cohorts']==''
        assert summary['ineligible_cohort_count']==1 and summary['low_sample_cohort_count']==0


def test_asof_missing_or_stale_observation_never_creates_precise_risk_numbers():
    row = dict(as_of_date="2025-12-31", store_id=1, product_id=1, snapshot_date=None,
               stock_on_hand=None, sold_30d=30, pending_po=10, due_po_14d=10, overdue_po=0, transfer_in_due_14d=0)
    for snapshot, stock in [(None, None), ("2025-12-01", 50)]:
        query = {"id": "input", "columns": list(row), "rows": [{**row, "snapshot_date": snapshot, "stock_on_hand": stock}]}
        result = run_analysis("inventory_asof", [query])
        assert result["status"] == "ok", result
        assert result["series"][0]["risk"] == "insufficient_observation"
        assert result["series"][0]["cover_days"] is None
        assert result["series"][0]["projected_stock_14d"] is None


def test_value90_formal_card_matches_inclusive_observation_and_two_stage_attribution():
    metrics = {metric["id"]: metric for metric in SCENES["ecommerce"]["metrics"]}
    card = metrics["realized_value_90d"]
    assert card["window_definition"]["observation_end_inclusive"] is True
    assert "89 days" in card["window_definition"]["maturity_condition"]
    assert "90 days" in card["window_definition"]["end_exclusive"]
    assert "成交渠道" in card["allocation_rule"] and "首次获客" in card["allocation_rule"]
    assert "eligible_90d=new_customers" in card["net_comparison_rule"]
    assert "+89" in metrics["repurchase_90d"]["description"]


def test_paid_conversion_and_subscription_opening_have_unambiguous_metric_names():
    cards = {card["id"]: card for card in SCENES["saas"]["metrics"]}
    assert "订阅开通" in cards["conversion_30d"]["name"]
    assert "付费转化" not in cards["conversion_30d"]["aliases"]
    assert "付费转化" in cards["paid_funnel_rate"]["aliases"]
    assert "支付成功" in cards["paid_funnel_rate"]["description"]
    assert "联合分组" in cards["paid_funnel_rate"]["description"]


def test_value90_expense_channel_is_not_acquisition_channel_and_net_gap_is_permitted():
    from insight.bi_queries import CUSTOMER_VALUE
    with duckdb.connect(":memory:") as db:
        for table in SCENES["ecommerce"]["tables"]:
            fields = ",".join(f'"{name}" {kind}' for name, kind in table["columns"].items())
            db.execute(f'CREATE TABLE "{table["name"]}" ({fields})')
        db.execute("INSERT INTO products(product_id,list_price) VALUES(1,100)")
        db.execute("INSERT INTO customer_acquisition(customer_id,acquisition_channel) VALUES(1,'first-touch-A')")
        db.execute("INSERT INTO orders(order_id,customer_id,order_date,order_month,channel,status,order_total) VALUES(1,1,'2025-12-31','2025-12-01','order-B','completed',100)")
        db.execute("INSERT INTO order_items(order_id,product_id,quantity,unit_price,unit_cost) VALUES(1,1,1,100,40)")
        db.execute("INSERT INTO marketing_spend(spend_date,month_start,channel,spend,acquisition_spend,retention_spend) VALUES('2025-12-31','2025-12-01','first-touch-A',100,100,0),('2025-12-31','2025-12-01','order-B',10,0,10)")
        # March 30 is exactly first purchase + 89 days; it is a complete final observation day.
        cursor = db.execute(CUSTOMER_VALUE.replace("2026-03-31", "2026-03-30"))
        query = {"id": "input", "columns": [col[0] for col in cursor.description], "rows": cursor.fetchall()}
        result = run_analysis("customer_value_90d", [query])
        assert result["status"] == "ok", result
        channel = next(row for row in result["series"] if row["channel"] == "first-touch-A")
        assert channel["eligible_90d"] == channel["new_customers"] == 1
        assert channel["realized_value_90d"] == 50  # 100 revenue - 40 cost - 10 actual order-channel retention cost.
        assert channel["cac"] == 100
        assert channel["value_minus_cac"] == -50


def test_new_data_regeneration_is_deterministic_without_overwriting_old_files(bi_paths, tmp_path):
    regenerated = generate_all(ROOT / "scenarios", tmp_path)
    for name, path in regenerated.items():
        with duckdb.connect(str(path), read_only=True) as regenerated_db, duckdb.connect(str(bi_paths[name]), read_only=True) as original:
            for table in SCENES[name]["tables"]:
                sql = f'SELECT COUNT(*),SUM(hash(t)) FROM "{table["name"]}" t'
                assert regenerated_db.execute(sql).fetchall() == original.execute(sql).fetchall()


def test_q06_to_q10_match_independent_raw_observation_oracles(bi_paths, all_bi_results):
    from bi_domain_oracle import compute_domain_gold
    gold = compute_domain_gold(bi_paths)
    mappings = {"q07": ("mrr-monthly-bridge", ["period"]), "q08": ("account-retention", ["cohort", "feature_group", "window"]),
                "q09": ("store-driver-comparison", ["store_id"]), "q10": ("inventory-asof", ["store_id", "product_id"])}
    for question, (diagnostic, keys) in mappings.items():
        actual = {tuple(str(row[key]) for key in keys): row for row in all_bi_results[diagnostic][1]["series"]}
        expected = gold[question]["series"]
        assert len(actual) == len(expected)
        for reference in expected:
            row = actual[tuple(str(reference[key]) for key in keys)]
            for field, value in reference.items():
                if isinstance(value, (int, float)):
                    assert row[field] == pytest.approx(value, abs=1e-5), (question, field, row, reference)
                else:
                    assert row[field] == value
    funnel = all_bi_results["conversion-funnel"][1]
    table = next(t["rows"] for t in funnel["tables"] if t["id"] == "funnel_comparison")
    for row, metric in zip(table, ["registered", "activated", "trial_started", "paid"]):
        assert row["baseline"] == gold["q06"]["periods"]["2025-Q3"][metric]
        assert row["current"] == gold["q06"]["periods"]["2025-Q4"][metric]
    for dimension in ['channel','company_size']:
        dimension_table=next(t['rows'] for t in funnel['tables'] if t['id']=='funnel_'+dimension)
        changes={}
        for row in dimension_table:
            if row['period']!='2025-Q4':
                continue
            before=[r for r in gold['q06']['groups'] if r['period']=='2025-Q3' and r[dimension]==row[dimension]]
            after=[r for r in gold['q06']['groups'] if r['period']=='2025-Q4' and r[dimension]==row[dimension]]
            delta=100*(sum(r['paid'] for r in after)/sum(r['registered'] for r in after)
                       -sum(r['paid'] for r in before)/sum(r['registered'] for r in before))
            assert row['paid_pct_delta_pp']==pytest.approx(delta,abs=1e-6)
            changes[row[dimension]]=delta
        fact=next(f for f in funnel['facts'] if f.get('dimension')==dimension)
        assert fact['groups']==[min(changes,key=changes.get)]
        assert fact['value']==pytest.approx(min(changes.values()),abs=1e-6)
    for question, diagnostic, table_ids in [("q07", "mrr-monthly-bridge", ["account_mrr_changes", "annual_account_changes"]),
                                             ("q08", "account-retention", ["retention_summary"]),
                                             ("q09", "store-driver-comparison", ["declining_store_categories", "declining_store_dates"])]:
        for table_id in table_ids:
            actual = next(table["rows"] for table in all_bi_results[diagnostic][1]["tables"] if table["id"] == table_id)
            expected = gold[question][table_id]
            assert len(actual) == len(expected)
            for row, reference in zip(actual, expected):
                for key, value in reference.items():
                    if isinstance(value, (int, float)):
                        assert row[key] == pytest.approx(value, abs=1e-5)
                    else:
                        assert row[key] == value
