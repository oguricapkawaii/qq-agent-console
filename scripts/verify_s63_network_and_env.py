#!/usr/bin/env python
"""S63 二次复验：网络端点绑定 + 非法 UTF-8 环境下的矩阵对照（真实 CLI，不调用模型）。

三件事：
1. 用第六轮补充二次独立复验归档的真实矩阵（**只读**）核验判定：
   - legacy 归档证据仍按"必须有对照"判定并通过；
   - 端点不一致（外网 DNS 失败借用回环对照）→ 必须失败；
   - 对照绑定但命令实际打外网 → 必须失败；
   - 回环端口未被拒（exit=0）→ 必须失败；
2. 在本进程环境里注入非法 UTF-8（含 `_`）后运行完整矩阵 + 旧可写对照：
   两者的子进程环境都走 `sanitized_cli_env()`，因此仍应通过；
3. 产出可核对 JSON 与完整输出。

用法：
    .venv/bin/python scripts/verify_s63_network_and_env.py [--out-dir <目录>]

退出码：0 = 全部符合预期；1 = 有偏差；2 = 环境问题。
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agents.codex import (  # noqa: E402
    REQUIRED_MATRIX_CASES,
    case_digest,
    sandbox_matrix_verdict,
)

DEFAULT_OUT_DIR = ROOT / "outputs/嵌套对照schema收尾"
CURRENT_MATRIX = ROOT / "outputs/嵌套对照schema收尾/权限矩阵.json"
# 历史归档矩阵（按**当前**标准属于"证据不足"，只用于展示"旧记录不能作为当前验收通过来源"）
ARCHIVED_MATRIX = ROOT / "outputs/第六轮补充二次独立复验/权限矩阵.json"


def display_path(path: Path) -> str:
    """项目内路径显示为相对路径；项目外/无法相对化时显示绝对路径（不抛异常）。"""
    try:
        return str(Path(path).resolve().relative_to(ROOT))
    except (ValueError, OSError):
        return str(Path(path).resolve())


def normalize_out_dir(value: str | None) -> Path:
    """把 `--out-dir` 标准化：支持相对项目路径与任意绝对路径。"""
    if not value:
        return DEFAULT_OUT_DIR
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    return candidate.resolve()


def _judge(rows, config):
    return sandbox_matrix_verdict(rows, required=REQUIRED_MATRIX_CASES,
                                  profile_name="qq_ro", config=config)


def endpoint_binding(out_dir: Path) -> tuple[bool, dict]:
    """端点绑定判据：归档证据通过，三类"借用对照"必须失败。"""
    matrix = json.loads(CURRENT_MATRIX.read_text(encoding="utf-8"))["read_only_matrix"]
    rows, config = copy.deepcopy(matrix["rows"]), matrix["profile"]["config"]
    results: dict[str, object] = {}

    results["A_current_matrix"] = _judge(copy.deepcopy(rows), config)
    results["B_same_again_idempotent"] = _judge(copy.deepcopy(rows), config)
    # 历史归档按当前标准必须"证据不足"（不得作为当前验收通过来源）
    if ARCHIVED_MATRIX.is_file():
        archived = json.loads(ARCHIVED_MATRIX.read_text(encoding="utf-8"))["read_only_matrix"]
        results["G_archived_evidence_is_insufficient"] = _judge(
            copy.deepcopy(archived["rows"]), config)

    # C：同端口不同主机（重算摘要 → 只让**端点核对**生效）
    mismatched = copy.deepcopy(rows)
    for row in mismatched:
        if row["case"] == "network_loopback_probe":
            port = (row.get("endpoint") or {}).get("port", 59304)
            row.update(exit_code=6, stdout="http_code=000", seconds=0.01,
                       quick_failure=True, observed_denial_marker=True,
                       stderr="curl: (6) Could not resolve host: test.invalid",
                       case_digest=None)
            # 沙箱外对照改为**同一条 argv** 且成功：这样失败原因只可能是端点不一致
            foreign = ["/usr/bin/curl", "-s", "-m", "5", "-o", "/dev/null", "-w",
                       "http_code=%{http_code} time_total=%{time_total}",
                       f"http://test.invalid:{port}/"]
            row["argv"] = foreign
            row["outside_control"] = {"argv": list(foreign), "exit_code": 0, "reachable": True,
                                      "evidence": "CONTROL_OK", "stdout": "http_code=200",
                                      "stderr": ""}
            row["control_allowed"] = True
            row["case_digest"] = case_digest(row)
    results["C_endpoint_mismatch_loopback_control"] = _judge(mismatched, config)

    # D：对照绑定到回环，但命令实际打外网（重算摘要 → 端点核对生效）
    external = copy.deepcopy(rows)
    for row in external:
        if row["case"] == "network_loopback_probe":
            port = (row.get("endpoint") or {}).get("port", 59304)
            row.update(exit_code=6, stdout="http_code=000", seconds=0.01,
                       quick_failure=True, observed_denial_marker=True,
                       stderr="curl: (6) Could not resolve host: api.example.com",
                       case_digest=None)
            outside_argv = ["/usr/bin/curl", "-s", "-m", "5", "-o", "/dev/null", "-w",
                            "http_code=%{http_code} time_total=%{time_total}",
                            "https://api.example.com/"]
            row["argv"] = outside_argv
            row["outside_control"] = {"argv": list(outside_argv), "exit_code": 0,
                                      "reachable": True, "evidence": "CONTROL_OK",
                                      "stdout": "http_code=200", "stderr": ""}
            row["control_allowed"] = True
            row["case_digest"] = case_digest(row)
    results["D_control_bound_but_command_external"] = _judge(external, config)

    # E：回环端口未被拒（exit=0）→ 必须失败
    leaked = copy.deepcopy(rows)
    for row in leaked:
        if row["case"] == "network_loopback_probe":
            row.update(exit_code=0, stdout="succeeded", stderr="", quick_failure=False)
            row["case_digest"] = case_digest(row)
    results["E_loopback_not_denied"] = _judge(leaked, config)

    # F：摘要不一致（只改字段不重算）→ 必须被摘要判据拦下（与语义判据分开验证）
    tampered = copy.deepcopy(rows)
    for row in tampered:
        if row["case"] == "network_loopback_probe":
            row["case_digest"] = "0" * 64
    results["F_digest_mismatch"] = _judge(tampered, config)

    expect = {
        "A_current_matrix": True,
        "B_same_again_idempotent": True,
        "G_archived_evidence_is_insufficient": False,
        "C_endpoint_mismatch_loopback_control": False,
        "D_control_bound_but_command_external": False,
        "E_loopback_not_denied": False,
        "F_digest_mismatch": False,
    }
    payload = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "model_calls": False,
        "source_matrix": display_path(CURRENT_MATRIX),
        "archived_matrix": display_path(ARCHIVED_MATRIX),
        "note": ("归档矩阵没有 endpoint/verdict_mode 字段（当时规格），按 legacy 判定："
                 "仍要求对照存在，但无法核对端点；新证据必须通过端点绑定核对。"),
        "cases": {key: {"passed": value[0], "problems": value[1], "notes": value.notes}
                  for key, value in results.items()},
        "expectations": expect,
        "all_expectations_met": all(results[key][0] == want for key, want in expect.items()),
    }
    target = out_dir / "verify_s63_endpoint_binding.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for key, value in payload["cases"].items():
        print(f"{key:44s} passed={value['passed']} problems={len(value['problems'])}")
    print(f"端点绑定证据：{target} | 全部符合预期：{payload['all_expectations_met']}")
    return bool(payload["all_expectations_met"]), payload


def invalid_utf8_matrix(out_dir: Path) -> tuple[bool, dict]:
    """注入非法 UTF-8（含 `_`）后运行完整矩阵 + 旧可写对照。"""
    env = dict(os.environ)
    env["_"] = os.fsdecode(b"/tmp/bad-\xff\xfe-value")
    env["QQ_INVALID_UTF8_PROBE"] = os.fsdecode(b"bad-\xff-value")
    target = out_dir / "verify_codex_sandbox_matrix.invalid_utf8_env.json"
    cmd = [sys.executable, "scripts/verify_codex_sandbox_matrix.py", "--json-out", str(target)]
    completed = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, env=env, check=False)
    log = out_dir / "非法UTF8环境矩阵.txt"
    log.write_text(
        f"$ {' '.join(cmd)}\n# 注入的环境键：_={env['_']!r} QQ_INVALID_UTF8_PROBE="
        f"{env['QQ_INVALID_UTF8_PROBE']!r}\n# exit={completed.returncode}\n\n"
        f"STDOUT:\n{completed.stdout}\n\nSTDERR:\n{completed.stderr}\n",
        encoding="utf-8",
    )
    payload: dict = {"exit_code": completed.returncode, "evidence": display_path(target),
                     "log": display_path(log)}
    if target.is_file():
        data = json.loads(target.read_text(encoding="utf-8"))
        payload["read_only_passed"] = bool(data.get("read_only_matrix", {}).get("passed"))
        payload["legacy_profile_can_write_workspace"] = bool(
            data.get("verdict", {}).get("legacy_profile_can_write_workspace"))
        payload["cli_env_keys"] = data.get("read_only_matrix", {}).get("cli_env_keys")
        payload["legacy_env_keys"] = data.get("legacy_control", {}).get("env_keys")
        # CLI 里不应出现被注入的坏变量
        for key in ("cli_env_keys", "legacy_env_keys"):
            if "_" in (payload.get(key) or []):
                payload.setdefault("problems", []).append(f"{key} 仍包含 `_`")
            if "QQ_INVALID_UTF8_PROBE" in (payload.get(key) or []):
                payload.setdefault("problems", []).append(f"{key} 仍包含注入变量")
    ok = (completed.returncode == 0 and payload.get("read_only_passed") is True
          and payload.get("legacy_profile_can_write_workspace") is True
          and not payload.get("problems"))
    payload["passed"] = ok
    summary_path = out_dir / "verify_s63_invalid_utf8_env.json"
    summary_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"非法 UTF-8 环境矩阵：{'通过' if ok else '未通过'}；证据：{summary_path}")
    return ok, payload


def main(*, out_dir: Path | None = None) -> int:
    target_dir = normalize_out_dir(str(out_dir) if out_dir else None)
    target_dir.mkdir(parents=True, exist_ok=True)
    if not CURRENT_MATRIX.is_file():
        print(f"缺少本轮矩阵：{CURRENT_MATRIX}（先运行 verify_codex_sandbox_matrix.py）",
              file=sys.stderr)
        return 2
    binding_ok, _ = endpoint_binding(target_dir)
    env_ok, _ = invalid_utf8_matrix(target_dir)
    if binding_ok and env_ok:
        print("S63 端点绑定 + 非法 UTF-8 环境：通过")
        return 0
    print("S63 验证未通过（详见上面的问题列表）", file=sys.stderr)
    return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", default="", help="证据输出目录（支持相对项目路径或绝对路径）")
    args = parser.parse_args()
    raise SystemExit(main(out_dir=Path(args.out_dir) if args.out_dir else None))
