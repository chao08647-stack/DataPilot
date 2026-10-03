"""Readonly user-approved questions. No SQL, fixed answers or automatic execution."""
from copy import deepcopy
from hashlib import sha256

QUESTION_CATALOG_VERSION = "lightbi-2025-v1"
QUESTIONS = [
    {"id": "q01", "domain_id": "ecommerce", "title": "月度经营趋势", "question": "2025 年各月销售额、订单量、客单价和贡献利润如何变化？哪些月份值得重点关注？"},
    {"id": "q02", "domain_id": "ecommerce", "title": "季度收入与利润拆解", "question": "2025 年第四季度相比第三季度，销售额与贡献利润分别如何变化？按渠道、品类、折扣、退款和费用拆解。"},
    {"id": "q03", "domain_id": "ecommerce", "title": "获客成本与90日价值", "question": "2025 年各获客渠道的投入、新客成本和客户首购后 90 日价值有什么差异？"},
    {"id": "q04", "domain_id": "ecommerce", "title": "成熟订单退款与履约", "question": "2025 年第四季度成熟订单的退款，集中在哪些商品、供应商和原因？配送延迟订单的退款率是否更高？"},
    {"id": "q05", "domain_id": "ecommerce", "title": "新老客贡献与复购", "question": "2025 年新客和老客分别贡献多少销售额与利润？不同首购月份客户的后续复购有什么差异？"},
    {"id": "q06", "domain_id": "saas", "title": "注册增长与付费转化", "question": "第四季度相比第三季度，注册增长与 30 日付费转化是否同步？主要流失在哪个环节、哪些渠道和企业规模？"},
    {"id": "q07", "domain_id": "saas", "title": "MRR变动与客户贡献", "question": "2025 年每个月的 MRR 如何变化？新增、扩张、收缩、流失和恢复分别贡献多少，哪些客户影响最大？"},
    {"id": "q08", "domain_id": "saas", "title": "客户留存与关键功能", "question": "不同注册月份客户的 30／60／90 日留存如何？使用关键功能与未使用客户之间有什么差异？"},
    {"id": "q09", "domain_id": "retail", "title": "同店下滑拆解", "question": "第四季度哪些同店销售下滑？主要来自营业天数、客流、成交比例还是客单价，集中在哪些日期和品类？"},
    {"id": "q10", "domain_id": "retail", "title": "年末库存风险与调拨", "question": "截至 2025 年末，哪些门店畅销品存在缺货风险，哪些商品积压？结合在途采购和调拨，应优先处理哪些对象？"},
]


def get_questions(domain_id=None):
    return deepcopy([q for q in QUESTIONS if domain_id is None or q["domain_id"] == domain_id])


def get_question(question_id):
    for question in QUESTIONS:
        if question["id"] == question_id:
            return deepcopy(question)
    raise KeyError(question_id)


def catalog_run_key(question_id, domain_id, data_version, model_version):
    question = get_question(question_id)
    if question["domain_id"] != domain_id or not data_version or not model_version:
        raise ValueError("Catalog reservation requires matching domain and concrete versions")
    identity = "\0".join((QUESTION_CATALOG_VERSION, question_id, domain_id, data_version, model_version))
    return "catalog:" + sha256(identity.encode("utf-8")).hexdigest()
