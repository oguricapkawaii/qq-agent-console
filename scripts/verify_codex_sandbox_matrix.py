#!/usr/bin/env python
"""V03 真实权限边界矩阵（本机证明，**不调用模型**，直接执行命令）。

为什么需要它：模型说"我没有执行"**不等于**硬拒绝。这里用真实 CLI 的
`codex sandbox -P <profile>` 在**与生产 argv 完全一致的权限 profile** 下直接执行命令，
逐条记录 exit code、stdout/stderr 与目标文件的实际状态。

矩阵规格由 `app.agents.codex.sandbox_matrix_spec()` 提供（与真实适配器同源），
证据里的每条 `argv` 都可以直接复制成 shell 命令重跑，可复算规则：
`exit_code == 0` 当且仅当 `expect_allowed == true`。

覆盖维度（正负配对）：
- 合法读取：工作区内哨兵文件（必须成功）；
- 越界读取：工作区外文件、`..` 相对越界、敏感文件（.env/凭据）、不存在的路径（对照）；
- 写入：工作区内写、工作区外写、/tmp 写（必须被拒且目标未被改写）；
- 网络：curl / nc（必须被拒，且 macOS 侧报 Operation not permitted）；
- 对照矩阵：同一批命令在旧的**可写** profile（qq_files）下运行，证明"旧配置确实能改工作区"，
  因此"改用只读 profile"不是形式主义。

用法：
    .venv/bin/python scripts/verify_codex_sandbox_matrix.py
    .venv/bin/python scripts/verify_codex_sandbox_matrix.py --json-out <路径>

退出码：0 = 只读矩阵与对照矩阵都符合预期；1 = 有偏差；2 = 环境问题。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agents.codex import (  # noqa: E402
    READ_ONLY_PROFILE,
    permission_config,
    read_only_permission_config,
    run_sandbox_matrix,
    sanitized_cli_env,
)

DEFAULT_OUT = ROOT / "outputs/嵌套对照schema收尾/verify_codex_sandbox_matrix.py.json"

# 旧 profile 名为 qq_files（工作区可写）；新 profile 名为 qq_ro（只读限根）。
LEGACY_PROFILE = "qq_files"


def _codex_binary() -> str:
    from app.config import load_settings

    settings = load_settings()
    backend = (settings.console_backends or {}).get("codex") or {}
    return str(backend.get("binary") or settings.codex_binary
               or os.environ.get("CODEX_BINARY") or "codex")


def _legacy_control(binary: str, *, workspace: Path) -> dict:
    """对照：可写 profile 下写工作区**必须成功**。

    若新旧 profile 表现一致，说明 profile 根本没生效（正是 V03 的历史根因）。
    """
    write_target = workspace / "write_target.txt"
    write_target.write_text("ORIGINAL", encoding="utf-8")
    argv = ["/bin/sh", "-c", f"echo HACKED > {write_target}"]
    command = [binary, "sandbox", "-P", LEGACY_PROFILE, "-C", str(workspace),
               "-c", permission_config(), "--", *argv]
    # 与主矩阵使用同一套净化环境：继承环境里的非法 UTF-8 会让 CLI panic(101)，
    # 那属于复跑机制的环境问题，不能被误读成"沙箱放宽/收紧"（S63）。
    env = sanitized_cli_env()
    completed = subprocess.run(command, capture_output=True, text=True, timeout=30,
                               check=False, env=env)
    return {
        "case": "legacy_profile_write_inside_workspace",
        "env_keys": sorted(env),
        "argv": argv,
        "shell": " ".join(argv),
        "exit_code": completed.returncode,
        "stdout": (completed.stdout or "")[:500],
        "stderr": (completed.stderr or "")[:500],
        "write_target_after": write_target.read_text(encoding="utf-8").strip(),
        "expect_allowed": True,
        "note": "旧 profile（qq_files）工作区可写：写成功说明它不能当作只读隔离",
    }


def normalize_out(value, default):
    """把 `--json-out` 标准化：相对路径按项目根解析，绝对路径原样使用。"""
    if not value:
        return default
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    return candidate.resolve()


def main(*, out: Path | None = None) -> int:
    binary = _codex_binary()
    if not shutil.which(binary) and not Path(binary).is_file():
        print(f"找不到 codex CLI：{binary}", file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory(prefix="qq-sandbox-matrix-") as raw:
        base = Path(raw).resolve()
        workspace = base / "workspace"
        workspace.mkdir()
        read_only = run_sandbox_matrix(binary, root=base, workspace=workspace)
        legacy = _legacy_control(binary, workspace=workspace)

    legacy_wrote = legacy["write_target_after"] == "HACKED"
    payload = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "model_calls": False,
        "qq_messages_sent": False,
        "production_changed": False,
        "cli": binary,
        "scope": "本机证明：仅对本机该版本 CLI 与这两份权限 profile 成立",
        "profiles": {
            LEGACY_PROFILE: {"config": permission_config(), "writable": True},
            READ_ONLY_PROFILE: {"config": read_only_permission_config(), "writable": False},
        },
        "read_only_matrix": read_only,
        "legacy_control": legacy,
        "network_observations": list(read_only.get("verdict_notes") or []),
        "verdict": {
            "passed": bool(read_only.get("passed")) and legacy_wrote,
            "read_only_profile_enforced": bool(read_only.get("passed")),
            "read_only_problems": list(read_only.get("problems") or []),
            "legacy_profile_can_write_workspace": legacy_wrote,
            "note": (
                "只读矩阵必须全部符合预期（允许的成功、拒绝的非零退出、无副作用）；"
                "对照矩阵必须证明旧的可写 profile 确实能改工作区，"
                "否则无法说明新 profile 的拒绝来自 profile 本身。"
            ),
        },
    }
    target = normalize_out(out, DEFAULT_OUT) if not isinstance(out, Path) else out
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload["verdict"], ensure_ascii=False, indent=2))
    print(f"证据：{target}")
    if not payload["verdict"]["passed"]:
        print("矩阵未通过：\n"
              + "\n".join(f"· {item}" for item in payload["verdict"]["read_only_problems"]),
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--json-out", default="", help="证据输出路径")
    args = parser.parse_args()
    raise SystemExit(main(out=Path(args.json_out) if args.json_out else None))
