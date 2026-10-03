"""Seeded raw observations for the light-BI revision; no stored question answers.

The reporting year is 2025. Q1 2026 observations mature earlier cohorts, but
as-of inventory queries must explicitly exclude observations after their cutoff.
"""
from __future__ import annotations

import calendar
import csv
import datetime as dt
import random
import tempfile
import time
from pathlib import Path

START = dt.date(2024, 1, 1)
END = dt.date(2026, 3, 31)
CHANNELS = ["自然搜索", "内容推荐", "付费搜索", "联盟推广"]


def days(start=START, end=END):
    return [start + dt.timedelta(days=i) for i in range((end-start).days+1)]


def months():
    return [dt.date(year, month, 1) for year in range(2024, 2027) for month in range(1, 13)
            if dt.date(year, month, 1) <= END]


def insert(db, table, rows):
    if not rows:
        return
    began=time.monotonic()
    # Bulk CSV import avoids expensive Python date-list binding on Windows.
    database_path=Path(db.execute("PRAGMA database_list").fetchone()[2])
    with tempfile.TemporaryDirectory(prefix="insight-bulk-",dir=database_path.parent) as directory:
        source=Path(directory)/"rows.csv"
        with source.open("w",encoding="utf-8",newline="") as handle:
            csv.writer(handle).writerows(rows)
        db.execute(f'COPY "{table}" FROM ? (FORMAT CSV, HEADER FALSE)',[str(source)])
    print(f"seed {table}: {len(rows)} rows ({time.monotonic()-began:.2f}s)",flush=True)


def populate_calendar(db):
    insert(db, "business_calendar", [(d, d.replace(day=1), d.day == calendar.monthrange(d.year, d.month)[1]) for d in days()])


def ecommerce(db):
    rng = random.Random(771101)
    populate_calendar(db)
    first = {}
    customers, acquisition = [], []
    for customer in range(1, 2001):
        if customer <= 500:
            first_day = dt.date(2024, 2, 1) + dt.timedelta(days=(customer*37)%300)
        else:
            first_day = dt.date(2025, 1, 1) + dt.timedelta(days=((customer-501)*37)%365)
        first[customer] = first_day
        touch = first_day-dt.timedelta(days=1+customer%10)
        customers.append((customer, f"合成客户{customer:04}", ["华东","华南","华北","西南"][customer%4], touch))
        acquisition.append((customer, touch, CHANNELS[customer%4]))
    insert(db, "customers", customers)
    insert(db, "customer_acquisition", acquisition)
    categories = ["家电数码", "居家日用", "食品", "服饰", "美妆", "运动"]
    products, price, cost = [], {}, {}
    for product in range(1, 121):
        category = (product-1)//20
        price[product] = float([500,120,45,180,160,240][category]+product%20*7)
        cost[product] = round(price[product]*[.68,.55,.58,.48,.38,.52][category],2)
        products.append((product, f"合成商品{product:03}", categories[category], price[product], cost[product]))
    insert(db, "products", products)
    insert(db, "suppliers", [(i,f"合成供货商{i:02}",["华东","华南","华北"][i%3]) for i in range(1,13)])
    insert(db, "product_batches", [(p+120*batch,p,(p-1)%12+1,dt.date(2024,1,1) if batch==0 else dt.date(2025,10,1)) for batch in (0,1) for p in range(1,121)])
    insert(db, "campaigns", [(i+1,c,f"{c}常规获客",START,END) for i,c in enumerate(CHANNELS)])
    # Each customer has a completed first purchase; a real one-time cohort remains.
    schedule = [(date, customer, True) for customer,date in first.items()]
    repeaters = [c for c in first if c%5 != 0]
    eligible_by_date={day:[c for c in repeaters if first[c]<day] for day in days()}
    monthly_weights = [(m, (3 if m.year==2025 else 1)*(2 if m.month>=10 else 1)) for m in months()]
    periods, weights = zip(*monthly_weights)
    for _ in range(58000):
        month = rng.choices(periods,weights=weights,k=1)[0]
        day = month.replace(day=rng.randint(1,calendar.monthrange(month.year,month.month)[1]))
        eligible = eligible_by_date[day]
        if not eligible:
            day = dt.date(2024, 2, 3)
            eligible = eligible_by_date[day]
        schedule.append((day,rng.choice(eligible),False))
    schedule.sort()
    orders,items,refunds,shipments,fees,promotions,mappings,cases=[],[],[],[],[],[],[],[]
    for order_id,(day,customer,is_first) in enumerate(schedule,1):
        acquired=CHANNELS[customer%4]
        channel=acquired if is_first or rng.random()<.65 else rng.choice(CHANNELS)
        status="completed" if is_first or order_id%23 else "cancelled"
        quarter4=day.year==2025 and day.month>=10
        gross=net=0.0
        lines=[]
        for line in range(1+rng.randrange(3)):
            product=rng.randrange(1,121)
            quantity=1+rng.randrange(3)
            discount=(.16 if channel=="付费搜索" else .10) if quarter4 and product<=60 else (.03 if order_id%7==0 else 0)
            unit=round(price[product]*(1-discount),2)
            unit_cost=round(cost[product]*(1.12 if quarter4 and product<=40 else 1),2)
            item_id=len(items)+1
            items.append((item_id,order_id,product,quantity,unit,unit_cost))
            mappings.append((item_id,product+(120 if day>=dt.date(2025,10,1) else 0)))
            gross+=quantity*price[product]
            net+=quantity*unit
            lines.append((item_id,quantity*unit,product))
        orders.append((order_id,customer,day,channel,status,round(net,2),day.replace(day=1)))
        if status!="completed":
            continue
        promotions.append((order_id,CHANNELS.index(channel)+1,round(gross-net,2)))
        any_late=False
        for part in range(2 if order_id%5==0 else 1):
            promised=day+dt.timedelta(days=3+part)
            late=(order_id+part)%(5 if quarter4 and channel=="付费搜索" else 19)==0
            delivered=promised+dt.timedelta(days=4 if late else -1)
            any_late|=late
            shipment_id=len(shipments)+1
            shipments.append((shipment_id,order_id,promised,delivered if delivered<=END else None,f"合成承运商{(order_id+part)%4+1}"))
            fees.append((shipment_id,shipment_id,round(7+net*.001+(4 if late else 0),2),round(net*.006/(2 if order_id%5==0 else 1),2)))
        probability=.22 if any_late else (.10 if quarter4 else .045)
        if rng.random()<probability:
            item_id,line_total,product=lines[0]
            for partial in range(2 if order_id%13==0 else 1):
                refund_date=day+dt.timedelta(days=rng.randrange(3,46)+partial)
                if refund_date>END:
                    continue
                refund_id=len(refunds)+1
                amount=round(line_total*(.2 if order_id%13==0 else .5),2)
                refunds.append((refund_id,order_id,item_id,refund_date,amount,"processing" if refund_id%17==0 else "completed"))
                reason="配送延迟" if any_late else ["规格不符","商品破损","个人原因"][product%3]
                cases.append((len(cases)+1,refund_id,reason,"closed",item_id))
                if refund_id%19==0:
                    cases.append((len(cases)+1,refund_id,"包装问题","closed",item_id))
    for name,rows in [("orders",orders),("order_items",items),("refunds",refunds),("shipments",shipments),("fulfillment_costs",fees),("order_promotions",promotions),("order_item_batches",mappings),("return_cases",cases)]:
        insert(db,name,rows)
    spends=[]
    for day in days():
        for index,channel in enumerate(CHANNELS):
            spend=round([60,280,1300,650][index]*(1.35 if day.year==2025 and day.month>=10 else 1)*(1+(day.day%7)*.015),2)
            acquisition_spend=round(spend*[.25,.65,.82,.72][index],2)
            spends.append((day,channel,spend,acquisition_spend,round(spend-acquisition_spend,2),day.replace(day=1)))
    insert(db,"marketing_spend",spends)


def saas(db):
    rng=random.Random(772202)
    populate_calendar(db)
    accounts,users,events,features,trials,usage,subs,versions,changes,invoices,attempts,attribution=[],[],[],[],[],[],[],[],[],[],[],[]
    acquisition=["自然搜索","内容推荐","付费搜索","伙伴推荐"]
    insert(db,"acquisition_campaigns",[(i+1,c,f"{c}获客") for i,c in enumerate(acquisition)])
    for account in range(1,1001):
        if account<=180:
            signup=dt.date(2024,2,1)+dt.timedelta(days=(account*47)%300)
        elif account<=690:
            signup=dt.date(2025,1,1)+dt.timedelta(days=((account-181)*37)%273)
        else:
            signup=dt.date(2025,10,1)+dt.timedelta(days=((account-691)*17)%92)
        channel=acquisition[account%4]
        size=["小型","中型","小型","大型","小型"][account%5]
        accounts.append((account,f"合成企业{account:04}",signup,["零售","制造","服务","科技"][account%4],channel,size))
        attribution.append((account,account%4+1))
        key_feature=account%5!=0 and rng.random()<.58
        activation_rate=.76 if signup<dt.date(2025,10,1) else (.52 if channel=="付费搜索" else .69)
        activated=rng.random()<activation_rate
        trial=activated and rng.random()<(.85 if size!="小型" else .70)
        paid=trial and rng.random()<(.82 if size=="大型" else .62)
        for member in range(5):
            user=(account-1)*5+member+1
            users.append((user,account,signup,"admin" if member==0 else "member"))
            for age in range(member, (END-signup).days+1,7):
                chance=(.82 if key_feature else .48)*(1 if age<60 else .86 if age<150 else .64)
                if rng.random()<chance:
                    events.append((len(events)+1,user,signup+dt.timedelta(days=age),["report_created","workflow_run","data_viewed"][(account+member+age)%3]))
        if activated:
            features.append((len(features)+1,account,(account-1)*5+1,signup+dt.timedelta(days=2),"first_value","report_created"))
        key_date=signup+dt.timedelta(days=3 if key_feature else 45)
        if key_date<=END and (key_feature or account%3==0):
            features.append((len(features)+1,account,(account-1)*5+1,key_date,"key_feature","automation_saved"))
        if trial:
            trials.append((account,account,signup+dt.timedelta(days=4),signup+dt.timedelta(days=18),"converted" if paid else "expired"))
        for age in range(0,(END-signup).days+1,14):
            usage.append((account,signup+dt.timedelta(days=age),3 if key_feature else 1,12 if key_feature else 3,1 if account%17==0 else 0))
        if not paid:
            continue
        start=signup+dt.timedelta(days=9)
        base={"小型":150.0,"中型":600.0,"大型":2400.0}[size]
        end=start+dt.timedelta(days=120+account%31) if account%9==0 else None
        if end and end>END:
            end=None
        subs.append((account,account,size,start,end,"annual" if account%4==0 else "monthly",base))
        timeline=[(start,base,"activated")]
        change_day=start+dt.timedelta(days=70)
        if change_day<=END and (end is None or change_day<end):
            if account%4==0:
                timeline.append((change_day,round(base*1.4,2),"expanded"))
            elif account%4==1:
                timeline.append((change_day,round(base*.8,2),"contracted"))
        if end:
            timeline.append((end,0.0,"cancelled"))
            restart=end+dt.timedelta(days=45)
            if account%18==0 and restart<=END:
                timeline.append((restart,base,"reactivated"))
        previous=0.0
        for index,(effective,price,kind) in enumerate(timeline):
            changes.append((len(changes)+1,account,effective,kind,previous,price))
            until=timeline[index+1][0] if index+1<len(timeline) else None
            if price:
                versions.append((len(versions)+1,account,account,effective,until,price,size,5))
            previous=price
        for month in months():
            bill=month.replace(day=min(start.day,calendar.monthrange(month.year,month.month)[1]))
            if bill<start or bill>END or (account%4==0 and (bill.year-start.year)*12+bill.month-start.month not in [0,12,24]):
                continue
            prices=[price for effective,price,_ in timeline if effective<=bill]
            value=prices[-1] if prices else 0
            if not value:
                continue
            invoice=len(invoices)+1
            status="open" if invoice%19==0 else "paid"
            invoices.append((invoice,account,account,bill,value*(12 if account%4==0 else 1),status))
            if invoice%7==0:
                attempts.append((len(attempts)+1,invoice,bill,"failed","银行拒付"))
            if bill+dt.timedelta(days=1)<=END:
                attempts.append((len(attempts)+1,invoice,bill+dt.timedelta(days=1),"success" if status=="paid" else "failed","" if status=="paid" else "余额不足"))
    for name,rows in [("accounts",accounts),("users",users),("events",events),("feature_events",features),("trials",trials),("account_usage",usage),("subscriptions",subs),("subscription_versions",versions),("subscription_changes",changes),("invoices",invoices),("payment_attempts",attempts),("account_attribution",attribution)]:
        insert(db,name,rows)


def retail(db):
    rng=random.Random(773303)
    populate_calendar(db)
    stores=[]
    for store in range(1,25):
        opened=dt.date(2023,1,1) if store<=19 or store==24 else {20:dt.date(2025,2,1),21:dt.date(2025,7,1),22:dt.date(2025,10,15),23:dt.date(2025,12,1)}[store]
        closed={19:dt.date(2025,9,20),24:dt.date(2025,11,15)}.get(store)
        stores.append((store,f"合成门店{store:02}",["华东","华南","华北","西南"][(store-1)//6],"旗舰店" if store%6==0 else "标准店",opened,closed))
    insert(db,"stores",stores)
    insert(db,"products",[(p,f"门店商品{p:03}",["食品","饮料","日用","个护"][(p-1)//20],float(10+p%20*4),round((10+p%20*4)*.57,2)) for p in range(1,81)])
    insert(db,"suppliers",[(i,f"合成补货商{i:02}") for i in range(1,9)])
    calendars,staff,traffic,transactions,items=[],[],[],[],[]
    for day in days():
        for store,_,_,_,opened,closed in stores:
            exists=day>=opened and (closed is None or day<closed)
            temporary=store in {2,7} and day.year==2025 and day.month>=10 and day.day%10==0
            is_open=exists and not temporary
            hours=8 if store%3==0 else 10
            calendars.append((store,day,is_open,hours if is_open else 0))
            staff.append((store,day,4 if is_open else 0,(hours*4) if is_open else 0))
            weak=day.year==2025 and day.month>=10 and store in {2,7,17}
            visitors=round((100+store*7+day.weekday()*12)*(.70 if weak and store!=17 else 1)) if is_open else 0
            traffic.append((store,day,visitors))
            count=max(0,round(visitors*(.06 if weak and store in {7,17} else .09)))
            for slot in range(count):
                transaction=len(transactions)+1
                status="void" if transaction%41==0 else "completed"
                total=0.0
                for line in range(1+rng.randrange(3)):
                    product=1+((day.toordinal()+store+slot*3+line*7)%80)
                    quantity=1+rng.randrange(2)
                    price=float(10+product%20*4)
                    total+=quantity*price
                    items.append((len(items)+1,transaction,product,quantity,price,round(price*.57,2),store))
                transactions.append((transaction,store,day,status,total))
    for name,rows in [("store_calendar",calendars),("staff_schedules",staff),("footfall",traffic),("transactions",transactions),("transaction_items",items)]:
        insert(db,name,rows)
    insert(db,"inventory_periods",[(m.strftime("%Y-%m"),m,m.replace(day=calendar.monthrange(m.year,m.month)[1]),m-dt.timedelta(days=1),calendar.monthrange(m.year,m.month)[1]) for m in months()])
    transfers=[]
    for month in months():
        for source in range(1,13):
            for product in (1,21,41,61):
                shipped=month.replace(day=27)
                arrival=shipped+dt.timedelta(days=7)
                transfers.append((len(transfers)+1,source,source+12,product,shipped,12,shipped-dt.timedelta(days=2),arrival,arrival if arrival<=END else None))
    insert(db,"inventory_transfers",transfers)
    # Set-based deterministic daily observations avoid keeping millions of Python tuples.
    db.execute("""INSERT INTO inventory_snapshots
        SELECT s.store_id,p.product_id,c.calendar_date,
          CASE WHEN c.calendar_date>=DATE '2025-10-01' AND s.store_id IN (2,7) AND p.product_id<=5 THEN CASE WHEN day(c.calendar_date)%3=0 THEN 0 ELSE 2 END
               WHEN c.calendar_date>=DATE '2025-09-01' AND p.product_id>=76 THEN 320+p.product_id
               ELSE 12+((date_diff('day',DATE '2024-01-01',c.calendar_date)*3+s.store_id*7+p.product_id*11)%45) END
        FROM stores s CROSS JOIN products p CROSS JOIN business_calendar c
        WHERE c.calendar_date>=s.open_date AND(c.calendar_date<s.closed_date OR s.closed_date IS NULL)
          AND NOT(s.store_id=4 AND p.product_id=4 AND day(c.calendar_date)=15)""")
    # Raw stock movements reconcile with observed balances; missing observations remain missing.
    db.execute("""CREATE TEMP TABLE stock_flow AS WITH sold AS(
        SELECT t.store_id,i.product_id,t.transaction_date event_date,SUM(i.quantity) sold
        FROM transactions t JOIN transaction_items i ON t.transaction_id=i.transaction_id WHERE t.status='completed' GROUP BY 1,2,3),
        incoming AS(SELECT to_store_id store_id,product_id,received_date event_date,SUM(quantity) quantity FROM inventory_transfers WHERE received_date IS NOT NULL GROUP BY 1,2,3),
        outgoing AS(SELECT from_store_id store_id,product_id,transfer_date event_date,SUM(quantity) quantity FROM inventory_transfers GROUP BY 1,2,3),
        balances AS(SELECT store_id,product_id,snapshot_date,stock_on_hand,COALESCE(LAG(stock_on_hand) OVER(PARTITION BY store_id,product_id ORDER BY snapshot_date),25) opening FROM inventory_snapshots)
        SELECT b.*,COALESCE(s.sold,0) sold,COALESCE(i.quantity,0) incoming,COALESCE(o.quantity,0) outgoing,
          b.stock_on_hand-b.opening+COALESCE(s.sold,0)-COALESCE(i.quantity,0)+COALESCE(o.quantity,0) need
        FROM balances b LEFT JOIN sold s ON b.store_id=s.store_id AND b.product_id=s.product_id AND b.snapshot_date=s.event_date
        LEFT JOIN incoming i ON b.store_id=i.store_id AND b.product_id=i.product_id AND b.snapshot_date=i.event_date
        LEFT JOIN outgoing o ON b.store_id=o.store_id AND b.product_id=o.product_id AND b.snapshot_date=o.event_date""")
    db.execute("""INSERT INTO purchase_orders
        SELECT ROW_NUMBER() OVER(ORDER BY snapshot_date,store_id,product_id),store_id,product_id,(product_id-1)%8+1,
               snapshot_date-INTERVAL '7 days',snapshot_date-CASE WHEN store_id IN(2,7) AND product_id<=5 THEN INTERVAL '2 days' ELSE INTERVAL '0 days' END,need
        FROM stock_flow WHERE need>0""")
    db.execute("""INSERT INTO goods_receipts SELECT po_id,po_id,ordered_date+INTERVAL '7 days',ordered_qty FROM purchase_orders""")
    db.execute("""INSERT INTO inventory_movements SELECT ROW_NUMBER() OVER(ORDER BY event_date,store_id,product_id,kind),store_id,product_id,event_date,kind,quantity FROM(
        SELECT store_id,product_id,snapshot_date event_date,'receipt' kind,GREATEST(need,0) quantity FROM stock_flow
        UNION ALL SELECT store_id,product_id,snapshot_date,'sale',sold FROM stock_flow
        UNION ALL SELECT store_id,product_id,snapshot_date,'transfer_in',incoming FROM stock_flow
        UNION ALL SELECT store_id,product_id,snapshot_date,'transfer_out',outgoing FROM stock_flow
        UNION ALL SELECT store_id,product_id,snapshot_date,'adjustment',LEAST(need,0) FROM stock_flow) WHERE quantity<>0""")
    next_po=db.execute("SELECT MAX(po_id)+1 FROM purchase_orders").fetchone()[0]
    insert(db,"purchase_orders",[(next_po+i,2 if i<5 else 7,1+i%5,1,dt.date(2025,12,28),dt.date(2026,1,5),80) for i in range(10)])
    db.execute("DROP TABLE stock_flow")


GENERATORS = {"ecommerce": ecommerce, "saas": saas, "retail": retail}
