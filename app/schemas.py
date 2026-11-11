"""Pydantic 请求/响应模型。

日期统一使用 ISO 8601（yyyy-mm-dd），SQLite 内以 TEXT 存储；
ISO 日期字符串的字典序与时间序一致，可直接做半开区间比较。
"""
from __future__ import annotations

from datetime import date

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator


class SegmentIn(BaseModel):
    """人事导出的一条员工-成本中心生效区间（左闭右开）。"""

    employee_id: str = Field(..., min_length=1, description="员工编号", examples=["E001"])
    cost_center: str = Field(..., min_length=1, description="成本中心编号", examples=["CC-SH-01"])
    effective_from: date = Field(..., description="生效起日（含）", examples=["2026-01-01"])
    effective_to: date = Field(..., description="生效止日（不含）", examples=["2026-04-01"])

    @field_validator("employee_id", "cost_center")
    @classmethod
    def _strip_and_require(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("不能为空字符串")
        return v


class RevisionSubmitRequest(BaseModel):
    """一批补录/归属区间。整批要么全部成功，要么整批拒绝。"""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "note": "HR 初始归属 Q1",
                "entries": [
                    {
                        "employee_id": "E001",
                        "cost_center": "CC-SH-01",
                        "effective_from": "2026-01-01",
                        "effective_to": "2026-04-01",
                    },
                    {
                        "employee_id": "E002",
                        "cost_center": "CC-BJ-01",
                        "effective_from": "2026-01-01",
                        "effective_to": "2026-04-01",
                    },
                ],
            }
        }
    )

    entries: list[SegmentIn] = Field(..., min_length=1, description="本批归属区间（至少一条）")
    note: str | None = Field(default=None, description="备注，如人事调岗补录批次号", examples=["HR 调岗补录 2026-11"])


class RevisionOut(BaseModel):
    revision_id: int
    entry_count: int
    note: str | None
    created_at: str


class WorkHourRecord(BaseModel):
    """工时系统导出的一条工时记录。"""

    employee_id: str = Field(
        ...,
        min_length=1,
        validation_alias=AliasChoices("employee_id", "employee", "employee_no"),
        examples=["E001"],
    )
    work_date: date = Field(
        ...,
        validation_alias=AliasChoices("work_date", "date", "workday"),
        description="工作日期",
        examples=["2026-02-15"],
    )
    hours: float = Field(
        ...,
        validation_alias=AliasChoices("hours", "hour", "duration_hours"),
        description="小时数（原样回显，不参与归属判断）",
        examples=[8.0],
    )

    @field_validator("employee_id")
    @classmethod
    def _strip_employee(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("不能为空字符串")
        return v


class AttributionQueryRequest(BaseModel):
    """按指定修订号的口径查询工时归属。"""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "revision_id": 1,
                "records": [
                    {"employee_id": "E001", "work_date": "2026-03-15", "hours": 8},
                    {"employee_id": "E002", "work_date": "2026-02-10", "hours": 7.5},
                ],
            }
        }
    )

    revision_id: int = Field(..., ge=0, description="月结口径修订号；0 表示尚无任何补录的空口径")
    records: list[WorkHourRecord] = Field(..., min_length=1, description="待归属的工时记录")


class AttributionResult(BaseModel):
    employee_id: str
    work_date: date
    hours: float
    revision_id: int
    cost_center: str | None = Field(..., description="成本中心；None 表示未归属")
    attributed: bool
    matched_revision_id: int | None = Field(..., description="命中区间所属的修订号，便于审计")


class SegmentsQueryRequest(BaseModel):
    employee_id: str | None = Field(default=None, description="可选，按员工过滤")


# ---------------------------------------------------------------------------
# 成本中心层级（管理账汇总）
# ---------------------------------------------------------------------------


class HierarchyRelationIn(BaseModel):
    """一条成本中心父子关系及其生效区间（左闭右开）。

    `parent_cost_center` 省略或为 null 时，表示在区间内把该成本中心
    显式声明为根节点（没有父级）。
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "cost_center": "CC-GZ-02",
                "parent_cost_center": "CC-SOUTH",
                "effective_from": "2026-01-01",
                "effective_to": "2027-01-01",
            }
        }
    )

    cost_center: str = Field(
        ...,
        min_length=1,
        validation_alias=AliasChoices("cost_center", "child_cost_center", "child"),
        description="子成本中心编号",
        examples=["CC-GZ-02"],
    )
    parent_cost_center: str | None = Field(
        default=None,
        validation_alias=AliasChoices("parent_cost_center", "parent", "parent_cc"),
        description="父成本中心编号；null 表示该期间内显式声明为根节点",
        examples=["CC-SOUTH"],
    )
    effective_from: date = Field(
        ...,
        validation_alias=AliasChoices("effective_from", "valid_from", "start_date"),
        description="生效起日（含）",
        examples=["2026-01-01"],
    )
    effective_to: date = Field(
        ...,
        validation_alias=AliasChoices("effective_to", "valid_to", "end_date"),
        description="生效止日（不含）",
        examples=["2027-01-01"],
    )

    @field_validator("cost_center")
    @classmethod
    def _strip_cost_center(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("cost_center 不能为空字符串")
        return v

    @field_validator("parent_cost_center")
    @classmethod
    def _strip_parent(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v:
            raise ValueError("parent_cost_center 不能为空字符串；根节点请使用 null")
        return v


class HierarchyPublishRequest(BaseModel):
    """发布一批层级关系，整批校验通过后生成一个层级版本。"""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "note": "2026 组织架构",
                "relations": [
                    {
                        "cost_center": "CC-SH-01",
                        "parent_cost_center": "CC-EAST",
                        "effective_from": "2026-01-01",
                        "effective_to": "2027-01-01",
                    },
                    {
                        "cost_center": "CC-BJ-01",
                        "parent_cost_center": "CC-NORTH",
                        "effective_from": "2026-01-01",
                        "effective_to": "2027-01-01",
                    },
                    {
                        "cost_center": "CC-GZ-02",
                        "parent_cost_center": "CC-SOUTH",
                        "effective_from": "2026-01-01",
                        "effective_to": "2027-01-01",
                    },
                    {
                        "cost_center": "CC-EAST",
                        "parent_cost_center": "CC-ROOT",
                        "effective_from": "2026-01-01",
                        "effective_to": "2027-01-01",
                    },
                    {
                        "cost_center": "CC-NORTH",
                        "parent_cost_center": "CC-ROOT",
                        "effective_from": "2026-01-01",
                        "effective_to": "2027-01-01",
                    },
                    {
                        "cost_center": "CC-SOUTH",
                        "parent_cost_center": "CC-ROOT",
                        "effective_from": "2026-01-01",
                        "effective_to": "2027-01-01",
                    },
                    {
                        "cost_center": "CC-ROOT",
                        "parent_cost_center": None,
                        "effective_from": "2026-01-01",
                        "effective_to": "2027-01-01",
                    },
                ],
            }
        }
    )

    relations: list[HierarchyRelationIn] = Field(
        ...,
        min_length=1,
        validation_alias=AliasChoices("relations", "entries"),
        description="本批层级父子关系（至少一条）",
    )
    note: str | None = Field(
        default=None,
        description="备注，如组织架构调整单号",
        examples=["2026 年组织架构：广州并入华南大区"],
    )


class HierarchyVersionOut(BaseModel):
    hierarchy_version_id: int
    relation_count: int
    note: str | None
    created_at: str


class HierarchyRelationsQueryRequest(BaseModel):
    cost_center: str | None = Field(
        default=None, description="可选，按子成本中心过滤"
    )


class HierarchyAggregationRequest(BaseModel):
    """按固定「归属修订号 + 层级版本号」汇总工时。"""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "revision_id": 2,
                "hierarchy_version_id": 1,
                "records": [
                    {"employee_id": "E001", "work_date": "2026-05-15", "hours": 8},
                    {"employee_id": "E001", "work_date": "2026-03-15", "hours": 8},
                    {"employee_id": "E002", "work_date": "2026-02-10", "hours": 7.5},
                ],
            }
        }
    )

    revision_id: int = Field(
        ...,
        ge=0,
        validation_alias=AliasChoices(
            "revision_id", "attribution_revision_id", "attribution_revision"
        ),
        description="归属修订号（与 /api/attribution/query 的 revision_id 相同）；0 表示空口径",
    )
    hierarchy_version_id: int = Field(
        ...,
        ge=1,
        validation_alias=AliasChoices(
            "hierarchy_version_id", "hierarchy_version", "hierarchy_id",
            "version_id", "version",
        ),
        description="层级版本号；聚合可见版本号 <= 该值的全部关系，后续发布不影响",
    )
    records: list[WorkHourRecord] = Field(
        ...,
        min_length=1,
        validation_alias=AliasChoices("records", "work_records", "entries"),
        description="待汇总的工时明细",
    )


class AggregatedRecord(BaseModel):
    """单条工时的归属与层级路径。"""

    employee_id: str
    work_date: date
    hours: float
    cost_center: str
    attributed: bool
    matched_revision_id: int | None
    path: list[str] = Field(
        ...,
        description="从员工成本中心到根节点的路径（叶子 -> 根）",
        examples=[["CC-GZ-02", "CC-SOUTH", "CC-ROOT"]],
    )


class CostCenterNodeTotal(BaseModel):
    """层级中某个节点累计的小时数（含自身及全部后代）。"""

    cost_center: str
    hours: float
    record_count: int = Field(..., description="贡献到该节点的工时记录条数")


class HierarchyAggregationResult(BaseModel):
    revision_id: int
    attribution_revision_id: int = Field(..., description="与 revision_id 相同，便于审计")
    hierarchy_version_id: int
    record_count: int
    records: list[AggregatedRecord] = Field(..., description="逐条工时的归属与路径")
    nodes: list[CostCenterNodeTotal] = Field(
        ..., description="各节点小时汇总（按 cost_center 升序）"
    )
    node_totals: list[CostCenterNodeTotal] = Field(
        ..., description="与 nodes 相同（便于按不同客户端习惯读取的别名）"
    )
    totals: dict[str, float] = Field(..., description="cost_center -> hours 的便捷映射")

