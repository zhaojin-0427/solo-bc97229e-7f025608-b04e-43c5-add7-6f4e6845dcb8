# 工时归属与成本中心层级汇总 API（FastAPI + SQLite）

财务数仓场景：员工调岗补录会改变成本中心归属，但**月结时的口径必须冻结**；
管理账还需要把已归属工时沿**成本中心组织树**层层汇总。

本服务包含两块能力，均为「只追加（append-only）+ 固定版本」：

1. **归属修订**：人事导出的员工 / 成本中心 / 生效日期区间按批提交，每批成功返回
   一个递增修订号；补录只覆盖指定区间，区间外原归属保留，允许空档；
2. **层级汇总**：带生效起止日期的成本中心父子关系按批发布，成功返回递增的
   层级版本号；按固定的「归属修订号 + 层级版本号」解析每条工时的成本中心到根
   节点路径，并把小时数汇总到路径上的每个节点。

通用规则：

- 日期区间为**左闭右开** `[effective_from, effective_to)`；
- 任何批次校验失败都**整批拒绝**：数据与版本号均不变；
- 给定修订号 / 层级版本号查询时，结果只由该版本可见的数据决定，后续发布
  **不会改变旧版本的查询结果**；
- 汇总时任一记录出现缺失或冲突路径，**整批拒绝**，绝不返回部分汇总。

## 目录结构

```
.
├── app/
│   ├── main.py        # FastAPI 路由与异常处理
│   ├── service.py     # 批次校验、修订/层级版本提交、归属与层级汇总（核心业务规则）
│   ├── database.py    # SQLite 连接、WAL、建表
│   ├── schemas.py     # Pydantic 请求/响应模型（含 Swagger 示例）
│   └── errors.py      # 业务错误类型
├── tests/test_api.py  # 端到端测试（pytest + TestClient）
├── scripts/demo.sh    # 命令行端到端演示
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── requirements-dev.txt
```

## 一、Docker Compose 启动（交付方式）

```bash
docker compose up -d --build
```

- 服务地址：<http://localhost:8000>
- 交互式 API 文档（Swagger UI）：<http://localhost:8000/docs>
- ReDoc 文档：<http://localhost:8000/redoc>
- SQLite 数据文件位于命名卷 `attribution-data`（容器内 `/data/attribution.db`），
  容器重建后修订 / 层级版本历史保留。

停止与清理：

```bash
docker compose down          # 停止，保留数据
docker compose down -v       # 停止并删除数据卷（清空所有修订与层级版本）
```

健康检查：

```bash
curl -s http://localhost:8000/health   # {"status":"ok"}
```

## 二、在交互式文档中演示「发布 + 汇总」

打开 <http://localhost:8000/docs>，各接口均带有可直接 `Try it out` 的示例请求体。
建议按下面顺序操作：

### 1. 建立归属口径

`POST /api/revisions`，提交初始归属（返回 `revision_id: 1`）：

```json
{
  "note": "HR 初始归属 2026",
  "entries": [
    {"employee_id": "E001", "cost_center": "CC-SH-01",
     "effective_from": "2026-01-01", "effective_to": "2026-04-01"},
    {"employee_id": "E002", "cost_center": "CC-BJ-01",
     "effective_from": "2026-01-01", "effective_to": "2026-04-01"}
  ]
}
```

再提交调岗补录（返回 `revision_id: 2`）：

```json
{
  "note": "E001 四月起转广州",
  "entries": [
    {"employee_id": "E001", "cost_center": "CC-GZ-02",
     "effective_from": "2026-04-01", "effective_to": "2026-07-01"}
  ]
}
```

### 2. 发布成本中心层级版本

`POST /api/hierarchy/versions`，发布 2026 年组织树（返回
`hierarchy_version_id: 1`）：

```json
{
  "note": "2026 组织架构",
  "relations": [
    {"cost_center": "CC-SH-01", "parent_cost_center": "CC-EAST",
     "effective_from": "2026-01-01", "effective_to": "2027-01-01"},
    {"cost_center": "CC-BJ-01", "parent_cost_center": "CC-NORTH",
     "effective_from": "2026-01-01", "effective_to": "2027-01-01"},
    {"cost_center": "CC-GZ-02", "parent_cost_center": "CC-SOUTH",
     "effective_from": "2026-01-01", "effective_to": "2027-01-01"},
    {"cost_center": "CC-EAST", "parent_cost_center": "CC-ROOT",
     "effective_from": "2026-01-01", "effective_to": "2027-01-01"},
    {"cost_center": "CC-NORTH", "parent_cost_center": "CC-ROOT",
     "effective_from": "2026-01-01", "effective_to": "2027-01-01"},
    {"cost_center": "CC-SOUTH", "parent_cost_center": "CC-ROOT",
     "effective_from": "2026-01-01", "effective_to": "2027-01-01"},
    {"cost_center": "CC-ROOT", "parent_cost_center": null,
     "effective_from": "2026-01-01", "effective_to": "2027-01-01"}
  ]
}
```

`parent_cost_center` 为 `null` 表示显式声明根节点；只作为父节点出现、从未作为
子节点出现的成本中心也会被视为隐式根节点。

### 3. 固定两个版本做层级汇总

`POST /api/aggregation/query`：

```json
{
  "revision_id": 2,
  "hierarchy_version_id": 1,
  "records": [
    {"employee_id": "E001", "work_date": "2026-05-15", "hours": 8},
    {"employee_id": "E001", "work_date": "2026-03-15", "hours": 8},
    {"employee_id": "E002", "work_date": "2026-02-10", "hours": 7.5}
  ]
}
```

响应（节选）：

```json
{
  "revision_id": 2,
  "attribution_revision_id": 2,
  "hierarchy_version_id": 1,
  "record_count": 3,
  "records": [
    {
      "employee_id": "E001", "work_date": "2026-05-15", "hours": 8.0,
      "cost_center": "CC-GZ-02", "attributed": true,
      "matched_revision_id": 2,
      "path": ["CC-GZ-02", "CC-SOUTH", "CC-ROOT"]
    }
  ],
  "nodes": [
    {"cost_center": "CC-BJ-01", "hours": 7.5, "record_count": 1},
    {"cost_center": "CC-EAST", "hours": 8.0, "record_count": 1},
    {"cost_center": "CC-GZ-02", "hours": 8.0, "record_count": 1},
    {"cost_center": "CC-NORTH", "hours": 7.5, "record_count": 1},
    {"cost_center": "CC-ROOT", "hours": 23.5, "record_count": 3},
    {"cost_center": "CC-SH-01", "hours": 8.0, "record_count": 1},
    {"cost_center": "CC-SOUTH", "hours": 8.0, "record_count": 1}
  ],
  "totals": {
    "CC-BJ-01": 7.5,
    "CC-EAST": 8.0,
    "CC-GZ-02": 8.0,
    "CC-NORTH": 7.5,
    "CC-ROOT": 23.5,
    "CC-SH-01": 8.0,
    "CC-SOUTH": 8.0
  }
}
```

要点：

- `records[].path` 是**叶子 -> 根**的路径；
- 子节点的小时数会同时计入它的所有祖先，因此根节点小时数 = 全部已归属小时；
- `revision_id` 固定归属口径，`hierarchy_version_id` 固定组织树版本。

### 4. 演示整批拒绝

- 发布层级时，同一成本中心同期间出现两个父级（例如两条
  `CC-GZ-02` 的 `[2026-04-01, 2026-07-01)` 与 `[2026-05-01, 2026-08-01)`），
  返回 `400 OVERLAPPING_PARENT_ASSIGNMENTS`，且不消耗版本号；
- 发布 `CC-A -> CC-B`、`CC-B -> CC-A` 的环，返回 `400 HIERARCHY_CYCLE`；
- 汇总时若某员工未归属，或某成本中心在固定层级版本 / 日期上没有唯一父级路径，
  返回 `400 PATH_NOT_FOUND`（冲突时 `400 CONFLICTING_HIERARCHY_PATH`），
  响应中**没有** `nodes` / `totals`，不会返回部分汇总。

### 5. 演示版本固定

发布一个新版本（可以只包含发生变化的关系，其余沿用旧版本），再用原
`hierarchy_version_id` 重放上面的汇总请求，结果应完全一致；用新版本查询则命中
新路径。归属修订同理：补录后旧的 `revision_id` 结果不变。

也可以一条命令跑完等价的命令行演示：

```bash
./scripts/demo.sh
```

## 三、API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| POST | `/api/revisions` | 提交一批归属区间，成功返回递增修订号（201） |
| GET | `/api/revisions` | 列出全部修订号 |
| POST | `/api/attribution/query` | 按指定修订号逐条返回工时成本中心 / 未归属 |
| POST | `/api/revisions/{id}/segments` | 查看某修订号提交的原始区间（审计） |
| POST | `/api/hierarchy/versions` | 发布一批层级父子关系，成功返回递增层级版本号（201） |
| GET | `/api/hierarchy/versions` | 列出全部层级版本号 |
| GET / POST | `/api/hierarchy/versions/{id}/relations` | 查看某层级版本提交的原始关系（审计，可按 `cost_center` 过滤） |
| POST | `/api/aggregation/query` | 按固定归属修订号 + 层级版本号汇总各节点小时 |

## 四、核心设计

### 4.1 归属修订：旧口径为什么不会变

1. **只追加，不改写历史**。每批写入一条 `revisions` 记录与若干带 `revision_id`
   的 `attribution_segments`；任何修订都不执行 UPDATE/DELETE。
2. **查询带时间旅行条件**。修订 N 的查询只扫描 `revision_id <= N` 的区间，
   并对同一日期点取其中修订号最大者：

   ```sql
   WHERE employee_id = ?
     AND effective_from <= :work_date
     AND effective_to   > :work_date   -- 左闭右开
     AND revision_id <= :query_revision
   ORDER BY revision_id DESC, id DESC
   LIMIT 1
   ```

3. **整批原子性**。全部业务校验在写库前完成；修订号分配与区间插入在同一个
   `BEGIN IMMEDIATE` 事务内，失败即回滚，自增修订号不会被失败批次消耗。
4. **半开区间语义**。重叠判定 `a.from < b.to AND b.from < a.to`；首尾相接
   （`a.to = b.from`）不算重叠，`work_date = effective_to` 当天不归属该段。

### 4.2 层级版本：发布与校验

层级版本与归属修订采用相同的叠加 / 覆盖语义：**版本 N 可见版本号 <= N 的全部
父子关系**；同一成本中心在同一日期被多个版本声明时，版本号最大者生效
（新版本覆盖旧版本）。聚合固定 `hierarchy_version_id`，因此后续发布新版本
不会改变旧版本结果。发布新版本时可以只提交发生变化的关系（增量批次），
未被新版本提及的成本中心沿用更早版本。

发布前的整批校验：

1. `effective_from < effective_to`，否则 `INVALID_DATE_RANGE`；
2. 同一 `cost_center` 的区间两两不重叠（半开区间），否则
   `OVERLAPPING_PARENT_ASSIGNMENTS` —— 同一成本中心同期间只能有一个父级；
3. 在任意生效日期上沿父级指针都不能回到自身，否则 `HIERARCHY_CYCLE`。
   检查只取各区间起点作为候选日期：生效边集合只在 `effective_from` 处变化，
   若某个环在某天存在，则环上各边最晚的起日当天该环仍存在。

同时，层级版本号分配与关系插入也在同一个 `BEGIN IMMEDIATE` 事务内完成，
失败批次不会消耗版本号。

### 4.3 汇总：固定版本 + 整批拒绝

聚合在同一个只读连接内完成，并固定两个版本：

1. 用 `revision_id` 解析每条工时的成本中心（规则同 4.1）；
2. 在截至该 `hierarchy_version_id` 的可见关系中，按 `work_date` 找到唯一生效
   父级（同一期间多个版本声明时取版本号最大者），逐级走到根，得到 `path`
   （叶子 -> 根）；
3. 把该条工时的小时数累加到路径上的每个节点，得到 `nodes` / `totals`。

以下任一情况都会使**整个请求**失败（400），且响应中不包含任何汇总：

- 工时记录在该修订口径下未归属（没有成本中心，无法解析路径）；
- 成本中心在截至该版本的全部关系中都不存在，或在该日期没有生效的父级（空档）；
- 同一成本中心在该日期出现多个生效父级，或路径成环（无法确定唯一路径）。

实现上先完成全部记录的解析与累加，全部成功后才构造响应，因此不会出现
“前几条已汇总、后一条报错”的部分结果。

## 五、本地开发与测试（不用 Docker）

```bash
pip install -r requirements-dev.txt
pytest -q
ATTRIBUTION_DB_PATH=./data/dev.db uvicorn app.main:app --reload
```

`ATTRIBUTION_DB_PATH` 可指定 SQLite 文件位置，默认 `./data/attribution.db`。
（数据库文件名沿用基线命名，其中同时保存归属修订与层级版本数据。）

## 六、错误响应格式

```json
{
  "error": {
    "code": "OVERLAPPING_PARENT_ASSIGNMENTS",
    "message": "成本中心 CC-A 在本批存在重叠期间：第 1 条 [...] 与第 2 条 [...]；同一成本中心同期间只能有一个父级",
    "details": {"cost_center": "CC-A", "indices": [0, 1]}
  }
}
```

| code | HTTP 状态码 | 触发条件 |
| --- | --- | --- |
| `INVALID_DATE_RANGE` | 400 | 起日不早于止日（归属区间或层级关系） |
| `OVERLAPPING_SEGMENTS` | 400 | 归属批次内同一员工区间重叠 |
| `OVERLAPPING_PARENT_ASSIGNMENTS` | 400 | 层级批次内同一成本中心同期间有多个父级 |
| `HIERARCHY_CYCLE` | 400 | 层级关系在同期间成环 |
| `REVISION_NOT_FOUND` | 404 | 查询/审计了不存在的归属修订号 |
| `HIERARCHY_VERSION_NOT_FOUND` | 404 | 查询/审计了不存在的层级版本号 |
| `PATH_NOT_FOUND` | 400 | 汇总时记录未归属，或成本中心缺少生效唯一父级路径 |
| `CONFLICTING_HIERARCHY_PATH` | 400 | 汇总时同一成本中心同日期出现多个父级或路径成环 |
| Pydantic 校验错误 | 422 | 空批次、字段缺失、日期格式错误等 |
