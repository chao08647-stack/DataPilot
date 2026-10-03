"""Deterministic diagnostic evidence. Accounting attribution is not causality."""
from __future__ import annotations

import math
from itertools import permutations
from numbers import Number

TOOLS = {"profit_bridge", "refund_diagnosis", "conversion_funnel", "mrr_bridge", "same_store_decomposition", "inventory_reconciliation"}
TOOLS.update({"period_overview", "grouped_profit_change", "customer_value_90d", "customer_repeat_cohort", "mrr_monthly_bridge", "account_retention", "store_driver_comparison", "inventory_asof"})


def _result(tool, ids):
    return {"tool": tool, "status": "ok", "method": "", "facts": [], "series": [],
            "reconciliation": {}, "limitations": [], "evidence_ids": ids}


def _input(queries, config):
    selected = [q for q in queries if not config.get("query_id") or q["id"] == config["query_id"]]
    if len(selected) != 1:
        raise ValueError("必须提供一个明确的诊断查询，或通过query_id选择输入。")
    query = selected[0]
    if query.get("truncated") or query.get("prompt_sampled"):
        raise ValueError("输入结果已截断，不能据此生成完整业务诊断。")
    if not query.get("rows"):
        raise ValueError("当前范围没有诊断数据。")
    columns = query.get("columns", [])
    if len(columns) != len(set(columns)):
        raise ValueError("诊断输入存在重复列名。")
    rows = [dict(zip(columns, row)) if not isinstance(row, dict) else dict(row) for row in query["rows"]]
    return rows, [query["id"]]


def _required(rows, columns):
    for row in rows:
        if any(column not in row or row[column] is None for column in columns):
            raise ValueError("缺少必要字段或观测值：" + ",".join(columns))


def _number(value):
    if isinstance(value, bool) or not isinstance(value, Number) or not math.isfinite(float(value)):
        raise ValueError("必要指标必须是有限数值，不能把缺失观测填为零。")
    return float(value)


def _periods(rows, config):
    _required(rows, ["period"])
    available = sorted({str(row["period"]) for row in rows})
    if len(available) < 2:
        raise ValueError("至少需要两个完整可比较期间，当前筛选缺少基期或报告期。")
    baseline, current = str(config.get("baseline_period", available[-2])), str(config.get("current_period", available[-1]))
    if baseline == current or baseline not in available or current not in available:
        raise ValueError("指定的基期和报告期不完整或相同。")
    return baseline, current


def _fact(result, name, value, unit="", **extra):
    result["facts"].append({"name": name, "value": round(value, 6) if isinstance(value, float) else value,
                            "unit": unit, "evidence_ids": result["evidence_ids"], **extra})


def _profit(rows, config, result):
    components = {"gross_sales": ("标价成交额", 1), "discounts": ("折扣", -1), "refunds": ("成功退款", -1),
                  "cogs": ("成交成本", -1), "fulfillment": ("履约费用", -1), "payment_fees": ("支付费用", -1), "marketing": ("营销支出", -1)}
    _required(rows, ["period", *components])
    baseline, current = _periods(rows, config)
    totals = {p: {key: sum(_number(row[key]) for row in rows if str(row["period"]) == p) for key in components} for p in (baseline, current)}
    if any(value < 0 for period in totals.values() for value in period.values()):
        raise ValueError("利润桥接输入费用采用正数金额，冲销须先按正式口径净额化。")
    profit = {p: sum(totals[p][key] * sign for key, (_, sign) in components.items()) for p in (baseline, current)}
    result["series"] = [{"component": label, "delta": round((totals[current][key]-totals[baseline][key])*sign, 6)} for key, (label, sign) in components.items()]
    residual = profit[current] - profit[baseline] - sum(r["delta"] for r in result["series"])
    result["method"] = "贡献利润=标价成交额−折扣−成功退款−成交成本−履约−支付费用−营销；按相同期间口径逐项桥接。"
    result["reconciliation"] = {"balanced": abs(residual)<1e-5, "residual": round(residual,6), "baseline_period": baseline, "current_period": current, "opening": profit[baseline], "closing": profit[current]}
    _fact(result, "基期贡献利润", profit[baseline], "元")
    _fact(result, "报告期贡献利润", profit[current], "元")
    _fact(result, "贡献利润变化", profit[current]-profit[baseline], "元")
    result["limitations"] = ["仅覆盖已提供的费用项目，不等于企业净利润。", "费用桥接是账面差额分解，不证明促销或广告的增量因果效果。"]
    result["chart"] = {"type":"waterfall","title":"贡献利润变动桥接","x":"component","y":["delta"],"start_value":profit[baseline],"unit":"元"}


def _refund(rows, config, result):
    _required(rows, ["group", "orders", "refunded_orders", "late_orders", "late_refunded_orders"])
    for row in rows:
        n, returned, late, late_returned = (_number(row[key]) for key in ("orders","refunded_orders","late_orders","late_refunded_orders"))
        if n <= 0 or not (0<=late_returned<=late<=n and late_returned<=returned<=n and returned-late_returned<=n-late):
            raise ValueError("退款诊断分子分母不一致或样本量为零。")
        late_rate = late_returned/late if late else None
        on_time_rate = (returned-late_returned)/(n-late) if n>late else None
        result["series"].append({"group":str(row["group"]),"orders":n,"refunded_orders":returned,"refund_pct":round(returned/n*100,4),
            "late_orders":late,"late_refund_pct":round(late_rate*100,4) if late_rate is not None else None,
            "on_time_refund_pct":round(on_time_rate*100,4) if on_time_rate is not None else None,
            "gap_pp":round((late_rate-on_time_rate)*100,4) if late_rate is not None and on_time_rate is not None else None})
    result["method"] = "以已成熟订单队列为分母，对退款订单去重，比较延迟与按时履约组的退款率；展示样本量与百分点差。"
    _fact(result, "诊断订单数", sum(r["orders"] for r in result["series"]), "单")
    _fact(result, "退款订单数", sum(r["refunded_orders"] for r in result["series"]), "单")
    result["reconciliation"] = {"balanced":True,"groups":len(rows)}
    result["limitations"] = ["分组须互斥且覆盖同一成熟订单队列；跨期退款按订单队列归属。", "延迟与退款率差异属于观察关联，不能直接证明延迟造成退款。"]
    if any(r["gap_pp"] is None for r in result["series"]):
        result["limitations"].append("部分分组缺少延迟或按时样本，不计算该组对比差值。")
    result["chart"] = {"type":"bar","title":"分组退款与履约关联","x":"group","y":["late_refund_pct","on_time_refund_pct"],"unit":"%"}


def _funnel(rows, config, result):
    keys = ["registered", "activated", "trial_started", "paid"]
    _required(rows, keys)
    comparison = None
    if all("period" in row for row in rows):
        baseline, current = _periods(rows, config)
        comparison = [row for row in rows if str(row["period"]) == baseline]
        rows = [row for row in rows if str(row["period"]) == current]
    for row in rows + (comparison or []):
        values = [_number(row[key]) for key in keys]
        if any(value < 0 for value in values) or any(a < b for a, b in zip(values, values[1:])):
            raise ValueError("各分组必须满足顺序漏斗计数约束。")
    counts = {key:sum(_number(r[key]) for r in rows) for key in keys}
    values = list(counts.values())
    if values[0] <= 0 or any(v<0 for v in values) or any(a<b for a,b in zip(values,values[1:])):
        raise ValueError("顺序漏斗必须使用成熟的同一账户队列，后续步骤不能多于前序步骤。")
    names = ["注册账户","完成激活","开始试用","付费账户"]
    for index,(key,name) in enumerate(zip(keys,names)):
        result["series"].append({"stage":name,"accounts":counts[key],"from_previous_pct":round(counts[key]/values[index-1]*100,4) if index and values[index-1] else None})
    result["method"] = "同一成熟账户队列内按顺序完成注册→价值激活→试用→付费，账户去重；汇总分子分母后计算转化。"
    _fact(result,"30日付费转化率",counts["paid"]/counts["registered"]*100,"%")
    _fact(result,"成熟注册账户数",counts["registered"],"账户")
    result["reconciliation"] = {"balanced":True,"monotonic":True}
    if comparison:
        previous = {key: sum(_number(row[key]) for row in comparison) for key in keys}
        if previous["registered"] <= 0:
            raise ValueError("基期成熟注册账户数为零，无法比较转化率。")
        before_rate = previous["paid"] / previous["registered"]
        after_rate = counts["paid"] / counts["registered"]
        _fact(result, "基期30日付费转化率", before_rate * 100, "%")
        _fact(result, "付费转化率变化", (after_rate-before_rate) * 100, "百分点")
        result["reconciliation"].update(baseline_period=baseline, current_period=current)
        if all("group" in row for row in rows + comparison):
            groups = [{str(row["group"]): row for row in part} for part in (comparison, rows)]
            if any(len(group) != len(part) for group, part in zip(groups, (comparison, rows))):
                raise ValueError("来源拆解要求每个期间每个来源只有一行。")
            if groups[0].keys() == groups[1].keys() and all(_number(row["registered"]) > 0 for group in groups for row in group.values()):
                mix = within = 0.0
                for group in groups[0]:
                    a, b = groups[0][group], groups[1][group]
                    wa, wb = a["registered"]/previous["registered"], b["registered"]/counts["registered"]
                    ca, cb = a["paid"]/a["registered"], b["paid"]/b["registered"]
                    mix += (wb-wa)*(ca+cb)/2
                    within += (cb-ca)*(wa+wb)/2
                _fact(result, "来源结构变化贡献", mix*100, "百分点")
                _fact(result, "来源内转化变化贡献", within*100, "百分点")
                result["reconciliation"]["conversion_residual_pp"] = round((after_rate-before_rate-mix-within)*100, 8)
    result["limitations"] = ["漏斗只描述步骤流失位置，不证明某版本或渠道导致转化下降。", "各分组必须来自相同完整观察窗口且互斥，未成熟账户应在查询中排除。"]
    if comparison and all("channel" in row and "company_size" in row for row in rows + comparison):
        result["grouping_dimensions"] = ["channel", "company_size"]
        result["method"] += " 来源结构及来源内贡献基于渠道×企业规模的联合组进行对称分解，不是纯渠道归因；单维渠道/规模汇总只用于分组对比。"
        result["limitations"].append("联合组结构/组内贡献不可解释成各渠道自身变化；如需纯渠道分解，必须在渠道粒度重新计算。")
    result["chart"] = {"type":"funnel","title":"成熟账户付费漏斗","x":"stage","y":["accounts"],"unit":"账户"}
    if comparison:
        stages=[{"stage":label,"baseline":previous[key],"current":counts[key],"delta":counts[key]-previous[key]} for key,label in zip(keys,names)]
        result["tables"]=[{"id":"funnel_comparison","title":"两期间成熟账户漏斗比较","rows":stages,"chart":{"type":"bar","x":"stage","y":["baseline","current"],"unit":"账户"}}]
        for dimension in ["channel","company_size"]:
            if not all(dimension in row for row in rows+comparison):
                continue
            segments=[]
            changes={}
            for value in sorted({str(row[dimension]) for row in rows+comparison}):
                period_rates={}
                current_segment=None
                for period,source in [(baseline,comparison),(current,rows)]:
                    part={key:sum(_number(row[key]) for row in source if str(row[dimension])==value) for key in keys}
                    rate=part["paid"]/part["registered"]*100 if part["registered"] else None
                    period_rates[period]=rate
                    segment={"period":period,dimension:value,**part,"paid_pct":round(rate,6) if rate is not None else None,
                        "activation_loss":part["registered"]-part["activated"],"trial_loss":part["activated"]-part["trial_started"],"payment_loss":part["trial_started"]-part["paid"],
                        "paid_pct_delta_pp":None,"paid_pct_decline_rank":None}
                    segments.append(segment)
                    if period==current:
                        current_segment=segment
                # Sum the full group's numerators/denominators first. Never
                # average subgroup percentages or subtract rounded rates.
                if all(rate is not None for rate in period_rates.values()):
                    delta=round(period_rates[current]-period_rates[baseline],6)
                    current_segment["paid_pct_delta_pp"]=delta
                    if delta<0:
                        changes[value]=delta
            ranks={delta:index+1 for index,delta in enumerate(sorted(set(changes.values())))}
            for segment in segments:
                if segment["period"]==current and segment[dimension] in changes:
                    segment["paid_pct_decline_rank"]=ranks[changes[segment[dimension]]]
            if changes:
                worst=min(changes.values())
                leaders=sorted(value for value,delta in changes.items() if delta==worst)
                label={"channel":"渠道","company_size":"企业规模"}[dimension]
                _fact(result,f"{label}付费转化率最大下降：{'、'.join(leaders)}",worst,"百分点",
                      dimension=dimension,groups=leaders,baseline_period=baseline,current_period=current,
                      ranking_basis="单维汇总分子/分母；报告期付费率−基期付费率；下降最负优先，并列全部保留")
            result["tables"].append({"id":"funnel_"+dimension,"title":"漏斗分组："+dimension,"rows":segments,"chart":{"type":"bar","x":dimension,"group_by":"period","y":["paid_pct"],"unit":"%"}})
        result["method"] += " 单维表的paid_pct_delta_pp仅在报告期记录相对基期的百分点变化；paid_pct_decline_rank仅对负变化排序，1为最大下降，并列同名次，非下降或分母缺失不排名。"


def _mrr(rows, config, result):
    _required(rows,["period","account_id","mrr","first_started"])
    baseline,current = _periods(rows,config)
    if not config.get("complete_snapshot",False):
        raise ValueError("MRR桥接需要声明完整账户快照，不能把截取或Top-N中缺失的账户当作零。")
    balances = {baseline:{},current:{}}
    first = {}
    for row in rows:
        period = str(row["period"])
        if period not in balances:
            continue
        key = str(row["account_id"])
        if key in balances[period]:
            raise ValueError("MRR输入必须先按期间与账户汇总，不能重复累计订阅明细。")
        value = _number(row["mrr"])
        if value<0:
            raise ValueError("账户MRR不能为负数。")
        balances[period][key] = value
        first[key] = str(row["first_started"])
    parts = {"新增":0.0,"扩张":0.0,"收缩":0.0,"流失":0.0,"恢复":0.0}
    for key in balances[baseline].keys()|balances[current].keys():
        before,after=balances[baseline].get(key,0.0),balances[current].get(key,0.0)
        if not before and after:
            parts["恢复" if first[key]<=baseline else "新增"] += after
        elif before and not after:
            parts["流失"] -= before
        elif after>before:
            parts["扩张"] += after-before
        elif before>after:
            parts["收缩"] -= before-after
    opening,closing=sum(balances[baseline].values()),sum(balances[current].values())
    residual=closing-opening-sum(parts.values())
    result["series"]=[{"component":key,"delta":round(value,6)} for key,value in parts.items()]
    result["method"]="完整期初/期末账户MRR快照桥接，月价已经归一；按首次开通日期区分新增与恢复。"
    result["reconciliation"]={"balanced":abs(residual)<1e-5,"residual":round(residual,6),"opening":opening,"closing":closing,"baseline_period":baseline,"current_period":current}
    _fact(result,"期初MRR",opening,"元/月")
    _fact(result,"期末MRR",closing,"元/月")
    if opening:
        retained=sum(balances[current].get(key,0) for key,value in balances[baseline].items() if value>0)
        _fact(result,"期初账户群净收入留存率",retained/opening*100,"%")
    result["limitations"]=["这是账户期初期末净变动桥接，不代表期间全部订阅事件的毛额。", "客户使用下降、工单与取消仅提供关联线索，不能直接认定流失原因。"]
    result["chart"]={"type":"waterfall","title":"MRR变化桥接","x":"component","y":["delta"],"start_value":opening,"unit":"元/月"}


def _shapley_product(before, after):
    effects=[0.0]*len(before)
    orders=list(permutations(range(len(before))))
    for order in orders:
        values=list(before)
        for index in order:
            previous=math.prod(values)
            values[index]=after[index]
            effects[index]+=math.prod(values)-previous
    return [value/len(orders) for value in effects]


def _same_store(rows, config, result):
    _required(rows,["period","store_id","open_days","visitors","transactions","sales"])
    baseline,current=_periods(rows,config)
    groups={p:{} for p in (baseline,current)}
    for row in rows:
        p=str(row["period"])
        if p in groups:
            key=str(row["store_id"])
            if key in groups[p]:
                raise ValueError("同店分析需先按门店与期间汇总，不能重复行。")
            groups[p][key]=row
    common=set(groups[baseline])&set(groups[current])
    comparable={key for key in common if all(_number(groups[p][key]["open_days"])>0 for p in groups)}
    if not comparable:
        raise ValueError("当前范围没有两个期间均营业的可比门店。")
    factors={}
    sales={}
    for p in groups:
        sums={k:sum(_number(groups[p][key][k]) for key in comparable) for k in ("open_days","visitors","transactions","sales")}
        if any(sums[k]<=0 for k in ("open_days","visitors","transactions")):
            raise ValueError("营业门店天数、客流或交易数为零，不能可靠分解乘法指标。")
        factors[p]=[sums["open_days"],sums["visitors"]/sums["open_days"],sums["transactions"]/sums["visitors"],sums["sales"]/sums["transactions"]]
        sales[p]=sums["sales"]
    effects=_shapley_product(factors[baseline],factors[current])
    result["series"]=[{"component":name,"delta":round(value,6)} for name,value in zip(["营业门店天数","日均客流","客流成交比","客单价"],effects)]
    residual=sales[current]-sales[baseline]-sum(effects)
    result["method"]="同一可比门店集合：销售=营业门店天数×日均客流×交易/客流×客单价；24种替换顺序平均分摊交互项。"
    result["reconciliation"]={"balanced":abs(residual)<1e-5,"residual":round(residual,6),"opening":sales[baseline],"closing":sales[current],"comparable_stores":len(comparable),"excluded_stores":sorted((set(groups[baseline])|set(groups[current]))-comparable)}
    _fact(result,"同店销售变化",sales[current]-sales[baseline],"元")
    _fact(result,"可比门店数",len(comparable),"家")
    result["limitations"]=["分解为会计恒等式下的对称归因，不代表排班、员工能力或促销的因果效果。", "客流是访问次数，不是去重顾客；需另外核实营业日历、同店资格与数据完整性。"]
    result["chart"]={"type":"waterfall","title":"同店销售变化分解","x":"component","y":["delta"],"start_value":sales[baseline],"unit":"元"}


def _inventory(rows, config, result):
    if all("period" in row for row in rows):
        current = str(config.get("current_period", max(str(row["period"]) for row in rows)))
        rows = [row for row in rows if str(row["period"]) == current]
        if not rows:
            raise ValueError("指定库存期间没有完整快照。")
        result["reconciliation"]["current_period"] = current
    fields=["store_id","product_id","opening_stock","receipts","sold_units","transfer_in","transfer_out","adjustments","closing_stock"]
    _required(rows,fields)
    pairs = {(str(row["store_id"]), str(row["product_id"])) for row in rows}
    if len(pairs) != len(rows):
        raise ValueError("库存诊断每个门店与商品只能有一行同期间汇总。")
    residual_total=0.0
    for row in rows:
        opening,receipts,sold,incoming,outgoing,adjustments,closing=(_number(row[k]) for k in fields[2:])
        expected=opening+receipts+incoming-sold-outgoing+adjustments
        residual=closing-expected
        residual_total+=abs(residual)
        result["series"].append({"store_id":str(row["store_id"]),"product_id":str(row["product_id"]),"opening_stock":opening,
            "closing_stock":closing,"expected_stock":expected,"residual":round(residual,6),"receipts":receipts,"sold_units":sold,
            "missing_snapshots":row.get("missing_snapshots"),"zero_snapshots":row.get("zero_snapshots"),"late_receipts":row.get("late_receipts")})
    result["method"]="逐门店×商品核对：期末=期初+收货+调入−销售−调出+盘点调整；绝对残差不能跨商品相互抵消。"
    result["reconciliation"].update(balanced=residual_total<1e-5,absolute_residual=round(residual_total,6),checked_pairs=len(rows))
    _fact(result,"库存不平衡对象数",sum(abs(r["residual"])>=1e-5 for r in result["series"]),"个")
    _fact(result,"已观测期末库存",sum(r["closing_stock"] for r in result["series"]),"件")
    result["limitations"]=["缺失期初或期末快照时拒绝勾稽，不将缺失视为零。", "缺货损失销售与补货收益需要额外需求模型，只能作为带假设估算。"]
    result["chart"]={"type":"heatmap","title":"门店商品库存勾稽残差","x":"store_id","group_by":"product_id","y":["residual"],"unit":"件"}


def run_analysis(tool: str, queries: list[dict], config: dict | None = None) -> dict:
    if tool not in TOOLS:
        raise ValueError("Unknown deterministic analysis tool")
    config=config or {}
    result=_result(tool,[q.get("id","") for q in queries])
    try:
        rows,ids=_input(queries,config)
        result["evidence_ids"]=ids
        from insight.bi_analytics import HANDLERS, enrich_evidence
        {"profit_bridge":_profit,"refund_diagnosis":_refund,"conversion_funnel":_funnel,"mrr_bridge":_mrr,
         "same_store_decomposition":_same_store,"inventory_reconciliation":_inventory,**HANDLERS}[tool](rows,config,result)
        enrich_evidence(tool,queries,config,result)
    except (ValueError,TypeError,KeyError) as exc:
        result.update(status="insufficient_data",facts=[],series=[],reconciliation={"complete":False},limitations=[str(exc)])
        result.pop("chart",None)
        result.pop("tables",None)
    return result
