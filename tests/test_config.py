"""配置加载测试：环境变量列表解析、YAML 加载与优先级。"""

from __future__ import annotations

import os

import pytest

from app.config import Settings, apply_yaml_config, load_settings


@pytest.fixture
def restore_env():
    """快照并恢复 os.environ（apply_yaml_config 会直接 setdefault 环境变量）。"""
    snapshot = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(snapshot)


def test_settings_parses_env_id_lists(monkeypatch):
    monkeypatch.setenv("QQ_ALLOWED_USER_IDS", "111, 222")
    monkeypatch.setenv("QQ_ALLOWED_GROUP_IDS", "333")
    monkeypatch.setenv("QQ_IGNORE_USER_IDS", "444，555")
    settings = Settings(_env_file=None)
    assert settings.qq_allowed_user_ids == [111, 222]
    assert settings.qq_allowed_group_ids == [333]
    assert settings.qq_ignore_user_ids == [444, 555]


def test_settings_defaults_deny_all(restore_env):
    for name in ("QQ_ALLOWED_USER_IDS", "QQ_ALLOWED_GROUP_IDS", "QQ_IGNORE_USER_IDS"):
        os.environ.pop(name, None)
    settings = Settings(_env_file=None)
    assert settings.qq_allowed_user_ids == []
    assert settings.qq_allowed_group_ids == []
    assert settings.request_timeout_seconds == 60.0
    assert settings.max_context_turns == 8


def test_yaml_config_applies_and_env_wins(tmp_path, restore_env, monkeypatch):
    config = """
qq:
  allowed_user_ids: [1, 2]
  allowed_group_ids: [300]
storage:
  database_path: /tmp/yaml-test.db
agents:
  glm:
    model: glm-4-plus
    enabled: false
transport:
  kind: onebot_http
limits:
  max_context_turns: 3
  rate_limit_per_minute: 12
"""
    path = tmp_path / "config.yaml"
    path.write_text(config, encoding="utf-8")

    monkeypatch.delenv("QQ_ALLOWED_USER_IDS", raising=False)
    monkeypatch.delenv("MAX_CONTEXT_TURNS", raising=False)
    monkeypatch.delenv("RATE_LIMIT_PER_MINUTE", raising=False)
    monkeypatch.setenv("CONFIG_FILE", str(path))
    # 真实环境变量优先级高于 YAML
    monkeypatch.setenv("MAX_CONTEXT_TURNS", "5")
    # 隔离真实 .env：本用例验证的是 YAML 注入本身
    monkeypatch.setenv("ENV_FILE", str(tmp_path / "no-such.env"))

    settings = load_settings()
    assert settings.qq_allowed_user_ids == [1, 2]
    assert settings.qq_allowed_group_ids == [300]
    assert settings.database_path == "/tmp/yaml-test.db"
    assert settings.glm_model == "glm-4-plus"
    assert settings.glm_enabled is False
    assert settings.transport == "onebot_http"
    assert settings.max_context_turns == 5  # 环境变量覆盖 YAML
    assert settings.rate_limit_per_minute == 12


def test_env_file_wins_over_yaml(tmp_path, restore_env, monkeypatch):
    """文档承诺的优先级：真实环境变量 > .env > YAML（YAML 不覆盖 .env 已有的键）。"""
    env_file = tmp_path / ".env"
    env_file.write_text("QQ_ALLOWED_USER_IDS=7,8\n", encoding="utf-8")
    config = """
qq:
  allowed_user_ids: [1, 2]
"""
    path = tmp_path / "config.yaml"
    path.write_text(config, encoding="utf-8")
    monkeypatch.delenv("QQ_ALLOWED_USER_IDS", raising=False)
    monkeypatch.setenv("CONFIG_FILE", str(path))
    monkeypatch.setenv("ENV_FILE", str(env_file))

    settings = load_settings()
    assert settings.qq_allowed_user_ids == [7, 8]  # .env 优先于 YAML


def test_read_env_file_does_not_mutate_process_env(tmp_path, monkeypatch):
    """读取 .env 不应污染进程环境（否则会盖住测试与真实部署的隔离）。"""
    from app.config import read_env_file

    env_file = tmp_path / ".env"
    env_file.write_text("QQ_ONLY_FOR_TEST=1\n", encoding="utf-8")
    monkeypatch.delenv("QQ_ONLY_FOR_TEST", raising=False)
    values = read_env_file(env_file)
    assert values["QQ_ONLY_FOR_TEST"] == "1"
    assert "QQ_ONLY_FOR_TEST" not in os.environ


def test_apply_yaml_setdefault_does_not_override(tmp_path, restore_env):
    """setdefault 语义：已存在的环境变量不被 YAML 覆盖。"""
    os.environ.pop("QQ_IGNORE_USER_IDS", None)
    os.environ["QQ_IGNORE_USER_IDS"] = "42"
    path = tmp_path / "c.yaml"
    path.write_text("qq:\n  ignore_user_ids: [7]\n", encoding="utf-8")
    apply_yaml_config(path)
    assert os.environ["QQ_IGNORE_USER_IDS"] == "42"
    os.environ.pop("QQ_IGNORE_USER_IDS", None)
    apply_yaml_config(path)
    assert os.environ["QQ_IGNORE_USER_IDS"] == "7"


def test_apply_yaml_ignores_missing_sections(tmp_path, restore_env):
    os.environ.pop("QQ_ALLOWED_USER_IDS", None)
    path = tmp_path / "partial.yaml"
    path.write_text("qq:\n  allowed_user_ids: [7]\n", encoding="utf-8")
    apply_yaml_config(path)
    assert os.environ.get("QQ_ALLOWED_USER_IDS") == "7"


def test_codex_directory_roots_yaml_json(tmp_path, restore_env):
    os.environ.pop('CODEX_ALLOWED_ROOTS', None)
    path=tmp_path/'roots.yaml'
    path.write_text('agents:\n  codex:\n    allowed_roots: ["/tmp/project with spaces", "/tmp/other"]\n')
    apply_yaml_config(path)
    settings=Settings(_env_file=None)
    assert settings.codex_allowed_roots==['/tmp/project with spaces','/tmp/other']
