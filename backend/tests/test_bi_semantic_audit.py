"""Independent adversarial semantic fixtures; no generated answers or model calls."""
import datetime as dt

import duckdb
import pytest

from insight import bi_queries
from insight.analytics import run_analysis
from insight.config import PROJECT_ROOT
from insight.scenarios import load_scenarios
from insight.sql import validate_sql


@pytest.fixture
def database():
    opened = []
    catalogs = load_scenarios(PROJECT_ROOT / "scenarios")

    def create(domain):
        conn = duckdb.connect(":memory:")
        opened.append(conn)
        for table in catalogs[domain]["tables"]:
            columns = ",".join(f'"{name}" {kind}' for name, kind in table["columns"].items())
            conn.execute(f'CREATE TABLE "{table["name"]}" ({columns})')
        return conn

    yield create
    for conn in opened:
        conn.close()


def query(conn, sql, query_id="input"):
    result = conn.execute(sql)
    return {"id": query_id, "columns": [c[0] for c in result.description], "rows": result.fetchall(), "truncated": False}


def test_retention_only_early_approved_feature_defines_exposure(database):
    conn = database("saas")
    conn.execute("INSERT INTO accounts(account_id,signup_date) VALUES(1,'2025-10-01')")
    conn.execute("INSERT INTO feature_events(account_id,event_date,event_type,feature_name) VALUES(1,'2025-10-03','key_feature','unrelated_feature'),(1,'2025-11-15','key_feature','automation_saved')")
    result = query(conn, bi_queries.RETENTION)
    group = result["rows"][0][result["columns"].index("feature_group")]
    assert group == "no_early_key_feature"


def test_monthly_mrr_new_subscription_reactivation_uses_account_history(database):
    conn = database("saas")
    conn.execute("INSERT INTO business_calendar(calendar_date,is_month_end) VALUES('2024-12-31',true),('2025-01-31',true),('2025-02-28',true)")
    conn.execute("INSERT INTO accounts(account_id) VALUES(1),(2)")
    conn.execute("INSERT INTO subscriptions(subscription_id,account_id,start_date,end_date,monthly_price) VALUES(1,1,'2024-11-01','2025-01-10',100),(2,1,'2025-02-01',NULL,100),(3,2,'2024-11-01',NULL,50)")
    conn.execute("INSERT INTO subscription_versions(subscription_id,account_id,effective_from,effective_to,monthly_price) VALUES(1,1,'2024-11-01','2025-01-10',100),(2,1,'2025-02-01',NULL,100),(3,2,'2024-11-01',NULL,50)")
    inputs = query(conn, bi_queries.MRR_MONTHLY)
    result = run_analysis("mrr_monthly_bridge", [inputs], {"complete_snapshot": True})
    assert result["status"] == "ok", result
    february = next(row for row in result["series"] if row["period"] == "2025-02")
    assert february["reactivation"] == 100 and february["new_mrr"] == 0


def test_inventory_asof_ignores_future_actuals_and_uncreated_orders(database):
    conn = database("retail")
    conn.execute("INSERT INTO stores(store_id,open_date) VALUES(1,'2023-01-01')")
    conn.execute("INSERT INTO products(product_id,product_name,category) VALUES(1,'item','category')")
    conn.execute("INSERT INTO inventory_snapshots VALUES(1,1,'2025-12-31',5)")
    conn.execute("INSERT INTO transactions(transaction_id,store_id,transaction_date,status,transaction_total) VALUES(1,1,'2025-12-31','completed',100)")
    conn.execute("INSERT INTO transaction_items(transaction_id,store_id,product_id,quantity) VALUES(1,1,1,30)")
    conn.execute("INSERT INTO purchase_orders(po_id,store_id,product_id,ordered_date,expected_date,ordered_qty) VALUES(1,1,1,'2025-12-28','2026-01-05',10)")
    conn.execute("INSERT INTO inventory_transfers(from_store_id,to_store_id,product_id,quantity,created_date,transfer_date,expected_arrival_date,received_date) VALUES(2,1,1,7,'2025-12-27','2025-12-28','2026-01-04',NULL)")
    before = query(conn, bi_queries.INVENTORY_ASOF)
    conn.execute("INSERT INTO inventory_snapshots VALUES(1,1,'2026-01-01',9999)")
    conn.execute("INSERT INTO transactions(transaction_id,store_id,transaction_date,status,transaction_total) VALUES(2,1,'2026-01-01','completed',99999)")
    conn.execute("INSERT INTO transaction_items(transaction_id,store_id,product_id,quantity) VALUES(2,1,1,9999)")
    conn.execute("INSERT INTO goods_receipts(po_id,received_date,quantity) VALUES(1,'2026-01-05',10)")
    conn.execute("INSERT INTO purchase_orders(po_id,store_id,product_id,ordered_date,expected_date,ordered_qty) VALUES(2,1,1,'2026-01-01','2026-01-03',500)")
    conn.execute("UPDATE inventory_transfers SET received_date='2026-01-04'")
    assert query(conn, bi_queries.INVENTORY_ASOF)["rows"] == before["rows"]
    values = dict(zip(before["columns"], before["rows"][0]))
    assert values["stock_on_hand"] == 5 and values["pending_po"] == 10
    assert values["sold_30d"] == 30 and values["transfer_in_due_14d"] == 7


def test_customer_ninety_day_refunds_and_orders_respect_exclusive_day90(database):
    conn = database("ecommerce")
    first = dt.date(2025, 1, 1)
    conn.execute("INSERT INTO products(product_id,list_price) VALUES(1,100)")
    conn.execute("INSERT INTO customer_acquisition(customer_id,acquisition_channel) VALUES(1,'channel')")
    for order_id, age in [(1, 0), (2, 89), (3, 90)]:
        day = first + dt.timedelta(days=age)
        conn.execute("INSERT INTO orders(order_id,customer_id,order_date,order_month,channel,status,order_total) VALUES(?,1,?,?,'channel','completed',100)", [order_id, day, day.replace(day=1)])
        conn.execute("INSERT INTO order_items(order_id,product_id,quantity,unit_price,unit_cost) VALUES(?,1,1,100,0)", [order_id])
        conn.execute("INSERT INTO marketing_spend(spend_date,month_start,channel,spend,acquisition_spend,retention_spend) VALUES(?,?,'channel',0,0,0)", [day, day.replace(day=1)])
    conn.execute("INSERT INTO refunds(order_id,refund_date,refund_amount,status) VALUES(1,?,5,'completed'),(2,?,50,'completed'),(2,?,30,'processing')", [first + dt.timedelta(days=89), first + dt.timedelta(days=90), first + dt.timedelta(days=89)])
    result = query(conn, bi_queries.CUSTOMER_VALUE)
    values = dict(zip(result["columns"], result["rows"][0]))
    assert values["new_customers"] == values["eligible_90d"] == 1
    assert values["sales_90d"] == 200
    assert values["realized_90d_contribution_before_acquisition"] == 195


def test_order_fee_preaggregation_and_category_allocations_restore_totals(database):
    conn = database("ecommerce")
    conn.execute("INSERT INTO products(product_id,category,list_price) VALUES(1,'A',100),(2,'B',50)")
    conn.execute("INSERT INTO orders(order_id,customer_id,order_date,order_month,channel,status,order_total) VALUES(1,1,'2025-10-01','2025-10-01','channel','completed',135)")
    conn.execute("INSERT INTO order_items(order_id,product_id,quantity,unit_price,unit_cost) VALUES(1,1,1,90,40),(1,2,1,45,20)")
    conn.execute("INSERT INTO shipments(shipment_id,order_id) VALUES(1,1),(2,1)")
    conn.execute("INSERT INTO fulfillment_costs(shipment_id,shipping_cost,payment_fee) VALUES(1,3,1),(2,4,2)")
    conn.execute("INSERT INTO refunds(order_id,refund_date,refund_amount,status) VALUES(1,'2025-10-05',5,'completed'),(1,'2025-10-06',7,'completed')")
    conn.execute("INSERT INTO marketing_spend(spend_date,month_start,channel,spend) VALUES('2025-10-01','2025-10-01','channel',10)")
    base = dict(zip((q := query(conn, bi_queries.PERIOD_OVERVIEW))["columns"], q["rows"][0]))
    assert base["orders"] == 1 and base["sales"] == 135 and base["refunds"] == 12
    assert base["fulfillment"] == 7 and base["payment_fees"] == 3 and base["marketing"] == 10
    grouped = query(conn, bi_queries.GROUPED_PROFIT)
    for component in ["gross_sales", "discounts", "refunds", "cogs", "fulfillment", "payment_fees", "marketing"]:
        assert sum(row[grouped["columns"].index(component)] for row in grouped["rows"]) == pytest.approx(base[component])


def test_inventory_known_selling_item_with_no_asof_snapshot_is_not_omitted(database):
    conn = database("retail")
    conn.execute("INSERT INTO stores(store_id,open_date) VALUES(1,'2023-01-01')")
    conn.execute("INSERT INTO products(product_id,product_name,category) VALUES(1,'observed','category'),(2,'unobserved','category')")
    conn.execute("INSERT INTO inventory_snapshots VALUES(1,1,'2025-12-31',5)")
    conn.execute("INSERT INTO transactions(transaction_id,store_id,transaction_date,status) VALUES(1,1,'2025-12-31','completed')")
    conn.execute("INSERT INTO transaction_items(transaction_id,store_id,product_id,quantity) VALUES(1,1,2,100)")
    inputs = query(conn, bi_queries.INVENTORY_ASOF)
    assert {row[inputs["columns"].index("product_id")] for row in inputs["rows"]} == {1, 2}
    missing = next(row for row in inputs["rows"] if row[inputs["columns"].index("product_id")] == 2)
    assert missing[inputs["columns"].index("snapshot_date")] is None


def test_inventory_candidate_union_and_mrr_history_keep_approved_sql_relations():
    scenes = load_scenarios(PROJECT_ROOT / "scenarios")
    for domain, statement in [("retail", bi_queries.INVENTORY_ASOF), ("saas", bi_queries.MRR_MONTHLY)]:
        scene = scenes[domain]
        validate_sql(statement, scene, [t["name"] for t in scene["tables"]], allow_subqueries=True)


def test_refund_reason_amount_conserves_but_distinct_orders_are_not_additive(database):
    conn = database("ecommerce")
    conn.execute("INSERT INTO orders(order_id,order_date,channel,status) VALUES(1,'2025-10-01','channel','completed')")
    conn.execute("INSERT INTO shipments(order_id,promised_date,delivered_date) VALUES(1,'2025-10-03','2025-10-04')")
    conn.execute("INSERT INTO refunds(refund_id,order_id,item_id,refund_date,refund_amount,status) VALUES(1,1,1,'2025-10-05',5,'completed'),(2,1,1,'2025-10-06',7,'completed')")
    conn.execute("INSERT INTO return_cases(refund_id,reason) VALUES(1,'cause-A'),(1,'cause-B'),(2,'cause-C')")
    catalog = load_scenarios(PROJECT_ROOT / "scenarios")["ecommerce"]
    diagnostic = next(d for d in catalog["diagnostics"] if d["id"] == "refund-fulfillment")
    statement = next(q["sql"] for q in diagnostic["queries"] if q["id"] == "refund_reasons")
    result = query(conn, statement)
    assert sum(row[result["columns"].index("refunded_amount")] for row in result["rows"]) == 12
    assert sum(row[result["columns"].index("refunded_orders")] for row in result["rows"]) == 2
    assert conn.execute("SELECT COUNT(DISTINCT order_id) FROM refunds").fetchone()[0] == 1


def test_paid_funnel_day30_inclusive_day31_exclusive_and_retry_deduplicated(database):
    conn = database("saas")
    for account in (1, 2):
        conn.execute("INSERT INTO accounts(account_id,signup_date,acquisition_channel,company_size) VALUES(?,'2025-12-31','channel','small')", [account])
        conn.execute("INSERT INTO feature_events(account_id,event_date,event_type) VALUES(?,'2026-01-02','first_value')", [account])
        conn.execute("INSERT INTO trials(account_id,start_date) VALUES(?,'2026-01-04')", [account])
        conn.execute("INSERT INTO invoices(invoice_id,account_id) VALUES(?,?)", [account, account])
    conn.execute("INSERT INTO payment_attempts(invoice_id,attempt_date,outcome) VALUES(1,'2026-01-29','failed'),(1,'2026-01-30','success'),(1,'2026-01-30','success'),(2,'2026-01-31','success')")
    result = query(conn, bi_queries.FUNNEL)
    values = dict(zip(result["columns"], result["rows"][0]))
    assert values["registered"] == values["activated"] == values["trial_started"] == 2
    assert values["paid"] == 1


def test_acquisition_spend_channel_with_no_first_customers_is_not_silently_dropped(database):
    conn = database("ecommerce")
    conn.execute("INSERT INTO marketing_spend(spend_date,channel,spend,acquisition_spend,retention_spend,month_start) VALUES('2025-01-01','empty-channel',120,100,20,'2025-01-01')")
    inputs = query(conn, bi_queries.CUSTOMER_VALUE)
    assert len(inputs["rows"]) == 1
    values = dict(zip(inputs["columns"], inputs["rows"][0]))
    assert values["channel"] == "empty-channel"
    assert values["new_customers"] == values["eligible_90d"] == 0
    assert values["acquisition_spend"] == 100
    result = run_analysis("customer_value_90d", [inputs])
    assert result["status"] == "ok", result
    assert result["series"][0]["cac"] is None
    assert result["series"][0]["realized_value_90d"] is None
