"""Chart contracts validate saved evidence; charts never manufacture or aggregate data."""
import math
import re

KINDS = {"bar", "line", "pie", "donut", "kpi", "table", "waterfall", "funnel", "heatmap", "horizontal_bar", "stacked_bar", "area", "scatter", "combo"}


def numeric(value):
    return value is None or type(value) in (int, float) and math.isfinite(value)


def as_table(chart, reason):
    return {**chart, "type": "table", "title": f"{chart.get('title', '查询结果')}（{reason}）", "x": None, "y": [], "series": [], "validation_error": reason}


def validate_charts(charts, queries):
    by_id = {q["id"]: q for q in queries}
    result = []
    for raw in charts:
        chart = dict(raw)
        if chart.get("type") not in KINDS:
            continue
        query = by_id.get(chart.get("query_id"))
        if query is None:
            continue
        columns, rows = query["columns"], query["rows"]
        series = chart.get("series") or []
        ys = chart.get("y") or [s["column"] for s in series]
        chart["y"] = ys
        if (chart.get("x") is not None and chart["x"] not in columns
                or not set(ys).issubset(columns)
                or any(s.get("column") not in ys for s in series)):
            continue
        if chart.get("group_by") is not None and chart["group_by"] not in columns:
            continue
        divisor = chart.get("value_divisor", 1)
        if type(divisor) not in (int, float) or not math.isfinite(divisor) or divisor <= 0:
            continue
        if chart["type"] == "kpi" and (query.get("total_rows", len(rows)) != 1 or query.get("truncated") or not ys):
            result.append(as_table(chart, "多行结果不能作为单值 KPI"))
            continue
        if chart["type"] not in {"table", "kpi"} and (not chart.get("x") or not ys):
            continue
        if chart["type"] != "table" and any(not all(numeric(r[columns.index(y)]) for r in rows) for y in ys):
            continue
        if chart["type"] in {"pie", "donut"}:
            if query.get("truncated") or set(ys) & set(query.get("non_additive_columns", [])) or len(ys) != 1 or any(r[columns.index(ys[0])] is not None and r[columns.index(ys[0])] < 0 for r in rows):
                result.append(as_table(chart, "占比需要完整且非负的同口径数据"))
                continue
        if chart["type"] == "scatter":
            if not all(numeric(r[columns.index(chart["x"])]) for r in rows):
                continue
            if chart.get("size") is not None and (chart["size"] not in columns or not all(numeric(r[columns.index(chart["size"])]) for r in rows)):
                continue
        if chart["type"] == "heatmap" and chart.get("group_by") not in columns:
            continue
        if chart["type"] not in {"scatter", "table", "kpi", "waterfall"} and chart.get("x"):
            keys = [columns.index(chart["x"])]
            if chart.get("group_by"):
                keys.append(columns.index(chart["group_by"]))
            identities = [tuple(str(row[i]) for i in keys) for row in rows]
            if len(set(identities)) != len(identities):
                result.append(as_table(chart, "维度重复，未擅自聚合"))
                continue
        axes = {}
        for item in series:
            axis, unit = item.get("axis") or "left", item.get("unit")
            if axis not in {"left", "right"}:
                break
            if unit:
                axes.setdefault(axis, set()).add(unit)
        else:
            if chart["type"] not in {"table", "kpi"} and any(len(units) > 1 for units in axes.values()):
                result.append(as_table(chart, "不同单位不能共用数值轴"))
                continue
            if chart["type"] == "waterfall":
                start = chart.get("start_value")
                # A plain sequence of deltas may begin at zero; never invent a baseline.
                if start is None:
                    chart["start_value"] = 0
                elif not numeric(start):
                    continue
                if chart.get("total_column") is not None and chart["total_column"] not in columns:
                    continue
            result.append(chart)
    return result or [{"type": "table", "title": "查询结果", "query_id": q["id"], "x": None, "y": []} for q in queries]


def apply_chart_units(charts, queries, scenario, preferences, question):
    """Only explicitly monetary series may share a monetary display divisor."""
    metric_units = {metric["id"]: metric["unit"] for metric in scenario["metrics"]}
    by_id = {query["id"]: query for query in queries}
    requested = preferences.get("currency_display_unit", "元")
    explicit = re.findall(r"(?:单位|以|按|用|显示为).{0,3}?(万元|元)", question)
    if explicit:
        requested = explicit[-1]
    result = []
    for chart in charts:
        value = dict(chart)
        ids = by_id.get(chart["query_id"], {}).get("metric_ids", [])
        series_units = {s.get("unit") for s in chart.get("series", [])}
        if ids and {metric_units.get(mid) for mid in ids} == {"元"} and (not series_units or series_units == {"元"}):
            scaled = requested in {"万", "万元"} and chart["type"] != "table"
            value.update(unit="万元" if scaled else "元", value_divisor=10000 if scaled else 1)
        result.append(value)
    return result
