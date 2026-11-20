"""并发测试：真实 HTTP 服务下并发提交冲红批，结果须与按生效顺序逐批处理一致。"""
import concurrent.futures as cf
import json
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

import app.db as db
from app.main import app

ROSTER = {
    "round_id": "RC",
    "topics": [
        {"topic_code": "T1", "quota": 3, "samples": ["A1", "A2", "A3"],
         "candidates": ["S1", "S2", "S3", "S4"]},
        {"topic_code": "T2", "quota": 3, "samples": ["B1", "B2", "B3"],
         "candidates": ["S3", "S4", "S5", "S6"]},
        {"topic_code": "T3", "quota": 2, "samples": ["D1", "D2"],
         "candidates": ["S1", "S5", "S7"]},
        {"topic_code": "T4", "quota": 2, "samples": ["E1", "E2"], "candidates": ["S8"]},
    ],
}

BATCHES = {
    "B01": ["A1", "B1"],
    "B02": ["A2"],
    "B03": ["B2", "D1"],
    "B04": ["S2"],          # 冲掉备选
    "B05": ["D2", "E1"],
    "B06": ["A3", "B3"],
    "B07": ["S8"],          # 冲掉 T4 唯一备选
    "B08": ["E2"],
    "B09": ["UNKNOWN-1"],   # 未知票号
    "B10": ["S1", "D1"],    # D1 可能已被前面批次冲掉：幂等集合仍按现状处理
}


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture()
def live_server(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "live.db"))
    db.init_db()
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(200):
        try:
            httpx.get(f"{base}/health", timeout=0.5)
            break
        except Exception:
            time.sleep(0.05)
    else:
        raise RuntimeError("server did not start")
    yield base
    server.should_exit = True
    t.join(timeout=10)


def _post_batch(client, base, batch_no, invoices):
    r = client.post(f"{base}/rounds/RC/redflush-batches",
                    json={"batch_no": batch_no, "invoice_nos": invoices}, timeout=30)
    return r.status_code, r.json()


def _strip_ts(receipt):
    r = dict(receipt)
    r.pop("created_at", None)
    return r


def test_concurrent_batches_equal_serial_replay(live_server, tmp_path, monkeypatch):
    with httpx.Client() as c:
        assert c.post(f"{live_server}/rounds", json=ROSTER, timeout=30).status_code == 201
        # 10 个不同批次真正并发提交
        with cf.ThreadPoolExecutor(max_workers=10) as ex:
            results = list(ex.map(lambda kv: _post_batch(c, live_server, *kv), BATCHES.items()))
    assert all(sc == 201 for sc, _ in results)

    with httpx.Client() as c:
        concurrent_receipts = c.get(f"{live_server}/rounds/RC/redflush-batches", timeout=30).json()
        concurrent_state = c.get(f"{live_server}/rounds/RC", timeout=30).json()

    # 生效顺序：seq 恰好是 1..N 各一次
    assert sorted(r["seq"] for r in concurrent_receipts) == list(range(1, len(BATCHES) + 1))
    assert concurrent_state["batches_processed"] == len(BATCHES)

    # 串行重放：按并发跑出的生效顺序逐批提交到全新数据库
    monkeypatch.setenv("DB_PATH", str(tmp_path / "serial.db"))
    db.init_db()
    with TestClient(app) as s:
        assert s.post("/rounds", json=ROSTER).status_code == 201
        serial_receipts = []
        for r in sorted(concurrent_receipts, key=lambda x: x["seq"]):
            resp = s.post("/rounds/RC/redflush-batches",
                          json={"batch_no": r["batch_no"], "invoice_nos": r["invoice_nos"]})
            assert resp.status_code == 201
            serial_receipts.append(resp.json())
        serial_state = s.get("/rounds/RC").json()

    # 并发结果 == 按生效顺序逐批处理的结果
    assert [_strip_ts(r) for r in concurrent_receipts] == [_strip_ts(r) for r in serial_receipts]
    assert concurrent_state == serial_state

    # 全局不变量：每张票最多占一个名额，样本数不超过名额
    seen = set()
    for t in concurrent_state["topics"]:
        assert len(t["samples"]) <= t["quota"]
        for inv in t["samples"]:
            assert inv not in seen, f"票号 {inv} 占了多个名额"
            seen.add(inv)


def test_concurrent_same_batch_same_set_replays(live_server):
    """同轮同批号同组票号并发重送：只有一次生效，其余拿到原回执。"""
    with httpx.Client() as c:
        assert c.post(f"{live_server}/rounds", json=ROSTER, timeout=30).status_code == 201
        with cf.ThreadPoolExecutor(max_workers=6) as ex:
            results = list(ex.map(lambda _: _post_batch(c, live_server, "BIDEM", ["A1", "B1"]),
                                  range(6)))
    statuses = sorted(sc for sc, _ in results)
    assert statuses == [200, 200, 200, 200, 200, 201]
    bodies = {json.dumps(_strip_ts(b), sort_keys=True) for _, b in results}
    assert len(bodies) == 1  # 回执完全一致
    with httpx.Client() as c:
        assert c.get(f"{live_server}/rounds/RC", timeout=30).json()["batches_processed"] == 1


def test_concurrent_same_batch_different_set_conflicts(live_server):
    """同轮同批号不同票号集合并发：只有一批生效，其余报 409。"""
    with httpx.Client() as c:
        assert c.post(f"{live_server}/rounds", json=ROSTER, timeout=30).status_code == 201
        sets = [["A1"], ["B1"], ["D1"], ["E1"]]
        with cf.ThreadPoolExecutor(max_workers=4) as ex:
            results = list(ex.map(lambda inv: _post_batch(c, live_server, "BCONF", inv), sets))
    statuses = sorted(sc for sc, _ in results)
    assert statuses == [201, 409, 409, 409]
    with httpx.Client() as c:
        assert c.get(f"{live_server}/rounds/RC", timeout=30).json()["batches_processed"] == 1
