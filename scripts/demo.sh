#!/usr/bin/env bash
# 端到端演示脚本：归属修订 + 层级版本发布 + 固定版本汇总 + 整批拒绝。
# 用法：
#   docker compose up -d --build       # 先启动服务
#   ./scripts/demo.sh                  # 本机演示（默认 BASE=http://localhost:8000）
set -euo pipefail

BASE="${BASE:-http://localhost:8000}"

pretty() {
  if command -v jq >/dev/null 2>&1; then jq .;
  elif command -v python3 >/dev/null 2>&1; then python3 -m json.tool;
  else cat; fi
}

post() { # $1=path  $2=json body
  curl -sS -X POST "${BASE}$1" -H 'Content-Type: application/json' -d "$2"
}

echo "==================================================================="
echo "0. 健康检查  GET /health"
echo "==================================================================="
curl -sS "${BASE}/health" | pretty
echo

echo "==================================================================="
echo "1. 归属修订 1：人事初始归属（Q1）"
echo "   E001 -> CC-SH-01 [2026-01-01, 2026-04-01)"
echo "   E002 -> CC-BJ-01 [2026-01-01, 2026-04-01)"
echo "==================================================================="
post /api/revisions '{
  "note": "HR 初始归属 Q1",
  "entries": [
    {"employee_id": "E001", "cost_center": "CC-SH-01",
     "effective_from": "2026-01-01", "effective_to": "2026-04-01"},
    {"employee_id": "E002", "cost_center": "CC-BJ-01",
     "effective_from": "2026-01-01", "effective_to": "2026-04-01"}
  ]
}' | pretty
echo

echo "==================================================================="
echo "2. 按修订 1 口径查询工时（含左闭右开边界、空档、未归属员工）"
echo "   2026-01-01 命中(左端点含); 2026-04-01 不命中(右端点不含/空档);"
echo "   E999 无任何归属"
echo "==================================================================="
post /api/attribution/query '{
  "revision_id": 1,
  "records": [
    {"employee_id": "E001", "work_date": "2026-01-01", "hours": 8},
    {"employee_id": "E001", "work_date": "2026-03-31", "hours": 6},
    {"employee_id": "E001", "work_date": "2026-04-01", "hours": 8},
    {"employee_id": "E002", "work_date": "2026-02-10", "hours": 7.5},
    {"employee_id": "E999", "work_date": "2026-02-10", "hours": 4}
  ]
}' | pretty
echo

echo "==================================================================="
echo "3. 非法归属批次 A：起日不早于止日 -> 整批拒绝 (400)"
echo "==================================================================="
post /api/revisions '{
  "entries": [
    {"employee_id": "E001", "cost_center": "CC-X",
     "effective_from": "2026-05-01", "effective_to": "2026-05-01"}
  ]
}' | pretty
echo

echo "==================================================================="
echo "4. 非法归属批次 B：同员工批内区间重叠 -> 整批拒绝 (400)"
echo "==================================================================="
post /api/revisions '{
  "entries": [
    {"employee_id": "E001", "cost_center": "CC-X",
     "effective_from": "2026-06-01", "effective_to": "2026-08-01"},
    {"employee_id": "E001", "cost_center": "CC-Y",
     "effective_from": "2026-07-01", "effective_to": "2026-09-01"}
  ]
}' | pretty
echo

echo "==================================================================="
echo "5. 确认修订号未被失败批次消耗：下一个成功批次仍是修订 2"
echo "==================================================================="
curl -sS "${BASE}/api/revisions" | pretty
echo

echo "==================================================================="
echo "6. 归属修订 2：调岗补录 E001 4~6 月转到 CC-GZ-02（跨段覆盖，区间外保留）"
echo "   与修订 1 跨批重叠是允许的；批内重叠才拒绝"
echo "==================================================================="
post /api/revisions '{
  "note": "HR 调岗补录：E001 四月起转广州成本中心",
  "entries": [
    {"employee_id": "E001", "cost_center": "CC-GZ-02",
     "effective_from": "2026-04-01", "effective_to": "2026-07-01"}
  ]
}' | pretty
echo

echo "==================================================================="
echo "7. 归属口径冻结验证：同样的请求再查修订 1，结果与补录前完全一致"
echo "==================================================================="
post /api/attribution/query '{
  "revision_id": 1,
  "records": [
    {"employee_id": "E001", "work_date": "2026-05-15", "hours": 8}
  ]
}' | pretty
echo

echo "==================================================================="
echo "8. 按修订 2 口径查询：5 月命中新成本中心；3 月区间外保留旧归属"
echo "==================================================================="
post /api/attribution/query '{
  "revision_id": 2,
  "records": [
    {"employee_id": "E001", "work_date": "2026-05-15", "hours": 8},
    {"employee_id": "E001", "work_date": "2026-03-15", "hours": 8},
    {"employee_id": "E001", "work_date": "2026-07-01", "hours": 8}
  ]
}' | pretty
echo

echo "==================================================================="
echo "9. 发布层级版本 1：2026 组织树"
echo "   CC-SH-01 -> CC-EAST -> CC-ROOT;  CC-BJ-01 -> CC-NORTH -> CC-ROOT"
echo "   CC-GZ-02 -> CC-SOUTH -> CC-ROOT;  CC-ROOT 显式声明为根"
echo "==================================================================="
post /api/hierarchy/versions '{
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
}' | pretty
echo

echo "==================================================================="
echo "10. 非法层级批次 A：同一成本中心同期间两个父级 -> 整批拒绝 (400)"
echo "==================================================================="
post /api/hierarchy/versions '{
  "relations": [
    {"cost_center": "CC-GZ-02", "parent_cost_center": "CC-SOUTH",
     "effective_from": "2026-04-01", "effective_to": "2026-07-01"},
    {"cost_center": "CC-GZ-02", "parent_cost_center": "CC-EAST",
     "effective_from": "2026-05-01", "effective_to": "2026-08-01"}
  ]
}' | pretty
echo

echo "==================================================================="
echo "11. 非法层级批次 B：父子关系成环 -> 整批拒绝 (400)"
echo "==================================================================="
post /api/hierarchy/versions '{
  "relations": [
    {"cost_center": "CC-A", "parent_cost_center": "CC-B",
     "effective_from": "2026-01-01", "effective_to": "2027-01-01"},
    {"cost_center": "CC-B", "parent_cost_center": "CC-A",
     "effective_from": "2026-01-01", "effective_to": "2027-01-01"}
  ]
}' | pretty
echo

echo "==================================================================="
echo "12. 确认层级版本号未被失败批次消耗：仍然只有版本 1"
echo "==================================================================="
curl -sS "${BASE}/api/hierarchy/versions" | pretty
echo

echo "==================================================================="
echo "13. 固定 归属修订 2 + 层级版本 1 汇总：逐条路径 + 各节点小时"
echo "   E001 5 月 -> CC-GZ-02 -> SOUTH -> ROOT (8h)"
echo "   E001 3 月 -> CC-SH-01 -> EAST  -> ROOT (8h)"
echo "   E002 2 月 -> CC-BJ-01 -> NORTH -> ROOT (7.5h)"
echo "   根节点应为 23.5h"
echo "==================================================================="
post /api/aggregation/query '{
  "revision_id": 2,
  "hierarchy_version_id": 1,
  "records": [
    {"employee_id": "E001", "work_date": "2026-05-15", "hours": 8},
    {"employee_id": "E001", "work_date": "2026-03-15", "hours": 8},
    {"employee_id": "E002", "work_date": "2026-02-10", "hours": 7.5}
  ]
}' | pretty
echo

echo "==================================================================="
echo "14. 发布层级版本 2（增量）：CC-GZ-02 改挂 CC-EAST，其余沿用版本 1"
echo "   版本 2 可见 <= 2 的关系；同期间取版本号最大者，旧版本 1 查询不受影响"
echo "==================================================================="
post /api/hierarchy/versions '{
  "note": "组织调整：广州并入华东",
  "relations": [
    {"cost_center": "CC-SH-01", "parent_cost_center": "CC-EAST",
     "effective_from": "2026-01-01", "effective_to": "2027-01-01"},
    {"cost_center": "CC-BJ-01", "parent_cost_center": "CC-NORTH",
     "effective_from": "2026-01-01", "effective_to": "2027-01-01"},
    {"cost_center": "CC-GZ-02", "parent_cost_center": "CC-EAST",
     "effective_from": "2026-01-01", "effective_to": "2027-01-01"},
    {"cost_center": "CC-EAST", "parent_cost_center": "CC-ROOT",
     "effective_from": "2026-01-01", "effective_to": "2027-01-01"},
    {"cost_center": "CC-NORTH", "parent_cost_center": "CC-ROOT",
     "effective_from": "2026-01-01", "effective_to": "2027-01-01"},
    {"cost_center": "CC-ROOT", "parent_cost_center": null,
     "effective_from": "2026-01-01", "effective_to": "2027-01-01"}
  ]
}' | pretty
echo

echo "==================================================================="
echo "15. 版本固定验证：重放层级版本 1 的汇总，结果与第 13 步完全一致"
echo "==================================================================="
post /api/aggregation/query '{
  "revision_id": 2,
  "hierarchy_version_id": 1,
  "records": [
    {"employee_id": "E001", "work_date": "2026-05-15", "hours": 8},
    {"employee_id": "E001", "work_date": "2026-03-15", "hours": 8},
    {"employee_id": "E002", "work_date": "2026-02-10", "hours": 7.5}
  ]
}' | pretty
echo

echo "==================================================================="
echo "16. 按层级版本 2 汇总：广州 8h 计入 CC-EAST（16h），SOUTH 不再出现"
echo "==================================================================="
post /api/aggregation/query '{
  "revision_id": 2,
  "hierarchy_version_id": 2,
  "records": [
    {"employee_id": "E001", "work_date": "2026-05-15", "hours": 8},
    {"employee_id": "E001", "work_date": "2026-03-15", "hours": 8},
    {"employee_id": "E002", "work_date": "2026-02-10", "hours": 7.5}
  ]
}' | pretty
echo

echo "==================================================================="
echo "17. 缺失路径整批拒绝：E999 未归属 -> 400，不返回任何部分汇总"
echo "==================================================================="
post /api/aggregation/query '{
  "revision_id": 2,
  "hierarchy_version_id": 2,
  "records": [
    {"employee_id": "E001", "work_date": "2026-05-15", "hours": 8},
    {"employee_id": "E999", "work_date": "2026-05-15", "hours": 4}
  ]
}' | pretty
echo

echo "==================================================================="
echo "18. 审计：全部修订号、层级版本与版本 1 的原始关系"
echo "==================================================================="
curl -sS "${BASE}/api/revisions" | pretty
curl -sS "${BASE}/api/hierarchy/versions" | pretty
curl -sS "${BASE}/api/hierarchy/versions/1/relations" | pretty
echo
echo "演示完成。交互式文档：${BASE}/docs"
