"""Test-only expected values from raw observations, never imported by the app.

No application query, metric calculator, generator or model is used here. Each
raw table is read once; Python keys and loops implement the accounting and
cohort definitions independently. The helper accepts a connection for tiny
adversarial fixtures, or read-only paths to the actual synthetic databases.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import duckdb

AS_OF = date(2026, 3, 31)
YEAR_START, YEAR_END = date(2025, 1, 1), date(2026, 1, 1)
FIELDS = ("orders", "sales", "gross_sales", "discounts", "refunds", "cogs",
          "fulfillment", "payment_fees", "marketing", "contribution_profit")


def _records(conn, name):
    # All identifiers below are literal test-owned raw table names, not inputs.
    cursor = conn.execute(f'SELECT * FROM "{name}"')
    columns = [field[0] for field in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _rounded(value):
    if isinstance(value, dict):
        return {key: _rounded(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_rounded(item) for item in value]
    return round(value, 6) if isinstance(value, float) else value


def _totals(rows):
    result = {key: sum(row.get(key, 0) for row in rows) for key in FIELDS}
    result["aov"] = result["sales"] / result["orders"] if result["orders"] else None
    return result


def ecommerce_gold(conn):
    """Return q01..q05, with stable keys matching analytical output columns."""
    orders = {row["order_id"]: row for row in _records(conn, "orders")
              if row["status"] == "completed"}
    products = {row["product_id"]: row for row in _records(conn, "products")}
    items = _records(conn, "order_items")
    refunds = [row for row in _records(conn, "refunds") if row["status"] == "completed"]
    shipments = _records(conn, "shipments")
    spends = _records(conn, "marketing_spend")
    acquisition = {row["customer_id"]: row["acquisition_channel"]
                   for row in _records(conn, "customer_acquisition")}
    first, order_groups, monthly_sales = {}, defaultdict(list), defaultdict(float)
    for order in orders.values():
        customer, day = order["customer_id"], order["order_date"]
        first[customer] = min(first.get(customer, day), day)
        order_groups[customer].append(order)
        monthly_sales[(day.replace(day=1), order["channel"])] += order["order_total"]

    ledger = {key: {"orders": 1, "sales": row["order_total"], "gross_sales": 0.,
                   "cogs": 0., "refunds": 0., "fulfillment": 0., "payment_fees": 0.}
              for key, row in orders.items()}
    category_items = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
    for item in items:
        identifier = item["order_id"]
        if identifier not in ledger:
            continue
        product = products[item["product_id"]]
        gross = item["quantity"] * product["list_price"]
        cost = item["quantity"] * item["unit_cost"]
        ledger[identifier]["gross_sales"] += gross
        ledger[identifier]["cogs"] += cost
        group = category_items[identifier][product["category"]]
        group["gross_sales"] += gross
        group["cogs"] += cost
    shipment_orders = {row["shipment_id"]: row["order_id"] for row in shipments}
    for fee in _records(conn, "fulfillment_costs"):
        identifier = shipment_orders.get(fee["shipment_id"])
        if identifier in ledger:
            ledger[identifier]["fulfillment"] += fee["shipping_cost"]
            ledger[identifier]["payment_fees"] += fee["payment_fee"]
    refund_ids_by_order = defaultdict(list)
    for refund in refunds:
        identifier = refund["order_id"]
        if identifier not in orders:
            continue
        day = orders[identifier]["order_date"]
        if day <= refund["refund_date"] <= min(day + timedelta(days=30), AS_OF):
            ledger[identifier]["refunds"] += refund["refund_amount"]
            refund_ids_by_order[identifier].append(refund)

    monthly_spend = defaultdict(lambda: defaultdict(float))
    acquisition_spend = defaultdict(float)
    for spend in spends:
        key = (spend["spend_date"].replace(day=1), spend["channel"])
        monthly_spend[key]["marketing"] += spend["spend"]
        monthly_spend[key]["retention_marketing"] += spend["retention_spend"]
        if YEAR_START <= spend["spend_date"] < YEAR_END:
            acquisition_spend[spend["channel"]] += spend["acquisition_spend"]
    for identifier, values in ledger.items():
        order = orders[identifier]
        key = (order["order_date"].replace(day=1), order["channel"])
        share = values["sales"] / monthly_sales[key]
        for column in ("marketing", "retention_marketing"):
            values[column] = monthly_spend[key][column] * share
        values["discounts"] = values["gross_sales"] - values["sales"]
        values["contribution_profit"] = values["sales"] - sum(
            values[k] for k in ("cogs", "refunds", "fulfillment", "payment_fees", "marketing"))

    report_ids = [key for key, order in orders.items()
                  if YEAR_START <= order["order_date"] < YEAR_END]
    monthly = defaultdict(list)
    for key in report_ids:
        monthly[orders[key]["order_date"].strftime("%Y-%m")].append(ledger[key])
    series = [{"period": period, **_totals(values)} for period, values in sorted(monthly.items())]
    q01 = {"series": series, "annual_summary": _totals([ledger[key] for key in report_ids])}

    period_totals, detail = defaultdict(list), defaultdict(lambda: defaultdict(float))
    for key in report_ids:
        order, value = orders[key], ledger[key]
        if order["order_date"].month < 7:
            continue
        period = "2025-Q3" if order["order_date"].month < 10 else "2025-Q4"
        period_totals[period].append(value)
        for category, component in category_items[key].items():
            output = detail[(period, order["channel"], category)]
            output["gross_sales"] += component["gross_sales"]
            output["cogs"] += component["cogs"]
            share = component["gross_sales"] / value["gross_sales"]
            for column in ("discounts", "refunds", "fulfillment", "payment_fees", "marketing"):
                output[column] += value[column] * share
    q02 = {"periods": {period: _totals(values) for period, values in sorted(period_totals.items())},
           "detail": [{"period": p, "channel": c, "category": g, **values}
                      for (p, c, g), values in sorted(detail.items())]}
    q02["sales_delta"] = q02["periods"]["2025-Q4"]["sales"] - q02["periods"]["2025-Q3"]["sales"]
    q02["profit_delta"] = (q02["periods"]["2025-Q4"]["contribution_profit"]
                           - q02["periods"]["2025-Q3"]["contribution_profit"])

    refunds90 = defaultdict(float)
    for refund in refunds:
        order = orders.get(refund["order_id"])
        if order is None:
            continue
        customer = order["customer_id"]
        start, end = first[customer], first[customer] + timedelta(days=90)
        if start <= order["order_date"] < end and start <= refund["refund_date"] < end:
            refunds90[customer] += refund["refund_amount"]
    channels = {channel: {"channel": channel, "new_customers": 0, "eligible_90d": 0,
                         "sales_90d": 0., "realized_90d_contribution_before_acquisition": 0.,
                         "acquisition_spend": amount}
                for channel, amount in acquisition_spend.items()}
    for customer, start in first.items():
        if not YEAR_START <= start < YEAR_END:
            continue
        channel = acquisition[customer]
        row = channels.setdefault(channel, {"channel": channel, "new_customers": 0,
                                            "eligible_90d": 0, "sales_90d": 0.,
                                            "realized_90d_contribution_before_acquisition": 0.,
                                            "acquisition_spend": 0.})
        row["new_customers"] += 1
        if start + timedelta(days=89) > AS_OF:
            continue
        row["eligible_90d"] += 1
        contribution = -refunds90[customer]
        for order in order_groups[customer]:
            if start <= order["order_date"] < start + timedelta(days=90):
                value = ledger[order["order_id"]]
                row["sales_90d"] += value["sales"]
                contribution += value["sales"] - sum(value[k] for k in (
                    "cogs", "fulfillment", "payment_fees", "retention_marketing"))
        row["realized_90d_contribution_before_acquisition"] += contribution
    for row in channels.values():
        row["cac"] = row["acquisition_spend"] / row["new_customers"] if row["new_customers"] else None
        row["realized_value_90d"] = (row["realized_90d_contribution_before_acquisition"]
                                     / row["eligible_90d"] if row["eligible_90d"] else None)
    q03 = {"series": [channels[channel] for channel in sorted(channels)]}

    customers = defaultdict(lambda: {"customers": 0, "sales": 0., "contribution_profit": 0.})
    cohorts = defaultdict(lambda: {"customers": 0, "eligible_90d": 0, "repurchased_90d": 0})
    for customer, start in first.items():
        if start >= YEAR_END:
            continue
        kind = "new" if start >= YEAR_START else "existing"
        customers[kind]["customers"] += 1
        for order in order_groups[customer]:
            if YEAR_START <= order["order_date"] < YEAR_END:
                for key in ("sales", "contribution_profit"):
                    customers[kind][key] += ledger[order["order_id"]][key]
        if kind == "new":
            row = cohorts[start.strftime("%Y-%m")]
            row["customers"] += 1
            if start + timedelta(days=89) <= AS_OF:
                row["eligible_90d"] += 1
                row["repurchased_90d"] += sum(
                    start <= order["order_date"] < start + timedelta(days=90)
                    for order in order_groups[customer]) >= 2
    q05 = {"new_existing": [{"customer_kind": kind, **row} for kind, row in sorted(customers.items())],
           "series": [{"cohort": cohort, **row, "repurchase_pct":
                       row["repurchased_90d"] * 100 / row["eligible_90d"] if row["eligible_90d"] else None}
                      for cohort, row in sorted(cohorts.items())]}
    unallocated = [{"month": month.isoformat(), "channel": channel, "spend": values["marketing"]}
                   for (month, channel), values in monthly_spend.items()
                   if YEAR_START <= month < YEAR_END and not monthly_sales[(month, channel)]
                   and values["marketing"]]
    recorded = sum(values["marketing"] for (month, _), values in monthly_spend.items()
                   if YEAR_START <= month < YEAR_END)
    coverage = {"unallocated_2025_month_channels": unallocated, "recorded_2025_marketing": recorded,
                "allocated_2025_marketing": q01["annual_summary"]["marketing"],
                "acquisition_channels_without_first_customers": [row["channel"] for row in channels.values()
                                                                 if not row["new_customers"]]}
    q04 = _refund_gold(conn, orders, shipments, refund_ids_by_order, products)
    return _rounded({"q01": q01, "q02": q02, "q03": q03, "q04": q04, "q05": q05,
                     "coverage_checks": coverage})


def _refund_gold(conn, orders, shipments, refunds_by_order, products):
    packages, reasons = defaultdict(list), defaultdict(set)
    for shipment in shipments:
        packages[shipment["order_id"]].append(shipment)
    for case in _records(conn, "return_cases"):
        if case["reason"] is not None:
            reasons[case["refund_id"]].add(case["reason"])
    suppliers = {row["supplier_id"]: row["supplier_name"] for row in _records(conn, "suppliers")}
    batches = {row["batch_id"]: row for row in _records(conn, "product_batches")}
    labels = defaultdict(lambda: {"supplier": set(), "product": set()})
    for mapping in _records(conn, "order_item_batches"):
        batch = batches.get(mapping["batch_id"])
        if batch:
            if batch["supplier_id"] in suppliers:
                labels[mapping["item_id"]]["supplier"].add(batch["supplier_id"])
            if batch["product_id"] in products:
                labels[mapping["item_id"]]["product"].add(batch["product_id"])

    def label(values, names, multiple):
        return names[next(iter(values))] if len(values) == 1 else multiple if values else "未记录"

    rows, details = defaultdict(lambda: defaultdict(int)), {}
    for identifier, order in orders.items():
        day = order["order_date"]
        if not date(2025, 10, 1) <= day < YEAR_END or day + timedelta(days=30) > AS_OF:
            continue
        row = rows[order["channel"]]
        row["mature_orders"] += 1
        observed = packages[identifier]
        known = bool(observed) and all(p["promised_date"] is not None and p["delivered_date"] is not None
                                       and p["delivered_date"] <= AS_OF for p in observed)
        row["unknown_fulfillment_orders"] += not known
        if not known:
            continue
        late = any(p["delivered_date"] > p["promised_date"] for p in observed)
        successful = refunds_by_order[identifier]
        row["orders"] += 1
        row["refunded_orders"] += bool(successful)
        row["late_orders"] += late
        row["late_refunded_orders"] += late and bool(successful)
        for refund in successful:
            groups = labels[refund["item_id"]]
            supplier = label(groups["supplier"], suppliers, "多供应商")
            product = label(groups["product"], {key: value["product_name"] for key, value in products.items()}, "多商品")
            reason = label(reasons[refund["refund_id"]], {key: key for key in reasons[refund["refund_id"]]}, "多原因")
            key = (order["channel"], supplier, product, reason)
            target = details.setdefault(key, {"order_ids": set(), "refund_count": 0, "refunded_amount": 0.})
            target["order_ids"].add(identifier)
            target["refund_count"] += 1
            target["refunded_amount"] += refund["refund_amount"]
    detail = [{"channel": c, "supplier_name": s, "product_name": p, "reason": r,
               "refunded_orders": len(v["order_ids"]), "refund_count": v["refund_count"],
               "refunded_amount": v["refunded_amount"]} for (c, s, p, r), v in sorted(details.items())]
    return {"series": [{"group": channel, **dict(row)} for channel, row in sorted(rows.items())],
            "refund_reasons": detail, "refunded_amount": sum(row["refunded_amount"] for row in detail),
            "refund_count": sum(row["refund_count"] for row in detail)}


def compute_gold(paths: dict[str, Path]) -> dict:
    with duckdb.connect(str(paths["ecommerce"]), read_only=True) as connection:
        return ecommerce_gold(connection)
