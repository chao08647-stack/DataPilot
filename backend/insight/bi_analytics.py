"""Generic BI calculations over complete query evidence, never question-ID routing."""
from __future__ import annotations

from collections import defaultdict
from datetime import date

from insight.analytics import (
    _fact,
    _mrr,
    _number,
    _periods,
    _profit,
    _required,
    _result,
    _shapley_product,
)


def _table(identifier, title, rows, chart=None):
    return {"id":identifier,"title":title,"rows":rows,**({"chart":chart} if chart else {})}


def _ratio(numerator, denominator):
    return round(numerator/denominator,6) if denominator else None


def period_overview(rows, config, result):
    keys=["orders","sales","gross_sales","discounts","refunds","cogs","fulfillment","payment_fees","marketing","contribution_profit"]
    _required(rows,["period",*keys])
    if len({str(r["period"]) for r in rows}) != len(rows):
        raise ValueError("期间概览要求每个期间一行完整汇总。")
    previous=None
    residual=0.0
    for row in sorted(rows,key=lambda r:str(r["period"])):
        values={k:_number(row[k]) for k in keys}
        if values["orders"]<=0:
            raise ValueError("订单量必须为正，不能用缺失结果生成客单价。")
        computed=values["sales"]-sum(values[k] for k in ["refunds","cogs","fulfillment","payment_fees","marketing"])
        residual+=abs(computed-values["contribution_profit"])+abs(values["gross_sales"]-values["discounts"]-values["sales"])
        result["series"].append({"period":str(row["period"]),**values,"aov":_ratio(values["sales"],values["orders"]),
            "sales_change_pct":_ratio((values["sales"]-previous["sales"])*100,previous["sales"]) if previous else None,
            "profit_change":round(values["contribution_profit"]-previous["contribution_profit"],6) if previous else None})
        previous=values
    if residual>1e-4:
        raise ValueError("折前金额/折扣/成交额或贡献利润分项无法还原，拒绝输出不平衡概览。")
    for key,title,unit in [("sales","期间销售额","元"),("orders","期间有效订单量","单"),("contribution_profit","期间贡献利润","元")]:
        _fact(result,title,sum(r[key] for r in result["series"]),unit)
    result["method"]="每期间完整汇总；客单价=成交额/有效订单量，贡献利润逐项勾稽；环比只在实际相邻输入期间比较。"
    result["reconciliation"]={"balanced":True,"absolute_residual":round(residual,6),"periods":len(rows)}
    totals={key:sum(row[key] for row in result["series"]) for key in keys}
    totals["aov"]=_ratio(totals["sales"],totals["orders"])
    result["tables"]=[_table("annual_summary","所选完整期间经营汇总",[totals])]
    result["limitations"]=["经营贡献不等于净利润。营销分摊为估计分配，不代表增量营销效果。"]
    result["chart"]={"type":"line","title":"销售与贡献利润趋势","x":"period","y":["sales","contribution_profit"],"unit":"元"}


def grouped_profit_change(rows, config, result):
    _required(rows,["period","channel","category"])
    before,after=_periods(rows,config)
    _profit(rows,config,result)
    tables=[]
    for dimension in ["channel","category"]:
        groups=defaultdict(lambda:defaultdict(lambda:defaultdict(float)))
        for row in rows:
            period=str(row["period"])
            if period not in {before,after}:
                continue
            for key in ["gross_sales","discounts","refunds","cogs","fulfillment","payment_fees","marketing"]:
                groups[str(row[dimension])][period][key]+=_number(row[key])
        segments=[]
        for group,periods in sorted(groups.items()):
            a,b=periods[before],periods[after]
            def profit(v):
                return v["gross_sales"]-sum(v[k] for k in ["discounts","refunds","cogs","fulfillment","payment_fees","marketing"])
            segments.append({"segment":group,"sales_before":a["gross_sales"]-a["discounts"],"sales_after":b["gross_sales"]-b["discounts"],
                "sales_delta":(b["gross_sales"]-b["discounts"])-(a["gross_sales"]-a["discounts"]),"profit_before":profit(a),"profit_after":profit(b),"profit_delta":profit(b)-profit(a),
                **{key+"_effect":a[key]-b[key] for key in ["discounts","refunds","cogs","fulfillment","payment_fees","marketing"]}})
        tables.append(_table(dimension+"_contribution",dimension+"差额分解",segments,{"type":"bar","x":"segment","y":["sales_delta","profit_delta"],"unit":"元"}))
    result["tables"]=tables
    result["limitations"].append("订单级折扣及费用按明细折前金额占比分摊；渠道表与品类表是同一总量的两种视角，不能把两者再次相加。")


def customer_value_90d(rows, config, result):
    fields=["channel","new_customers","eligible_90d","sales_90d","realized_90d_contribution_before_acquisition","acquisition_spend"]
    _required(rows,fields)
    for row in rows:
        values={k:_number(row[k]) for k in fields[1:]}
        n,e=values["new_customers"],values["eligible_90d"]
        if not 0<=e<=n or n<0 or values["acquisition_spend"]<0:
            raise ValueError("客户队列、成熟分母或获客支出无效。")
        cac=_ratio(values["acquisition_spend"],n)
        value=_ratio(values["realized_90d_contribution_before_acquisition"],e)
        result["series"].append({"channel":str(row["channel"]),**values,"cac":cac,"realized_value_90d":value,
            "value_minus_cac":round(value-cac,6) if value is not None and cac is not None and e==n else None,"low_sample":e<30})
    _fact(result,"期间首购客户数",sum(r["new_customers"] for r in result["series"]),"客户")
    _fact(result,"渠道拉新支出",sum(r["acquisition_spend"] for r in result["series"]),"元")
    result["method"]="CAC按首次获客触点归属；90日价值只用完整[首购日,首购日+90天)内已实现贡献，拉新支出单列而不重复扣除。"
    result["reconciliation"]={"balanced":True,"complete_90d":all(r["eligible_90d"]==r["new_customers"] for r in result["series"])}
    result["limitations"]=["90日已实现贡献不是完整LTV；小于30客户标低样本。", "成熟样本与全部拉新客户不一致时，不直接相减给出价值减CAC。"]
    result["chart"]={"type":"scatter","title":"首次获客渠道成本与90日价值","x":"cac","y":["realized_value_90d"],"group_by":"channel","x_unit":"元/客户","unit":"元/客户"}


def customer_repeat_cohort(rows, config, result):
    _required(rows,["customer_id","first_date","customer_kind","sales","contribution_profit","eligible_90d","repurchased_90d"])
    if len({str(r["customer_id"]) for r in rows})!=len(rows):
        raise ValueError("客户贡献输入必须每客户一行。")
    groups=defaultdict(lambda:defaultdict(float))
    cohorts=defaultdict(lambda:defaultdict(float))
    for row in rows:
        kind=str(row["customer_kind"])
        if kind not in {"new","existing"}:
            raise ValueError("客户分类必须由固定统计期首购日期确定。")
        groups[kind]["customers"]+=1
        for key in ["sales","contribution_profit"]:
            groups[kind][key]+=_number(row[key])
        if kind=="new":
            cohort=str(row["first_date"])[:7]
            eligible=_number(row["eligible_90d"])
            repeat=_number(row["repurchased_90d"])
            if eligible not in {0,1} or repeat not in {0,1}:
                raise ValueError("成熟及复购标记只能为0/1。")
            cohorts[cohort]["customers"]+=1
            cohorts[cohort]["eligible_90d"]+=eligible
            cohorts[cohort]["repurchased_90d"]+=repeat if eligible else 0
    result["series"]=[{"cohort":c,**v,"repurchase_pct":_ratio(v["repurchased_90d"]*100,v["eligible_90d"]),"low_sample":v["eligible_90d"]<30} for c,v in sorted(cohorts.items())]
    result["tables"]=[_table("new_existing","固定统计期新老客贡献",[{"customer_kind":k,**v} for k,v in sorted(groups.items())],{"type":"bar","x":"customer_kind","y":["sales","contribution_profit"],"unit":"元"})]
    for kind,value in groups.items():
        _fact(result,kind+"客户销售额",value["sales"],"元")
        _fact(result,kind+"客户贡献利润",value["contribution_profit"],"元")
    result["method"]="固定全年新老客归属；新客2025内所有交易均归新客。首购月队列仅成熟90日者进入复购分母。"
    result["reconciliation"]={"balanced":True,"customers":len(rows),"sales":sum(v["sales"] for v in groups.values()),"contribution_profit":sum(v["contribution_profit"] for v in groups.values())}
    result["limitations"]=["复购为90日窗口至少两笔有效订单，不等于购买频次或长期留存。"]
    result["chart"]={"type":"bar","title":"首购月份90日复购率","x":"cohort","y":["repurchase_pct"],"unit":"%"}


def mrr_monthly_bridge(rows, config, result):
    _required(rows,["period","account_id","mrr","first_started"])
    periods=sorted({str(r["period"]) for r in rows})
    if len(periods)<2 or not config.get("complete_snapshot"):
        raise ValueError("连续MRR桥接需要至少两个完整账户月末快照。")
    accounts=defaultdict(dict)
    for row in rows:
        p,key=str(row["period"]),str(row["account_id"])
        if key in accounts[p]:
            raise ValueError("每月每账户只能一行MRR。")
        accounts[p][key]=_number(row["mrr"])
    top=[]
    for before,after in zip(periods,periods[1:]):
        a,b=date.fromisoformat(before),date.fromisoformat(after)
        if (b.year-a.year)*12+b.month-a.month != 1:
            raise ValueError("MRR月份不连续，不能把跨期差额当作单月变动。")
        part=_result("mrr_bridge",result["evidence_ids"])
        _mrr(rows,{"complete_snapshot":True,"baseline_period":before,"current_period":after},part)
        pieces={r["component"]:r["delta"] for r in part["series"]}
        result["series"].append({"period":after[:7],"opening_mrr":part["reconciliation"]["opening"],"closing_mrr":part["reconciliation"]["closing"],
            "new_mrr":pieces["新增"],"expansion":pieces["扩张"],"contraction":pieces["收缩"],"churn":pieces["流失"],"reactivation":pieces["恢复"],"residual":part["reconciliation"]["residual"]})
        changes=[{"period":after[:7],"account_id":key,"before":accounts[before].get(key,0),"after":accounts[after].get(key,0),"delta":accounts[after].get(key,0)-accounts[before].get(key,0)} for key in accounts[before].keys()|accounts[after].keys()]
        top.extend(sorted(changes,key=lambda r:(-abs(r["delta"]),r["account_id"]))[:5])
    components=[("new_mrr","新增"),("expansion","扩张"),("contraction","收缩"),("churn","流失"),("reactivation","恢复")]
    bridge=[{"component":name,"delta":sum(row[key] for row in result["series"])} for key,name in components]
    annual=[{"account_id":key,"opening_mrr":accounts[periods[0]].get(key,0),"closing_mrr":accounts[periods[-1]].get(key,0),"delta":accounts[periods[-1]].get(key,0)-accounts[periods[0]].get(key,0)} for key in accounts[periods[0]].keys()|accounts[periods[-1]].keys()]
    annual.sort(key=lambda row:(-abs(row["delta"]),row["account_id"]))
    result["tables"]=[_table("account_mrr_changes","每月影响最大的客户",top),
        _table("annual_mrr_bridge","报告期累计MRR净变动",bridge,{"type":"waterfall","x":"component","y":["delta"],"start_value":result["series"][0]["opening_mrr"],"unit":"元/月"}),
        _table("annual_account_changes","完整报告期客户MRR净变化前20",annual[:20])]
    _fact(result,"报告期首月期初MRR",result["series"][0]["opening_mrr"],"元/月")
    _fact(result,"报告期末MRR",result["series"][-1]["closing_mrr"],"元/月")
    result["method"]="逐连续月固定期初/期末账户快照，按首次开通区分新增和恢复，五项净变动逐月还原。"
    result["reconciliation"]={"balanced":all(abs(r["residual"])<1e-5 for r in result["series"]),"months":len(result["series"])}
    result["limitations"]=["快照桥接反映每月净变动，不代表期间全部事件毛额；不得把年付收款当MRR。"]
    result["chart"]={"type":"line","title":"月末MRR","x":"period","y":["closing_mrr"],"unit":"元/月"}


def account_retention(rows, config, result):
    _required(rows,["cohort","feature_group","accounts",*[f"{prefix}_{window}" for prefix in ["eligible","retained"] for window in [30,60,90]]])
    if len({(str(r["cohort"]),str(r["feature_group"])) for r in rows}) != len(rows):
        raise ValueError("每个注册月份与关键功能分组只能有一行完整账户汇总。")
    for row in rows:
        for window in [30,60,90]:
            eligible,retained=_number(row[f"eligible_{window}"]),_number(row[f"retained_{window}"])
            if not 0<=retained<=eligible<=_number(row["accounts"]):
                raise ValueError("留存分子分母或成熟账户数不一致。")
            result["series"].append({"cohort":str(row["cohort"]),"feature_group":str(row["feature_group"]),"window":f"D{window}",
                "retention_group":str(row["feature_group"])+f" / D{window}","eligible":eligible,"retained":retained,
                "retention_pct":_ratio(retained*100,eligible),"low_sample":eligible<30})
    result["method"]="账户去重；D30/60/90分别为注册后24–30/54–60/84–90天业务活跃。按注册后前7天[day0,day7)关键功能使用分组，不使用分组窗口之后的未来暴露。"
    result["reconciliation"]={"balanced":True,"groups":len(rows)}
    result["limitations"]=["小于30账户为低样本；差异是观察关联，不能称关键功能提升留存的因果效果。", "各窗口只用已完整观察队列，未成熟分母为0时显示NULL。"]
    result["chart"]={"type":"heatmap","title":"注册队列账户留存","x":"cohort","group_by":"retention_group","y":["retention_pct"],"unit":"%"}
    _fact(result,"留存分组数",len(rows),"组")
    _fact(result,"注册月份数",len({row["cohort"] for row in rows}),"月")
    _fact(result,"完整留存结果行数",len(result["series"]),"行")
    grouped=defaultdict(list)
    for row in result["series"]:
        grouped[(row["feature_group"],row["window"])].append(row)
    summaries=[]
    for (feature_group,window),part in sorted(grouped.items()):
        eligible=sum(row["eligible"] for row in part)
        retained=sum(row["retained"] for row in part)
        rates={row["cohort"]:row["retained"]*100/row["eligible"] for row in part if row["eligible"]>0}
        minimum=min(rates.values()) if rates else None
        maximum=max(rates.values()) if rates else None
        summaries.append({"feature_group":feature_group,"window":window,"eligible":eligible,"retained":retained,
            "retention_pct":_ratio(retained*100,eligible),"cohort_count":len(part),"eligible_cohort_count":len(rates),
            "low_sample_cohort_count":sum(0<row["eligible"]<30 for row in part),
            "ineligible_cohort_count":sum(row["eligible"]==0 for row in part),
            "min_retention_pct":round(minimum,6) if minimum is not None else None,
            "max_retention_pct":round(maximum,6) if maximum is not None else None,
            "min_retention_cohorts":", ".join(sorted(cohort for cohort,rate in rates.items() if rate==minimum)),
            "max_retention_cohorts":", ".join(sorted(cohort for cohort,rate in rates.items() if rate==maximum))})
        _fact(result,f"{feature_group} 汇总{window}留存率",_ratio(retained*100,eligible),"%",
              eligible=eligible,retained=retained,feature_group=feature_group,window=window)
    result["tables"]=[_table("retention_summary","完整队列按功能组和窗口汇总",summaries)]
    low_groups={(row["cohort"],row["feature_group"]) for row in result["series"] if 0<row["eligible"]<30}
    _fact(result,"存在低样本窗口的注册月份×功能组数",len(low_groups),"组")
    result["method"]+=" 全量汇总先相加成熟分母及留存分子再计算加权率，不平均队列百分比；极值基于全部有成熟账户的队列，保留并列月份。"
    result["limitations"].append("汇总极值包括低样本队列，不代表稳定趋势；low_sample_cohort_count仅计1–29账户，分母为0的未成熟队列另计ineligible_cohort_count。不同功能组汇总差异仍可能受队列构成混杂。")


def store_driver_comparison(rows, config, result):
    _required(rows,["period","store_id","open_days","visitors","transactions","sales"])
    before,after=_periods(rows,config)
    groups=defaultdict(dict)
    for row in rows:
        period=str(row["period"])
        if period in {before,after}:
            key=str(row["store_id"])
            if period in groups[key]:
                raise ValueError("每个可比门店每期间必须只有一行。")
            groups[key][period]=row
    excluded=[]
    for store,parts in sorted(groups.items()):
        if before not in parts or after not in parts:
            excluded.append(store)
            continue
        factors=[]
        for p in [before,after]:
            v={k:_number(parts[p][k]) for k in ["open_days","visitors","transactions","sales"]}
            if min(v["open_days"],v["visitors"],v["transactions"])<=0:
                raise ValueError("可比门店缺少有效营业、客流或交易观测，不能计算乘法分解。")
            factors.append([v["open_days"],v["visitors"]/v["open_days"],v["transactions"]/v["visitors"],v["sales"]/v["transactions"]])
        effects=_shapley_product(*factors)
        delta=_number(parts[after]["sales"])-_number(parts[before]["sales"])
        result["series"].append({"store_id":store,"sales_before":parts[before]["sales"],"sales_after":parts[after]["sales"],"sales_delta":delta,
            **{k:round(v,6) for k,v in zip(["open_days_effect","daily_traffic_effect","conversion_effect","aov_effect"],effects)},"residual":round(delta-sum(effects),6)})
    if not result["series"]:
        raise ValueError("没有完整的可比门店集合。")
    result["series"].sort(key=lambda r:r["sales_delta"])
    result["method"]="先固定同时期合资格同店，保留临时停业；逐店对营业天数×日均客流×交易/客流×客单价做24顺序对称分解。"
    result["reconciliation"]={"balanced":all(abs(r["residual"])<1e-5 for r in result["series"]),"comparable_stores":len(result["series"]),"excluded_stores":excluded}
    _fact(result,"同店销售下降门店数",sum(r["sales_delta"]<0 for r in result["series"]),"店")
    _fact(result,"可比门店销售差额",sum(r["sales_delta"] for r in result["series"]),"元")
    result["limitations"]=["客流仅门店粒度，不计算品类转化率。品类和日期只定位销售差异，不推断客流因果。"]
    result["chart"]={"type":"bar","title":"同店销售变化","x":"store_id","y":["sales_delta"],"unit":"元"}


def inventory_asof(rows, config, result):
    fields=["as_of_date","store_id","product_id","snapshot_date","stock_on_hand","sold_30d","pending_po","due_po_14d","overdue_po","transfer_in_due_14d"]
    _required(rows,[key for key in fields if key not in {"snapshot_date","stock_on_hand"}])
    if len({(str(r["store_id"]),str(r["product_id"])) for r in rows})!=len(rows):
        raise ValueError("时点库存输入必须每店每商品一行。")
    for row in rows:
        cutoff=date.fromisoformat(str(row["as_of_date"])[:10])
        if row.get("snapshot_date") is None or row.get("stock_on_hand") is None:
            result["series"].append({**row,"store_id":str(row["store_id"]),"product_id":str(row["product_id"]),"cover_days":None,"projected_stock_14d":None,"risk":"insufficient_observation","priority":"review_data"})
            continue
        snapshot=date.fromisoformat(str(row["snapshot_date"])[:10])
        if snapshot>cutoff or config.get("as_of_date") and str(cutoff)!=str(config["as_of_date"]):
            raise ValueError("库存快照超过历史时点，或查询时点与批准时点不一致。")
        values={k:_number(row[k]) for k in fields[4:]}
        if any(v<0 for v in values.values()) or values["due_po_14d"]>values["pending_po"]:
            raise ValueError("库存、销量或在途数量无效。")
        daily=values["sold_30d"]/30
        cover=_ratio(values["stock_on_hand"],daily)
        projected=values["stock_on_hand"]+values["due_po_14d"]+values["transfer_in_due_14d"]-daily*14
        stale=(cutoff-snapshot).days>7
        if stale:
            cover=projected=None
        risk="insufficient_observation" if stale else "shortage" if daily>0 and cover<7 else "excess" if values["stock_on_hand"]>0 and (daily==0 or cover>90) else "normal"
        result["series"].append({"store_id":str(row["store_id"]),"product_id":str(row["product_id"]),"product_name":row.get("product_name"),"category":row.get("category"),"snapshot_date":str(snapshot),
            **values,"cover_days":cover,"projected_stock_14d":round(projected,6) if projected is not None else None,"risk":risk,"priority":"review_data" if stale else "urgent" if risk=="shortage" and projected<0 else "watch" if risk!="normal" else "normal"})
    result["series"].sort(key=lambda r:({"urgent":0,"review_data":1,"watch":2,"normal":3}[r["priority"]],
        {"shortage":0,"insufficient_observation":1,"excess":2,"normal":3}[r["risk"]],-r["sold_30d"]))
    risks=[r for r in result["series"] if r["risk"]!="normal"]
    result["tables"]=[_table("inventory_attention","需优先核查的门店商品",risks)]
    _fact(result,"缺货风险对象数",sum(r["risk"]=="shortage" for r in result["series"]),"个")
    _fact(result,"积压对象数",sum(r["risk"]=="excess" for r in result["series"]),"个")
    _fact(result,"库存观测不足对象数",sum(r["risk"]=="insufficient_observation" for r in result["series"]),"个")
    result["method"]="只用时点前30日销售速度与已知库存，结合当时可见采购/已发运调拨的未来14日承诺；覆盖<7天提示紧缺，>90天或零销量有库存提示积压。"
    result["reconciliation"]={"balanced":True,"as_of_date":str(rows[0]["as_of_date"]),"objects":len(rows)}
    result["limitations"]=["14日覆盖是假设销售速度延续且承诺供货兑现的情景估计，不使用未来真实收货或销售，不是最优补货量。", "快照超过7天陈旧时标证据不足，需核查盘点和在途承诺。"]
    result["chart"]={"type":"heatmap","title":"时点库存覆盖天数","x":"store_id","group_by":"product_id","y":["cover_days"],"unit":"天"}


def enrich_evidence(tool,queries,config,result):
    """Consume complete auxiliary evidence, never the model's first-page sample."""
    from insight.analytics import _input
    auxiliary=config.get("detail_query_id")
    if not auxiliary:
        return
    rows,ids=_input(queries,{"query_id":auxiliary})
    result["evidence_ids"].extend(ids)
    if tool=="refund_diagnosis":
        _required(rows,["refunded_amount"])
        tables=[]
        for dimension in ["product_name","supplier_name","reason"]:
            if not all(dimension in row for row in rows):
                continue
            groups=defaultdict(float)
            for row in rows:
                groups[str(row[dimension])]+=_number(row["refunded_amount"])
            total=sum(groups.values())
            ordered=sorted(groups.items(),key=lambda item:(-item[1],item[0]))
            data=[{dimension:name,"refunded_amount":value,"amount_share_pct":_ratio(value*100,total)} for name,value in ordered]
            tables.append(_table("refund_"+dimension,"成熟同队列退款金额："+dimension,data,{"type":"bar","x":dimension,"y":["refunded_amount"],"unit":"元"}))
        result["tables"]=tables
        _fact(result,"同队列成功退款金额",sum(_number(row["refunded_amount"]) for row in rows),"元")
        result["limitations"].append("原因/商品跨组去重退款订单数不可相加；金额按去重退款笔及其归属明细可加，不解释为退款订单比例。")
    elif tool=="store_driver_comparison":
        _required(rows,["store_id","transaction_date","category","sales"])
        declining={row["store_id"] for row in result["series"] if row["sales_delta"]<0}
        baseline,current=_periods([{ "period":p } for p in sorted({str(row["transaction_date"])[:4]+"-Q"+str((int(str(row["transaction_date"])[5:7])-1)//3+1) for row in rows})],config)
        categories=defaultdict(lambda:defaultdict(float))
        daily=defaultdict(float)
        for row in rows:
            day=str(row["transaction_date"])[:10]
            period=day[:4]+"-Q"+str((int(day[5:7])-1)//3+1)
            if str(row["store_id"]) not in declining or period not in {baseline,current}:
                continue
            categories[str(row["category"])][period]+=_number(row["sales"])
            daily[day]+=_number(row["sales"])
        category_rows=[{"category":key,"sales_before":value[baseline],"sales_after":value[current],"sales_delta":value[current]-value[baseline]} for key,value in categories.items()]
        category_rows.sort(key=lambda row:row["sales_delta"])
        def bounds(period):
            year,quarter=int(period[:4]),int(period[-1])
            start=date(year,quarter*3-2,1)
            end=date(year+1,1,1) if quarter==4 else date(year,quarter*3+1,1)
            return start,end
        start,end=bounds(baseline)
        average=sum(value[baseline] for value in categories.values())/(end-start).days
        start,end=bounds(current)
        from datetime import timedelta
        dates=[]
        for offset in range((end-start).days):
            day=str(start+timedelta(days=offset))
            sales=daily.get(day,0)
            dates.append({"date":day,"sales":sales,"baseline_daily_average":average,"difference_to_baseline_average":sales-average})
        dates.sort(key=lambda row:(row["difference_to_baseline_average"],row["date"]))
        result["tables"]=[_table("declining_store_categories","下滑同店集合的品类销售差异",category_rows,{"type":"bar","x":"category","y":["sales_before","sales_after"],"unit":"元"}),
            _table("declining_store_dates","下滑同店销售偏低日期（相对基期日均）",dates[:20],{"type":"bar","x":"date","y":["difference_to_baseline_average"],"unit":"元"})]
        result["limitations"].append("日期偏差相对基期自然日日均，不是同星期配对或因果估计；表仅覆盖已确认下滑同店集合。")


HANDLERS={function.__name__:function for function in [period_overview,grouped_profit_change,customer_value_90d,customer_repeat_cohort,mrr_monthly_bridge,account_retention,store_driver_comparison,inventory_asof]}
