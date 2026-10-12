"""收尾补丁：exec 指令只用解释器命令名，PATH 覆盖在所有任务后恢复。

背景：Codex 模型拒绝参数里含受保护目录（`/Users/.../Documents/...`）的绝对解释器路径
（见 `scaffold_exec_instruction` 注释与 evidence/WebSocket握手失败.json 的 codex exec 步骤）。
因此真实 DAG 的 exec 指令只给 `python3`/`python` 命令名，解释器目录由
`interpreter_on_path()` 在本验证进程内前置到 PATH；这些用例验证：

- 指令里绝不出现 `sys.executable` 及其目录；
- PATH 前置后 `which(interpreter_command())` 确实指向项目解释器；
- `async with` 在正常返回、异常与 PATH 未设置三种情况下都恢复原值；
- `run_verification` 把整个任务执行包在该上下文内，阶段异常也恢复 PATH。
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_script():
    spec = importlib.util.spec_from_file_location(
        "verify_final_delivery_interpreter", ROOT / "scripts/verify_final_delivery.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _interpreter_dir() -> str:
    return str(Path(sys.executable).parent)


def test_scaffold_instruction_uses_command_name_not_absolute_path() -> None:
    module = load_script()
    command = module.interpreter_command()
    instruction = module.scaffold_exec_instruction()
    assert command in module.INTERPRETER_COMMAND_NAMES
    assert f"{command} -m pytest -q {module.SCAFFOLD_TEST_NAME}" in instruction
    # 受保护目录的绝对解释器路径绝不出现在给模型的指令里
    assert sys.executable not in instruction
    assert _interpreter_dir() not in instruction
    assert (Path(_interpreter_dir()) / command).is_file()


async def test_interpreter_on_path_prepends_and_resolves_project_interpreter() -> None:
    module = load_script()
    original = os.environ.get("PATH")
    async with module.interpreter_on_path():
        assert os.environ["PATH"].split(os.pathsep)[0] == _interpreter_dir()
        resolved = shutil.which(module.interpreter_command())
        assert resolved is not None
        assert Path(resolved).parent == Path(_interpreter_dir())
    assert os.environ.get("PATH") == original


async def test_interpreter_on_path_restores_after_exception() -> None:
    module = load_script()
    original = os.environ.get("PATH")

    class Boom(RuntimeError):
        pass

    with pytest.raises(Boom):
        async with module.interpreter_on_path():
            assert os.environ["PATH"].split(os.pathsep)[0] == _interpreter_dir()
            raise Boom("任务执行中途失败")
    assert os.environ.get("PATH") == original


async def test_interpreter_on_path_restores_unset_path(monkeypatch) -> None:
    module = load_script()
    monkeypatch.delenv("PATH", raising=False)
    async with module.interpreter_on_path():
        assert os.environ["PATH"].split(os.pathsep)[0] == _interpreter_dir()
    assert "PATH" not in os.environ


async def test_interpreter_on_path_moves_existing_later_entry_to_front(monkeypatch) -> None:
    module = load_script()
    original = os.pathsep.join(["/usr/bin", _interpreter_dir(), "/bin"])
    monkeypatch.setenv("PATH", original)
    async with module.interpreter_on_path():
        assert os.environ["PATH"].split(os.pathsep)[0] == _interpreter_dir()
        assert Path(shutil.which(module.interpreter_command())).parent == Path(_interpreter_dir())
    assert os.environ["PATH"] == original


async def test_run_verification_restores_path_when_phase_raises(monkeypatch, tmp_path) -> None:
    module = load_script()
    original = os.environ.get("PATH")
    seen: dict[str, str] = {}

    class FakeRecorder:
        def record(self, check_id, *args, **kwargs):
            seen["check_id"] = check_id

    class FakeVerifier:
        deps_ok = True
        force_real = False

        def __init__(self, **kwargs):
            self.rec = FakeRecorder()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def phase_dependencies(self):
            return None

        def phase_runtime(self):
            seen["inside"] = os.environ.get("PATH", "")
            raise RuntimeError("phase boom")

        def finalize_budget(self):
            return None

        def report(self, *, mode):
            return {"counts": {module.PASS: 0, module.FAIL: 1, module.BLOCKED: 0,
                               module.SKIP: 0, module.NA: 0, module.NOT_RUN: 0},
                    "checks": []}

        def write_report(self, document, *, real):
            return None

        def print_summary(self, document, evidence_path):
            return None

    monkeypatch.setattr(module, "FinalDeliveryVerifier", FakeVerifier)
    code = await module.run_verification(run_pytest=False, json_out=tmp_path / "out.json")
    assert code == 1
    assert seen["inside"].split(os.pathsep)[0] == _interpreter_dir()
    assert seen["check_id"] == "harness.fatal"
    assert os.environ.get("PATH") == original
