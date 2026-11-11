"""FastAPI 入口：归属修订 API 路由与应用装配。"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse

from .database import init_db
from .errors import AppError
from .schemas import (
    AttributionQueryRequest,
    AttributionResult,
    HierarchyAggregationRequest,
    HierarchyAggregationResult,
    HierarchyPublishRequest,
    HierarchyRelationsQueryRequest,
    HierarchyVersionOut,
    RevisionOut,
    RevisionSubmitRequest,
    SegmentsQueryRequest,
)
from .service import (
    aggregate_hierarchy,
    attribute_hours,
    list_hierarchy_relations,
    list_hierarchy_versions,
    list_revisions,
    list_segments,
    publish_hierarchy_version,
    submit_revision,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时确保表结构存在（幂等）。
    init_db()
    yield


app = FastAPI(
    title="工时归属与成本中心层级汇总 API",
    description=(
        "财务数仓服务：人事调岗补录导致的工时归属修正，以及按固定层级版本"
        "汇总到组织成本中心树（管理账口径）。\n\n"
        "**归属修订**\n\n"
        "- 每批成功提交返回递增修订号，批次内校验失败则整批拒绝；\n"
        "- 生效日期区间为左闭右开 `[effective_from, effective_to)`，允许空档；\n"
        "- 按修订号查询时只可见该修订号及之前的数据，后续跨段补录不改变旧修订查询结果。\n\n"
        "**成本中心层级**\n\n"
        "- 每批父子关系经整批校验后发布为一个递增的层级版本号；\n"
        "- 同一成本中心同期间只能有一个父级，且不能成环，否则整批拒绝；\n"
        "- 汇总固定「归属修订号 + 层级版本号」，逐条解析成本中心到根的路径，"
        "按节点累加小时；任一路径缺失或冲突则整批拒绝，绝不返回部分汇总；\n"
        "- 发布新版本不会改变旧版本的汇总结果（append-only）。"
    ),
    version="1.1.0",
    lifespan=lifespan,
)


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "code": exc.code,
                "message": exc.message,
                **({"details": exc.details} if exc.details else {}),
            }
        },
    )


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/docs")


@app.get("/health", tags=["运维"], summary="健康检查")
def health() -> dict:
    return {"status": "ok"}


@app.post(
    "/api/revisions",
    response_model=RevisionOut,
    status_code=201,
    tags=["归属修订"],
    summary="提交一批员工-成本中心归属区间",
    description=(
        "整批校验通过后写入，返回递增修订号。\n\n"
        "拒绝条件（整批回滚，数据与修订号均不变）：\n"
        "1. 起日不早于止日（`effective_from >= effective_to`）；\n"
        "2. 批内同一员工的区间相互重叠（首尾相接不算重叠）。"
    ),
)
def create_revision(req: RevisionSubmitRequest) -> RevisionOut:
    return RevisionOut(**submit_revision(req))


@app.get("/api/revisions", tags=["归属修订"], summary="列出全部修订号")
def get_revisions() -> list[RevisionOut]:
    return [RevisionOut(**row) for row in list_revisions()]


@app.post(
    "/api/attribution/query",
    response_model=list[AttributionResult],
    tags=["归属查询"],
    summary="按指定修订号的口径逐条查询工时归属",
    description=(
        "对每条工时记录返回成本中心或未归属。只可见 `revision_id` 及之前的补录；"
        "同日期点存在多个历史区间时取修订号最大者。`revision_id=0` 表示空口径。"
    ),
)
def query_attribution(req: AttributionQueryRequest) -> list[AttributionResult]:
    return attribute_hours(req)


@app.post(
    "/api/revisions/{revision_id}/segments",
    tags=["归属修订"],
    summary="查看某个修订号下提交的原始区间",
)
def get_revision_segments(revision_id: int, req: SegmentsQueryRequest | None = None) -> list[dict]:
    employee_id = req.employee_id if req else None
    return list_segments(revision_id, employee_id)


# ---------------------------------------------------------------------------
# 成本中心层级与汇总
# ---------------------------------------------------------------------------


@app.post(
    "/api/hierarchy/versions",
    response_model=HierarchyVersionOut,
    status_code=201,
    tags=["层级版本"],
    summary="发布一批成本中心父子关系，生成层级版本",
    description=(
        "整批校验通过后写入，返回递增的 `hierarchy_version_id`。\n\n"
        "拒绝条件（整批回滚，关系与版本号均不变）：\n"
        "1. 起日不早于止日（`effective_from >= effective_to`）；\n"
        "2. 同一成本中心在重叠期间出现多个父级（首尾相接不算重叠）；\n"
        "3. 父子关系成环（任意生效日期上沿父级指针回到自身）。\n\n"
        "`parent_cost_center` 省略或为 `null` 表示把该成本中心声明为根节点。\n\n"
        "版本 N 可见版本号 <= N 的全部关系；同一成本中心同一日期被多个版本声明时，"
        "版本号最大者生效（新版本覆盖旧版本）。"
    ),
)
@app.post(
    "/api/hierarchy/publish",
    response_model=HierarchyVersionOut,
    status_code=201,
    include_in_schema=False,
)
@app.post(
    "/api/hierarchy/revisions",
    response_model=HierarchyVersionOut,
    status_code=201,
    include_in_schema=False,
)
def publish_hierarchy(req: HierarchyPublishRequest) -> HierarchyVersionOut:
    return HierarchyVersionOut(**publish_hierarchy_version(req))


@app.get(
    "/api/hierarchy/versions",
    response_model=list[HierarchyVersionOut],
    tags=["层级版本"],
    summary="列出全部层级版本号",
)
@app.get(
    "/api/hierarchy/revisions",
    response_model=list[HierarchyVersionOut],
    include_in_schema=False,
)
def get_hierarchy_versions() -> list[HierarchyVersionOut]:
    return [HierarchyVersionOut(**row) for row in list_hierarchy_versions()]


@app.get(
    "/api/hierarchy/versions/{hierarchy_version_id}/relations",
    tags=["层级版本"],
    summary="查看某个层级版本提交的原始关系（审计）",
)
def get_hierarchy_relations(
    hierarchy_version_id: int, cost_center: str | None = None
) -> list[dict]:
    return list_hierarchy_relations(hierarchy_version_id, cost_center)


@app.post(
    "/api/hierarchy/versions/{hierarchy_version_id}/relations",
    tags=["层级版本"],
    summary="查看某个层级版本提交的原始关系（审计，可按成本中心过滤）",
)
def post_hierarchy_relations(
    hierarchy_version_id: int,
    req: HierarchyRelationsQueryRequest | None = None,
) -> list[dict]:
    return list_hierarchy_relations(
        hierarchy_version_id, req.cost_center if req else None
    )


@app.post(
    "/api/aggregation/query",
    response_model=HierarchyAggregationResult,
    tags=["层级汇总"],
    summary="按固定归属修订号与层级版本汇总工时到各节点",
    description=(
        "对每条工时记录，先用 `revision_id` 解析员工成本中心，再在截至 "
        "`hierarchy_version_id` 的可见关系（版本号 <= 该版本）中沿父级指针解析到"
        "根节点，并把小时数累加到路径上的每个节点。同一期间多版本声明时取版本号"
        "最大者。\n\n"
        "- 响应 `records` 给出逐条的归属与 `path`（叶子 -> 根）；\n"
        "- 响应 `nodes`/`totals` 给出各节点小时汇总（含自身及全部后代）；\n"
        "- 子节点小时会同时计入其所有祖先，因此根节点小时 = 全部已归属小时。\n\n"
        "**整批拒绝**：任一记录未归属，或成本中心在该版本/该日期缺少唯一父级路径"
        "（缺失、冲突、成环），接口返回 400，不返回任何部分汇总。"
    ),
)
@app.post(
    "/api/hierarchy/aggregation/query",
    response_model=HierarchyAggregationResult,
    include_in_schema=False,
)
@app.post(
    "/api/hierarchy/aggregate",
    response_model=HierarchyAggregationResult,
    include_in_schema=False,
)
def aggregate_by_hierarchy(req: HierarchyAggregationRequest) -> HierarchyAggregationResult:
    return aggregate_hierarchy(req)
