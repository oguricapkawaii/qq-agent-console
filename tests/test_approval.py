"""确认码机制测试：伪造、过期、越权、跨群、一次性使用，以及旧 /approve 的迁移提示。

新契约（见 app/security/confirmations.py、app/routing/dispatcher.py）：
- 确认码由 `/confirm <确认码>` 消费；`/approve` 已停用，只回迁移提示；
- 确认码一次性、绑定「类型 + 群 + 发起人 + 摘要 + TTL」，模型无法生成或自行确认；
- 目录候选选择用 `/workspace select <确认码> <序号>`，不能用 /confirm 消费。
"""

from __future__ import annotations

import asyncio

from app.security.confirmations import (
    ApprovalOutcome,
    ConfirmationKind,
    ConfirmationOutcome,
    InMemoryConfirmationStore,
)
from helpers import (
    ALLOWED_GROUP,
    ALLOWED_USER,
    ConsoleHarness,
    FakeAgent,
    make_settings,
    OTHER_GROUP,
    OTHER_USER,
)

ACTION = ConfirmationKind.ACTION


def make_harness(tmp_path, **overrides):
    settings = make_settings(
        tmp_path,
        qq_allowed_user_ids=[ALLOWED_USER, OTHER_USER],
        qq_allowed_group_ids=[ALLOWED_GROUP, OTHER_GROUP],
        **overrides,
    )
    return ConsoleHarness(settings, {"glm": FakeAgent("glm")})


async def test_bogus_confirm_code_rejected(tmp_path):
    """伪造的确认码无法通过。"""
    harness = make_harness(tmp_path)
    out = await harness.command("/confirm DEADBEEF")
    assert "无效或已被使用" in out


async def test_expired_confirm_code_rejected(tmp_path):
    harness = make_harness(tmp_path)
    record = harness.confirmations.create(
        kind=ACTION, user_id=ALLOWED_USER, group_id=ALLOWED_GROUP,
        summary="测试操作", ttl_seconds=0,
    )
    await asyncio.sleep(0.02)
    out = await harness.command(f"/confirm {record.code}")
    assert "过期" in out


async def test_confirm_code_bound_to_user(tmp_path):
    """确认码绑定发起用户：他人无法代为确认，且不消耗确认码。"""
    harness = make_harness(tmp_path)
    record = harness.confirmations.create(
        kind=ACTION, user_id=OTHER_USER, group_id=ALLOWED_GROUP,
        summary="危险操作", ttl_seconds=300,
    )

    out = await harness.command(f"/confirm {record.code}", user_id=ALLOWED_USER)
    assert "不属于你" in out

    # 本人仍可确认
    out = await harness.command(f"/confirm {record.code}", user_id=OTHER_USER)
    assert "已确认" in out


async def test_confirm_code_bound_to_group(tmp_path):
    """确认码绑定发起群：跨群使用被拒绝。"""
    harness = make_harness(tmp_path)
    record = harness.confirmations.create(
        kind=ACTION, user_id=ALLOWED_USER, group_id=OTHER_GROUP,
        summary="别的群的操作", ttl_seconds=300,
    )
    out = await harness.command(f"/confirm {record.code}")
    assert "别的群" in out


async def test_confirm_code_single_use(tmp_path):
    harness = make_harness(tmp_path)
    record = harness.confirmations.create(
        kind=ACTION, user_id=ALLOWED_USER, group_id=ALLOWED_GROUP,
        summary="危险操作", ttl_seconds=300,
    )
    assert "已确认" in await harness.command(f"/confirm {record.code}")
    # 同一个码不能二次使用
    assert "无效或已被使用" in await harness.command(f"/confirm {record.code}")


async def test_confirm_requires_args(tmp_path):
    harness = make_harness(tmp_path)
    assert "用法" in await harness.command("/confirm")


async def test_workspace_select_code_cannot_be_confirmed(tmp_path):
    """目录选择码必须走 /workspace select，不能被 /confirm 消费。"""
    harness = make_harness(tmp_path)
    record = harness.confirmations.create(
        kind=ConfirmationKind.WORKSPACE_SELECT, user_id=ALLOWED_USER,
        group_id=ALLOWED_GROUP, summary="选择目录", payload={"candidates": []},
    )
    out = await harness.command(f"/confirm {record.code}")
    assert "/workspace select" in out
    assert harness.confirmations.pending_count() == 1, "提示不应消耗确认码"


async def test_confirm_without_action_is_a_plain_confirmation(tmp_path):
    """没有附带动作的确认码：确认成功但不执行任何操作。"""
    harness = make_harness(tmp_path)
    record = harness.confirmations.create(
        kind=ACTION, user_id=ALLOWED_USER, group_id=ALLOWED_GROUP, summary="占位操作"
    )
    out = await harness.command(f"/confirm {record.code}")
    assert "已确认：占位操作" in out
    assert "没有需要额外执行的动作" in out


async def test_legacy_approve_is_migration_hint_only(tmp_path):
    """/approve 已停用：只回迁移提示，不确认、不消耗确认码。"""
    harness = make_harness(tmp_path)
    record = harness.confirmations.create(
        kind=ACTION, user_id=ALLOWED_USER, group_id=ALLOWED_GROUP, summary="占位操作"
    )
    out = await harness.command(f"/approve {record.code}")
    assert "旧口令" in out and "/confirm <确认码>" in out
    assert harness.confirmations.pending_count() == 1
    # 本人仍能用新命令确认
    assert "已确认" in await harness.command(f"/confirm {record.code}")


async def test_model_reply_cannot_self_confirm(tmp_path):
    """模型输出中出现 /confirm 不会被当作命令执行，也不会产生/消耗确认码。"""
    settings = make_settings(tmp_path)
    glm = FakeAgent("glm", reply="请管理员执行 /confirm ABCD1234 以继续")
    harness = ConsoleHarness(settings, {"glm": glm})

    out = await harness.command("/task glm -- 做点什么")

    assert harness.confirmations.pending_count() == 0
    # 回复是纯文本（带回复头），不会被再次解析为命令
    assert all(not text.startswith("/") for text in harness.transport.texts())
    assert "/confirm ABCD1234" in out


def test_store_propose_returns_unique_codes():
    store = InMemoryConfirmationStore(default_ttl_seconds=60)
    codes = {
        store.propose(user_id=1, task_id="t", summary="s").code for _ in range(100)
    }
    assert len(codes) == 100
    assert store.pending_count() == 100


def test_confirmation_outcome_enum_complete():
    assert {item.name for item in ConfirmationOutcome} == {
        "OK", "NOT_FOUND", "EXPIRED", "WRONG_USER", "WRONG_SCOPE", "WRONG_KIND",
    }
    # 旧名称是同一份实现的别名，避免出现第二套确认机制
    assert ApprovalOutcome is ConfirmationOutcome
