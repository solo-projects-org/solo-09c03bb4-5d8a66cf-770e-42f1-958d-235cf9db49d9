"""功能测试：登记清册、冲红补样、共享备选、最大匹配、幂等/冲突、重启持久化。"""
import pytest
from fastapi.testclient import TestClient

import app.db as db
from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    db.init_db()
    with TestClient(app) as c:
        yield c


def register(client, round_id="R1", topics=None):
    payload = {
        "round_id": round_id,
        "topics": topics
        or [{"topic_code": "T1", "quota": 2, "samples": ["A1", "A2"], "candidates": ["C1", "C2"]}],
    }
    return client.post("/rounds", json=payload)


def flush(client, batch_no, invoices, round_id="R1"):
    return client.post(f"/rounds/{round_id}/redflush-batches",
                       json={"batch_no": batch_no, "invoice_nos": invoices})


# ---------------------------------------------------------------- 登记

def test_register_and_get_state(client):
    r = register(client)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["round_id"] == "R1"
    t1 = body["topics"][0]
    assert t1["quota"] == 2 and t1["samples"] == ["A1", "A2"]
    assert t1["candidates"] == ["C1", "C2"] and t1["shortfall"] == 0
    assert body["batches_processed"] == 0

    got = client.get("/rounds/R1")
    assert got.status_code == 200 and got.json() == body


def test_register_idempotent_and_conflict(client):
    assert register(client).status_code == 201
    again = register(client)
    assert again.status_code == 200  # 同内容重登 -> 幂等返回现状
    diff = register(client, topics=[{"topic_code": "T9", "quota": 1, "samples": ["Z1"]}])
    assert diff.status_code == 409  # 同轮次不同清册 -> 冲突


@pytest.mark.parametrize(
    "topics",
    [
        # 专题代码重复
        [{"topic_code": "T1", "quota": 1}, {"topic_code": "T1", "quota": 1}],
        # 同一票号是多个专题的样本
        [{"topic_code": "T1", "quota": 1, "samples": ["X"]},
         {"topic_code": "T2", "quota": 1, "samples": ["X"]}],
        # 同一票号既是样本又是备选
        [{"topic_code": "T1", "quota": 1, "samples": ["X"]},
         {"topic_code": "T2", "quota": 1, "candidates": ["X"]}],
        # 已发样本数超过名额
        [{"topic_code": "T1", "quota": 1, "samples": ["A", "B"]}],
        # 名单内票号重复
        [{"topic_code": "T1", "quota": 2, "samples": ["A", "A"]}],
    ],
)
def test_register_validation(client, topics):
    r = register(client, topics=topics)
    assert r.status_code == 400, r.text


def test_shared_candidates_allowed(client):
    r = register(client, topics=[
        {"topic_code": "T1", "quota": 1, "samples": ["A1"], "candidates": ["X", "Y"]},
        {"topic_code": "T2", "quota": 1, "samples": ["B1"], "candidates": ["X", "Z"]},
    ])
    assert r.status_code == 201, r.text


# ---------------------------------------------------------------- 冲红补样

def test_redflush_basic_backfill(client):
    register(client)
    r = flush(client, "B1", ["A1"])
    assert r.status_code == 201, r.text
    receipt = r.json()
    assert receipt["seq"] == 1
    t1 = receipt["topics"][0]
    assert t1["vacancy_before"] == 1 and t1["replacements"] == ["C1"] and t1["shortfall"] == 0
    assert receipt["removed"]["samples"] == {"T1": ["A1"]}
    assert receipt["total_filled"] == 1

    state = client.get("/rounds/R1").json()["topics"][0]
    assert state["samples"] == ["A2", "C1"]  # 仍有效的已发样本照旧，替补补入
    assert state["candidates"] == ["C2"]


def test_shared_candidate_single_slot(client):
    """共享备选 X 只能补一个名额：先登记的专题优先，另一个留缺额。"""
    register(client, topics=[
        {"topic_code": "T1", "quota": 1, "samples": ["A1"], "candidates": ["X"]},
        {"topic_code": "T2", "quota": 1, "samples": ["B1"], "candidates": ["X"]},
    ])
    r = flush(client, "B1", ["A1", "B1"])
    receipt = r.json()
    assert receipt["total_filled"] == 1
    t1, t2 = receipt["topics"]
    assert t1["replacements"] == ["X"] and t1["shortfall"] == 0
    assert t2["replacements"] == [] and t2["shortfall"] == 1


def test_max_matching_not_greedy(client):
    """T1 备选 [X,Y]、T2 备选 [X]：最大匹配应让 T1 拿 Y、T2 拿 X，总数 2。"""
    register(client, topics=[
        {"topic_code": "T1", "quota": 1, "samples": ["A1"], "candidates": ["X", "Y"]},
        {"topic_code": "T2", "quota": 1, "samples": ["B1"], "candidates": ["X"]},
    ])
    receipt = flush(client, "B1", ["A1", "B1"]).json()
    assert receipt["total_filled"] == 2
    t1, t2 = receipt["topics"]
    assert t1["replacements"] == ["Y"] and t2["replacements"] == ["X"]


def test_flushed_candidate_exits_pool(client):
    register(client, topics=[{"topic_code": "T1", "quota": 1, "samples": ["A1"], "candidates": ["X"]}])
    r1 = flush(client, "B1", ["X"]).json()  # 冲掉备选 X
    assert r1["removed"]["candidates"] == {"T1": ["X"]}
    assert r1["total_filled"] == 0
    assert client.get("/rounds/R1").json()["topics"][0]["candidates"] == []

    r2 = flush(client, "B2", ["A1"]).json()  # 再冲样本，无备选可补 -> 缺额 1
    assert r2["topics"][0]["shortfall"] == 1


def test_replacement_can_be_flushed_and_refilled(client):
    register(client, topics=[
        {"topic_code": "T1", "quota": 1, "samples": ["A1"], "candidates": ["C1", "C2"]}
    ])
    assert flush(client, "B1", ["A1"]).json()["topics"][0]["replacements"] == ["C1"]
    r2 = flush(client, "B2", ["C1"]).json()  # C1 已是样本，冲掉后再补
    assert r2["removed"]["samples"] == {"T1": ["C1"]}
    assert r2["topics"][0]["replacements"] == ["C2"]
    assert client.get("/rounds/R1").json()["topics"][0]["samples"] == ["C2"]


def test_unknown_invoice_is_noop(client):
    register(client)
    r = flush(client, "B1", ["NOPE-1"]).json()
    assert r["unknown_invoices"] == ["NOPE-1"]
    assert r["removed"] == {"samples": {}, "candidates": {}}
    assert r["total_filled"] == 0
    state = client.get("/rounds/R1").json()["topics"][0]
    assert state["samples"] == ["A1", "A2"] and state["candidates"] == ["C1", "C2"]


# ---------------------------------------------------------------- 幂等与冲突

def test_failed_batch_rolls_back(client, monkeypatch):
    """批处理中途失败：退出操作与回执整批回滚，名单保持原样。"""
    register(client)
    import app.service as service

    def boom(*_a, **_k):
        raise RuntimeError("模拟补样中途失败")

    monkeypatch.setattr(service, "allocate", boom)
    with pytest.raises(RuntimeError):
        flush(client, "B1", ["A1"])
    state = client.get("/rounds/R1").json()
    assert state["topics"][0]["samples"] == ["A1", "A2"]  # 退出操作已回滚
    assert state["topics"][0]["candidates"] == ["C1", "C2"]
    assert state["batches_processed"] == 0  # 回执未写入

def test_redflush_idempotent_replay(client):
    register(client, topics=[
        {"topic_code": "T1", "quota": 2, "samples": ["A1", "A2"], "candidates": ["C1", "C2"]}
    ])
    first = flush(client, "B1", ["A2", "A1"])
    assert first.status_code == 201
    replay = flush(client, "B1", ["A1", "A2"])  # 同组票号（换序）重送
    assert replay.status_code == 200
    assert replay.json() == first.json()  # 返回原回执
    assert client.get("/rounds/R1").json()["batches_processed"] == 1


def test_redflush_conflict_on_different_set(client):
    register(client)
    assert flush(client, "B1", ["A1"]).status_code == 201
    conflict = flush(client, "B1", ["A2"])  # 同轮同批号换一组票号
    assert conflict.status_code == 409
    assert client.get("/rounds/R1").json()["batches_processed"] == 1


# ---------------------------------------------------------------- 查询与持久化

def test_receipts_query(client):
    register(client)
    flush(client, "B1", ["A1"])
    flush(client, "B2", ["A2"])
    one = client.get("/rounds/R1/redflush-batches/B1")
    assert one.status_code == 200 and one.json()["batch_no"] == "B1"
    allr = client.get("/rounds/R1/redflush-batches")
    assert [r["batch_no"] for r in allr.json()] == ["B1", "B2"]
    assert client.get("/rounds/R1/redflush-batches/BX").status_code == 404
    assert client.get("/rounds/NOPE").status_code == 404
    assert client.get("/rounds/NOPE/redflush-batches").status_code == 404


def test_restart_persistence(tmp_path, monkeypatch):
    """重启（新建连接/新应用实例）后仍可查现行名单和回执。"""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "restart.db"))
    db.init_db()
    with TestClient(app) as c1:
        register(c1)
        assert flush(c1, "B1", ["A1"]).status_code == 201
    with TestClient(app) as c2:  # 模拟重启：同一数据库文件，全新连接
        state = c2.get("/rounds/R1").json()
        assert state["topics"][0]["samples"] == ["A2", "C1"]
        assert state["batches_processed"] == 1
        receipt = c2.get("/rounds/R1/redflush-batches/B1").json()
        assert receipt["topics"][0]["replacements"] == ["C1"]
