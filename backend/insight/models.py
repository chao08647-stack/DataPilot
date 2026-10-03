from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class RunCreate(BaseModel):
    domain_id: str | None = None
    scenario_id: str | None = None
    question: str = Field(min_length=2, max_length=4000)
    thread_id: str | None = None
    context: dict = Field(default_factory=dict)
    question_id: str | None = Field(default=None, max_length=80)
    retry_failed: bool = False

    @model_validator(mode="after")
    def domain_alias(self):
        if not (self.domain_id or self.scenario_id) or self.domain_id and self.scenario_id and self.domain_id != self.scenario_id:
            raise ValueError("必须提供唯一的 domain_id。")
        self.domain_id = self.scenario_id = self.domain_id or self.scenario_id
        if set(self.context) - {"dashboard_id", "card_id", "filters", "source_run_id", "query_id"}:
            raise ValueError("上下文只接受已保存证据的引用。")
        return self


class ResumeRequest(BaseModel):
    answer: str = Field(min_length=1, max_length=2000)


class MemoryUpdate(BaseModel):
    active: bool | None = None
    value: Any = None
    content: str | None = Field(default=None, max_length=4000)


class Intent(BaseModel):
    normalized_question: str
    metric_ids: list[str] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    filters: list[str] = Field(default_factory=list)
    time_range: str = ""
    time_source: Literal["user", "context", "default"] = Field(default="user", description="时间来自当次明确要求、可核实会话上下文或业务域默认报告期；未指定年份的注册月份/留存窗口不代表全量历史")
    task_tags: list[str] = Field(default_factory=list)
    query_type: Literal["detail", "aggregate", "comparison", "trend", "ranking"] = "aggregate"
    clarification: str | None = None
    dimension_override: bool = Field(default=False, description="仅当本次用户明确要求总额、不分组或明确指定维度时为true；不要把未提及维度解释为拒绝历史默认维度")


class Discovery(BaseModel):
    metric_ids: list[str]
    tables: list[str]
    dimensions: list[str] = Field(default_factory=list)
    filters: list[str] = Field(default_factory=list)
    time_range: str = ""
    rationale: str
    clarification: str | None = None


class QueryDraft(BaseModel):
    id: str
    sql: str
    rationale: str
    metric_ids: list[str] = Field(default_factory=list)


class SQLPlan(BaseModel):
    queries: list[QueryDraft] = Field(min_length=1, max_length=12)


class SingleQueryPlan(SQLPlan):
    """One diagnostic input is one bounded SQL task, not an entire investigation."""
    queries: list[QueryDraft] = Field(min_length=1, max_length=1)


class AnalysisStep(BaseModel):
    objective: str
    metric_ids: list[str]
    grain: str
    dimensions: list[str] = Field(default_factory=list)


class PeriodSelection(BaseModel):
    model_config = {"extra": "forbid"}
    baseline_period: str = Field(min_length=1, max_length=20, description="基期的period列原值，例如2025-Q3")
    current_period: str = Field(min_length=1, max_length=20, description="报告期的period列原值，例如2025-Q4")


class Investigation(BaseModel):
    steps: list[AnalysisStep] = Field(min_length=1, max_length=6)
    diagnostic_ids: list[str] = Field(default_factory=list, max_length=2)
    evidence_needed: list[str] = Field(default_factory=list)
    period_selections: dict[str, PeriodSelection] = Field(default_factory=dict, description="仅period_policy=pair工具且用户明确要求两期比较时，以diagnostic_id为key，value含baseline_period/current_period；格式与输入period列一致。全年逐月/连续趋势不填，严禁将日期范围首尾误当仅查询的两个期间。")


class Review(BaseModel):
    action: Literal["approve", "repair", "clarify"]
    feedback: str = ""
    clarification: str | None = None


class Finding(BaseModel):
    title: str
    detail: str
    evidence_ids: list[str] = Field(min_length=1)
    kind: Literal["fact", "decomposition", "hypothesis", "insufficient_data"] = "fact"
    business_objects: list[str] = Field(default_factory=list)


class Analysis(BaseModel):
    summary: str
    findings: list[Finding] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    follow_up_question: str | None = None
    evidence_gaps: list[str] = Field(default_factory=list)


class ChartSeries(BaseModel):
    column: str
    name: str | None = None
    unit: str | None = None
    type: Literal["bar", "line", "area", "scatter"] | None = None
    axis: Literal["left", "right"] = "left"
    stack: str | None = None


class Chart(BaseModel):
    type: Literal["bar", "line", "pie", "donut", "kpi", "table", "waterfall", "funnel", "heatmap", "horizontal_bar", "stacked_bar", "area", "scatter", "combo"]
    title: str
    query_id: str
    x: str | None = None
    y: list[str] = Field(default_factory=list)
    group_by: str | None = None
    start_value: float | None = None
    total_column: str | None = None
    series: list[ChartSeries] = Field(default_factory=list, max_length=12)
    unit: str | None = None
    x_unit: str | None = None
    x_name: str | None = None
    size: str | None = None
    value_divisor: float = Field(default=1, gt=0)


class Charts(BaseModel):
    charts: list[Chart] = Field(default_factory=list, max_length=8)


class Summary(BaseModel):
    summary: str
