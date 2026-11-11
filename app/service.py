"""核心业务逻辑：批次校验、修订提交、按修订口径归属查询。"""
from __future__ import annotations

from collections import defaultdict
from sqlite3 import Connection

from .database import get_conn
from .errors import AppError
from .schemas import (
    AggregatedRecord,
    AttributionQueryRequest,
    AttributionResult,
    CostCenterNodeTotal,
    HierarchyAggregationRequest,
    HierarchyAggregationResult,
    HierarchyPublishRequest,
    RevisionSubmitRequest,
    SegmentIn,
)


def validate_batch(entries: list[SegmentIn]) -> None:
    """整批写入前的全部校验。任何一条不通过即抛 AppError，不触碰数据库。

    规则：
      1. 起日必须严格早于止日（左闭右开 [from, to)）；
      2. 同一员工在本批内的区间不得重叠（首尾相接允许：a.to == b.from 不重叠）。
    """
    # 规则 1：起日不早于止日 -> 拒绝（from >= to 非法）
    for idx, e in enumerate(entries):
        if e.effective_from >= e.effective_to:
            raise AppError(
                code="INVALID_DATE_RANGE",
                message=(
                    f"第 {idx + 1} 条记录起日 {e.effective_from.isoformat()} "
                    f"不早于止日 {e.effective_to.isoformat()}，左闭右开区间要求起日严格早于止日"
                ),
                details={
                    "index": idx,
                    "employee_id": e.employee_id,
                    "effective_from": e.effective_from.isoformat(),
                    "effective_to": e.effective_to.isoformat(),
                },
            )

    # 规则 2：批内同员工区间重叠检查（半开区间重叠 <=> a.from < b.to and b.from < a.to）
    by_employee: dict[str, list[tuple[int, SegmentIn]]] = defaultdict(list)
    for idx, e in enumerate(entries):
        by_employee[e.employee_id].append((idx, e))

    for employee_id, items in by_employee.items():
        ordered = sorted(items, key=lambda pair: (pair[1].effective_from, pair[1].effective_to))
        for (i_a, a), (i_b, b) in zip(ordered, ordered[1:]):
            if a.effective_from < b.effective_to and b.effective_from < a.effective_to:
                raise AppError(
                    code="OVERLAPPING_SEGMENTS",
                    message=(
                        f"员工 {employee_id} 在本批存在重叠区间："
                        f"第 {i_a + 1} 条 [{a.effective_from.isoformat()}, {a.effective_to.isoformat()}) "
                        f"与第 {i_b + 1} 条 [{b.effective_from.isoformat()}, {b.effective_to.isoformat()})"
                    ),
                    details={
                        "employee_id": employee_id,
                        "indices": [i_a, i_b],
                    },
                )


def submit_revision(req: RevisionSubmitRequest) -> dict:
    """提交一批归属区间，成功返回新的递增修订号。

    修订号分配与区间插入在同一个 BEGIN IMMEDIATE 事务内完成：
    校验失败/插入失败都会回滚，修订号与数据均保持不变。
    """
    validate_batch(req.entries)

    with get_conn() as conn:
        # 立即拿写锁，避免两个并发批次同时分配到同一个修订号。
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "INSERT INTO revisions (entry_count, note) VALUES (?, ?)",
            (len(req.entries), req.note),
        )
        revision_id = cur.lastrowid
        conn.executemany(
            """
            INSERT INTO attribution_segments
                (revision_id, employee_id, cost_center, effective_from, effective_to)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    revision_id,
                    e.employee_id,
                    e.cost_center,
                    e.effective_from.isoformat(),
                    e.effective_to.isoformat(),
                )
                for e in req.entries
            ],
        )
        row = conn.execute(
            "SELECT revision_id, entry_count, note, created_at FROM revisions WHERE revision_id = ?",
            (revision_id,),
        ).fetchone()
    return dict(row)


def _lookup_one(
    conn: Connection, revision_id: int, employee_id: str, work_date_iso: str
) -> tuple[str | None, int | None]:
    """在 revision_id 口径下定位单条工时的成本中心。

    仅可见 revision_id <= 给定修订号 的区间；同日期点命中多个历史区间时，
    取修订号最大者（后补录覆盖先补录），区间外/空档则未归属。
    """
    row = conn.execute(
        """
        SELECT cost_center, revision_id AS matched_revision_id
        FROM attribution_segments
        WHERE employee_id = ?
          AND effective_from <= ?
          AND effective_to   > ?
          AND revision_id <= ?
        ORDER BY revision_id DESC, id DESC
        LIMIT 1
        """,
        (employee_id, work_date_iso, work_date_iso, revision_id),
    ).fetchone()
    if row is None:
        return None, None
    return row["cost_center"], row["matched_revision_id"]


def attribute_hours(req: AttributionQueryRequest) -> list[AttributionResult]:
    """逐条返回工时记录在指定修订口径下的成本中心（或未归属）。"""
    with get_conn() as conn:
        max_row = conn.execute("SELECT COALESCE(MAX(revision_id), 0) AS r FROM revisions").fetchone()
        if req.revision_id > max_row["r"]:
            raise AppError(
                code="REVISION_NOT_FOUND",
                message=f"修订号 {req.revision_id} 不存在，当前最大修订号为 {max_row['r']}",
                status_code=404,
            )

        results: list[AttributionResult] = []
        for rec in req.records:
            cost_center, matched = _lookup_one(
                conn, req.revision_id, rec.employee_id, rec.work_date.isoformat()
            )
            results.append(
                AttributionResult(
                    employee_id=rec.employee_id,
                    work_date=rec.work_date,
                    hours=rec.hours,
                    revision_id=req.revision_id,
                    cost_center=cost_center,
                    attributed=cost_center is not None,
                    matched_revision_id=matched,
                )
            )
    return results


def list_revisions() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT revision_id, entry_count, note, created_at FROM revisions ORDER BY revision_id"
        ).fetchall()
    return [dict(r) for r in rows]


def list_segments(revision_id: int, employee_id: str | None) -> list[dict]:
    with get_conn() as conn:
        exists = conn.execute(
            "SELECT 1 FROM revisions WHERE revision_id = ?", (revision_id,)
        ).fetchone()
        if exists is None:
            raise AppError(
                code="REVISION_NOT_FOUND",
                message=f"修订号 {revision_id} 不存在",
                status_code=404,
            )
        if employee_id:
            rows = conn.execute(
                """
                SELECT revision_id, employee_id, cost_center, effective_from, effective_to
                FROM attribution_segments
                WHERE revision_id = ? AND employee_id = ?
                ORDER BY employee_id, effective_from
                """,
                (revision_id, employee_id.strip()),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT revision_id, employee_id, cost_center, effective_from, effective_to
                FROM attribution_segments
                WHERE revision_id = ?
                ORDER BY employee_id, effective_from
                """,
                (revision_id,),
            ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# 成本中心层级：批次校验、版本发布、固定版本汇总
# ---------------------------------------------------------------------------


def _active_edges_at(
    entries: list, target_date
) -> dict[str, str]:
    """返回 target_date 当天生效的 child -> parent 边（忽略根节点声明）。

    区间为左闭右开；同一 child 若出现多条生效关系，调用方应已通过重叠校验，
    这里保留第一条以免 KeyError，真正的冲突会在聚合查询阶段显式拒绝。
    """
    edges: dict[str, str] = {}
    for e in entries:
        if e.parent_cost_center is None:
            continue
        if e.effective_from <= target_date < e.effective_to:
            edges.setdefault(e.cost_center, e.parent_cost_center)
    return edges


def _find_cycle(edges: dict[str, str]) -> list[str] | None:
    """在出度 <= 1 的图中找环；返回环上的节点序列（首尾相同），无环返回 None。"""
    for start in edges:
        seen: dict[str, int] = {}
        order: list[str] = []
        cur = start
        while cur in edges:
            if cur in seen:
                return order[seen[cur]:] + [cur]
            seen[cur] = len(order)
            order.append(cur)
            cur = edges[cur]
    return None


def validate_hierarchy_batch(entries: list) -> None:
    """层级关系批次整批校验；任何一条不通过即拒绝，不触碰数据库。

    规则：
      1. 起日必须严格早于止日（左闭右开）；
      2. 同一成本中心在本批内的生效区间不得重叠——同一期间只能有一个父级
         （首尾相接允许：a.to == b.from 不算重叠）；
      3. 不允许成环（在任意一个生效日期上沿父级指针都不能回到自身）。
    """
    # 规则 1：日期区间合法性
    for idx, e in enumerate(entries):
        if e.effective_from >= e.effective_to:
            raise AppError(
                code="INVALID_DATE_RANGE",
                message=(
                    f"第 {idx + 1} 条层级关系起日 {e.effective_from.isoformat()} "
                    f"不早于止日 {e.effective_to.isoformat()}，左闭右开区间要求起日严格早于止日"
                ),
                details={
                    "index": idx,
                    "cost_center": e.cost_center,
                    "effective_from": e.effective_from.isoformat(),
                    "effective_to": e.effective_to.isoformat(),
                },
            )

    # 规则 2：同一成本中心同期间只能有一个父级（半开区间重叠判定）
    by_cost_center: dict[str, list[tuple[int, object]]] = defaultdict(list)
    for idx, e in enumerate(entries):
        by_cost_center[e.cost_center].append((idx, e))

    for cost_center, items in by_cost_center.items():
        ordered = sorted(items, key=lambda pair: (pair[1].effective_from, pair[1].effective_to))
        for (i_a, a), (i_b, b) in zip(ordered, ordered[1:]):
            if a.effective_from < b.effective_to and b.effective_from < a.effective_to:
                raise AppError(
                    code="OVERLAPPING_PARENT_ASSIGNMENTS",
                    message=(
                        f"成本中心 {cost_center} 在本批存在重叠期间："
                        f"第 {i_a + 1} 条 [{a.effective_from.isoformat()}, {a.effective_to.isoformat()}) "
                        f"与第 {i_b + 1} 条 [{b.effective_from.isoformat()}, {b.effective_to.isoformat()})；"
                        "同一成本中心同期间只能有一个父级"
                    ),
                    details={
                        "cost_center": cost_center,
                        "indices": [i_a, i_b],
                    },
                )

    # 规则 3：不能成环。只需检查各区间起点：生效边集合只在 effective_from
    # 处发生变化，若某天存在环，则环上各边的最晚起始日（必为某个 from）
    # 当天环仍然存在。
    candidate_dates = sorted({e.effective_from for e in entries})
    for d in candidate_dates:
        cycle = _find_cycle(_active_edges_at(entries, d))
        if cycle is not None:
            raise AppError(
                code="HIERARCHY_CYCLE",
                message=(
                    f"层级关系在 {d.isoformat()} 生效期间存在环："
                    + " -> ".join(cycle)
                ),
                details={
                    "effective_date": d.isoformat(),
                    "cycle": cycle,
                },
            )


def publish_hierarchy_version(req: HierarchyPublishRequest) -> dict:
    """发布一个层级版本：整批校验通过后写入，返回递增的层级版本号。

    与归属修订一致，版本号分配与关系插入在同一个 BEGIN IMMEDIATE 事务内完成；
    校验失败不会消耗版本号。
    """
    validate_hierarchy_batch(req.relations)

    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "INSERT INTO hierarchy_versions (relation_count, note) VALUES (?, ?)",
            (len(req.relations), req.note),
        )
        hierarchy_version_id = cur.lastrowid
        conn.executemany(
            """
            INSERT INTO hierarchy_relations
                (hierarchy_version_id, cost_center, parent_cost_center,
                 effective_from, effective_to)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    hierarchy_version_id,
                    e.cost_center,
                    e.parent_cost_center,
                    e.effective_from.isoformat(),
                    e.effective_to.isoformat(),
                )
                for e in req.relations
            ],
        )
        row = conn.execute(
            """
            SELECT hierarchy_version_id, relation_count, note, created_at
            FROM hierarchy_versions WHERE hierarchy_version_id = ?
            """,
            (hierarchy_version_id,),
        ).fetchone()
    return dict(row)


def list_hierarchy_versions() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT hierarchy_version_id, relation_count, note, created_at
            FROM hierarchy_versions ORDER BY hierarchy_version_id
            """
        ).fetchall()
    return [dict(r) for r in rows]


def list_hierarchy_relations(
    hierarchy_version_id: int, cost_center: str | None = None
) -> list[dict]:
    with get_conn() as conn:
        exists = conn.execute(
            "SELECT 1 FROM hierarchy_versions WHERE hierarchy_version_id = ?",
            (hierarchy_version_id,),
        ).fetchone()
        if exists is None:
            raise AppError(
                code="HIERARCHY_VERSION_NOT_FOUND",
                message=f"层级版本 {hierarchy_version_id} 不存在",
                status_code=404,
            )
        if cost_center:
            rows = conn.execute(
                """
                SELECT hierarchy_version_id, cost_center, parent_cost_center,
                       effective_from, effective_to
                FROM hierarchy_relations
                WHERE hierarchy_version_id = ? AND cost_center = ?
                ORDER BY cost_center, effective_from
                """,
                (hierarchy_version_id, cost_center.strip()),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT hierarchy_version_id, cost_center, parent_cost_center,
                       effective_from, effective_to
                FROM hierarchy_relations
                WHERE hierarchy_version_id = ?
                ORDER BY cost_center, effective_from
                """,
                (hierarchy_version_id,),
            ).fetchall()
    return [dict(r) for r in rows]


class _HierarchySnapshot:
    """截至某个固定层级版本的只读索引，聚合期间不随新发布变化。

    版本语义与归属修订一致：版本 N 可见「版本号 <= N」的全部关系；
    同一成本中心在同一日期若被多个版本声明，取版本号最大者（新版本覆盖旧版本）。
    """

    def __init__(self, relations: list[dict]):
        # child -> [(hierarchy_version_id, effective_from, effective_to, parent)]
        self._by_child: dict[str, list[tuple[int, str, str, str | None]]] = defaultdict(list)
        self.child_nodes: set[str] = set()
        self.parent_nodes: set[str] = set()
        for r in relations:
            self._by_child[r["cost_center"]].append(
                (
                    r["hierarchy_version_id"],
                    r["effective_from"],
                    r["effective_to"],
                    r["parent_cost_center"],
                )
            )
            self.child_nodes.add(r["cost_center"])
            if r["parent_cost_center"] is not None:
                self.parent_nodes.add(r["parent_cost_center"])

    @property
    def parent_only_nodes(self) -> set[str]:
        """只作为父节点出现、从未作为子节点出现 => 隐式根节点。"""
        return self.parent_nodes - self.child_nodes

    def active_relations(
        self, cost_center: str, work_date_iso: str
    ) -> list[tuple[int, str | None]]:
        """返回该日期生效的 (层级版本号, 父级) 列表。"""
        return [
            (version_id, parent)
            for version_id, from_iso, to_iso, parent in self._by_child.get(cost_center, ())
            if from_iso <= work_date_iso < to_iso
        ]

    def resolve_path(
        self, employee_id: str, work_date_iso: str, cost_center: str
    ) -> list[str]:
        """从成本中心沿父级指针走到根；缺失或冲突路径整批拒绝。"""
        path = [cost_center]
        cur = cost_center
        while True:
            active = self.active_relations(cur, work_date_iso)
            if not active:
                # 显式/隐式根节点：路径到此终止。
                if cur in self.parent_only_nodes:
                    return path
                raise AppError(
                    code="PATH_NOT_FOUND",
                    message=(
                        f"员工 {employee_id} 在 {work_date_iso} 的成本中心 {cur} "
                        f"截至层级版本中缺少生效的父级关系（或成本中心不在该版本中），"
                        "整批拒绝，不返回部分汇总"
                    ),
                    details={
                        "employee_id": employee_id,
                        "work_date": work_date_iso,
                        "cost_center": cur,
                        "reason": "NO_ACTIVE_HIERARCHY_RELATION"
                        if cur in self.child_nodes
                        else "COST_CENTER_NOT_IN_HIERARCHY_VERSION",
                    },
                )

            # 同一期间可能被多个层级版本声明：版本号最大者生效（新版本覆盖旧版本）。
            latest_version = max(version_id for version_id, _ in active)
            chosen = [
                (version_id, parent)
                for version_id, parent in active
                if version_id == latest_version
            ]
            if len(chosen) > 1:
                candidate_parents = sorted(str(parent) for _, parent in chosen)
                raise AppError(
                    code="CONFLICTING_HIERARCHY_PATH",
                    message=(
                        f"员工 {employee_id} 在 {work_date_iso} 的成本中心 {cur} "
                        f"在同一层级版本 {latest_version} 内同时存在多个生效父级 "
                        f"{candidate_parents}，无法确定唯一层级路径，整批拒绝"
                    ),
                    details={
                        "employee_id": employee_id,
                        "work_date": work_date_iso,
                        "cost_center": cur,
                        "hierarchy_version_id": latest_version,
                        "candidate_parents": candidate_parents,
                    },
                )

            parent = chosen[0][1]
            if parent is None:
                # 显式根节点声明。
                return path
            if parent in path:
                raise AppError(
                    code="CONFLICTING_HIERARCHY_PATH",
                    message=(
                        f"员工 {employee_id} 在 {work_date_iso} 的层级路径出现环："
                        + " -> ".join(path + [parent])
                        + "，整批拒绝"
                    ),
                    details={
                        "employee_id": employee_id,
                        "work_date": work_date_iso,
                        "cycle": path + [parent],
                    },
                )
            path.append(parent)
            cur = parent


def aggregate_hierarchy(req: HierarchyAggregationRequest) -> HierarchyAggregationResult:
    """固定归属修订号与层级版本号，按每条工作日期的路径汇总小时。

    任何一条记录出现缺失/冲突路径都会整批拒绝（抛 AppError），不会返回部分结果。
    """
    with get_conn() as conn:
        max_row = conn.execute(
            "SELECT COALESCE(MAX(revision_id), 0) AS r FROM revisions"
        ).fetchone()
        if req.revision_id > max_row["r"]:
            raise AppError(
                code="REVISION_NOT_FOUND",
                message=f"修订号 {req.revision_id} 不存在，当前最大修订号为 {max_row['r']}",
                status_code=404,
                details={"revision_id": req.revision_id},
            )

        version_row = conn.execute(
            "SELECT 1 FROM hierarchy_versions WHERE hierarchy_version_id = ?",
            (req.hierarchy_version_id,),
        ).fetchone()
        if version_row is None:
            raise AppError(
                code="HIERARCHY_VERSION_NOT_FOUND",
                message=f"层级版本 {req.hierarchy_version_id} 不存在",
                status_code=404,
                details={"hierarchy_version_id": req.hierarchy_version_id},
            )

        # 一次性读取「截至固定版本」的全部关系，构造只读快照。
        # 版本 N 可见 <= N 的关系；同日期多版本声明时由快照按版本号最大者覆盖。
        relations = [
            dict(r)
            for r in conn.execute(
                """
                SELECT hierarchy_version_id, cost_center, parent_cost_center,
                       effective_from, effective_to
                FROM hierarchy_relations
                WHERE hierarchy_version_id <= ?
                """,
                (req.hierarchy_version_id,),
            ).fetchall()
        ]
        snapshot = _HierarchySnapshot(relations)

        records: list[AggregatedRecord] = []
        totals: dict[str, float] = defaultdict(float)
        node_record_counts: dict[str, int] = defaultdict(int)

        for rec in req.records:
            work_date_iso = rec.work_date.isoformat()
            cost_center, matched_revision_id = _lookup_one(
                conn, req.revision_id, rec.employee_id, work_date_iso
            )
            if cost_center is None:
                raise AppError(
                    code="PATH_NOT_FOUND",
                    message=(
                        f"员工 {rec.employee_id} 在 {work_date_iso} 在归属修订 "
                        f"{req.revision_id} 口径下未归属任何成本中心，无法解析层级路径，"
                        "整批拒绝，不返回部分汇总"
                    ),
                    details={
                        "employee_id": rec.employee_id,
                        "work_date": work_date_iso,
                        "reason": "UNATTRIBUTED_WORK_RECORD",
                    },
                )

            path = snapshot.resolve_path(rec.employee_id, work_date_iso, cost_center)
            for node in path:
                totals[node] += rec.hours
                node_record_counts[node] += 1

            records.append(
                AggregatedRecord(
                    employee_id=rec.employee_id,
                    work_date=rec.work_date,
                    hours=rec.hours,
                    cost_center=cost_center,
                    attributed=True,
                    matched_revision_id=matched_revision_id,
                    path=path,
                )
            )

    # 全部记录解析成功后才生成汇总，保证不会“部分返回”。
    node_totals = [
        CostCenterNodeTotal(
            cost_center=cc,
            hours=round(hours, 6),
            record_count=node_record_counts[cc],
        )
        for cc, hours in sorted(totals.items())
    ]
    totals_map = {cc: round(hours, 6) for cc, hours in sorted(totals.items())}

    return HierarchyAggregationResult(
        revision_id=req.revision_id,
        attribution_revision_id=req.revision_id,
        hierarchy_version_id=req.hierarchy_version_id,
        record_count=len(records),
        records=records,
        nodes=node_totals,
        node_totals=node_totals,
        totals=totals_map,
    )
