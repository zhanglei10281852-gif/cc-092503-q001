from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor


def _make_approver(client, admin, username):
    created = client.post(
        "/api/users",
        headers=admin["headers"],
        json={
            "username": username,
            "password": "Approver!23456",
            "display_name": f"审批人-{username}",
            "role_codes": ["approver"],
        },
    )
    assert created.status_code == 201, created.text
    login = client.post(
        "/api/auth/login",
        json={"username": username, "password": "Approver!23456", "client_label": "tests"},
    )
    assert login.status_code == 200, login.text
    return {"headers": {"Authorization": f"Bearer {login.json()['token']}"}, "id": created.json()["id"]}


def _bootstrap_request(client, admin, quantity=20, destroy_quantity=10):
    suffix = uuid.uuid4().hex[:8]
    location = client.post(
        "/api/samples/locations",
        headers=admin["headers"],
        json={
            "code": f"APR-L-{suffix}",
            "building": "样品楼",
            "room": "常温库",
            "cabinet": "一号柜",
            "shelf": "一层",
            "sensitivity": "normal",
            "capacity_units": 50,
        },
    )
    assert location.status_code == 201, location.text
    batch = client.post(
        "/api/samples/batches",
        headers=admin["headers"],
        json={"batch_code": f"APR-B-{suffix}", "project_code": "P-APR", "expected_count": 1},
    )
    assert batch.status_code == 201, batch.text
    sample = client.post(
        "/api/samples",
        headers=admin["headers"],
        json={
            "sample_code": f"APR-S-{suffix}",
            "batch_id": batch.json()["id"],
            "sample_type": "水样",
            "quantity": quantity,
            "unit": "mL",
            "location_id": location.json()["id"],
        },
    )
    assert sample.status_code == 201, sample.text
    approval = client.post(
        "/api/samples/approvals",
        headers=admin["headers"],
        json={
            "action_type": "destruction",
            "resource_type": "sample",
            "resource_id": sample.json()["id"],
            "payload": {"quantity": destroy_quantity},
        },
    )
    assert approval.status_code == 201, approval.text
    return sample.json(), approval.json()


def _decide(client, approver, request_id, decision="approve", comment="同意"):
    return client.post(
        f"/api/samples/approvals/{request_id}/decisions",
        headers=approver["headers"],
        json={"decision": decision, "comment": comment},
    )


def _decision_audit_rows(client, admin, request_id):
    audit = client.get(
        "/api/audit",
        headers=admin["headers"],
        params={"action": "approval.decide", "resource_type": "approval_request", "size": 100},
    )
    assert audit.status_code == 200, audit.text
    return [row for row in audit.json()["data"] if row["resource_id"] == str(request_id)]


def test_identical_decision_replay_returns_existing_result(client, admin):
    approver = _make_approver(client, admin, "approver.replay")
    _, approval = _bootstrap_request(client, admin)

    first = _decide(client, approver, approval["id"])
    assert first.status_code == 200, first.text
    assert first.json()["replayed"] is False
    assert first.json()["state"] == "pending"

    second = _decide(client, approver, approval["id"])
    assert second.status_code == 200, second.text
    assert second.json()["replayed"] is True
    assert second.json()["state"] == "pending"
    assert len(second.json()["decisions"]) == 1
    assert second.json()["decisions"][0]["approver_user_id"] == approver["id"]

    # 重放不产生额外审计记录
    assert len(_decision_audit_rows(client, admin, approval["id"])) == 1


def test_changed_decision_is_stable_business_conflict(client, admin):
    approver = _make_approver(client, admin, "approver.change")
    _, approval = _bootstrap_request(client, admin)

    assert _decide(client, approver, approval["id"], decision="approve").status_code == 200

    flipped = _decide(client, approver, approval["id"], decision="reject", comment="改变主意")
    assert flipped.status_code == 409
    assert flipped.json()["error"]["code"] == "conflict"
    assert flipped.json()["error"]["context"]["existing_decision"] == "approve"

    reworded = _decide(client, approver, approval["id"], decision="approve", comment="换个措辞")
    assert reworded.status_code == 409
    assert reworded.json()["error"]["code"] == "conflict"

    # 冲突尝试不改变既有决定与票数
    retry = _decide(client, approver, approval["id"], decision="approve", comment="同意")
    assert retry.status_code == 200
    assert retry.json()["replayed"] is True
    assert len(retry.json()["decisions"]) == 1
    assert retry.json()["state"] == "pending"


def test_distinct_approvers_accumulate_to_approval(client, admin):
    first = _make_approver(client, admin, "approver.accum.one")
    second = _make_approver(client, admin, "approver.accum.two")
    _, approval = _bootstrap_request(client, admin)

    one = _decide(client, first, approval["id"])
    assert one.json()["state"] == "pending"
    two = _decide(client, second, approval["id"], comment="复核同意")
    assert two.json()["state"] == "approved"
    assert {item["approver_user_id"] for item in two.json()["decisions"]} == {first["id"], second["id"]}


def test_decision_after_terminal_state_conflicts_but_retry_replays(client, admin):
    first = _make_approver(client, admin, "approver.term.one")
    second = _make_approver(client, admin, "approver.term.two")
    third = _make_approver(client, admin, "approver.term.three")
    _, approval = _bootstrap_request(client, admin)

    _decide(client, first, approval["id"])
    assert _decide(client, second, approval["id"]).json()["state"] == "approved"

    late = _decide(client, third, approval["id"])
    assert late.status_code == 409
    assert late.json()["error"]["code"] == "conflict"
    assert late.json()["error"]["context"]["state"] == "approved"

    replay = _decide(client, first, approval["id"])
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert replay.json()["state"] == "approved"
    assert len(replay.json()["decisions"]) == 2


def test_rejection_is_terminal_and_retry_safe(client, admin):
    first = _make_approver(client, admin, "approver.reject.one")
    second = _make_approver(client, admin, "approver.reject.two")
    _, approval = _bootstrap_request(client, admin)

    rejected = _decide(client, first, approval["id"], decision="reject", comment="风险过高")
    assert rejected.json()["state"] == "rejected"

    late = _decide(client, second, approval["id"])
    assert late.status_code == 409
    assert late.json()["error"]["context"]["state"] == "rejected"

    replay = _decide(client, first, approval["id"], decision="reject", comment="风险过高")
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert replay.json()["state"] == "rejected"
    assert len(replay.json()["decisions"]) == 1


def test_concurrent_identical_decisions_count_single_vote(client, admin):
    approver = _make_approver(client, admin, "approver.concurrent")
    _, approval = _bootstrap_request(client, admin)

    with ThreadPoolExecutor(max_workers=6) as pool:
        responses = list(pool.map(lambda _: _decide(client, approver, approval["id"]), range(6)))

    assert all(response.status_code == 200 for response in responses)
    bodies = [response.json() for response in responses]
    assert sum(1 for body in bodies if body["replayed"] is False) == 1
    assert all(body["state"] == "pending" for body in bodies)
    assert all(len(body["decisions"]) == 1 for body in bodies)
    assert len(_decision_audit_rows(client, admin, approval["id"])) == 1


def test_concurrent_distinct_approvers_complete_exactly_once(client, admin):
    first = _make_approver(client, admin, "approver.race.one")
    second = _make_approver(client, admin, "approver.race.two")
    _, approval = _bootstrap_request(client, admin)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(_decide, client, first, approval["id"], "approve", "甲同意"),
            pool.submit(_decide, client, second, approval["id"], "approve", "乙同意"),
        ]
        responses = [future.result() for future in futures]

    assert all(response.status_code == 200 for response in responses)
    states = sorted(response.json()["state"] for response in responses)
    assert states == ["approved", "pending"]
    final = next(response.json() for response in responses if response.json()["state"] == "approved")
    assert len(final["decisions"]) == 2
    assert len(_decision_audit_rows(client, admin, approval["id"])) == 2


def test_destruction_execution_flow_remains_intact(client, admin):
    first = _make_approver(client, admin, "approver.exec.one")
    second = _make_approver(client, admin, "approver.exec.two")
    third = _make_approver(client, admin, "approver.exec.three")
    sample, approval = _bootstrap_request(client, admin, quantity=20, destroy_quantity=10)

    _decide(client, first, approval["id"])
    assert _decide(client, second, approval["id"]).json()["state"] == "approved"

    execution = client.post(
        f"/api/sample-operations/destructions/{approval['id']}",
        headers=admin["headers"],
        json={"method": "高温灭活", "witness_one": first["id"], "witness_two": second["id"]},
    )
    assert execution.status_code == 201, execution.text
    assert execution.json()["replayed"] is False
    assert execution.json()["sample"]["quantity"] == 10
    assert execution.json()["sample"]["lifecycle_state"] == "partially_consumed"

    # 既有销毁执行流程保持不变：终态后重复执行得到稳定冲突，且不会二次扣减
    repeated_execution = client.post(
        f"/api/sample-operations/destructions/{approval['id']}",
        headers=admin["headers"],
        json={"method": "高温灭活", "witness_one": first["id"], "witness_two": second["id"]},
    )
    assert repeated_execution.status_code == 409

    # 执行完毕后：新审批人补交 -> 409；已决审批人相同重试 -> 重放
    late = _decide(client, third, approval["id"])
    assert late.status_code == 409
    assert late.json()["error"]["context"]["state"] == "executed"
    replay = _decide(client, first, approval["id"])
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert replay.json()["state"] == "executed"

    detail = client.get(f"/api/samples/{sample['id']}", headers=admin["headers"])
    assert detail.json()["quantity"] == 10
