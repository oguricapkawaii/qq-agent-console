#!/usr/bin/env python
"""S61 封边会话视图的命令级证据（真实 CLI，**不调用模型**）。

为什么需要它：枚举式 deny 无法覆盖"深层 / 会话期间新建"的敏感文件，因此
broker/text 的会话改用**固定的无敏感隔离视图**（空目录 + `:workspace_roots="deny"`）。
本脚本用真实 CLI 逐条证明：真实工作区里的深层 `.env`、普通文件、配置生成后新建的任意文件
都不可读；同时证明"扫描不完整时整个根降级为 deny"（fail-closed），并保留
"限根只读 + 普通文件可读"的正负配对。

用法：
    .venv/bin/python scripts/verify_s61_sealed_view.py [--json-out <路径>]

退出码：0 = 全部符合预期；1 = 有偏差；2 = 环境问题。
"""

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agents.codex import (
    READ_ONLY_PROFILE, _run_contract, broker_session_view, read_only_permission_config,
    scan_workspace_sensitive, sealed_read_only_permission_config,
)
from app.config import load_settings

_settings = load_settings()
_binary = ((_settings.console_backends or {}).get("codex", {}) or {}).get("binary") \
    or getattr(_settings, "codex_binary", "") or "codex"
binary = str(_binary)
out = {}
def normalize_out(value, default):
    """把 `--json-out` 标准化：相对路径按项目根解析，绝对路径原样使用。"""
    if not value:
        return default
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    return candidate.resolve()


def row(label, result, note=""):
    out[label] = {"note": note, "exit_code": result.get("exit_code"),
                  "stdout": (result.get("stdout") or "")[:200],
                  "stderr": (result.get("stderr") or "")[:200],
                  "argv": result.get("argv")}
with tempfile.TemporaryDirectory(prefix="qq-s61-") as raw:
    root = Path(raw); ws = root / "workspace"; ws.mkdir()
    deep = ws
    for i in range(7):
        deep = deep / str(i); deep.mkdir()
    (deep / ".env").write_text("FAKE_DEEP_PRIVATE_SENTINEL")
    (ws / ".env").write_text("FAKE_ROOT_PRIVATE_SENTINEL")
    (ws / "ok.txt").write_text("OK_SENTINEL")
    view = broker_session_view(ws)
    sealed = sealed_read_only_permission_config(view, (ws,))
    def run(config, workspace, target):
        return _run_contract(binary, profile=READ_ONLY_PROFILE, config=config,
                             workspace=workspace, argv=["/bin/cat", str(target)], timeout=15)
    # 1) 生产形状：CLI 在空会话视图里，真实工作区整体 deny
    row("sealed_view/production_shape/deep_env", run(sealed, view, deep / ".env"),
        "生产 broker/text 路径：cwd=空会话视图，真实工作区不在可读根")
    row("sealed_view/production_shape/root_env", run(sealed, view, ws / ".env"))
    row("sealed_view/production_shape/normal_file", run(sealed, view, ws / "ok.txt"))
    # 2) 直接把封边 profile 套在真实工作区上（对照：整根 deny，全部不可读）
    row("sealed_on_real_root/deep_env", run(sealed, ws, deep / ".env"))
    row("sealed_on_real_root/normal_file", run(sealed, ws, ws / "ok.txt"),
        "整根 deny 的代价：普通文件也不可读（broker 模式本就不需要原生读）")
    # 3) 生成配置后由外部创建的文件
    late = ws / ".env.late"; arbitrary = ws / "secret-token.txt"
    late.write_text("FAKE_LATE_PRIVATE_SENTINEL")
    arbitrary.write_text("FAKE_ARBITRARY_SENTINEL")
    row("sealed_on_real_root/late_created_env", run(sealed, ws, late),
        "配置生成后新增的 .env：整根 deny 下不可读")
    row("sealed_on_real_root/late_created_arbitrary", run(sealed, ws, arbitrary),
        "配置生成后新增的任意名文件：整根 deny 下不可读")
    # 4) 限根只读（原生 read-edit 用）：完整扫描时普通文件可读、已枚举敏感被拒
    rooted = read_only_permission_config(ws)
    row("rooted/deep_env", run(rooted, ws, deep / ".env"))
    row("rooted/normal_file", run(rooted, ws, ws / "ok.txt"))
    row("rooted/late_created_env", run(rooted, ws, late),
        "已知残留：完整扫描 + 会话中新建的 .env.* 不在清单内（原生 read-edit 路径）")
    # 5) 扫描不完整（70 层）→ 整个根 deny
    cur = ws
    for i in range(70):
        cur = cur / f"d{i}"; cur.mkdir()
    scan = scan_workspace_sensitive(ws)
    sealed_deep = read_only_permission_config(ws)
    out["scan/incomplete"] = {"complete": scan["complete"], "reasons": scan["reasons"][:2]}
    out["scan/incomplete_root_denied"] = {
        "workspace_roots": "deny" if '":workspace_roots"="deny' in sealed_deep else "read"
    }
    row("rooted/incomplete_scan_normal_file", run(sealed_deep, ws, ws / "ok.txt"),
        "扫描不完整 → fail-closed：普通文件也不可读")
payload = {
    "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    "model_calls": False, "qq_messages_sent": False, "production_changed": False,
    "scope": "本机命令级证据（真实 CLI `codex sandbox`，不调用模型）",
    "design": {
        "broker_text_session": "cwd 指向空的会话隔离目录；`:workspace_roots` 直接 deny；真实工作区不在可读根",
        "native_read_edit": "限根只读 + 根内敏感绝对路径 deny；扫描结论不完整时整个根降级为 deny",
        "why": "S61：枚举式 deny 无法覆盖深层/新增文件，因此 broker/text 改用固定的无敏感隔离视图",
    },
    "results": out,
}
_explicit = sys.argv[sys.argv.index("--json-out") + 1] if "--json-out" in sys.argv else None
target = normalize_out(_explicit, ROOT / "outputs/第六轮补充二次整改/verify_s61_sealed_view.py.json")
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
for key, value in out.items():
    if "exit_code" in value:
        print(f"{key:48s} exit={value['exit_code']} out={value['stdout'][:28]!r}")
    else:
        print(f"{key:48s} {json.dumps(value, ensure_ascii=False)[:90]}")
print(f"written {target}")
# 判定：封边视图下真实工作区必须全部不可读；限根只读必须"敏感拒、普通文件可读"；
# 扫描不完整必须整根 deny（fail-closed）。
expect = {
    "sealed_view/production_shape/deep_env": "deny",
    "sealed_view/production_shape/root_env": "deny",
    "sealed_view/production_shape/normal_file": "deny",
    "sealed_on_real_root/deep_env": "deny",
    "sealed_on_real_root/late_created_env": "deny",
    "sealed_on_real_root/late_created_arbitrary": "deny",
    "rooted/deep_env": "deny",
    "rooted/normal_file": "allow",
    "rooted/late_created_env": "deny",
    "rooted/incomplete_scan_normal_file": "deny",
}
problems = []
for name, want in expect.items():
    row = out.get(name) or {}
    code = row.get("exit_code")
    if want == "deny" and code == 0:
        problems.append(f"{name} 期望拒绝但 exit=0")
    if want == "allow" and code != 0:
        problems.append(f"{name} 期望允许但 exit={code}")
if not out.get("scan/incomplete", {}).get("complete") is False:
    problems.append("超深目录场景没有报告扫描不完整")
if out.get("scan/incomplete_root_denied", {}).get("workspace_roots") != "deny":
    problems.append("扫描不完整时没有把整个根降级为 deny")
print(f"S61 封边证据：{'通过' if not problems else '未通过'}")
if problems:
    print("未通过原因：\n" + "\n".join(f"· {item}" for item in problems), file=sys.stderr)
    raise SystemExit(1)
raise SystemExit(0)
