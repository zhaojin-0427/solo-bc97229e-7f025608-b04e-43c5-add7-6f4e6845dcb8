"""端到端测试：使用临时数据库，覆盖全部业务规则。

运行：ATTRIBUTION_DB_PATH 指向临时文件后 pytest。
"""
from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("ATTRIBUTION_DB_PATH", str(db_file))

    import app.database as database
    import app.main as main

    importlib.reload(database)
    importlib.reload(main)
    database.init_db()
    with TestClient(main.app) as c:
        yield c


# ---------- 基础 ----------

def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_empty_batch_rejected(client):
    r = client.post("/api/revisions", json={"entries": []})
    assert r.status_code == 422  # pydantic min_length


# ---------- 修订号递增 ----------

def test_revision_numbers_increment(client):
    r1 = client.post("/api/revisions", json={
        "entries": [{"employee_id": "E1", "cost_center": "CC-A",
                     "effective_from": "2026-01-01", "effective_to": "2026-02-01"}]
    })
    r2 = client.post("/api/revisions", json={
        "entries": [{"employee_id": "E1", "cost_center": "CC-B",
                     "effective_from": "2026-03-01", "effective_to": "2026-04-01"}]
    })
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.json()["revision_id"] == 1
    assert r2.json()["revision_id"] == 2
    assert r1.json()["entry_count"] == 1


# ---------- 整批拒绝规则 ----------

def test_invalid_range_from_not_before_to_rejects_whole_batch(client):
    r = client.post("/api/revisions", json={"entries": [
        {"employee_id": "E1", "cost_center": "CC-A",
         "effective_from": "2026-02-01", "effective_to": "2026-02-01"},
    ]})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_DATE_RANGE"
    # 数据与修订号均不变
    assert client.get("/api/revisions").json() == []


def test_overlap_within_batch_rejects_whole_batch(client):
    # 先放一个合法修订号 1
    client.post("/api/revisions", json={"entries": [
        {"employee_id": "E1", "cost_center": "CC-A",
         "effective_from": "2026-01-01", "effective_to": "2026-03-01"}
    ]})
    # 批内同员工重叠 -> 拒绝
    r = client.post("/api/revisions", json={"entries": [
        {"employee_id": "E1", "cost_center": "CC-X",
         "effective_from": "2026-06-01", "effective_to": "2026-08-01"},
        {"employee_id": "E1", "cost_center": "CC-Y",
         "effective_from": "2026-07-01", "effective_to": "2026-09-01"},
    ]})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "OVERLAPPING_SEGMENTS"
    # 修订号没有被消耗，下一批仍是 2
    r2 = client.post("/api/revisions", json={"entries": [
        {"employee_id": "E2", "cost_center": "CC-Z",
         "effective_from": "2026-01-01", "effective_to": "2026-02-01"}
    ]})
    assert r2.status_code == 201
    assert r2.json()["revision_id"] == 2


def test_adjacent_segments_allowed(client):
    # 首尾相接（半开）不算重叠
    r = client.post("/api/revisions", json={"entries": [
        {"employee_id": "E1", "cost_center": "CC-A",
         "effective_from": "2026-01-01", "effective_to": "2026-02-01"},
        {"employee_id": "E1", "cost_center": "CC-B",
         "effective_from": "2026-02-01", "effective_to": "2026-03-01"},
    ]})
    assert r.status_code == 201


def test_overlap_check_is_per_employee(client):
    # 不同员工同一日期区间不构成重叠
    r = client.post("/api/revisions", json={"entries": [
        {"employee_id": "E1", "cost_center": "CC-A",
         "effective_from": "2026-01-01", "effective_to": "2026-03-01"},
        {"employee_id": "E2", "cost_center": "CC-B",
         "effective_from": "2026-01-01", "effective_to": "2026-03-01"},
    ]})
    assert r.status_code == 201


# ---------- 归属查询：左闭右开、空档、未归属 ----------

def test_half_open_boundary_and_gap(client):
    client.post("/api/revisions", json={"entries": [
        {"employee_id": "E1", "cost_center": "CC-A",
         "effective_from": "2026-01-01", "effective_to": "2026-02-01"},
        # 2 月整月空档
        {"employee_id": "E1", "cost_center": "CC-B",
         "effective_from": "2026-03-01", "effective_to": "2026-04-01"},
    ]})
    r = client.post("/api/attribution/query", json={
        "revision_id": 1,
        "records": [
            {"employee_id": "E1", "work_date": "2026-01-01", "hours": 8},   # 左端点含
            {"employee_id": "E1", "work_date": "2026-01-31", "hours": 4},   # 区间内
            {"employee_id": "E1", "work_date": "2026-02-01", "hours": 8},   # 右端点不含 + 空档
            {"employee_id": "E1", "work_date": "2026-03-01", "hours": 8},   # 下一段左端点
            {"employee_id": "E9", "work_date": "2026-01-01", "hours": 8},   # 未知员工
        ],
    })
    assert r.status_code == 200
    results = r.json()
    assert results[0]["cost_center"] == "CC-A" and results[0]["attributed"] is True
    assert results[1]["cost_center"] == "CC-A"
    assert results[2]["cost_center"] is None and results[2]["attributed"] is False
    assert results[3]["cost_center"] == "CC-B"
    assert results[4]["cost_center"] is None


# ---------- 核心：旧修订口径不可变 ----------

def test_old_revision_result_immutable_after_later_backfill(client):
    # 修订 1：E1 全年归属 CC-OLD
    client.post("/api/revisions", json={"entries": [
        {"employee_id": "E1", "cost_center": "CC-OLD",
         "effective_from": "2026-01-01", "effective_to": "2027-01-01"}
    ]})
    body = {
        "revision_id": 1,
        "records": [{"employee_id": "E1", "work_date": "2026-05-15", "hours": 8}],
    }
    before = client.post("/api/attribution/query", json=body).json()
    assert before[0]["cost_center"] == "CC-OLD"

    # 修订 2：跨段补录，覆盖 4~6 月（与旧区间跨批重叠是允许的，批内才禁止）
    r2 = client.post("/api/revisions", json={"entries": [
        {"employee_id": "E1", "cost_center": "CC-NEW",
         "effective_from": "2026-04-01", "effective_to": "2026-07-01"}
    ]})
    assert r2.json()["revision_id"] == 2

    # 旧修订 1 的查询结果保持不变（月结口径冻结）
    after = client.post("/api/attribution/query", json=body).json()
    assert after == before
    assert after[0]["cost_center"] == "CC-OLD"
    assert after[0]["matched_revision_id"] == 1

    # 新修订 2 下同一天命中新成本中心，区间外仍保留旧归属
    q2 = client.post("/api/attribution/query", json={
        "revision_id": 2,
        "records": [
            {"employee_id": "E1", "work_date": "2026-05-15", "hours": 8},
            {"employee_id": "E1", "work_date": "2026-03-15", "hours": 8},
        ],
    }).json()
    assert q2[0]["cost_center"] == "CC-NEW" and q2[0]["matched_revision_id"] == 2
    assert q2[1]["cost_center"] == "CC-OLD" and q2[1]["matched_revision_id"] == 1


def test_revision_zero_is_empty_caliber(client):
    client.post("/api/revisions", json={"entries": [
        {"employee_id": "E1", "cost_center": "CC-A",
         "effective_from": "2026-01-01", "effective_to": "2026-02-01"}
    ]})
    r = client.post("/api/attribution/query", json={
        "revision_id": 0,
        "records": [{"employee_id": "E1", "work_date": "2026-01-15", "hours": 8}],
    })
    assert r.status_code == 200
    assert r.json()[0]["cost_center"] is None


def test_query_unknown_revision_404(client):
    r = client.post("/api/attribution/query", json={
        "revision_id": 99,
        "records": [{"employee_id": "E1", "work_date": "2026-01-15", "hours": 8}],
    })
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "REVISION_NOT_FOUND"


def test_revision_segments_listing(client):
    client.post("/api/revisions", json={"entries": [
        {"employee_id": "E1", "cost_center": "CC-A",
         "effective_from": "2026-01-01", "effective_to": "2026-02-01"},
        {"employee_id": "E2", "cost_center": "CC-B",
         "effective_from": "2026-01-01", "effective_to": "2026-02-01"},
    ]}, )
    r = client.post("/api/revisions/1/segments", json={"employee_id": "E1"})
    assert r.status_code == 200
    assert len(r.json()) == 1
    assert r.json()[0]["cost_center"] == "CC-A"

    assert client.post("/api/revisions/99/segments", json={}).status_code == 404


# ===========================================================================
# 成本中心层级与汇总
# ===========================================================================

def _publish(client, relations, note=None):
    body = {"relations": relations}
    if note is not None:
        body["note"] = note
    return client.post("/api/hierarchy/versions", json=body)


def _agg(client, revision_id, hierarchy_version_id, records):
    return client.post("/api/aggregation/query", json={
        "revision_id": revision_id,
        "hierarchy_version_id": hierarchy_version_id,
        "records": records,
    })


def _rel(child, parent, frm="2026-01-01", to="2027-01-01"):
    return {
        "cost_center": child,
        "parent_cost_center": parent,
        "effective_from": frm,
        "effective_to": to,
    }


def _attribute_tree(client):
    """归属修订 1：E1 在 CC-GZ-02，E2 在 CC-GZ-01，均覆盖 2026 全年。"""
    return client.post("/api/revisions", json={"entries": [
        {"employee_id": "E1", "cost_center": "CC-GZ-02",
         "effective_from": "2026-01-01", "effective_to": "2027-01-01"},
        {"employee_id": "E2", "cost_center": "CC-GZ-01",
         "effective_from": "2026-01-01", "effective_to": "2027-01-01"},
    ]})


def _publish_tree(client):
    return _publish(client, [
        _rel("CC-GZ-01", "CC-SOUTH"),
        _rel("CC-GZ-02", "CC-SOUTH"),
        _rel("CC-SOUTH", "CC-ROOT"),
        _rel("CC-ROOT", None),
    ], note="华南大区")


def test_hierarchy_publish_returns_incrementing_versions(client):
    r1 = _publish(client, [_rel("CC-A", "CC-ROOT"), _rel("CC-ROOT", None)])
    assert r1.status_code == 201
    assert r1.json()["hierarchy_version_id"] == 1
    assert r1.json()["relation_count"] == 2

    r2 = _publish(client, [_rel("CC-B", "CC-ROOT"), _rel("CC-ROOT", None)])
    assert r2.status_code == 201
    assert r2.json()["hierarchy_version_id"] == 2

    versions = client.get("/api/hierarchy/versions").json()
    assert [v["hierarchy_version_id"] for v in versions] == [1, 2]
    assert versions[0]["note"] is None


def test_hierarchy_invalid_date_range_rejects_whole_batch(client):
    r = _publish(client, [_rel("CC-A", "CC-ROOT", "2026-02-01", "2026-02-01")])
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_DATE_RANGE"
    assert client.get("/api/hierarchy/versions").json() == []
    # 版本号未被失败批次消耗
    ok = _publish(client, [_rel("CC-A", "CC-ROOT")])
    assert ok.status_code == 201
    assert ok.json()["hierarchy_version_id"] == 1


def test_hierarchy_overlapping_parent_assignment_rejects_whole_batch(client):
    _publish(client, [_rel("CC-A", "CC-P1")])
    r = _publish(client, [
        _rel("CC-A", "CC-P1", "2026-06-01", "2026-08-01"),
        _rel("CC-A", "CC-P2", "2026-07-01", "2026-09-01"),
    ])
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "OVERLAPPING_PARENT_ASSIGNMENTS"
    assert [v["hierarchy_version_id"] for v in client.get("/api/hierarchy/versions").json()] == [1]
    # 下一合法批次仍是版本 2
    ok = _publish(client, [_rel("CC-B", "CC-P1")])
    assert ok.json()["hierarchy_version_id"] == 2


def test_hierarchy_adjacent_periods_for_same_cost_center_allowed(client):
    r = _publish(client, [
        _rel("CC-A", "CC-P1", "2026-01-01", "2026-04-01"),
        _rel("CC-A", "CC-P2", "2026-04-01", "2026-07-01"),
    ])
    assert r.status_code == 201


def test_hierarchy_cycle_rejected(client):
    r = _publish(client, [
        _rel("CC-A", "CC-B"), _rel("CC-B", "CC-C"), _rel("CC-C", "CC-A"),
    ])
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "HIERARCHY_CYCLE"
    assert client.get("/api/hierarchy/versions").json() == []

    r2 = _publish(client, [_rel("CC-A", "CC-A")])
    assert r2.status_code == 400
    assert r2.json()["error"]["code"] == "HIERARCHY_CYCLE"


def test_hierarchy_cycle_across_disjoint_periods_allowed(client):
    # 不同期间的两条反向关系不构成“同期间成环”
    r = _publish(client, [
        _rel("CC-A", "CC-B", "2026-01-01", "2026-03-01"),
        _rel("CC-B", "CC-A", "2026-03-01", "2026-06-01"),
    ])
    assert r.status_code == 201


def test_hierarchy_relations_audit(client):
    _publish(client, [_rel("CC-A", "CC-ROOT"), _rel("CC-B", "CC-A")])
    r = client.get("/api/hierarchy/versions/1/relations?cost_center=CC-A")
    assert r.status_code == 200
    assert r.json()[0]["parent_cost_center"] == "CC-ROOT"
    r2 = client.post("/api/hierarchy/versions/1/relations", json={"cost_center": "CC-B"})
    assert r2.status_code == 200 and len(r2.json()) == 1
    assert client.get("/api/hierarchy/versions/99/relations").status_code == 404


def test_aggregation_rolls_up_to_all_ancestors(client):
    _attribute_tree(client)
    v = _publish_tree(client).json()["hierarchy_version_id"]

    r = _agg(client, 1, v, [
        {"employee_id": "E1", "work_date": "2026-05-15", "hours": 8},
        {"employee_id": "E2", "work_date": "2026-05-15", "hours": 7.5},
        {"employee_id": "E1", "work_date": "2026-05-16", "hours": 4},
    ])
    assert r.status_code == 200
    body = r.json()
    assert body["revision_id"] == 1
    assert body["attribution_revision_id"] == 1
    assert body["hierarchy_version_id"] == v
    assert body["record_count"] == 3

    # 逐条路径：叶子 -> 根
    assert body["records"][0]["path"] == ["CC-GZ-02", "CC-SOUTH", "CC-ROOT"]
    assert body["records"][1]["path"] == ["CC-GZ-01", "CC-SOUTH", "CC-ROOT"]

    assert body["totals"] == {
        "CC-GZ-01": 7.5,
        "CC-GZ-02": 12.0,
        "CC-SOUTH": 19.5,
        "CC-ROOT": 19.5,
    }
    nodes = {n["cost_center"]: n for n in body["nodes"]}
    assert nodes["CC-GZ-02"]["hours"] == 12.0 and nodes["CC-GZ-02"]["record_count"] == 2
    assert nodes["CC-SOUTH"]["record_count"] == 3
    assert nodes["CC-ROOT"]["hours"] == 19.5


def test_aggregation_time_scoped_reparent_within_version(client):
    _attribute_tree(client)
    # E1 的 CC-GZ-02 在上半年属 SOUTH，下半年属 NORTH；E2 全年属 SOUTH 区域外
    v = _publish(client, [
        _rel("CC-GZ-02", "CC-SOUTH", "2026-01-01", "2026-07-01"),
        _rel("CC-GZ-02", "CC-NORTH", "2026-07-01", "2027-01-01"),
        _rel("CC-GZ-01", "CC-SOUTH", "2026-01-01", "2027-01-01"),
        _rel("CC-SOUTH", "CC-ROOT", "2026-01-01", "2027-01-01"),
        _rel("CC-NORTH", "CC-ROOT", "2026-01-01", "2027-01-01"),
        _rel("CC-ROOT", None, "2026-01-01", "2027-01-01"),
    ]).json()["hierarchy_version_id"]

    r = _agg(client, 1, v, [
        {"employee_id": "E1", "work_date": "2026-06-30", "hours": 8},
        {"employee_id": "E1", "work_date": "2026-07-01", "hours": 8},
    ]).json()
    assert r["records"][0]["path"] == ["CC-GZ-02", "CC-SOUTH", "CC-ROOT"]
    assert r["records"][1]["path"] == ["CC-GZ-02", "CC-NORTH", "CC-ROOT"]
    assert r["totals"] == {"CC-GZ-02": 16.0, "CC-NORTH": 8.0, "CC-ROOT": 16.0, "CC-SOUTH": 8.0}


def test_aggregation_missing_path_rejects_whole_batch_without_partial(client):
    _attribute_tree(client)
    # 层级版本只包含 CC-GZ-01，不包含 E1 的 CC-GZ-02
    v = _publish(client, [_rel("CC-GZ-01", "CC-ROOT"), _rel("CC-ROOT", None)]).json()[
        "hierarchy_version_id"
    ]
    r = _agg(client, 1, v, [
        {"employee_id": "E2", "work_date": "2026-05-15", "hours": 8},  # 可解析
        {"employee_id": "E1", "work_date": "2026-05-15", "hours": 8},  # 缺失
    ])
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["code"] == "PATH_NOT_FOUND"
    assert err["details"]["cost_center"] == "CC-GZ-02"
    # 不返回任何部分汇总
    assert "nodes" not in r.json() and "totals" not in r.json()


def test_aggregation_unattributed_record_rejects_whole_batch(client):
    _attribute_tree(client)
    v = _publish(client, [_rel("CC-GZ-02", "CC-ROOT"), _rel("CC-ROOT", None)]).json()[
        "hierarchy_version_id"
    ]
    r = _agg(client, 1, v, [
        {"employee_id": "E1", "work_date": "2026-05-15", "hours": 8},
        {"employee_id": "E999", "work_date": "2026-05-15", "hours": 8},
    ])
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "PATH_NOT_FOUND"
    assert r.json()["error"]["details"]["reason"] == "UNATTRIBUTED_WORK_RECORD"


def test_aggregation_conflicting_path_rejects_whole_batch(client, monkeypatch):
    """即使数据库中存在同期间两个父级（绕过发布校验），查询也必须整批拒绝。"""
    _attribute_tree(client)
    v = _publish(client, [_rel("CC-GZ-02", "CC-SOUTH"), _rel("CC-SOUTH", "CC-ROOT")]).json()[
        "hierarchy_version_id"
    ]

    import app.database as database
    with database.get_conn() as conn:
        conn.execute(
            """
            INSERT INTO hierarchy_relations
                (hierarchy_version_id, cost_center, parent_cost_center,
                 effective_from, effective_to)
            VALUES (?, ?, ?, ?, ?)
            """,
            (v, "CC-GZ-02", "CC-OTHER", "2026-01-01", "2027-01-01"),
        )

    r = _agg(client, 1, v, [
        {"employee_id": "E1", "work_date": "2026-05-15", "hours": 8},
    ])
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "CONFLICTING_HIERARCHY_PATH"
    assert "nodes" not in r.json()


def test_aggregation_pins_hierarchy_version(client):
    _attribute_tree(client)
    v1 = _publish(client, [
        _rel("CC-GZ-02", "CC-OLD"),
        _rel("CC-OLD", "CC-ROOT"),
        _rel("CC-ROOT", None),
        _rel("CC-GZ-01", "CC-OLD"),
    ]).json()["hierarchy_version_id"]

    records = [{"employee_id": "E1", "work_date": "2026-05-15", "hours": 8}]
    before = _agg(client, 1, v1, records).json()
    assert before["totals"] == {"CC-GZ-02": 8.0, "CC-OLD": 8.0, "CC-ROOT": 8.0}

    v2 = _publish(client, [
        _rel("CC-GZ-02", "CC-NEW"),
        _rel("CC-NEW", "CC-ROOT"),
        _rel("CC-ROOT", None),
        _rel("CC-GZ-01", "CC-OLD"),
        _rel("CC-OLD", "CC-ROOT"),
    ]).json()["hierarchy_version_id"]

    # 旧版本结果完全不变
    after = _agg(client, 1, v1, records).json()
    assert after == before

    # 新版本按新层级汇总
    new = _agg(client, 1, v2, records).json()
    assert new["totals"] == {"CC-GZ-02": 8.0, "CC-NEW": 8.0, "CC-ROOT": 8.0}
    assert new["records"][0]["path"] == ["CC-GZ-02", "CC-NEW", "CC-ROOT"]


def test_aggregation_pins_attribution_revision(client):
    # 修订 1：E1 -> CC-A；修订 2：E1 -> CC-B（区间覆盖）
    client.post("/api/revisions", json={"entries": [
        {"employee_id": "E1", "cost_center": "CC-A",
         "effective_from": "2026-01-01", "effective_to": "2027-01-01"}
    ]})
    client.post("/api/revisions", json={"entries": [
        {"employee_id": "E1", "cost_center": "CC-B",
         "effective_from": "2026-04-01", "effective_to": "2026-07-01"}
    ]})
    v = _publish(client, [
        _rel("CC-A", "CC-ROOT"), _rel("CC-B", "CC-ROOT"), _rel("CC-ROOT", None),
    ]).json()["hierarchy_version_id"]

    rec = [{"employee_id": "E1", "work_date": "2026-05-15", "hours": 8}]
    old = _agg(client, 1, v, rec).json()
    assert old["records"][0]["cost_center"] == "CC-A"
    assert old["totals"] == {"CC-A": 8.0, "CC-ROOT": 8.0}

    new = _agg(client, 2, v, rec).json()
    assert new["records"][0]["cost_center"] == "CC-B"
    assert new["records"][0]["matched_revision_id"] == 2
    assert new["totals"] == {"CC-B": 8.0, "CC-ROOT": 8.0}


def test_aggregation_version_not_found(client):
    _attribute_tree(client)
    r = _agg(client, 1, 99, [{"employee_id": "E1", "work_date": "2026-05-15", "hours": 8}])
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "HIERARCHY_VERSION_NOT_FOUND"


def test_aggregation_revision_not_found(client):
    _attribute_tree(client)
    v = _publish_tree(client).json()["hierarchy_version_id"]
    r = _agg(client, 99, v, [{"employee_id": "E1", "work_date": "2026-05-15", "hours": 8}])
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "REVISION_NOT_FOUND"


def test_aggregation_half_open_hierarchy_boundary_is_missing(client):
    client.post("/api/revisions", json={"entries": [
        {"employee_id": "E1", "cost_center": "CC-A",
         "effective_from": "2026-01-01", "effective_to": "2027-01-01"}
    ]})
    v = _publish(client, [
        _rel("CC-A", "CC-ROOT", "2026-01-01", "2026-04-01"),
        _rel("CC-ROOT", None, "2026-01-01", "2027-01-01"),
    ]).json()["hierarchy_version_id"]

    ok = _agg(client, 1, v, [{"employee_id": "E1", "work_date": "2026-03-31", "hours": 8}])
    assert ok.status_code == 200
    assert ok.json()["records"][0]["path"] == ["CC-A", "CC-ROOT"]

    # 右端点不含 -> CC-A 当天没有生效父级 -> 整批拒绝
    bad = _agg(client, 1, v, [{"employee_id": "E1", "work_date": "2026-04-01", "hours": 8}])
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "PATH_NOT_FOUND"


def test_aggregation_implicit_and_explicit_root(client):
    client.post("/api/revisions", json={"entries": [
        {"employee_id": "E1", "cost_center": "CC-A",
         "effective_from": "2026-01-01", "effective_to": "2027-01-01"},
        {"employee_id": "E2", "cost_center": "CC-B",
         "effective_from": "2026-01-01", "effective_to": "2027-01-01"},
    ]})
    # CC-ROOT 只作为父节点出现 -> 隐式根；CC-B 显式 null 声明为根
    v = _publish(client, [
        _rel("CC-A", "CC-ROOT"),
        _rel("CC-B", None),
    ]).json()["hierarchy_version_id"]
    r = _agg(client, 1, v, [
        {"employee_id": "E1", "work_date": "2026-05-15", "hours": 8},
        {"employee_id": "E2", "work_date": "2026-05-15", "hours": 6},
    ]).json()
    assert r["records"][0]["path"] == ["CC-A", "CC-ROOT"]
    assert r["records"][1]["path"] == ["CC-B"]
    assert r["totals"] == {"CC-A": 8.0, "CC-B": 6.0, "CC-ROOT": 8.0}


def test_aggregation_accepts_documented_field_aliases(client):
    """字段别名向后兼容：attribution_revision_id / entries / parent / start_date 等。"""
    _attribute_tree(client)
    r = client.post("/api/aggregation/query", json={
        "attribution_revision_id": 1,
        "hierarchy_version": 1,
        "records": [{"employee_id": "E1", "work_date": "2026-05-15", "hours": 8}],
    })
    # 尚未发布层级版本 -> 版本不存在
    assert r.status_code == 404
    pub = client.post("/api/hierarchy/publish", json={
        "entries": [
            {"child": "CC-GZ-02", "parent": "CC-ROOT",
             "start_date": "2026-01-01", "end_date": "2027-01-01"},
            {"child": "CC-ROOT", "parent": None,
             "start_date": "2026-01-01", "end_date": "2027-01-01"},
        ]
    })
    assert pub.status_code == 201
    r2 = client.post("/api/aggregation/query", json={
        "attribution_revision_id": 1,
        "hierarchy_version": 1,
        "records": [{"employee_id": "E1", "work_date": "2026-05-15", "hours": 8}],
    })
    assert r2.status_code == 200
    assert r2.json()["totals"] == {"CC-GZ-02": 8.0, "CC-ROOT": 8.0}


def test_hierarchy_zero_records_rejected(client):
    assert _publish(client, []).status_code == 422


def test_aggregation_version_is_cumulative_and_incremental(client):
    """版本 N 可见 <= N 的关系：增量版本未提及的成本中心沿用旧版本。"""
    _attribute_tree(client)
    v1 = _publish_tree(client).json()["hierarchy_version_id"]
    # 版本 2 提交 CC-GZ-02 的变化及其新父级 CC-EAST 的挂接，未提及 CC-GZ-01 / 根
    v2 = _publish(client, [
        _rel("CC-GZ-02", "CC-EAST"),
        _rel("CC-EAST", "CC-ROOT"),
    ]).json()["hierarchy_version_id"]

    r = _agg(client, 1, v2, [
        {"employee_id": "E1", "work_date": "2026-05-15", "hours": 8},   # 新关系
        {"employee_id": "E2", "work_date": "2026-05-15", "hours": 7.5},  # 沿用版本 1
    ]).json()
    assert r["records"][0]["path"] == ["CC-GZ-02", "CC-EAST", "CC-ROOT"]
    assert r["records"][1]["path"] == ["CC-GZ-01", "CC-SOUTH", "CC-ROOT"]
    assert r["totals"] == {
        "CC-EAST": 8.0, "CC-GZ-01": 7.5, "CC-GZ-02": 8.0,
        "CC-ROOT": 15.5, "CC-SOUTH": 7.5,
    }

    # 旧版本 1 仍然完全不变
    old = _agg(client, 1, v1, [
        {"employee_id": "E1", "work_date": "2026-05-15", "hours": 8},
        {"employee_id": "E2", "work_date": "2026-05-15", "hours": 7.5},
    ]).json()
    assert old["totals"] == {
        "CC-GZ-01": 7.5, "CC-GZ-02": 8.0,
        "CC-ROOT": 15.5, "CC-SOUTH": 15.5,
    }


def test_aggregation_later_version_overrides_only_its_effective_period(client):
    """同一成本中心跨版本重叠时，按日期取生效关系且版本号最大者优先。"""
    client.post("/api/revisions", json={"entries": [
        {"employee_id": "E1", "cost_center": "CC-GZ-02",
         "effective_from": "2026-01-01", "effective_to": "2027-01-01"}
    ]})
    v1 = _publish(client, [
        _rel("CC-GZ-02", "CC-P1", "2026-01-01", "2026-12-31"),
        _rel("CC-P1", "CC-ROOT", "2026-01-01", "2027-01-01"),
        _rel("CC-ROOT", None, "2026-01-01", "2027-01-01"),
    ]).json()["hierarchy_version_id"]
    v2 = _publish(client, [
        _rel("CC-GZ-02", "CC-P2", "2026-04-01", "2026-07-01"),
        _rel("CC-P2", "CC-ROOT", "2026-04-01", "2026-07-01"),
    ]).json()["hierarchy_version_id"]

    r = _agg(client, 1, v2, [
        {"employee_id": "E1", "work_date": "2026-01-15", "hours": 1},
        {"employee_id": "E1", "work_date": "2026-05-15", "hours": 2},
        {"employee_id": "E1", "work_date": "2026-08-15", "hours": 3},
    ]).json()
    assert r["records"][0]["path"] == ["CC-GZ-02", "CC-P1", "CC-ROOT"]
    assert r["records"][1]["path"] == ["CC-GZ-02", "CC-P2", "CC-ROOT"]
    assert r["records"][2]["path"] == ["CC-GZ-02", "CC-P1", "CC-ROOT"]
    assert r["totals"] == {"CC-GZ-02": 6.0, "CC-P1": 4.0, "CC-P2": 2.0, "CC-ROOT": 6.0}


def test_aggregation_cross_version_cycle_is_conflict(client):
    """单批合法但组合到某一版本后成环时，汇总必须整批拒绝。"""
    client.post("/api/revisions", json={"entries": [
        {"employee_id": "E1", "cost_center": "CC-A",
         "effective_from": "2026-01-01", "effective_to": "2027-01-01"}
    ]})
    _publish(client, [
        _rel("CC-A", "CC-B"), _rel("CC-B", "CC-ROOT"), _rel("CC-ROOT", None),
    ])
    _publish(client, [_rel("CC-B", "CC-A")])

    r = _agg(client, 1, 2, [{"employee_id": "E1", "work_date": "2026-05-15", "hours": 8}])
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "CONFLICTING_HIERARCHY_PATH"
    assert "nodes" not in r.json()

    # 版本 1 自身仍然可正常汇总
    ok = _agg(client, 1, 1, [{"employee_id": "E1", "work_date": "2026-05-15", "hours": 8}])
    assert ok.status_code == 200
    assert ok.json()["totals"] == {"CC-A": 8.0, "CC-B": 8.0, "CC-ROOT": 8.0}
