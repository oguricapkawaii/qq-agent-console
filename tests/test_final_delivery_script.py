"""统一最终验证脚本的契约测试（不发起任何真实调用）。

覆盖：
- `scripts/verify_final_delivery.py` 可导入、CLI 对真实模式有预算门禁；
- 脚本生成的离线 Settings 能被真实 `AgentRegistry.from_settings` 解析为三后端；
- OneBot 模拟路由能真实接收 JSON POST（注解可被 FastAPI 解析，不退化成 query → 422）；
- 临时工作区位于受保护项目树之外，且符合真实工作区合同（searchable + read 授权）；
- 真实调用预算按每次 adapter 调用硬计数（不做最坏轮次预留）；
- Recorder 的通过/失败/跳过/不适用语义（异常绝不冒充通过）。
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_script():
    spec = importlib.util.spec_from_file_location(
        "verify_final_delivery", ROOT / "scripts/verify_final_delivery.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["verify_final_delivery"] = module
    spec.loader.exec_module(module)
    return module


def test_cli_blocks_real_mode_without_budget() -> None:
    module = load_script()
    assert module.main(["--run-real"]) == 2


def test_a_matrix_covers_all_items() -> None:
    module = load_script()
    assert set(module.A_MATRIX) == {f"A{index:02d}" for index in range(1, 23)}
    for title, files in module.A_MATRIX.values():
        assert title
        for name in files:
            assert (ROOT / name).is_file(), name


def test_offline_settings_registers_three_mock_backends(tmp_path) -> None:
    module = load_script()
    from app.agents.registry import AgentRegistry
    from app.security.sandbox import available

    settings = module.offline_settings(
        tmp_path, sim_url="http://127.0.0.1:1", access_token="token",
        api_tokens=[module.api_token("t", "tok", 100, scopes=["task"])],
        responses={"deepseek": module.read_script(), "zcode": module.edit_script(),
                   "codex": module.exec_script()},
    )
    assert settings.transport == "onebot_http"
    assert str(tmp_path / "bot.db") == settings.database_path
    registry = AgentRegistry.from_settings(settings)
    assert {item.name for item in registry.configured_agents()} >= {"deepseek", "zcode", "codex"}
    codex = registry.backends.for_agent("codex")[0].descriptor
    assert "read" in codex.broker_modes and "edit" in codex.broker_modes
    if available():
        assert "exec" in codex.broker_modes, "有平台隔离时 exec 应由公共策略提供"


async def test_onebot_simulator_accepts_json_post() -> None:
    """真实 POST /send_group_msg 必须 200：Request 注解可在模块全局解析，不能退化成 query。"""
    import httpx

    module = load_script()
    sim = module.OneBotSimulator()
    server = await module.LocalServer(sim.app, name="onebot-sim-test").start()
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{server.port}", trust_env=False, timeout=10.0
        ) as client:
            response = await client.post(
                "/send_group_msg",
                json={"group_id": 200, "message": "FINAL_VERIFY_PING"},
                headers={"Authorization": "Bearer sim"},
            )
        assert response.status_code == 200, response.text
        assert sim.texts() == ["FINAL_VERIFY_PING"]
        assert sim.authorizations == ["Bearer sim"]
    finally:
        await server.stop()


def test_scratch_root_is_outside_the_protected_project_tree() -> None:
    """临时授权根必须在项目树之外：否则 ACL 的 protected 优先，find/select 永远无根。"""
    from app.security.paths import is_protected
    from app.workspaces.configurator import default_protected

    module = load_script()
    root = module.scratch_root()
    try:
        resolved = Path(os.path.realpath(str(root)))
        assert not resolved.is_relative_to(ROOT)
        assert not is_protected(resolved, default_protected())
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_offline_settings_declares_authorized_searchable_root(tmp_path) -> None:
    """离线 Settings 的临时根必须符合真实工作区合同：searchable 且成员有 read。"""
    module = load_script()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings = module.offline_settings(
        tmp_path, sim_url="http://127.0.0.1:1", access_token="t",
        api_tokens=[], responses={}, root=workspace,
    )
    contract = module.FinalDeliveryVerifier.assert_authorized_search_root(
        settings, user_id=100, group_id=200)
    assert contract["searchable_roots"] >= 1
    assert "read" in contract["modes"]


async def test_real_call_gate_counts_each_call_and_hard_stops() -> None:
    """预算按每次真实 adapter 调用扣减；超限调用被硬拒绝且不转发给底层。"""
    module = load_script()

    class _Adapter:
        name = "fake-real"

        def __init__(self) -> None:
            self.calls = 0

        async def chat_with_options(self, messages, *, timeout, options):
            self.calls += 1
            return "ok"

        async def chat(self, messages, *, timeout):
            self.calls += 1
            return "ok"

    gate = module.RealCallGate(2)
    inner = _Adapter()
    wrapped = gate.wrap(inner, unit="unit-test")
    assert await wrapped.chat_with_options([], timeout=1, options=None) == "ok"
    assert await wrapped.chat([], timeout=1) == "ok"
    assert gate.calls == 2 and inner.calls == 2
    with pytest.raises(module.RealBudgetExceeded):
        await wrapped.chat_with_options([], timeout=1, options=None)
    assert gate.exceeded is True and gate.calls == 2 and inner.calls == 2


def test_install_real_adapters_reuses_parent_adapters_with_guardrails(tmp_path) -> None:
    """父进程 adapter 同名接入 + 计数门禁；workdir 指向临时区、native_tools 强制关闭。"""
    module = load_script()

    class _Adapter:
        name = "codex"

        def __init__(self) -> None:
            self.workdir = "/orig"
            self.runtime_options = {"native_tools": True}

        async def chat_with_options(self, messages, *, timeout, options):
            return "ok"

    class _Runtime:
        def __init__(self, adapter):
            self.id = "codex"
            self.agent = "codex"
            self.adapter = adapter

    class _App:
        def __init__(self, runtimes):
            self.state = type("S", (), {"backends": type("R", (), {"backends": runtimes})()})()

    built, injected = _Adapter(), _Adapter()
    verifier = module.FinalDeliveryVerifier(agents={"codex": injected}, budget=3)
    app = _App({"codex": _Runtime(built)})
    swapped = verifier.install_real_adapters(app, tmp_path / "ws")
    assert swapped == ["codex"]
    runtime = app.state.backends.backends["codex"]
    assert isinstance(runtime.adapter, module.RealCallAdapter)
    assert runtime.adapter.inner is injected
    assert injected.workdir == str(tmp_path / "ws")
    assert injected.runtime_options["native_tools"] is False


def test_report_separates_actual_calls_from_budget() -> None:
    """报告区分实际调用数与声明预算，预留恒为 0，默认 fake 模式不读凭据。"""
    module = load_script()
    verifier = module.FinalDeliveryVerifier(budget=5)
    verifier.gate.acquire(unit="http_text", backend="deepseek")
    verifier.gate.acquire(unit="dag.real", backend="codex")
    document = verifier.report(mode="offline")
    real = document["real_calls"]
    assert real["calls"] == 2 and real["used"] == 2
    assert real["declared_budget"] == 5 and real["remaining"] == 3
    assert real["reserved"] == 0 and real["exceeded"] is False
    assert real["credentials_read"] is False
    assert real["units"] == ["dag.real", "http_text"]
    assert verifier.real_used == 2 and len(verifier.real_entries) == 2


def test_recorder_never_turns_exception_into_pass() -> None:
    module = load_script()
    recorder = module.Recorder()
    with recorder.step("ok", "通过项"):
        pass
    with recorder.step("bad", "失败项"):
        raise AssertionError("boom")
    with recorder.step("skip", "跳过项", skip_reason="理由"):
        pass
    with recorder.step("na", "不适用项", applicable=False, na_reason="平台"):
        pass
    statuses = {check.check_id: check.status for check in recorder.checks}
    assert statuses == {"ok": "passed", "bad": "failed", "skip": "skipped", "na": "not_applicable"}
    assert "boom" in recorder.checks[1].detail


def test_scripts_are_json_serializable() -> None:
    module = load_script()
    for script in (module.read_script(), module.edit_script(), module.exec_script(),
                   module.text_script()):
        json.dumps(script)
        for item in script:
            json.dumps(item, ensure_ascii=False)


def test_evidence_path_is_workspace_relative() -> None:
    module = load_script()
    with tempfile.TemporaryDirectory() as temp:
        candidate = module._evidence_path("outputs/验收复现/x.json", Path(temp) / "d.json")
    assert candidate.is_absolute()
    assert candidate.is_relative_to(ROOT)


@pytest.mark.parametrize("name", ["read_script", "edit_script", "exec_script"])
def test_action_names_match_broker_protocol(name: str) -> None:
    module = load_script()
    script = getattr(module, name)()
    action = script[0]["actions"][0]["action"]
    assert action in {"read", "propose_changes", "run_command"}
