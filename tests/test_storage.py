"""SQLite 上下文存储直接测试。"""

from __future__ import annotations

from app.models import ChatMessage
from app.storage import ContextStore


def make_store(tmp_path) -> ContextStore:
    return ContextStore(str(tmp_path / "ctx.db"))


def test_append_and_recent_roundtrip(tmp_path):
    store = make_store(tmp_path)
    store.append(group_id=1, user_id=10, agent="glm", role="user", content="q1")
    store.append(group_id=1, user_id=10, agent="glm", role="assistant", content="a1")
    store.append(group_id=1, user_id=10, agent="glm", role="user", content="q2")

    recent = store.recent(group_id=1, user_id=10, agent="glm", max_turns=8)
    assert recent == [
        ChatMessage(role="user", content="q1"),
        ChatMessage(role="assistant", content="a1"),
        ChatMessage(role="user", content="q2"),
    ]


def test_recent_respects_turn_limit(tmp_path):
    store = make_store(tmp_path)
    for i in range(5):
        store.append(group_id=1, user_id=10, agent="glm", role="user", content=f"q{i}")
        store.append(group_id=1, user_id=10, agent="glm", role="assistant", content=f"a{i}")

    recent = store.recent(group_id=1, user_id=10, agent="glm", max_turns=2)
    assert [m.content for m in recent] == ["q3", "a3", "q4", "a4"]


def test_keys_are_isolated(tmp_path):
    store = make_store(tmp_path)
    store.append(group_id=1, user_id=10, agent="glm", role="user", content="mine")
    # 不同 agent / 用户 / 群互不可见
    assert store.recent(group_id=1, user_id=10, agent="deepseek", max_turns=5) == []
    assert store.recent(group_id=1, user_id=11, agent="glm", max_turns=5) == []
    assert store.recent(group_id=2, user_id=10, agent="glm", max_turns=5) == []


def test_trim_removes_old_rows(tmp_path):
    store = make_store(tmp_path)
    for i in range(5):
        store.append(group_id=1, user_id=10, agent="glm", role="user", content=f"q{i}")
        store.append(group_id=1, user_id=10, agent="glm", role="assistant", content=f"a{i}")

    removed = store.trim(group_id=1, user_id=10, agent="glm", max_turns=2)
    assert removed == 6  # 保留 4 条，删除 6 条
    recent = store.recent(group_id=1, user_id=10, agent="glm", max_turns=10)
    assert [m.content for m in recent] == ["q3", "a3", "q4", "a4"]


def test_clear_removes_only_target_key(tmp_path):
    store = make_store(tmp_path)
    store.append(group_id=1, user_id=10, agent="glm", role="user", content="a")
    store.append(group_id=1, user_id=10, agent="deepseek", role="user", content="b")

    removed = store.clear(group_id=1, user_id=10, agent="glm")
    assert removed == 1
    assert store.recent(group_id=1, user_id=10, agent="glm", max_turns=5) == []
    assert len(store.recent(group_id=1, user_id=10, agent="deepseek", max_turns=5)) == 1


def test_invalid_role_rejected(tmp_path):
    import pytest

    store = make_store(tmp_path)
    with pytest.raises(ValueError):
        store.append(group_id=1, user_id=10, agent="glm", role="system", content="x")


def test_in_memory_store(tmp_path):
    store = ContextStore(":memory:")
    store.append(group_id=1, user_id=1, agent="glm", role="user", content="x")
    assert len(store.recent(group_id=1, user_id=1, agent="glm", max_turns=1)) == 1
    store.close()
