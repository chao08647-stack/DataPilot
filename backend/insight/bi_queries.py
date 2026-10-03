"""Reusable, inspectable input contracts, not queries selected by question IDs."""

FINANCIALS = """WITH l AS(SELECT i.order_id,SUM(i.quantity*p.list_price) gross_sales,SUM(i.quantity*i.unit_cost) cogs FROM order_items i JOIN products p ON i.product_id=p.product_id GROUP BY i.order_id),
f AS(SELECT h.order_id,SUM(f.shipping_cost) fulfillment,SUM(f.payment_fee) payment_fees FROM shipments h JOIN fulfillment_costs f ON h.shipment_id=f.shipment_id GROUP BY h.order_id),
r AS(SELECT r.order_id,SUM(r.refund_amount) refunds FROM refunds r JOIN orders o ON r.order_id=o.order_id WHERE r.status='completed' AND r.refund_date>=o.order_date AND r.refund_date<=o.order_date+INTERVAL '30 days' AND r.refund_date<=DATE '2026-03-31' GROUP BY r.order_id),
d AS(SELECT order_month,channel,SUM(order_total) month_sales FROM orders WHERE status='completed' GROUP BY order_month,channel),
m AS(SELECT month_start,channel,SUM(spend) spend,SUM(retention_spend) retention_spend FROM marketing_spend GROUP BY month_start,channel),
financials AS(SELECT o.order_id,o.customer_id,o.order_date,o.channel,o.order_total sales,l.gross_sales,l.gross_sales-o.order_total discounts,l.cogs,COALESCE(r.refunds,0) refunds,COALESCE(f.fulfillment,0) fulfillment,COALESCE(f.payment_fees,0) payment_fees,
COALESCE(m.spend*o.order_total/NULLIF(d.month_sales,0),0) marketing,COALESCE(m.retention_spend*o.order_total/NULLIF(d.month_sales,0),0) retention_marketing
FROM orders o JOIN l ON o.order_id=l.order_id LEFT JOIN f ON o.order_id=f.order_id LEFT JOIN r ON o.order_id=r.order_id JOIN d ON o.order_month=d.order_month AND o.channel=d.channel LEFT JOIN m ON o.order_month=m.month_start AND o.channel=m.channel WHERE o.status='completed')"""

PROFIT = "sales-cogs-refunds-fulfillment-payment_fees-marketing"
PERIOD_OVERVIEW = FINANCIALS + f" SELECT strftime(order_date,'%Y-%m') period,COUNT(*) orders,SUM(sales) sales,SUM(gross_sales) gross_sales,SUM(discounts) discounts,SUM(refunds) refunds,SUM(cogs) cogs,SUM(fulfillment) fulfillment,SUM(payment_fees) payment_fees,SUM(marketing) marketing,SUM({PROFIT}) contribution_profit FROM financials WHERE order_date>=DATE '2025-01-01' AND order_date<DATE '2026-01-01' GROUP BY 1 ORDER BY 1"

GROUPED_PROFIT = FINANCIALS + """,category_lines AS(SELECT i.order_id,p.category,SUM(i.quantity*p.list_price) category_gross,SUM(i.quantity*i.unit_cost) category_cogs FROM order_items i JOIN products p ON i.product_id=p.product_id GROUP BY i.order_id,p.category)
SELECT CASE WHEN f.order_date<DATE '2025-10-01' THEN '2025-Q3' ELSE '2025-Q4' END period,f.channel,c.category,
SUM(c.category_gross) gross_sales,SUM(f.discounts*c.category_gross/f.gross_sales) discounts,SUM(f.refunds*c.category_gross/f.gross_sales) refunds,SUM(c.category_cogs) cogs,
SUM(f.fulfillment*c.category_gross/f.gross_sales) fulfillment,SUM(f.payment_fees*c.category_gross/f.gross_sales) payment_fees,SUM(f.marketing*c.category_gross/f.gross_sales) marketing
FROM financials f JOIN category_lines c ON f.order_id=c.order_id WHERE f.order_date>=DATE '2025-07-01' AND f.order_date<DATE '2026-01-01' GROUP BY 1,2,3 ORDER BY 1,2,3"""

CUSTOMER_VALUE = FINANCIALS + """,first_purchase AS(SELECT customer_id,MIN(order_date) first_date FROM orders WHERE status='completed' GROUP BY customer_id),
value90 AS(SELECT f.customer_id,SUM(f.sales) sales_90d,SUM(f.sales-f.cogs-f.fulfillment-f.payment_fees-f.retention_marketing) before_refund_90d FROM financials f JOIN first_purchase p ON f.customer_id=p.customer_id WHERE f.order_date>=p.first_date AND f.order_date<p.first_date+INTERVAL '90 days' GROUP BY f.customer_id),
refund90 AS(SELECT o.customer_id,SUM(r.refund_amount) refund_90d FROM refunds r JOIN orders o ON r.order_id=o.order_id JOIN first_purchase p ON o.customer_id=p.customer_id WHERE o.status='completed' AND r.status='completed' AND o.order_date>=p.first_date AND o.order_date<p.first_date+INTERVAL '90 days' AND r.refund_date>=p.first_date AND r.refund_date<p.first_date+INTERVAL '90 days' GROUP BY o.customer_id),
cohort AS(SELECT a.acquisition_channel channel,COUNT(*) new_customers,COUNT(*) FILTER(WHERE p.first_date+INTERVAL '89 days'<=DATE '2026-03-31') eligible_90d,
SUM(CASE WHEN p.first_date+INTERVAL '89 days'<=DATE '2026-03-31' THEN v.sales_90d ELSE 0 END) sales_90d,
SUM(CASE WHEN p.first_date+INTERVAL '89 days'<=DATE '2026-03-31' THEN v.before_refund_90d-COALESCE(r.refund_90d,0) ELSE 0 END) realized_90d_contribution_before_acquisition
FROM customer_acquisition a JOIN first_purchase p ON a.customer_id=p.customer_id JOIN value90 v ON a.customer_id=v.customer_id LEFT JOIN refund90 r ON a.customer_id=r.customer_id
WHERE p.first_date>=DATE '2025-01-01' AND p.first_date<DATE '2026-01-01' GROUP BY a.acquisition_channel),
spend AS(SELECT channel,SUM(acquisition_spend) acquisition_spend FROM marketing_spend WHERE spend_date>=DATE '2025-01-01' AND spend_date<DATE '2026-01-01' GROUP BY channel)
SELECT s.channel,COALESCE(c.new_customers,0) new_customers,COALESCE(c.eligible_90d,0) eligible_90d,COALESCE(c.sales_90d,0) sales_90d,COALESCE(c.realized_90d_contribution_before_acquisition,0) realized_90d_contribution_before_acquisition,s.acquisition_spend FROM spend s LEFT JOIN cohort c ON c.channel=s.channel ORDER BY s.channel"""

CUSTOMER_COHORTS = FINANCIALS + f""",first_purchase AS(SELECT customer_id,MIN(order_date) first_date FROM orders WHERE status='completed' GROUP BY customer_id),
report AS(SELECT customer_id,SUM(sales) sales,SUM({PROFIT}) contribution_profit FROM financials WHERE order_date>=DATE '2025-01-01' AND order_date<DATE '2026-01-01' GROUP BY customer_id),
repeat90 AS(SELECT o.customer_id,COUNT(*) orders_90d FROM orders o JOIN first_purchase p ON o.customer_id=p.customer_id WHERE o.status='completed' AND o.order_date>=p.first_date AND o.order_date<p.first_date+INTERVAL '90 days' GROUP BY o.customer_id)
SELECT p.customer_id,p.first_date,CASE WHEN p.first_date>=DATE '2025-01-01' THEN 'new' ELSE 'existing' END customer_kind,COALESCE(r.sales,0) sales,COALESCE(r.contribution_profit,0) contribution_profit,
CASE WHEN p.first_date+INTERVAL '89 days'<=DATE '2026-03-31' THEN 1 ELSE 0 END eligible_90d,CASE WHEN z.orders_90d>=2 THEN 1 ELSE 0 END repurchased_90d
FROM first_purchase p LEFT JOIN report r ON p.customer_id=r.customer_id LEFT JOIN repeat90 z ON p.customer_id=z.customer_id WHERE p.first_date<DATE '2026-01-01' ORDER BY p.customer_id"""

FUNNEL = """WITH f AS(SELECT account_id,MIN(event_date) first_value FROM feature_events WHERE event_type='first_value' GROUP BY account_id),t AS(SELECT account_id,MIN(start_date) trial_started FROM trials GROUP BY account_id),p AS(SELECT i.account_id,MIN(a.attempt_date) paid_date FROM invoices i JOIN payment_attempts a ON i.invoice_id=a.invoice_id WHERE a.outcome='success' GROUP BY i.account_id)
SELECT CASE WHEN a.signup_date<DATE '2025-10-01' THEN '2025-Q3' ELSE '2025-Q4' END period,a.acquisition_channel||' / '||a.company_size AS "group",a.acquisition_channel channel,a.company_size,
COUNT(*) registered,COUNT(*) FILTER(WHERE f.first_value>=a.signup_date AND f.first_value<=a.signup_date+INTERVAL '30 days') activated,
COUNT(*) FILTER(WHERE f.first_value>=a.signup_date AND t.trial_started>=f.first_value AND t.trial_started<=a.signup_date+INTERVAL '30 days') trial_started,
COUNT(*) FILTER(WHERE f.first_value>=a.signup_date AND t.trial_started>=f.first_value AND p.paid_date>=t.trial_started AND p.paid_date<=a.signup_date+INTERVAL '30 days') paid
FROM accounts a LEFT JOIN f ON a.account_id=f.account_id LEFT JOIN t ON a.account_id=t.account_id LEFT JOIN p ON a.account_id=p.account_id
WHERE a.signup_date>=DATE '2025-07-01' AND a.signup_date<DATE '2026-01-01' AND a.signup_date+INTERVAL '30 days'<=DATE '2026-03-31' GROUP BY 1,2,3,4 ORDER BY 1,2"""

MRR_MONTHLY = """WITH history AS(SELECT a.account_id,MIN(s.start_date) first_started FROM accounts a JOIN subscriptions s ON a.account_id=s.account_id GROUP BY a.account_id)
SELECT strftime(c.calendar_date,'%Y-%m-%d') period,v.account_id,SUM(v.monthly_price) mrr,MIN(h.first_started) first_started
FROM business_calendar c JOIN subscription_versions v ON c.calendar_date>=v.effective_from AND(c.calendar_date<v.effective_to OR v.effective_to IS NULL) JOIN history h ON v.account_id=h.account_id
WHERE c.is_month_end AND c.calendar_date>=DATE '2024-12-31' AND c.calendar_date<=DATE '2025-12-31' GROUP BY c.calendar_date,v.account_id ORDER BY c.calendar_date,v.account_id"""

RETENTION = """WITH activity AS(SELECT u.account_id,e.event_date FROM users u JOIN events e ON u.user_id=e.user_id WHERE e.event_name IN('report_created','workflow_run','data_viewed')),
active AS(SELECT a.account_id,MAX(CASE WHEN e.event_date>=a.signup_date+INTERVAL '24 days' AND e.event_date<=a.signup_date+INTERVAL '30 days' THEN 1 ELSE 0 END) r30,MAX(CASE WHEN e.event_date>=a.signup_date+INTERVAL '54 days' AND e.event_date<=a.signup_date+INTERVAL '60 days' THEN 1 ELSE 0 END) r60,MAX(CASE WHEN e.event_date>=a.signup_date+INTERVAL '84 days' AND e.event_date<=a.signup_date+INTERVAL '90 days' THEN 1 ELSE 0 END) r90 FROM accounts a LEFT JOIN activity e ON a.account_id=e.account_id GROUP BY a.account_id),
k AS(SELECT account_id,MIN(event_date) first_key FROM feature_events WHERE event_type='key_feature' AND feature_name='automation_saved' GROUP BY account_id)
SELECT strftime(a.signup_date,'%Y-%m') cohort,CASE WHEN k.first_key>=a.signup_date AND k.first_key<a.signup_date+INTERVAL '7 days' THEN 'early_key_feature' ELSE 'no_early_key_feature' END feature_group,COUNT(*) accounts,
COUNT(*) FILTER(WHERE a.signup_date+INTERVAL '30 days'<=DATE '2026-03-31') eligible_30,COUNT(*) FILTER(WHERE a.signup_date+INTERVAL '30 days'<=DATE '2026-03-31' AND v.r30=1) retained_30,
COUNT(*) FILTER(WHERE a.signup_date+INTERVAL '60 days'<=DATE '2026-03-31') eligible_60,COUNT(*) FILTER(WHERE a.signup_date+INTERVAL '60 days'<=DATE '2026-03-31' AND v.r60=1) retained_60,
COUNT(*) FILTER(WHERE a.signup_date+INTERVAL '90 days'<=DATE '2026-03-31') eligible_90,COUNT(*) FILTER(WHERE a.signup_date+INTERVAL '90 days'<=DATE '2026-03-31' AND v.r90=1) retained_90
FROM accounts a JOIN active v ON a.account_id=v.account_id LEFT JOIN k ON a.account_id=k.account_id WHERE a.signup_date>=DATE '2025-01-01' AND a.signup_date<DATE '2026-01-01' GROUP BY 1,2 ORDER BY 1,2"""

STORE_DRIVERS = """WITH s AS(SELECT store_id,transaction_date,COUNT(*) transactions,SUM(transaction_total) sales FROM transactions WHERE status='completed' GROUP BY store_id,transaction_date)
SELECT CASE WHEN f.visit_date<DATE '2025-10-01' THEN '2025-Q3' ELSE '2025-Q4' END period,f.store_id,COUNT(*) FILTER(WHERE c.is_open) open_days,SUM(f.visitors) visitors,SUM(COALESCE(s.transactions,0)) transactions,SUM(COALESCE(s.sales,0)) sales
FROM footfall f JOIN store_calendar c ON f.store_id=c.store_id AND f.visit_date=c.calendar_date JOIN stores d ON f.store_id=d.store_id LEFT JOIN s ON f.store_id=s.store_id AND f.visit_date=s.transaction_date
WHERE f.visit_date>=DATE '2025-07-01' AND f.visit_date<DATE '2026-01-01' AND d.open_date<=DATE '2025-07-01' AND(d.closed_date IS NULL OR d.closed_date>DATE '2025-12-31') GROUP BY 1,2 ORDER BY 1,2"""

STORE_CATEGORIES = """SELECT t.store_id,t.transaction_date,p.category,SUM(i.quantity*i.unit_price) sales,SUM(i.quantity) units FROM transactions t JOIN transaction_items i ON t.transaction_id=i.transaction_id JOIN products p ON i.product_id=p.product_id JOIN stores s ON t.store_id=s.store_id WHERE t.status='completed' AND t.transaction_date>=DATE '2025-07-01' AND t.transaction_date<DATE '2026-01-01' AND s.open_date<=DATE '2025-07-01' AND(s.closed_date IS NULL OR s.closed_date>DATE '2025-12-31') GROUP BY 1,2,3 ORDER BY 1,2,3"""

INVENTORY_ASOF = """WITH ranked AS(SELECT store_id,product_id,snapshot_date,stock_on_hand,ROW_NUMBER() OVER(PARTITION BY store_id,product_id ORDER BY snapshot_date DESC) rn FROM inventory_snapshots WHERE snapshot_date<=DATE '2025-12-31'),
sold AS(SELECT t.store_id,i.product_id,SUM(i.quantity) sold_30d FROM transaction_items i JOIN transactions t ON i.transaction_id=t.transaction_id WHERE t.status='completed' AND t.transaction_date>=DATE '2025-12-02' AND t.transaction_date<=DATE '2025-12-31' GROUP BY t.store_id,i.product_id),
received AS(SELECT po_id,SUM(quantity) received_qty FROM goods_receipts WHERE received_date<=DATE '2025-12-31' GROUP BY po_id),
po AS(SELECT p.store_id,p.product_id,SUM(GREATEST(p.ordered_qty-COALESCE(r.received_qty,0),0)) pending_po,SUM(CASE WHEN p.expected_date<=DATE '2026-01-14' THEN GREATEST(p.ordered_qty-COALESCE(r.received_qty,0),0) ELSE 0 END) due_po_14d,COUNT(*) FILTER(WHERE p.expected_date<DATE '2025-12-31' AND p.ordered_qty>COALESCE(r.received_qty,0)) overdue_po FROM purchase_orders p LEFT JOIN received r ON p.po_id=r.po_id WHERE p.ordered_date<=DATE '2025-12-31' GROUP BY p.store_id,p.product_id),
incoming AS(SELECT to_store_id store_id,product_id,SUM(quantity) transfer_in_due_14d FROM inventory_transfers WHERE created_date<=DATE '2025-12-31' AND transfer_date<=DATE '2025-12-31' AND(received_date IS NULL OR received_date>DATE '2025-12-31') AND expected_arrival_date<=DATE '2026-01-14' GROUP BY to_store_id,product_id),
candidate_keys AS(SELECT store_id,product_id FROM ranked WHERE rn=1 UNION SELECT store_id,product_id FROM sold UNION SELECT store_id,product_id FROM po UNION SELECT store_id,product_id FROM incoming),
objects AS(SELECT d.store_id,p.product_id,p.product_name,p.category FROM candidate_keys k JOIN stores d ON k.store_id=d.store_id JOIN products p ON k.product_id=p.product_id WHERE d.open_date<=DATE '2025-12-31' AND(d.closed_date IS NULL OR d.closed_date>DATE '2025-12-31'))
SELECT DATE '2025-12-31' as_of_date,c.store_id,c.product_id,c.product_name,c.category,r.snapshot_date,r.stock_on_hand,COALESCE(s.sold_30d,0) sold_30d,COALESCE(o.pending_po,0) pending_po,COALESCE(o.due_po_14d,0) due_po_14d,COALESCE(o.overdue_po,0) overdue_po,COALESCE(i.transfer_in_due_14d,0) transfer_in_due_14d
FROM objects c LEFT JOIN ranked r ON c.store_id=r.store_id AND c.product_id=r.product_id AND r.rn=1 LEFT JOIN sold s ON c.store_id=s.store_id AND c.product_id=s.product_id LEFT JOIN po o ON c.store_id=o.store_id AND c.product_id=o.product_id LEFT JOIN incoming i ON c.store_id=i.store_id AND c.product_id=i.product_id
ORDER BY c.store_id,c.product_id"""
