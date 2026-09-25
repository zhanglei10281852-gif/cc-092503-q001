from __future__ import annotations

import concurrent.futures


def _create_approver(client, admin, username, display_name):
    user = client.post(
        "/api/users",
        headers=admin["headers"],
        json={"username": username, "password": "Approver!23456", "display_name": display_name, "role_codes": ["approver"]},
    )
    assert user.status_code == 201, user.text
    login = client.post("/api/auth/login", json={"username": username, "password": "Approver!23456", "client_label": "tests"})
    assert login.status_code == 200, login.text
    return {"headers": {"Authorization": f"Bearer {login.json()['token']}"}, "id": user.json()["id"]}


def _create_destruction_request(client, admin, quantity=5):
    batch = client.post(
        "/api/samples/batches",
        headers=admin["headers"],
        json={"batch_code": "APR-BATCH", "project_code": "P-APR", "expected_count": 1},
    )
    assert batch.status_code == 201, batch.text
    sample = client.post(
        "/api/samples",
        headers=admin["headers"],
        json={"sample_code": "APR-SAMPLE", "batch_id": batch.json()["id"], "sample_type": "试剂", "quantity": 20, "unit": "g"},
    )
    assert sample.status_code == 201, sample.text
    approval = client.post(
        "/api/samples/approvals",
        headers=admin["headers"],
        json={"action_type": "destruction", "resource_type": "sample", "resource_id": sample.json()["id"], "payload": {"quantity": quantity}},
    )
    assert approval.status_code == 201, approval.text
    return sample.json(), approval.json()


def _decide(client, approver, request_id, decision="approve", comment="同意销毁"):
    return client.post(
        f"/api/samples/approvals/{request_id}/decisions",
        headers=approver["headers"],
        json={"decision": decision, "comment": comment},
    )


def _db_rows(sql, params=()):
    from app.database import get_connection

    return [dict(row) for row in get_connection().execute(sql, params).fetchall()]


def _decision_rows(request_id):
    return _db_rows("SELECT * FROM approval_decisions WHERE request_id=? ORDER BY id", (request_id,))


def _decide_audit_rows(request_id):
    return _db_rows(
        "SELECT * FROM audit_events WHERE action='approval.decide' AND resource_type='approval_request' AND resource_id=?",
        (str(request_id),),
    )


def _request_row(request_id):
    return _db_rows("SELECT * FROM approval_requests WHERE id=?", (request_id,))[0]


def test_identical_decision_retry_returns_existing_result(client, admin):
    approver = _create_approver(client, admin, "approver.one", "审批人一")
    _, approval = _create_destruction_request(client, admin)

    first = _decide(client, approver, approval["id"])
    assert first.status_code == 200, first.text
    assert first.json()["replayed"] is False
    assert first.json()["state"] == "pending"

    second = _decide(client, approver, approval["id"])
    assert second.status_code == 200, second.text
    assert second.json()["replayed"] is True
    assert second.json()["state"] == "pending"
    assert len(second.json()["decisions"]) == 1

    # 数据库证明：只有一条有效决定、一条审批审计，请求版本未被重试推高
    assert len(_decision_rows(approval["id"])) == 1
    assert len(_decide_audit_rows(approval["id"])) == 1
    assert _request_row(approval["id"])["version"] == first.json()["version"]


def test_changed_decision_is_stable_conflict(client, admin):
    approver = _create_approver(client, admin, "approver.one", "审批人一")
    _, approval = _create_destruction_request(client, admin)
    assert _decide(client, approver, approval["id"], decision="approve").status_code == 200

    changed = _decide(client, approver, approval["id"], decision="reject", comment="改变主意")
    assert changed.status_code == 409
    assert changed.json()["error"]["code"] == "conflict"

    different_comment = _decide(client, approver, approval["id"], decision="approve", comment="换个评语")
    assert different_comment.status_code == 409
    assert different_comment.json()["error"]["code"] == "conflict"

    # 冲突请求没有改写既有决定，也没有留下额外审计
    rows = _decision_rows(approval["id"])
    assert len(rows) == 1
    assert rows[0]["decision"] == "approve"
    assert rows[0]["comment"] == "同意销毁"
    assert len(_decide_audit_rows(approval["id"])) == 1


def test_two_distinct_approvers_still_accumulate(client, admin):
    approver_one = _create_approver(client, admin, "approver.one", "审批人一")
    approver_two = _create_approver(client, admin, "approver.two", "审批人二")
    _, approval = _create_destruction_request(client, admin)

    first = _decide(client, approver_one, approval["id"])
    assert first.status_code == 200
    assert first.json()["state"] == "pending"

    second = _decide(client, approver_two, approval["id"], comment="复核同意")
    assert second.status_code == 200
    assert second.json()["replayed"] is False
    assert second.json()["state"] == "approved"
    assert len(_decision_rows(approval["id"])) == 2


def test_replay_after_terminal_state_returns_existing_result(client, admin):
    approver_one = _create_approver(client, admin, "approver.one", "审批人一")
    approver_two = _create_approver(client, admin, "approver.two", "审批人二")
    _, approval = _create_destruction_request(client, admin)
    _decide(client, approver_one, approval["id"])
    finished = _decide(client, approver_two, approval["id"], comment="复核同意")
    assert finished.json()["state"] == "approved"

    # 审批完成后客户端重试同一决定：安全返回既有结果而不是报错
    replay = _decide(client, approver_one, approval["id"])
    assert replay.status_code == 200, replay.text
    assert replay.json()["replayed"] is True
    assert replay.json()["state"] == "approved"
    assert len(replay.json()["decisions"]) == 2
    assert len(_decision_rows(approval["id"])) == 2
    assert len(_decide_audit_rows(approval["id"])) == 2


def test_new_decision_after_terminal_state_conflicts(client, admin):
    approver_one = _create_approver(client, admin, "approver.one", "审批人一")
    approver_two = _create_approver(client, admin, "approver.two", "审批人二")
    approver_three = _create_approver(client, admin, "approver.three", "审批人三")
    _, approval = _create_destruction_request(client, admin)
    _decide(client, approver_one, approval["id"])
    _decide(client, approver_two, approval["id"], comment="复核同意")

    late = _decide(client, approver_three, approval["id"])
    assert late.status_code == 409
    assert late.json()["error"]["code"] == "conflict"
    assert late.json()["error"]["context"]["state"] == "approved"
    assert len(_decision_rows(approval["id"])) == 2


def test_concurrent_identical_retries_do_not_add_votes_or_audit(client, admin):
    approver = _create_approver(client, admin, "approver.one", "审批人一")
    _, approval = _create_destruction_request(client, admin)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(lambda _: _decide(client, approver, approval["id"]), range(8)))

    assert all(response.status_code == 200 for response in responses)
    assert sum(1 for response in responses if response.json()["replayed"] is False) == 1
    # 并发重试没有增加有效票数、没有提前通过双人审批、没有留下重复审计
    assert len(_decision_rows(approval["id"])) == 1
    assert _request_row(approval["id"])["state"] == "pending"
    assert len(_decide_audit_rows(approval["id"])) == 1


def test_destruction_execute_flow_unchanged(client, admin):
    approver_one = _create_approver(client, admin, "approver.one", "审批人一")
    approver_two = _create_approver(client, admin, "approver.two", "审批人二")
    sample, approval = _create_destruction_request(client, admin)
    _decide(client, approver_one, approval["id"])
    _decide(client, approver_two, approval["id"], comment="复核同意")

    payload = {"method": "高温焚烧", "witness_one": approver_one["id"], "witness_two": approver_two["id"]}
    executed = client.post(f"/api/sample-operations/destructions/{approval['id']}", headers=admin["headers"], json=payload)
    assert executed.status_code == 201, executed.text
    assert executed.json()["replayed"] is False
    assert executed.json()["record"]["destroyed_quantity"] == 5
    assert executed.json()["sample"]["quantity"] == 15
    assert _request_row(approval["id"])["state"] == "executed"

    again = client.post(f"/api/sample-operations/destructions/{approval['id']}", headers=admin["headers"], json=payload)
    assert again.status_code == 409
    assert len(_db_rows("SELECT * FROM destruction_records WHERE request_id=?", (approval["id"],))) == 1

    # 执行完毕后同一审批人的相同重试仍然安全返回既有结果
    replay = _decide(client, approver_one, approval["id"])
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert replay.json()["state"] == "executed"
    assert len(_decision_rows(approval["id"])) == 2
