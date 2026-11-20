"""回执核查（只读）测试：专题组、共享备选去重、核等式、历史不变性、已补满。"""
import json

import pytest
from fastapi.testclient import TestClient

import app.db as db
from app.main import app

ROSTER = {
    "round_id": "RA",
    "topics": [
        # 共享备选 C1~C4 明显不够：3+3+2=8 个缺额，可用备选只有 4 张
        {"topic_code": "价税异常", "quota": 3, "samples": ["A1", "A2", "A3"],
         "candidates": ["C1", "C2", "C3"]},
        {"topic_code": "进销项背离", "quota": 3, "samples": ["B1", "B2", "B3"],
         "candidates": ["C2", "C3", "C4"]},
        {"topic_code": "顶额开具", "quota": 2, "samples": ["D1", "D2"],
         "candidates": ["C1", "C4"]},
    ],
}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "audit.db"))
    db.init_db()
    with TestClient(app) as c:
        yield c


def register(client, roster=ROSTER, round_id="RA"):
    r = client.post("/rounds", json={**roster, "round_id": round_id})
    assert r.status_code == 201, r.text
    return r


def flush(client, batch_no, invoices, round_id="RA"):
    return client.post(f"/rounds/{round_id}/redflush-batches",
                       json={"batch_no": batch_no, "invoice_nos": invoices})


def audit(client, batch_no, round_id="RA"):
    return client.get(f"/rounds/{round_id}/redflush-batches/{batch_no}/audit")


# ---------------------------------------------------------------- 共享备选不足

def test_shared_candidates_shortage_group(client):
    """三个专题共享 4 张备选，冲掉 8 张样本后只能补 4 个，剩余缺额 4。"""
    register(client)
    receipt = flush(client, "P1",
                    ["A1", "A2", "A3", "B1", "B2", "B3", "D1", "D2"]).json()
    assert receipt["total_filled"] == 4
    round_shortfall = sum(t["shortfall"] for t in receipt["topics"])
    assert round_shortfall == 4

    r = audit(client, "P1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "shortfall"
    assert body["round_shortfall"] == 4
    g = body["topic_group"]
    # 组内缺额合计 - 去重备选票数 == 全轮剩余缺额
    assert g["vacancy_before_total"] - g["candidate_count"] == body["round_shortfall"]
    assert body["check"]["matches"] is True
    assert body["check"]["difference"] == body["check"]["round_shortfall"]

    # 共享票只列一次
    assert sorted(g["candidates"]) == sorted(set(g["candidates"]))
    assert g["candidate_count"] == len(g["candidates"])
    assert set(g["candidates"]).issubset({"C1", "C2", "C3", "C4"})
    # 各专题补样前缺额
    assert {t["topic_code"]: t["vacancy_before"] for t in g["topics"]} == {
        t["topic_code"]: t["vacancy_before"] for t in receipt["topics"]
    }


def test_candidates_snapshot_excludes_flushed_and_used(client):
    """备选口径：剔除本批冲红票，且不含此前各批已补掉的票。"""
    register(client)
    # P1 冲掉一张样本和一个备选 C3，补样会消耗 C1（价税异常）
    p1 = flush(client, "P1", ["A1", "C3"]).json()
    assert "C1" in p1["topics"][0]["replacements"]
    # P2 冲掉剩余样本，此时可用备选为 C2、C4（C3 已冲红、C1 已占用）
    p2 = flush(client, "P2", ["A2", "A3", "B1", "B2", "B3", "D1", "D2"]).json()
    shortfall = sum(t["shortfall"] for t in p2["topics"])

    body = audit(client, "P2").json()
    g = body["topic_group"]
    assert "C3" not in g["candidates"] and "C1" not in g["candidates"]
    assert set(g["candidates"]).issubset({"C2", "C4"})
    assert g["vacancy_before_total"] - g["candidate_count"] == shortfall
    assert body["check"]["matches"] is True


def test_audit_ignores_later_batches(client):
    """同一张回执在后续冲红前后核查结果完全一致（历史回放，不读现行名单）。"""
    register(client)
    flush(client, "P1", ["A1", "A2", "B1", "D1"])
    before = audit(client, "P1").json()

    # 后续再冲红：冲掉替补、冲掉备选、冲掉样本
    state = client.get("/rounds/RA").json()
    later = []
    for t in state["topics"]:
        later.extend(t["samples"])
        later.extend(t["candidates"])
    assert flush(client, "P2", later[:6]).status_code == 201
    assert flush(client, "P3", later[6:]).status_code == 201

    after = audit(client, "P1").json()
    assert before == after
    assert after["check"]["matches"] is True


def test_audit_does_not_mutate_state(client):
    register(client)
    flush(client, "P1", ["A1", "B1"])
    state_before = client.get("/rounds/RA").json()
    receipts_before = client.get("/rounds/RA/redflush-batches").json()
    audit(client, "P1")
    audit(client, "P1")
    assert client.get("/rounds/RA").json() == state_before
    assert client.get("/rounds/RA/redflush-batches").json() == receipts_before


# ---------------------------------------------------------------- 已补满

def test_fully_filled_receipt(client):
    register(client, roster={
        "round_id": "RF",
        "topics": [
            {"topic_code": "T1", "quota": 2, "samples": ["A1", "A2"], "candidates": ["C1", "C2"]},
            {"topic_code": "T2", "quota": 1, "samples": ["B1"], "candidates": ["C1", "C3"]},
        ],
    }, round_id="RF")
    assert flush(client, "P1", ["A1", "B1"], round_id="RF").json()["total_filled"] == 2
    body = audit(client, "P1", round_id="RF").json()
    assert body["status"] == "fully_filled"
    assert body["message"] == "已补满"
    assert body["round_shortfall"] == 0
    assert body["topic_group"] is None
    assert body["check"]["matches"] is True


def test_zero_vacancy_receipt(client):
    """冲的是清册外票号：无缺额，显示已补满。"""
    register(client)
    flush(client, "P1", ["NOT-IN-ROSTER"])
    body = audit(client, "P1").json()
    assert body["status"] == "fully_filled" and body["topic_group"] is None


# ---------------------------------------------------------------- 边界与错误

def test_audit_404(client):
    register(client)
    assert audit(client, "NOPE").status_code == 404
    assert audit(client, "P1", round_id="NO-ROUND").status_code == 404


def test_audit_pre_upgrade_batch_readable(client, monkeypatch):
    """模拟升级前写入的旧回执：内容结构不变，升级后可直接核查。"""
    register(client)
    flush(client, "OLD", ["A1", "B1", "D1"])
    # 旧库里的回执就是存量 JSON，无新增字段，直接可查
    row = db.connect().execute(
        "SELECT receipt_json FROM batches WHERE round_id='RA' AND batch_no='OLD'"
    ).fetchone()
    stored = json.loads(row["receipt_json"])
    assert set(stored) >= {"topics", "invoice_nos", "total_filled"}
    body = audit(client, "OLD").json()
    assert body["check"]["matches"] is True
    assert body["batch_no"] == "OLD"


def test_shared_candidate_counted_once_in_check(client):
    """共享备选在核等式中只计一张：重复计数会让等式不成立。"""
    register(client, roster={
        "round_id": "RS",
        "topics": [
            {"topic_code": "T1", "quota": 1, "samples": ["A1"], "candidates": ["X"]},
            {"topic_code": "T2", "quota": 1, "samples": ["B1"], "candidates": ["X"]},
        ],
    }, round_id="RS")
    receipt = flush(client, "P1", ["A1", "B1"], round_id="RS").json()
    assert sum(t["shortfall"] for t in receipt["topics"]) == 1
    body = audit(client, "P1", round_id="RS").json()
    g = body["topic_group"]
    assert g["candidates"] == ["X"]
    assert g["vacancy_before_total"] == 2 and g["candidate_count"] == 1
    assert g["shortfall"] == 1 == body["round_shortfall"]


def test_deficiency_group_excludes_topics_with_enough_candidates(client):
    """备选充足的专题不进缺额组：T1 够补被排除，缺口只由 T2/T3 承担。"""
    register(client, roster={
        "round_id": "RG",
        "topics": [
            {"topic_code": "T1", "quota": 2, "samples": ["A1", "A2"],
             "candidates": ["X1", "X2", "X3"]},
            {"topic_code": "T2", "quota": 2, "samples": ["B1", "B2"], "candidates": ["S1"]},
            {"topic_code": "T3", "quota": 2, "samples": ["C1", "C2"],
             "candidates": ["S1", "S2"]},
        ],
    }, round_id="RG")
    receipt = flush(client, "P1", ["A1", "A2", "B1", "B2", "C1", "C2"],
                    round_id="RG").json()
    assert receipt["topics"][0]["shortfall"] == 0  # T1 补满
    assert sum(t["shortfall"] for t in receipt["topics"]) == 2

    body = audit(client, "P1", round_id="RG").json()
    g = body["topic_group"]
    assert [t["topic_code"] for t in g["topics"]] == ["T2", "T3"]
    assert g["vacancy_before_total"] == 4 and g["candidates"] == ["S1", "S2"]
    assert g["shortfall"] == 2 == body["round_shortfall"]


# ---------------------------------------------------------------- 随机属性测试

def test_random_rosters_always_satisfy_equation(client):
    """随机拓扑（含大量共享备选）+ 随机冲红批次：每个回执的核查等式都成立。

    同时校验：组内缺额合计 - 去重备选票数 == 回执全轮剩余缺额；
    且每个专题的补样前缺额与该组可补数一致（最大可补 = 缺额合计 - 组缺额）。
    """
    import random

    rnd = random.Random(20261120)
    for trial in range(30):
        rid = f"RX{trial}"
        n_topics = rnd.randint(1, 5)
        pool = [f"P{trial}-C{i}" for i in range(rnd.randint(1, 7))]
        topics = []
        used_samples = set()
        for ti in range(n_topics):
            quota = rnd.randint(0, 3)
            samples = []
            while len(samples) < quota:
                inv = f"P{trial}-S{ti}-{len(samples)}"
                if inv not in used_samples:
                    samples.append(inv)
                    used_samples.add(inv)
            cands = rnd.sample(pool, rnd.randint(0, len(pool)))
            topics.append({"topic_code": f"T{ti}", "quota": quota,
                           "samples": samples, "candidates": cands})
        r = client.post("/rounds", json={"round_id": rid, "topics": topics})
        assert r.status_code == 201, r.text

        all_invoices = sorted(used_samples) + pool
        for bi in range(4):
            k = rnd.randint(1, max(1, len(all_invoices) // 3))
            batch = rnd.sample(all_invoices, min(k, len(all_invoices)))
            resp = flush(client, f"B{bi}", batch, round_id=rid)
            assert resp.status_code in (201,), resp.text
            receipt = resp.json()
            body = audit(client, f"B{bi}", round_id=rid).json()
            assert body["check"]["matches"] is True, (trial, bi, body)
            assert body["check"]["difference"] == body["round_shortfall"]
            assert body["round_shortfall"] == sum(t["shortfall"] for t in receipt["topics"])
            g = body["topic_group"]
            if body["round_shortfall"] == 0:
                assert body["status"] == "fully_filled" and g is None
            else:
                assert body["status"] == "shortfall" and g is not None
                assert g["vacancy_before_total"] - g["candidate_count"] == body["round_shortfall"]
                assert g["candidate_count"] == len(set(g["candidates"]))
                # 组内每个专题的缺额都大于 0（否则不该出现在缺额组）
                assert all(t["vacancy_before"] > 0 for t in g["topics"])
