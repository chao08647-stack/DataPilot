"""Independent, reproducible synthetic business data and trusted seed validation.

No parent-project code, configuration or data is read. Existing databases are never
replaced automatically: schema mismatches require an explicit new data directory.
"""
from __future__ import annotations

import calendar
import datetime as dt
import json
import math
import os
import random
import re
import tempfile
from pathlib import Path
from typing import Any

import duckdb

START = dt.date(2024, 1, 1)
END = dt.date(2025, 12, 31)


def load_scenarios(root: Path, *, profile: str | None = None) -> dict[str, dict]:
    result = {}
    for path in sorted(Path(root).glob("*/scenario.json")):
        scenario_id = path.parent.name
        with path.open(encoding="utf-8") as handle:
            scenario = json.load(handle)
        extension_path = path.with_name("enterprise.json")
        if extension_path.exists():
            extension = json.loads(extension_path.read_text(encoding="utf-8"))
            for key, value in extension.items():
                if key in {"tables", "metrics", "examples", "evaluation", "business_knowledge", "preferences", "dimensions", "diagnostics", "dashboard_templates"}:
                    identifier = "name" if key == "tables" else "id"
                    combined = {item[identifier]: item for item in scenario.get(key, [])}
                    combined.update({item[identifier]: item for item in value})
                    scenario[key] = list(combined.values())
                elif key == "relations":
                    scenario[key] = scenario.get(key, []) + value
                else:
                    scenario[key] = value
            removed = set(extension.get("remove_business_knowledge", []))
            scenario["business_knowledge"] = [item for item in scenario.get("business_knowledge", []) if item["id"] not in removed]
        # Older published metadata and databases remain available. A BI template
        # is an explicit independent descriptor, not a mutation of old files.
        bi_path = path.with_name("light_bi.json")
        if bi_path.exists() and profile != "enterprise":
            scenario = json.loads(bi_path.read_text(encoding="utf-8"))
        if scenario["id"] != scenario_id:
            raise ValueError(f"Scenario directory/id mismatch: {scenario_id}")
        names = [table["name"] for table in scenario["tables"]]
        if not names or len(names) != len(set(names)):
            raise ValueError("Each template must define unique business table names")
        result[scenario_id] = scenario
    return result


def _days():
    for offset in range((END - START).days + 1):
        yield START + dt.timedelta(days=offset)


def _months():
    for year in (2024, 2025):
        for month in range(1, 13):
            yield dt.date(year, month, 1)


def _insert(db, table: str, rows: list[tuple]) -> None:
    if rows:
        # Trusted bootstrap only, not the model SQL executor. Bind column arrays
        # in bounded batches rather than issuing thousands of single-row inserts.
        projection = ",".join("unnest(?)" for _ in rows[0])
        for offset in range(0, len(rows), 5000):
            columns = [list(values) for values in zip(*rows[offset:offset + 5000])]
            db.execute(f'INSERT INTO "{table}" SELECT {projection}', columns)


def _ecommerce(db):
    rng = random.Random(1101)
    _insert(db, "customers", [(i, f"合成客户{i:03}", ["华东", "华南", "华北"][(i-1)%3], START) for i in range(1, 25)])
    prices = [80.0, 120.0, 200.0, 350.0, 500.0, 650.0]
    costs = [45.0, 70.0, 115.0, 195.0, 290.0, 370.0]
    _insert(db, "products", [(i+1, f"示例商品{i+1}", ["配件", "家居", "数码"][i//2], prices[i], costs[i]) for i in range(6)])
    orders, items, refunds, spends = [], [], [], []
    order_id = item_id = refund_id = 0
    channels = ["自然流量", "付费推广", "平台商城"]
    for day in _days():
        campaign = day >= dt.date(2025, 10, 1)
        for channel_index, channel in enumerate(channels):
            spends.append((day, channel, round((0 if channel_index == 0 else 120+channel_index*30) * (1.7 if campaign else 1), 2)))
            for slot in range(4 if campaign else 2):
                order_id += 1
                status = "cancelled" if order_id % 17 == 0 else "completed"
                total = 0.0
                lines = []
                for line in range(2):
                    item_id += 1
                    product_id = ((day.toordinal() + slot + channel_index + line) % 6) + 1
                    quantity = rng.randint(1, 3)
                    price = round(prices[product_id-1] * (0.90 if campaign else 1), 2)
                    cost = round(costs[product_id-1] * (1.25 if campaign else 1), 2)
                    items.append((item_id, order_id, product_id, quantity, price, cost))
                    total += quantity*price
                    lines.append((item_id, quantity*price))
                orders.append((order_id, (order_id%24)+1, day, channel, status, round(total, 2)))
                if status == "completed" and order_id % 11 == 0:
                    refund_id += 1
                    refund_day = min(day+dt.timedelta(days=3), END)
                    refunds.append((refund_id, order_id, lines[0][0], refund_day, round(lines[0][1]*0.5, 2), "completed" if order_id%3 else "processing"))
    for table, rows in [("orders", orders), ("order_items", items), ("refunds", refunds), ("marketing_spend", spends)]:
        _insert(db, table, rows)


def _saas(db):
    accounts, users, events, subscriptions, changes, invoices = [], [], [], [], [], []
    rng = random.Random(2202)
    event_id = invoice_id = 0
    for month_index, month in enumerate(_months()):
        for slot in range(10):
            account_id = month_index*10+slot+1
            signup = month.replace(day=slot+1)
            accounts.append((account_id, f"合成组织{account_id:03}", signup, ["内容", "零售", "科技"][slot%3], ["自然流量", "推广"][slot%2]))
            for member in range(2):
                user_id = (account_id-1)*2+member+1
                users.append((user_id, account_id, signup, "admin" if member == 0 else "member"))
                for offset in (0, 1, 7, 14, 28, 35, 60, 90, 180, 365):
                    event_day = signup + dt.timedelta(days=offset)
                    if event_day <= END and (offset < 14 or rng.random() > 0.25):
                        event_id += 1
                        events.append((event_id, user_id, event_day, "workspace_open"))
            converts = slot < (3 if month >= dt.date(2025, 10, 1) else 6)
            if not converts:
                continue
            subscription_id = account_id
            start = signup+dt.timedelta(days=7)
            annual = slot%2 == 0
            monthly_price = [100.0, 300.0, 500.0][slot%3]
            end = min(start+dt.timedelta(days=210), END) if account_id%7 == 0 else None
            subscriptions.append((subscription_id, account_id, ["基础版", "专业版", "团队版"][slot%3], start, end, "annual" if annual else "monthly", monthly_price))
            changes.append((len(changes)+1, subscription_id, start, "activated", 0.0, monthly_price))
            if end:
                changes.append((len(changes)+1, subscription_id, end, "cancelled", monthly_price, 0.0))
            for bill_month in _months():
                bill_day = bill_month.replace(day=min(start.day, calendar.monthrange(bill_month.year, bill_month.month)[1]))
                if bill_day < start or (end and bill_day >= end):
                    continue
                elapsed_months = (bill_day.year-start.year)*12+bill_day.month-start.month
                if annual and elapsed_months%12:
                    continue
                invoice_id += 1
                invoices.append((invoice_id, account_id, subscription_id, bill_day, monthly_price*(12 if annual else 1), "paid" if invoice_id%13 else "open"))
    for table, rows in [("accounts", accounts), ("users", users), ("events", events), ("subscriptions", subscriptions), ("subscription_changes", changes), ("invoices", invoices)]:
        _insert(db, table, rows)


def _retail(db):
    rng = random.Random(3303)
    _insert(db, "stores", [(i, f"示例门店{i}", ["华东", "华南", "华北"][(i-1)//2], "标准店", dt.date(2023, 1, 1)) for i in range(1, 7)])
    _insert(db, "products", [(i, f"门店商品{i}", "食品" if i <= 2 else "日用", float(i*20), float(i*11)) for i in range(1, 5)])
    transactions, items, inventory, traffic = [], [], [], []
    transaction_id = item_id = 0
    for day in _days():
        for store_id in range(1, 7):
            weak_period = day >= dt.date(2025, 10, 1) and store_id == 2
            traffic.append((store_id, day, (85 if weak_period else 120)+store_id*5+day.weekday()*3))
            for product_id in range(1, 5):
                # Missing snapshot is deliberately different from an observed zero.
                if store_id == 4 and product_id == 4 and day.day == 15:
                    continue
                stock = 0 if weak_period and product_id == 1 and day.day%3 == 0 else 10+((day.toordinal()+store_id+product_id)%40)
                inventory.append((store_id, product_id, day, stock))
            for slot in range(1 if weak_period else 2):
                transaction_id += 1
                status = "void" if transaction_id%23 == 0 else "completed"
                total = 0.0
                for line in range(2):
                    item_id += 1
                    product_id = (day.toordinal()+store_id+slot+line)%4+1
                    quantity = rng.randint(1, 3)
                    price = float(product_id*20)
                    cost = float(product_id*11)
                    items.append((item_id, transaction_id, product_id, quantity, price, cost))
                    total += quantity*price
                transactions.append((transaction_id, store_id, day, status, total))
    for table, rows in [("transactions", transactions), ("transaction_items", items), ("inventory_snapshots", inventory), ("footfall", traffic)]:
        _insert(db, table, rows)


def _augment_enterprise(db, domain):
    if domain == "ecommerce":
        _enterprise_commerce(db)
    elif domain == "saas":
        _enterprise_saas(db)
    elif domain == "retail":
        _enterprise_retail(db)


def _enterprise_commerce(db):
    _insert(db,"suppliers",[(i,f"合成供应商{i}",["华东","华南","华北"][i-1]) for i in range(1,4)])
    batches=[]
    for product in range(1,7):
        for generation in (0,1):
            batches.append((product+generation*6,product,(product-1)%3+1,START if generation==0 else dt.date(2025,10,1)))
    _insert(db,"product_batches",batches)
    _insert(db,"campaigns",[(i,channel,"常规获客",START,dt.date(2025,9,30)) for i,channel in enumerate(["自然流量","付费推广","平台商城"],1)]+[(i+3,channel,"秋季促销",dt.date(2025,10,1),END) for i,channel in enumerate(["自然流量","付费推广","平台商城"],1)])
    orders=db.execute("SELECT order_id,order_date,channel,order_total FROM orders WHERE status='completed' ORDER BY order_id").fetchall()
    channels={"自然流量":1,"付费推广":2,"平台商城":3}
    item_rows=db.execute("SELECT i.item_id,i.order_id,i.product_id,i.quantity,p.list_price,o.order_date FROM order_items i JOIN products p ON i.product_id=p.product_id JOIN orders o ON i.order_id=o.order_id").fetchall()
    lists={}
    first_item={}
    _insert(db,"order_item_batches",[(i,product+(6 if day>=dt.date(2025,10,1) else 0)) for i,order,product,qty,price,day in item_rows])
    for item,order,product,qty,price,day in item_rows:
        lists[order]=lists.get(order,0)+qty*price
        first_item.setdefault(order,item)
    promotions,shipments,costs=[],[],[]
    late={}
    for order,day,channel,total in orders:
        discount=round(max(lists[order]-total,0),2)
        promotions.append((order,channels[channel]+(3 if day>=dt.date(2025,10,1) else 0),discount))
        delayed=order%(5 if day>=dt.date(2025,10,1) else 23)==0
        promised=day+dt.timedelta(days=2)
        delivered=promised+dt.timedelta(days=3 if delayed else 0)
        shipments.append((order,order,promised,delivered,"合成承运商A" if order%2 else "合成承运商B"))
        costs.append((order,order,round(8+(6 if delayed else 0)+(2 if total>1500 else 0),2),round(total*0.008,2)))
        late[order]=delayed
    _insert(db,"order_promotions",promotions)
    _insert(db,"shipments",shipments)
    _insert(db,"fulfillment_costs",costs)
    existing={row[0] for row in db.execute("SELECT DISTINCT order_id FROM refunds").fetchall()}
    next_id=db.execute("SELECT MAX(refund_id) FROM refunds").fetchone()[0]+1
    extra=[]
    for order,day,channel,total in orders:
        if dt.date(2025,10,1)<=day<=dt.date(2025,11,30) and late[order] and order not in existing:
            extra.append((next_id,order,first_item[order],day+dt.timedelta(days=10),round(total*0.15,2),"completed"))
            next_id+=1
    _insert(db,"refunds",extra)
    cases=[]
    for refund,order,item in db.execute("SELECT refund_id,order_id,item_id FROM refunds ORDER BY refund_id").fetchall():
        cases.append((refund,refund,"履约超时" if late.get(order) else "商品破损" if order%3 else "个人原因", "已受理",item))
    _insert(db,"return_cases",cases)


def _enterprise_saas(db):
    _insert(db,"business_calendar",[(day,day.replace(day=1),day.day==calendar.monthrange(day.year,day.month)[1]) for day in _days()])
    _insert(db,"acquisition_campaigns",[(1,"自然流量","内容注册"),(2,"推广","付费注册")])
    accounts=db.execute("SELECT account_id,signup_date,acquisition_channel FROM accounts ORDER BY account_id").fetchall()
    attributions,events,trials,usage=[],[],[],[]
    for account,day,channel in accounts:
        slot=(account-1)%10
        attributions.append((account,1 if channel=="自然流量" else 2))
        late=day>=dt.date(2025,10,1)
        activated=slot<(5 if late else 8)
        tried=slot<(4 if late else 7)
        if activated:
            events.append((len(events)+1,account,2*account-1,day+dt.timedelta(days=2),"first_value","报告创建"))
        if tried:
            trials.append((account,account,day+dt.timedelta(days=3),day+dt.timedelta(days=17),"completed" if day+dt.timedelta(days=17)<=END else "active"))
        for offset in range(0,min((END-day).days,120)+1,7):
            date=day+dt.timedelta(days=offset)
            unhealthy=account%7==0 and offset>=70
            usage.append((account,date,0 if unhealthy else 2,0 if unhealthy else 10+slot,3 if unhealthy else 0))
    _insert(db,"account_attribution",attributions)
    _insert(db,"feature_events",events)
    _insert(db,"trials",trials)
    _insert(db,"account_usage",usage)
    payments=[]
    for invoice,date,status in db.execute("SELECT invoice_id,invoice_date,status FROM invoices ORDER BY invoice_id").fetchall():
        if invoice%5==0:
            payments.append((len(payments)+1,invoice,date,"failed","银行拒付"))
        payments.append((len(payments)+1,invoice,date+dt.timedelta(days=1),"success" if status=="paid" else "failed","" if status=="paid" else "余额不足"))
    _insert(db,"payment_attempts",payments)
    versions=[]
    changes=[]
    change_id=db.execute("SELECT MAX(change_id) FROM subscription_changes").fetchone()[0]+1
    change_date=dt.date(2025,10,1)
    for sid,account,start,end,price,plan in db.execute("SELECT subscription_id,account_id,start_date,end_date,monthly_price,plan FROM subscriptions ORDER BY subscription_id").fetchall():
        eligible=start<change_date and (end is None or end>change_date)
        delta=100 if account%5==0 else -50 if account%5==1 else 0
        if eligible and delta:
            versions.append((len(versions)+1,sid,account,start,change_date,price,plan,3))
            versions.append((len(versions)+1,sid,account,change_date,end,price+delta,plan,5 if delta>0 else 2))
            changes.append((change_id,sid,change_date,"expanded" if delta>0 else "contracted",price,price+delta))
            change_id+=1
        else:
            versions.append((len(versions)+1,sid,account,start,end,price,plan,3))
        if end and end<dt.date(2025,11,1) and account%2==0:
            versions.append((len(versions)+1,sid,account,dt.date(2025,11,1),None,price,plan,3))
            changes.append((change_id,sid,dt.date(2025,11,1),"reactivated",0,price))
            change_id+=1
    _insert(db,"subscription_versions",versions)
    _insert(db,"subscription_changes",changes)


def _enterprise_retail(db):
    _insert(db,"inventory_periods",[(month.strftime("%Y-%m"),month,month.replace(day=calendar.monthrange(month.year,month.month)[1]),month-dt.timedelta(days=1),calendar.monthrange(month.year,month.month)[1]) for month in _months()])
    _insert(db,"suppliers",[(i,f"合成补货供应商{i}") for i in range(1,3)])
    calendars,staff=[],[]
    for day in _days():
        for store in range(1,7):
            hours=6 if store==2 and day>=dt.date(2025,10,1) else 10
            calendars.append((store,day,True,hours))
            staff.append((store,day,3 if hours==6 else 5,hours*(3 if hours==6 else 5)))
    _insert(db,"store_calendar",calendars)
    _insert(db,"staff_schedules",staff)
    sold={(store,product,date):qty for store,product,date,qty in db.execute("SELECT t.store_id,i.product_id,t.transaction_date,SUM(i.quantity) FROM transactions t JOIN transaction_items i ON t.transaction_id=i.transaction_id WHERE t.status='completed' GROUP BY 1,2,3").fetchall()}
    movements,purchases,receipts,transfers=[],[],[],[]
    balances={(s,p):25 for s in range(1,7) for p in range(1,5)}
    for day in _days():
        for store in range(1,7):
            for product in range(1,5):
                weak=day>=dt.date(2025,10,1) and store==2
                closing=0 if weak and product==1 and day.day%3==0 else 10+((day.toordinal()+store+product)%40)
                quantity=sold.get((store,product,day),0)
                incoming=3 if store==2 and day.day==7 else 0
                outgoing=3 if store==1 and day.day==7 else 0
                if outgoing:
                    transfers.append((len(transfers)+1,1,2,product,day,3))
                need=closing-balances[(store,product)]+quantity-incoming+outgoing
                receipt=max(need,0)
                adjustment=min(need,0)
                if receipt:
                    po=len(purchases)+1
                    delayed=weak and product==1 and day.day%5==0
                    purchases.append((po,store,product,(product-1)%2+1,day-dt.timedelta(days=7),day-dt.timedelta(days=2 if delayed else 0),receipt))
                    receipts.append((po,po,day,receipt))
                for kind,qty in [("receipt",receipt),("sale",quantity),("transfer_in",incoming),("transfer_out",outgoing),("adjustment",adjustment)]:
                    if qty:
                        movements.append((len(movements)+1,store,product,day,kind,qty))
                balances[(store,product)]=closing
    # An auditable stock-ledger discrepancy is observable, not a model knowledge hint.
    movements.append((len(movements)+1,3,2,dt.date(2025,11,20),"adjustment",5))
    _insert(db,"purchase_orders",purchases)
    _insert(db,"goods_receipts",receipts)
    _insert(db,"inventory_transfers",transfers)
    _insert(db,"inventory_movements",movements)


def _valid_database(path: Path, scenario: dict) -> bool:
    try:
        with duckdb.connect(str(path), read_only=True) as db:
            actual = {row[0] for row in db.execute("SHOW TABLES").fetchall()}
            if actual != {table["name"] for table in scenario["tables"]}:
                return False
            for table in scenario["tables"]:
                cols = db.execute(f'DESCRIBE "{table["name"]}"').fetchall()
                if {r[0]: r[1] for r in cols} != table["columns"]:
                    return False
                if not db.execute(f'SELECT count(*) FROM "{table["name"]}"').fetchone()[0]:
                    return False
        return True
    except duckdb.Error:
        return False


def generate_all(scenarios_root: Path, data_dir: Path, *, profile: str | None = None) -> dict[str, Path]:
    scenarios = load_scenarios(scenarios_root, profile=profile)
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    result = {}
    generators = {"ecommerce": _ecommerce, "saas": _saas, "retail": _retail}
    for scenario_id, scenario in scenarios.items():
        version = re.sub(r"[^a-zA-Z0-9_-]", "-", str(scenario["schema_version"]))
        filename = f"{scenario_id}-{version}.duckdb" if scenario.get("enterprise") else f"{scenario_id}.duckdb"
        destination = data_dir / filename
        if destination.exists():
            if not _valid_database(destination, scenario):
                raise FileExistsError(f"Refusing to replace existing invalid scenario database: {destination.name}")
            result[scenario_id] = destination
            continue
        with tempfile.TemporaryDirectory(prefix="insight-seed-", dir=data_dir) as temporary:
            staged = Path(temporary) / destination.name
            with duckdb.connect(str(staged)) as db:
                db.execute("SET threads=4")
                db.execute("BEGIN TRANSACTION")
                for table in scenario["tables"]:
                    columns = ",".join(f'"{name}" {kind}' for name, kind in table["columns"].items())
                    db.execute(f'CREATE TABLE "{table["name"]}" ({columns})')
                if str(scenario.get("data_version", "")).startswith("light-bi-3"):
                    from insight.bi_data import GENERATORS
                    GENERATORS[scenario.get("generator", scenario_id)](db)
                else:
                    generators[scenario_id](db)
                if scenario.get("enterprise") and not str(scenario.get("data_version", "")).startswith("light-bi-3"):
                    _augment_enterprise(db, scenario_id)
                db.execute("COMMIT")
            # A hard link is atomic and fails if another initializer already won.
            try:
                os.link(staged, destination)
            except FileExistsError:
                if not _valid_database(destination, scenario):
                    raise FileExistsError(f"Concurrent initialization produced an invalid database: {destination.name}") from None
        result[scenario_id] = destination
    return result


def _equal_rows(actual, expected) -> bool:
    if len(actual) != len(expected):
        return False
    for left, right in zip(actual, expected):
        if len(left) != len(right):
            return False
        for a, b in zip(left, right):
            if isinstance(b, (int, float)) and not isinstance(b, bool):
                if a is None or not math.isclose(float(a), b, rel_tol=1e-9, abs_tol=1e-6):
                    return False
            elif str(a) != str(b):
                return False
    return True


def validate_repair_seed(seed: dict[str, Any]) -> bool:
    """Execute only trusted checked-in fixtures; never accept fixtures from API/model."""
    try:
        with duckdb.connect(":memory:") as db:
            for statement in seed["fixture_sql"]:
                db.execute(statement)
            fixed = db.execute(seed["fixed_sql"]).fetchall()
            if not _equal_rows(fixed, seed["expected"]):
                return False
            try:
                broken = db.execute(seed["broken_sql"]).fetchall()
            except duckdb.Error:
                return seed["error_category"] != "semantic"
            return seed["error_category"] == "semantic" and not _equal_rows(broken, seed["expected"])
    except (duckdb.Error, KeyError, ValueError, TypeError):
        return False
