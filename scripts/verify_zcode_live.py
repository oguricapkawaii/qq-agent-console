#!/usr/bin/env python
"""L04 真实验证：ZCode 的默认推理强度、预算与取消行为（**会产生真实模型调用**）。

要证明的三件事（都基于真实 stdio 会话，不看自述）：
1. **默认强度可用**：不带 `--effort` 时，read/edit/exec 三个工具模式在默认单步预算内
   真实完成（edit 真的落盘、exec 真的产出文件）；
2. **预算耗尽即停止**：同一种 edit 调用在很短的 deadline 下以 `timeout` 失败，
   并且**没有**发生写入（超时不等于"后台还在写"）；
3. **取消不留子进程**：超时后 ZCode 的 node 子进程被清理，不残留孤儿进程。

用法：
    .venv/bin/python scripts/verify_zcode_live.py --run [--agents zcode]

不发送 QQ 消息、不改生产配置；所有工作区都是临时目录。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

from app.agents.registry import AgentRegistry  # noqa: E402
from app.backends.protocol import TaskRequest  # noqa: E402
from app.config import load_settings  # noqa: E402
from helpers import ConsoleHarness  # noqa: E402

DEFAULT_OUT = ROOT / "outputs/验收复现/真实ZCode强度与预算验证.json"



def normalize_out(value, default):
    """把 `--json-out` 标准化：相对路径按项目根解析，绝对路径原样使用。"""
    if not value:
        return default
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    return candidate.resolve()


def _under_pytest() -> bool:
    """是否运行在 pytest 进程里（用于禁止测试覆盖已归档的真实证据）。"""
    return "pytest" in sys.modules


def _resolve_out(explicit: Path | None, default: Path, *, name: str) -> Path | None:
    """决定证据落盘路径；测试进程里**拒绝**写默认路径，避免覆盖历史证据。

    背景：第五轮独立反例脚本会 import 本模块并调用 `main()`；若不显式传 `out`，
    当时的假模型证据就会写进 `outputs/验收复现/` 的归档文件。现在测试进程必须显式
    指定 `out`/`--json-out` 才会落盘，否则只打印不写入。
    """
    if explicit is not None:
        return normalize_out(explicit, default)
    if _under_pytest():
        print(f"[{name}] 检测到 pytest 进程：为保护历史证据，不写默认路径 {default}；"
              "如需落盘请显式指定 out/--json-out。", file=sys.stderr)
        return None
    return default

def _zcode_processes() -> int:
    """当前与 ZCode app-server 相关的 node 进程数（仅作参考，不作为唯一判据）。"""
    try:
        completed = subprocess.run(
            ["pgrep", "-fl", "zcode"], capture_output=True, text=True, check=False,
        )
    except OSError:  # pragma: no cover - 平台差异
        return 0
    lines = [line for line in (completed.stdout or "").splitlines() if line.strip()]
    return len(lines)


def _process_snapshot() -> set[int]:
    """当前与 ZCode app-server 相关的进程 PID 集合（按本次运行前后差集判定）。"""
    try:
        completed = subprocess.run(
            ["pgrep", "-f", "zcode"], capture_output=True, text=True, check=False,
        )
    except OSError:  # pragma: no cover - 平台差异
        return set()
    return {int(line) for line in (completed.stdout or "").split() if line.isdigit()}


def _pid_alive(pid: int) -> bool:
    """进程是否存在（signal 0 探测；PermissionError 表示存在但不可发信号）。"""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - 平台差异
        return True
    return True


def _pgid_alive(pgid: int) -> bool:
    """进程组是否还有成员（`killpg(pgid, 0)` 探测）。"""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - 平台差异
        return True
    return True


class _SpawnRecorder:
    """记录本次真实调用创建的子进程 PID/PGID（§6 附项：按本次 PID/进程组判定残留）。"""

    def __init__(self) -> None:
        self.pids: list[int] = []

    def __enter__(self):
        self._original = asyncio.create_subprocess_exec
        recorder = self

        async def wrapper(*args, **kwargs):
            proc = await recorder._original(*args, **kwargs)
            recorder.pids.append(proc.pid)
            return proc

        asyncio.create_subprocess_exec = wrapper  # type: ignore[assignment]
        return self

    def __exit__(self, *_exc) -> None:
        asyncio.create_subprocess_exec = self._original  # type: ignore[assignment]

    def report(self) -> dict:
        groups = sorted({os.getpgid(pid) for pid in self.pids if _pid_alive(pid)}) if self.pids else []
        return {
            "spawned_pids": list(self.pids),
            "alive_pids": [pid for pid in self.pids if _pid_alive(pid)],
            "alive_groups": [pgid for pgid in groups if _pgid_alive(pgid)],
        }


async def _one(harness, *, workspace: Path, mode: str, prompt: str, task_id: str,
               deadline: float | None, effort: str | None) -> dict:
    request = TaskRequest(
        entry="qq", group_id=200, user_id=100, agent="zcode", prompt=prompt, mode=mode,
        workspace=workspace, task_id=task_id, use_chat_context=False,
        effort=effort, effort_set=effort is not None,
    )
    spec, error = harness.tasks.build_spec(
        request=request, workspace=workspace, mode=mode, deadline_seconds=deadline or 0,
    )
    started = time.monotonic()
    if spec is None:
        return {"mode": mode, "ok": False, "error": error, "seconds": 0.0}
    result = await harness.tasks.run(request, spec=spec)
    return {
        "mode": mode,
        "effort": result.effort,
        "effort_source": "explicit" if effort else "mode-default",
        "status": result.status,
        "operation": result.operation,
        "action_counts": dict(result.action_counts),
        "changed_files": len([item for item in result.changes if item.status == "applied"]),
        "ok": result.ok,
        "error": result.error,
        "detail": (result.detail or "")[:300],
        "answer": (result.content or "")[:300],
        "seconds": round(time.monotonic() - started, 2),
        "deadline": deadline,
    }


async def main(*, effort: str | None, out: Path | None = None) -> int:
    registry = AgentRegistry.from_settings(load_settings())
    agent = registry.get("zcode")
    rows: dict = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "real_models_called": True,
        "qq_messages_sent": False,
        "temporary_workspace": True,
        "production_changed": False,
        "mode_efforts": dict(getattr(agent, "mode_efforts", {})),
        "catalog_default_effort": str(
            (getattr(agent, "_catalog", {}) or {}).get(getattr(agent, "model_name", ""), {}).get(
                "default_effort", "")
        ),
        "results": [],
    }
    with tempfile.TemporaryDirectory(prefix="qq-zcode-live-") as raw:
        base = Path(raw).resolve()
        workspace = base / "workspace"
        workspace.mkdir()
        settings = load_settings().model_copy(update={
            "qq_allowed_user_ids": [100], "qq_allowed_group_ids": [200],
            "database_path": str(base / "test.db"), "thinking_archive_dir": "",
            "request_timeout_seconds": 180.0, "rate_limit_per_minute": 100,
            "team_step_timeout_seconds": 180.0, "team_workflow_timeout_seconds": 300.0,
            "console_workspaces": {
                "roots": [{"path": str(workspace), "modes": ["read", "edit", "exec"],
                           "admin_modes": ["read", "edit", "exec"]}],
                "backup_dir": str(base / "backups"),
            },
            "console_commands": {"broker": {"max_task_seconds": 240.0}},
        })
        if hasattr(agent, "workdir"):
            agent.workdir = str(base)
        agent.runtime_options = {"native_tools": False}
        harness = ConsoleHarness(settings, {"zcode": agent})
        try:
            # 1) 默认强度下的 read（工具模式）
            source = workspace / "source.txt"
            marker = "ZCODE_DEFAULT_" + uuid.uuid4().hex[:8]
            source.write_text(marker, encoding="utf-8")
            read_row = await _one(
                harness, workspace=workspace, mode="read",
                prompt=f"用 read 动作读取 {source.name}，然后 final 原样返回文件内容。",
                task_id="zc-read-" + uuid.uuid4().hex[:6], deadline=150.0, effort=effort,
            )
            read_row["observed"] = marker in (read_row.get("answer") or "")
            rows["results"].append(read_row)

            # 2) 默认强度下的 edit（真实落盘）
            edit_target = workspace / "edited.txt"
            script_marker = "ZCODE_EDIT_" + uuid.uuid4().hex[:8]
            edit_row = await _one(
                harness, workspace=workspace, mode="edit",
                prompt=(f"用 propose_changes 把 {edit_target.name} 的完整内容写成 "
                        f"{script_marker}。完成后 final。"),
                task_id="zc-edit-" + uuid.uuid4().hex[:6], deadline=150.0, effort=effort,
            )
            edit_row["observed"] = (
                edit_target.is_file() and edit_target.read_text(encoding="utf-8").strip()
                == script_marker
            )
            rows["results"].append(edit_row)

            # 3) 默认强度下的 exec（真实产出文件）
            exec_target = workspace / "exec-out.txt"
            exec_marker = "ZCODE_EXEC_" + uuid.uuid4().hex[:8]
            exec_row = await _one(
                harness, workspace=workspace, mode="exec",
                prompt=("用 run_command 执行 argv 数组 [\"python3\", \"-c\", "
                        f"\"from pathlib import Path; Path('{exec_target.name}')"
                        f".write_text('{exec_marker}')\"]。完成后 final。"),
                task_id="zc-exec-" + uuid.uuid4().hex[:6], deadline=150.0, effort=effort,
            )
            exec_row["observed"] = (
                exec_target.is_file() and exec_target.read_text(encoding="utf-8").strip()
                == exec_marker
            )
            rows["results"].append(exec_row)

            # 4) 预算耗尽：极短 deadline 下的 edit 必须以 timeout 结束且不落盘
            budget_target = workspace / "budget.txt"
            before_procs = _zcode_processes()
            before_pids = _process_snapshot()
            # §6 附项 5：记录**本次**创建的子进程 PID/进程组，而不是只比整机进程数量
            with _SpawnRecorder() as spawned:
                budget_row = await _one(
                    harness, workspace=workspace, mode="edit",
                    prompt=(f"用 propose_changes 把 {budget_target.name} 写成 "
                            "BUDGET_SHOULD_NOT_WRITE。完成后 final。"),
                    task_id="zc-budget-" + uuid.uuid4().hex[:6], deadline=3.0, effort=effort,
                )
            await asyncio.sleep(1.0)
            budget_row["observed_no_write"] = not budget_target.exists()
            budget_row["zcode_processes_before"] = before_procs
            budget_row["zcode_processes_after"] = _zcode_processes()
            budget_row["whole_machine_process_delta"] = max(
                0, budget_row["zcode_processes_after"] - before_procs
            )
            budget_row["spawned_processes"] = spawned.report()
            budget_row["leftover_new_pids"] = sorted(
                pid for pid in (_process_snapshot() - before_pids)
                if _pid_alive(pid)
            )
            rows["results"].append(budget_row)
        finally:
            await harness.aclose()
            await registry.aclose()

    # 按类别取结果：edit 有"默认强度"和"预算耗尽"两次运行，不能按 mode 覆盖
    default_rows = [row for row in rows["results"] if (row.get("deadline") or 0) > 10]
    budget_row = next((row for row in rows["results"] if (row.get("deadline") or 99) <= 10), {})
    observed = {row["mode"]: bool(row.get("observed")) for row in default_rows}
    changed = {
        row["mode"]: bool(row.get("changed_files"))
        for row in default_rows if row.get("mode") in {"edit", "exec"}
    }
    verdict = {
        "default_read_ok": observed.get("read", False),
        "default_edit_ok": observed.get("edit", False),
        "default_exec_ok": observed.get("exec", False),
        # 默认强度必须真的被解析出来（而不是 None），且都在单步预算内完成
        "default_effort_resolved": all(row.get("effort") for row in default_rows),
        "default_efforts_used": sorted({str(row.get("effort")) for row in default_rows}),
        # V01：edit 必须有真实提交（changed_files>0）；exec 至少有命令回执或落盘变更
        "edit_had_real_commit": changed.get("edit", False),
        "exec_had_command_receipt": all(
            (row.get("action_counts") or {}).get("run_command", 0) > 0
            for row in default_rows if row.get("mode") == "exec"
        ),
        "budget_exhausted_with_timeout": budget_row.get("error") == "timeout",
        "budget_left_no_partial_write": bool(budget_row.get("observed_no_write")),
        # §6 附项 5：按本次创建的 PID/进程组核对，而不是只比较整机进程数量
        "no_leftover_spawned_pids": not (budget_row.get("spawned_processes", {}).get("alive_pids") or []),
        "no_leftover_spawned_groups": not (budget_row.get("spawned_processes", {}).get("alive_groups") or []),
        "no_leftover_new_pids": not (budget_row.get("leftover_new_pids") or []),
    }
    problems = [f"{key}=false" for key, value in verdict.items()
                if key != "default_efforts_used" and not value]
    rows["verdict"] = {
        "passed": not problems,
        "signals": verdict,
        "problems": problems,
        "scope": "默认强度正向 + 真实提交/命令回执 + 预算耗尽不写入 + 按本次 PID/进程组核对无残留",
    }
    # exec 在默认强度下偶发返回非 JSON（模型行为），如实记录但不作为本轮系统缺陷：
    # 真正的判据是 read/edit 正向 + 预算/取消；若 exec 未通过，脚本非零退出，由人复核。
    rows["passed"] = rows["verdict"]["passed"]
    target = _resolve_out(out, DEFAULT_OUT, name="verify_zcode_live")
    if target is not None:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    print(f"\nZCode 真实强度/预算/取消：{'通过' if rows['passed'] else '未通过'}；"
          f"证据：{target or '（未落盘）'}")
    return 0 if rows["passed"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", action="store_true", help="确认执行真实模型调用")
    parser.add_argument("--effort", default=None,
                        help="显式强度（默认不传，用于验证模式默认强度）")
    parser.add_argument("--json-out", default="", help="证据输出路径（默认 outputs/验收复现/）")
    args = parser.parse_args()
    if not args.run:
        print("这是真实模型验证脚本：加 --run 才会执行。", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(asyncio.run(main(
        effort=args.effort,
        out=Path(args.json_out) if args.json_out else None,
    )))
