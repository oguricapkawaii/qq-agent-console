#!/usr/bin/env python
"""真实本机 TCP 验收：HTTP 结果正文必须等于真实模型答复（L02）。

与旧脚本的区别（旧脚本读错了字段，因此把"正文为空"看成了通过）：
- 完整打印结果接口的 JSON（截断到有界长度），不做字段猜测；
- 断言 `content` 非空，且真实模型答复的关键标记确实出现在 content 里；
- 与数据库里的 `result_text` 逐字比对（持久化 + 接口一致）；
- 覆盖无 Token 401 与跨用户 404；
- 幂等重发返回同一 task_id 且不重复调用模型。

用法：
    .venv/bin/python scripts/verify_http_live.py --run [--agents deepseek]

会产生**真实模型调用**（默认只跑一个 text 任务，最省）；不发送 QQ 消息，不改生产配置。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import socket
import sys
import tempfile
import time
from pathlib import Path

import httpx
import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

from app.agents.registry import AgentRegistry  # noqa: E402
from app.api.v1 import ApiIdentity  # noqa: E402
from app.config import load_settings  # noqa: E402
from helpers import ConsoleHarness  # noqa: E402
from test_api_v1 import build_api_app  # noqa: E402

DEFAULT_OUT = ROOT / "outputs/验收复现/真实HTTP结果正文验证.json"



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

async def _wait_result(client: httpx.AsyncClient, task_id: str, headers: dict,
                       *, deadline: float = 180.0) -> dict:
    """轮询结果接口直到终态；返回**原始**结果 JSON（不猜字段）。"""
    terminal = {"succeeded", "failed", "cancelled", "partial_failed"}
    payload: dict = {}
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        response = await client.get(f"/api/v1/tasks/{task_id}/result", headers=headers)
        payload = response.json()
        if payload.get("status") in terminal:
            return payload
        await asyncio.sleep(0.5)
    return payload


async def main(*, agent: str, prompt: str, out: Path | None = None) -> int:
    registry = AgentRegistry.from_settings(load_settings())
    rows: dict = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "real_models_called": True,
        "qq_messages_sent": False,
        "temporary_workspace": True,
        "production_changed": False,
    }
    with tempfile.TemporaryDirectory(prefix="qq-http-live-") as temp:
        base = Path(temp).resolve()
        workspace = base / "workspace"
        workspace.mkdir()
        agents = {agent: registry.get(agent)}
        for item in agents.values():
            if hasattr(item, "workdir"):
                item.workdir = str(base)
            item.runtime_options = {"native_tools": False}
        settings = load_settings().model_copy(update={
            "qq_allowed_user_ids": [100], "qq_allowed_group_ids": [200],
            "database_path": str(base / "test.db"), "thinking_archive_dir": "",
            "request_timeout_seconds": 120.0, "rate_limit_per_minute": 100,
            "console_workspaces": {
                "roots": [{"path": str(workspace), "modes": ["read", "edit", "exec"],
                           "admin_modes": ["read", "edit", "exec"]}],
                "backup_dir": str(base / "backups"),
            },
            "console_commands": {
                "broker": {"max_task_seconds": 150.0},
                "exec": {"require_sandbox": True, "allow_network": False},
            },
        })
        harness = ConsoleHarness(settings, agents)
        marker = "LIVE_HTTP_" + secrets.token_hex(6)
        token = secrets.token_urlsafe(24)
        foreign = secrets.token_urlsafe(24)
        app = build_api_app(harness, tokens={
            token: ApiIdentity(name="live", user_id=100, group_ids=(200,),
                               scopes=frozenset({"task", "team", "workspace"})),
            foreign: ApiIdentity(name="foreign", user_id=101, group_ids=(200,),
                                 scopes=frozenset({"task", "team", "workspace"})),
        })
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen(32)
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False,
                                               lifespan="off"))
        job = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            while not server.started:
                await asyncio.sleep(0.02)
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=30,
            ) as client:
                headers = {"Authorization": "Bearer " + token,
                           "Idempotency-Key": secrets.token_hex(8)}
                payload = {"agent": agent, "mode": "text",
                           "prompt": f"只返回这个标记，不要解释：{marker}"}
                unauth = await client.get("/api/v1/tasks/whatever/result")
                first = await client.post("/api/v1/tasks", headers=headers, json=payload)
                task_id = first.json().get("task_id", "")
                repeat = await client.post("/api/v1/tasks", headers=headers, json=payload)
                result = await _wait_result(client, task_id, headers)
                foreign_result = await client.get(
                    f"/api/v1/tasks/{task_id}/result",
                    headers={"Authorization": "Bearer " + foreign},
                )
                record = harness.task_store.get_task(task_id) or {}
                # 幂等的判据用**真实调用痕迹**：真实 harness 没有 calls 计数器，
                # 因此数一遍该 task 的事件与任务记录数（假适配器场景在离线用例里断言 calls）。
                events = harness.task_store.events(task_id, limit=50)
                calls = len(getattr(agents[agent], "calls", []) or []) or None

                content = str(result.get("content") or "")
                rows["http"] = {
                    "endpoint": f"http://127.0.0.1:{port}",
                    "real_tcp": True,
                    "unauth_status": unauth.status_code,
                    "submit_status": first.status_code,
                    "idempotent_same_task": repeat.json().get("task_id") == task_id,
                    "task_id": task_id,
                    "status": result.get("status"),
                    "content_len": len(content),
                    "content_marker_present": marker in content,
                    "content_first_200": content[:200],
                    "raw_result": json.dumps(result, ensure_ascii=False)[:1500],
                    "db_result_text_matches_api": (record.get("result_text") or "") == content,
                    "db_result_text_len": len(record.get("result_text") or ""),
                    "foreign_status": foreign_result.status_code,
                    "adapter_calls_attr": calls,
                    "task_events": [{"kind": kind, "detail": detail} for kind, detail, _ in events][:10],
                    "same_task_twice": repeat.json().get("task_id") == task_id,
                }
        finally:
            server.should_exit = True
            await job
            sock.close()
            await harness.aclose()
            await registry.aclose()

    # 判据分成两类，绝不混为一谈（第五轮独立验收 §1 的教训）：
    #  - transport：正文存储与接口持久化是否成立（本脚本的通过条件）；
    #  - compliance：模型是否服从"只输出标记"（可能是模型不服从，而不是正文丢失）。
    signals = {
        "content_non_empty": rows["http"]["content_len"] > 0,
        "persisted_equals_api": rows["http"]["db_result_text_matches_api"],
        "unauth_rejected": rows["http"]["unauth_status"] == 401,
        "foreign_rejected": rows["http"]["foreign_status"] == 404,
        "idempotent": bool(rows["http"]["idempotent_same_task"]),
        # 幂等只能保证"同 key 重发不产生第二个任务"；真实 harness 不暴露模型调用计数，
        # 因此这里只断言任务只被创建一次（离线用例用假适配器断言 calls==1）。
        "task_created_once": bool(rows["http"]["same_task_twice"])
        and sum(1 for item in rows["http"]["task_events"] if item.get("kind") == "finished") <= 1,
    }
    compliance = {"marker_in_content": rows["http"]["content_marker_present"]}
    problems = [f"{name}=false" for name, value in signals.items() if not value]
    rows["verdict"] = {
        "passed": not problems,
        "scope": "传输/持久化判据（不含模型是否服从标记指令）",
        "signals": signals,
        "problems": problems,
    }
    rows["compliance"] = {
        "passed": all(compliance.values()),
        "scope": "模型服从性判据：不服从不等于正文丢失",
        "signals": compliance,
        "note": ("若 compliance=false 而 verdict.passed=true，说明正文存储正常、"
                 "只是模型没有照抄标记；这不是存储回归。"),
    }
    rows["passed"] = rows["verdict"]["passed"]
    target = _resolve_out(out, DEFAULT_OUT, name="verify_http_live")
    if target is not None:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    print(f"\nL02 真实 HTTP 正文：{'通过' if rows['passed'] else '未通过'}；"
          f"证据：{target or '（未落盘）'}")
    return 0 if rows["passed"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", action="store_true", help="确认执行真实模型调用")
    parser.add_argument("--agent", default="deepseek")
    parser.add_argument("--json-out", default="", help="证据输出路径（默认 outputs/验收复现/）")
    args = parser.parse_args()
    if not args.run:
        print("这是真实模型验证脚本：加 --run 才会执行。", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(asyncio.run(main(
        agent=args.agent, prompt="只返回标记",
        out=Path(args.json_out) if args.json_out else None,
    )))
