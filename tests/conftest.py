"""pytest 共享 fixture。"""

from __future__ import annotations

import pytest

from helpers import ALLOWED_GROUP, ALLOWED_USER, FakeAgent, build_dispatcher, make_settings


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path)


@pytest.fixture
def agents() -> dict[str, FakeAgent]:
    return {
        "glm": FakeAgent("glm"),
        "deepseek": FakeAgent("deepseek"),
        "gpt": FakeAgent("gpt"),
        "codex": FakeAgent("codex", configured=False),
    }


@pytest.fixture
def bot(settings, agents):
    """已装配好 的调度器 + 记录型传输。"""
    dispatcher, transport = build_dispatcher(settings, agents)
    return dispatcher, transport


@pytest.fixture
def allowed_ids():
    return ALLOWED_USER, ALLOWED_GROUP
