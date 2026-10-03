from insight.analytics import run_analysis


def test_promised_supply_does_not_hide_shortage_behind_excess_in_model_sample():
    common = {'as_of_date': '2025-12-31', 'snapshot_date': '2025-12-31', 'store_id': '1',
              'pending_po': 84, 'due_po_14d': 84, 'overdue_po': 0, 'transfer_in_due_14d': 0}
    rows = [{**common, 'product_id': 'excess', 'stock_on_hand': 1000, 'sold_30d': 300},
            {**common, 'product_id': 'shortage', 'stock_on_hand': 2, 'sold_30d': 15}]
    result = run_analysis('inventory_asof', [{'id': 'q1', 'columns': list(rows[0]),
        'rows': [list(r.values()) for r in rows], 'truncated': False}])
    attention = result['tables'][0]['rows']
    assert [row['risk'] for row in attention] == ['shortage', 'excess']
    assert [row['priority'] for row in attention] == ['watch', 'watch']
    assert attention[0]['projected_stock_14d'] > 0  # Promise is not an actual arrival.
