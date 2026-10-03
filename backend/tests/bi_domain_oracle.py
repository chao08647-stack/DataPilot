"""Independent test-only q06–q10 answers from raw records, not runtime SQL/tools."""
from collections import defaultdict
from datetime import date, timedelta
from itertools import combinations
from math import factorial, prod

import duckdb

OBSERVATION_END = date(2026, 3, 31)
ASOF = date(2025, 12, 31)


def records(db, table):
    cursor = db.execute(f'SELECT * FROM "{table}"')
    names = [field[0] for field in cursor.description]
    while batch := cursor.fetchmany(20000):
        for row in batch:
            yield dict(zip(names, row))


def rounded(value):
    if isinstance(value, dict):
        return {k: rounded(v) for k, v in value.items()}
    if isinstance(value, list):
        return [rounded(v) for v in value]
    return round(value, 6) if isinstance(value, float) else value


def retention_summary_gold(long_rows):
    """Tests-only aggregate of independently derived raw-account cohorts."""
    output=[]
    for group, window in sorted({(r['feature_group'],r['window']) for r in long_rows}):
        cohort_rows=[r for r in long_rows if r['feature_group']==group and r['window']==window]
        n=sum(r['eligible'] for r in cohort_rows)
        k=sum(r['retained'] for r in cohort_rows)
        observed=[(r['cohort'],100*r['retained']/r['eligible']) for r in cohort_rows if r['eligible']]
        rate_values=[rate for _,rate in observed]
        minimum=min(rate_values,default=None)
        maximum=max(rate_values,default=None)
        output.append({'feature_group':group,'window':window,'eligible':n,'retained':k,
            'retention_pct':100*k/n if n else None,'cohort_count':len(cohort_rows),
            'eligible_cohort_count':len(observed),'low_sample_cohort_count':len([r for r in cohort_rows if 0<r['eligible']<30]),
            'ineligible_cohort_count':len(cohort_rows)-len(observed),'min_retention_pct':minimum,'max_retention_pct':maximum,
            'min_retention_cohorts':', '.join(sorted(cohort for cohort,rate in observed if rate==minimum)),
            'max_retention_cohorts':', '.join(sorted(cohort for cohort,rate in observed if rate==maximum))})
    return output


def saas_gold(db):
    accounts = {r['account_id']: r for r in records(db, 'accounts')}
    features, trials, paid, key_feature = {}, {}, {}, {}
    for row in records(db, 'feature_events'):
        target = features if row['event_type'] == 'first_value' else key_feature if row['event_type'] == 'key_feature' and row['feature_name'] == 'automation_saved' else None
        if target is not None:
            key, day = row['account_id'], row['event_date']
            target[key] = min(target.get(key, day), day)
    for row in records(db, 'trials'):
        key, day = row['account_id'], row['start_date']
        trials[key] = min(trials.get(key, day), day)
    invoices = {r['invoice_id']: r['account_id'] for r in records(db, 'invoices')}
    for row in records(db, 'payment_attempts'):
        if row['outcome'] == 'success':
            key, day = invoices[row['invoice_id']], row['attempt_date']
            paid[key] = min(paid.get(key, day), day)
    grouped = defaultdict(lambda: dict(registered=0, activated=0, trial_started=0, paid=0))
    for key, account in accounts.items():
        start = account['signup_date']
        if not date(2025, 7, 1) <= start < date(2026, 1, 1) or start + timedelta(days=30) > OBSERVATION_END:
            continue
        period = '2025-Q3' if start.month < 10 else '2025-Q4'
        g = grouped[(period, account['acquisition_channel'], account['company_size'])]
        g['registered'] += 1
        activated = key in features and start <= features[key] <= start + timedelta(days=30)
        trial = activated and key in trials and features[key] <= trials[key] <= start + timedelta(days=30)
        payment = trial and key in paid and trials[key] <= paid[key] <= start + timedelta(days=30)
        g['activated'] += int(activated)
        g['trial_started'] += int(trial)
        g['paid'] += int(payment)
    periods = {period: {metric: sum(value[metric] for key, value in grouped.items() if key[0] == period) for metric in ['registered', 'activated', 'trial_started', 'paid']} for period in ['2025-Q3', '2025-Q4']}
    q6 = {'periods': periods, 'groups': [{'period': p, 'channel': c, 'company_size': s, **value} for (p, c, s), value in sorted(grouped.items())]}
    for value in q6['periods'].values():
        value['paid_pct'] = value['paid'] / value['registered'] * 100

    history, versions = {}, list(records(db, 'subscription_versions'))
    for row in records(db, 'subscriptions'):
        key, day = row['account_id'], row['start_date']
        history[key] = min(history.get(key, day), day)
    ends = [date(2024, 12, 31)] + [date(2025, month + 1, 1) - timedelta(days=1) if month < 12 else ASOF for month in range(1, 13)]
    snapshots = {}
    for end in ends:
        current = defaultdict(float)
        for row in versions:
            if row['effective_from'] <= end and (row['effective_to'] is None or end < row['effective_to']):
                current[row['account_id']] += row['monthly_price']
        snapshots[end] = current
    bridge, monthly_top = [], []
    for before, after in zip(ends, ends[1:]):
        left, right = snapshots[before], snapshots[after]
        parts = dict(new_mrr=0., expansion=0., contraction=0., churn=0., reactivation=0.)
        for account in left.keys() | right.keys():
            a, b = left[account], right[account]
            if a == 0 and b > 0:
                parts['reactivation' if history[account] <= before else 'new_mrr'] += b
            elif a > 0 and b == 0:
                parts['churn'] -= a
            elif b > a:
                parts['expansion'] += b - a
            else:
                parts['contraction'] += b - a
        bridge.append({'period': after.strftime('%Y-%m'), 'opening_mrr': sum(left.values()), 'closing_mrr': sum(right.values()), **parts})
        changes = [{'period': after.strftime('%Y-%m'), 'account_id': str(account), 'before': left[account], 'after': right[account], 'delta': right[account]-left[account]} for account in left.keys() | right.keys()]
        monthly_top.extend(sorted(changes, key=lambda row: (-abs(row['delta']), row['account_id']))[:5])
    annual = [{'account_id': str(account), 'opening_mrr': snapshots[ends[0]][account], 'closing_mrr': snapshots[ends[-1]][account], 'delta': snapshots[ends[-1]][account] - snapshots[ends[0]][account]} for account in snapshots[ends[0]].keys() | snapshots[ends[-1]].keys()]
    annual.sort(key=lambda row: (-abs(row['delta']), row['account_id']))
    q7 = {'series': bridge, 'annual_components': {key: sum(row[key] for row in bridge) for key in parts}, 'annual_account_changes': annual[:20], 'account_mrr_changes': monthly_top}

    users = {row['user_id']: row['account_id'] for row in records(db, 'users')}
    activity = defaultdict(set)
    for event in records(db, 'events'):
        if event['event_name'] in {'report_created', 'workflow_run', 'data_viewed'}:
            activity[users[event['user_id']]].add(event['event_date'])
    cohorts = defaultdict(lambda: {'accounts': 0, **{f'{prefix}_{window}': 0 for prefix in ['eligible', 'retained'] for window in [30, 60, 90]}})
    for identifier, account in accounts.items():
        start = account['signup_date']
        if start.year != 2025:
            continue
        early = identifier in key_feature and start <= key_feature[identifier] < start + timedelta(days=7)
        key = (start.strftime('%Y-%m'), 'early_key_feature' if early else 'no_early_key_feature')
        cohort = cohorts[key]
        cohort['accounts'] += 1
        for window in [30, 60, 90]:
            if start + timedelta(days=window) <= OBSERVATION_END:
                cohort[f'eligible_{window}'] += 1
                cohort[f'retained_{window}'] += int(any(window-6 <= (day-start).days <= window for day in activity[identifier]))
    long_rows = []
    for (cohort, feature_group), values in sorted(cohorts.items()):
        for window in [30, 60, 90]:
            eligible, retained = values[f'eligible_{window}'], values[f'retained_{window}']
            long_rows.append({'cohort': cohort, 'feature_group': feature_group, 'window': f'D{window}', 'eligible': eligible, 'retained': retained, 'retention_pct': retained/eligible*100 if eligible else None})
    return {'q06': q6, 'q07': q7, 'q08': {'series': long_rows,'retention_summary':retention_summary_gold(long_rows)}}


def product_attribution(before, after):
    """Subset form of Shapley; independently implemented, not app permutations."""
    result = []
    for index in range(4):
        others = [i for i in range(4) if i != index]
        contribution = 0.
        for size in range(4):
            weight = factorial(size) * factorial(3-size) / factorial(4)
            for coalition in combinations(others, size):
                contribution += weight * (after[index]-before[index]) * prod(after[j] if j in coalition else before[j] for j in others)
        result.append(contribution)
    return result


def retail_gold(db):
    stores = {row['store_id']: row for row in records(db, 'stores')}
    eligible = {key for key, row in stores.items() if row['open_date'] <= date(2025, 7, 1) and (row['closed_date'] is None or row['closed_date'] > ASOF)}
    groups = defaultdict(lambda: dict(open_days=0, visitors=0, transactions=0, sales=0.))
    def period(day):
        return '2025-Q3' if day < date(2025, 10, 1) else '2025-Q4'
    def included(store, day):
        return store in eligible and date(2025, 7, 1) <= day <= ASOF
    for row in records(db, 'store_calendar'):
        if included(row['store_id'], row['calendar_date']):
            groups[(row['store_id'], period(row['calendar_date']))]['open_days'] += int(row['is_open'])
    for row in records(db, 'footfall'):
        if included(row['store_id'], row['visit_date']):
            groups[(row['store_id'], period(row['visit_date']))]['visitors'] += row['visitors']
    transactions = {}
    for row in records(db, 'transactions'):
        if row['status'] != 'completed':
            continue
        transactions[row['transaction_id']] = (row['store_id'], row['transaction_date'])
        if included(row['store_id'], row['transaction_date']):
            group = groups[(row['store_id'], period(row['transaction_date']))]
            group['transactions'] += 1
            group['sales'] += row['transaction_total']
    series = []
    for store in sorted(eligible):
        a, b = groups[(store, '2025-Q3')], groups[(store, '2025-Q4')]
        if min(a['open_days'], b['open_days']) <= 0:
            continue
        vectors = [[v['open_days'], v['visitors']/v['open_days'], v['transactions']/v['visitors'], v['sales']/v['transactions']] for v in [a, b]]
        effects = product_attribution(*vectors)
        series.append({'store_id': str(store), 'sales_before': a['sales'], 'sales_after': b['sales'], 'sales_delta': b['sales']-a['sales'], **dict(zip(['open_days_effect', 'daily_traffic_effect', 'conversion_effect', 'aov_effect'], effects))})
    series.sort(key=lambda row: row['sales_delta'])
    q9 = {'series': series, 'comparable_stores': len(series), 'declining_stores': sum(row['sales_delta'] < 0 for row in series)}
    declining = {int(row['store_id']) for row in series if row['sales_delta'] < 0}
    products = {row['product_id']: row for row in records(db, 'products')}
    category_values = defaultdict(lambda: defaultdict(float))
    daily_values = defaultdict(float)

    latest, sold, received, purchases, incoming = {}, defaultdict(float), defaultdict(float), defaultdict(lambda: dict(pending_po=0., due_po_14d=0., overdue_po=0)), defaultdict(float)
    for row in records(db, 'inventory_snapshots'):
        key = row['store_id'], row['product_id']
        if row['snapshot_date'] <= ASOF and (key not in latest or row['snapshot_date'] > latest[key]['snapshot_date']):
            latest[key] = row
    for row in records(db, 'transaction_items'):
        if row['transaction_id'] in transactions:
            store, day = transactions[row['transaction_id']]
            if ASOF - timedelta(days=29) <= day <= ASOF:
                sold[(store, row['product_id'])] += row['quantity']
            if store in declining and date(2025, 7, 1) <= day <= ASOF:
                value = row['quantity']*row['unit_price']
                category_values[products[row['product_id']]['category']][period(day)] += value
                daily_values[day] += value
    category_rows = [{'category': name, 'sales_before': value['2025-Q3'], 'sales_after': value['2025-Q4'], 'sales_delta': value['2025-Q4']-value['2025-Q3']} for name, value in category_values.items()]
    category_rows.sort(key=lambda row: row['sales_delta'])
    baseline_daily = sum(row['sales_before'] for row in category_rows)/92
    dates = [{'date': str(date(2025, 10, 1)+timedelta(days=offset)), 'sales': daily_values[date(2025, 10, 1)+timedelta(days=offset)], 'baseline_daily_average': baseline_daily, 'difference_to_baseline_average': daily_values[date(2025, 10, 1)+timedelta(days=offset)]-baseline_daily} for offset in range(92)]
    dates.sort(key=lambda row: (row['difference_to_baseline_average'], row['date']))
    q9.update(declining_store_categories=category_rows, declining_store_dates=dates[:20])
    for row in records(db, 'goods_receipts'):
        if row['received_date'] <= ASOF:
            received[row['po_id']] += row['quantity']
    for row in records(db, 'purchase_orders'):
        if row['ordered_date'] <= ASOF:
            value = max(row['ordered_qty']-received[row['po_id']], 0)
            target = purchases[(row['store_id'], row['product_id'])]
            target['pending_po'] += value
            if row['expected_date'] <= ASOF + timedelta(days=14):
                target['due_po_14d'] += value
            target['overdue_po'] += int(row['expected_date'] < ASOF and value > 0)
    for row in records(db, 'inventory_transfers'):
        if row['created_date'] <= ASOF and row['transfer_date'] <= ASOF and (row['received_date'] is None or row['received_date'] > ASOF) and row['expected_arrival_date'] <= ASOF + timedelta(days=14):
            incoming[(row['to_store_id'], row['product_id'])] += row['quantity']
    rows = []
    for store, product in sorted(latest.keys() | sold.keys() | purchases.keys() | incoming.keys()):
        entry = stores[store]
        if entry['open_date'] > ASOF or entry['closed_date'] is not None and entry['closed_date'] <= ASOF:
            continue
        key = store, product
        snapshot = latest.get(key)
        stock = snapshot['stock_on_hand'] if snapshot else None
        velocity = sold[key]/30
        coverage = stock/velocity if stock is not None and velocity else None
        risk = 'insufficient_observation' if snapshot is None or (ASOF-snapshot['snapshot_date']).days > 7 else 'shortage' if velocity and coverage < 7 else 'excess' if stock > 0 and (velocity == 0 or coverage > 90) else 'normal'
        projected = stock + purchases[key]['due_po_14d'] + incoming[key] - velocity*14 if risk != 'insufficient_observation' else None
        rows.append({'store_id': str(store), 'product_id': str(product), 'stock_on_hand': stock, 'sold_30d': sold[key], **purchases[key], 'transfer_in_due_14d': incoming[key], 'cover_days': coverage if risk != 'insufficient_observation' else None, 'projected_stock_14d': projected, 'risk': risk})
    return {'q09': q9, 'q10': {'series': rows, 'risk_counts': {risk: sum(row['risk'] == risk for row in rows) for risk in ['shortage', 'excess', 'normal', 'insufficient_observation']}}}


def compute_domain_gold(paths):
    with duckdb.connect(str(paths['saas']), read_only=True) as db:
        result = saas_gold(db)
    with duckdb.connect(str(paths['retail']), read_only=True) as db:
        result.update(retail_gold(db))
    return rounded(result)
