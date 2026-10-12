"""沙箱内真实 pytest 可运行，且 /dev/null 写例外不放开其他边界。

真实验收实测（evidence/真实pytest阻塞.json）：项目 venv 的 pytest 在 sandbox-exec 内
启动时，logging 插件会 `open("/dev/null", "w")`；旧的全局 `deny file-write*` 让它在
configure 阶段 PermissionError 崩溃（INTERNALERROR，退出码 3）。

本用例验证修复既有效又最小：
- pytest 真能在沙箱内跑完并返回 0（**不**关闭 sandbox、**不**关闭 pytest 日志插件）；
- 相邻目录、目录外目录仍不可写，相邻/目录外的敏感文件仍不可读。

平台隔离不可用时按 `requires_platform_sandbox()` 明确 skip，绝不退回无隔离执行。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from app.security.exec_policy import ExecPolicy, run_command
from helpers import requires_platform_sandbox


def real(path: Path) -> Path:
    """断言与规则统一用真实路径（macOS 上 /tmp 是 /private/tmp 的符号链接）。"""
    return Path(os.path.realpath(str(path)))


def _probe_code(workspace: Path, sibling: Path, outside: Path) -> str:
    return (
        "from pathlib import Path\n"
        "def attempt(label, path):\n"
        "    try:\n"
        "        Path(path).write_text('LEAK')\n"
        "        print('WROTE', label)\n"
        "    except Exception as exc:\n"
        "        print('DENIED', label, type(exc).__name__)\n"
        f"attempt('workspace', {str(workspace / 'written.txt')!r})\n"
        f"attempt('sibling', {str(sibling / 'written.txt')!r})\n"
        f"attempt('outside', {str(outside / 'written.txt')!r})\n"
        "attempt('devnull', '/dev/null')\n"
        f"for label, path in (('sibling_env', {str(sibling / '.env')!r}), "
        f"('outside_pem', {str(outside / 'secret.pem')!r})):\n"
        "    try:\n"
        "        print('READ', label, Path(path).read_text())\n"
        "    except Exception as exc:\n"
        "        print('READ_DENIED', label, type(exc).__name__)\n"
    )


def test_profile_allows_only_devnull_as_extra_write(tmp_path) -> None:
    """纯 profile 断言（无需 sandbox-exec）：目录外写只多了 /dev/null 字面量。"""
    from app.security.sandbox import prepare_plan

    workspace = real(tmp_path / "workspace")
    workspace.mkdir()
    temp_dir = real(tmp_path / "task-temp")
    temp_dir.mkdir()
    plan = prepare_plan(
        workspace=workspace, temp_dir=temp_dir, protected=(real(tmp_path / "protected"),))
    text = plan.profile()
    assert '(allow file-write* (literal "/dev/null"))' in text
    # 全局写拒绝仍然存在，且 /dev/null 例外排在它之后（SBPL 后匹配者优先）
    assert text.index("(deny file-write*)") < text.index('(literal "/dev/null")')
    assert '(allow file-write* (subpath "/dev"))' not in text
    assert "(deny network*)" in text
    # workspace + 任务临时目录 + /dev/null：没有第四处写放行
    assert text.count("(allow file-write*") == 3


@requires_platform_sandbox()
async def test_sandboxed_pytest_runs_with_project_interpreter(tmp_path) -> None:
    """项目解释器 + `-m pytest` 必须在沙箱内真实跑完（默认插件全开）。"""
    root = real(tmp_path / "workspace")
    root.mkdir()
    (root / "test_scaffold_probe.py").write_text(
        "from pathlib import Path\n\n\n"
        "def test_probe():\n"
        "    assert 2 + 2 == 4\n"
        "    Path('marker.txt').write_text('ok', encoding='utf-8')\n",
        encoding="utf-8",
    )
    result = await run_command(
        [sys.executable, "-m", "pytest", "-q", "test_scaffold_probe.py"],
        workspace=root, cwd=root, policy=ExecPolicy(), timeout=120.0,
    )
    assert result.exit_code == 0, result.summary()
    assert "1 passed" in result.stdout, result.summary()
    assert (root / "marker.txt").read_text(encoding="utf-8") == "ok"
    # .pytest_cache 证明 pytest 会话真的跑完（logging 插件初始化成功、未被禁用）
    assert (root / ".pytest_cache").is_dir(), result.summary()


@requires_platform_sandbox()
async def test_sandbox_write_allowance_is_devnull_only(tmp_path) -> None:
    """唯一放行的目录外写必须是 /dev/null；相邻/目录外写入与敏感读仍被拒绝。"""
    workspace = real(tmp_path / "workspace")
    workspace.mkdir()
    sibling = real(tmp_path / "sibling")
    sibling.mkdir()
    outside = real(tmp_path / "outside")
    outside.mkdir()
    (sibling / ".env").write_text("SYNTHETIC_SIBLING_ENV", encoding="utf-8")
    (outside / "secret.pem").write_text("SYNTHETIC_PEM", encoding="utf-8")

    result = await run_command(
        [sys.executable, "-c", _probe_code(workspace, sibling, outside)],
        workspace=workspace, cwd=workspace, policy=ExecPolicy(), timeout=60.0,
    )
    assert result.exit_code == 0, result.summary()
    out = result.stdout
    assert "WROTE workspace" in out, result.summary()
    assert "WROTE devnull" in out, result.summary()
    assert "WROTE sibling" not in out and "WROTE outside" not in out, result.summary()
    assert "DENIED sibling" in out and "DENIED outside" in out, result.summary()
    assert "READ_DENIED sibling_env" in out, result.summary()
    assert "READ_DENIED outside_pem" in out, result.summary()
    assert "SYNTHETIC" not in out, result.summary()
    assert (workspace / "written.txt").read_text(encoding="utf-8") == "LEAK"
    assert not (sibling / "written.txt").exists()
    assert not (outside / "written.txt").exists()
