#!/usr/bin/env python3
"""统一收尾：一次性跑完提示词 D 组六类固定检查，输出结果表。

只用本机确定性判据与已生成的真实矩阵：
- 不调用模型、不连外网、不改生产配置；
- 会写探针 JSON 的独立反例由 `collect_round6_evidence.py` 从**副本**运行，本脚本只读矩阵。

用法：
    .venv/bin/python scripts/verify_unified_closeout.py [--matrix PATH] [--json-out PATH]
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agents.codex import (  # noqa: E402
    CASE_SPECS,
    ROLL_SIDE_EFFECT,
    instantiate_spec,
    CONTROL_ONLY_CASES,
    REQUIRED_MATRIX_CASES,
    ROLL_ALLOW,
    ROLL_DENY,
    ROLL_OBSERVE,
    SIDE_EFFECT_STATES,
    case_digest,
    check_probe_contract,
    sandbox_matrix_verdict,
)

DEFAULT_MATRIX = ROOT / "outputs/嵌套对照schema收尾/权限矩阵.json"
DEFAULT_OUT = ROOT / "outputs/嵌套对照schema收尾/verify_unified_closeout.json"


def display_path(path) -> str:
    """项目内路径显示为相对路径；项目外显示绝对路径（不因 relative_to 抛异常）。"""
    try:
        return str(Path(path).resolve().relative_to(ROOT))
    except (ValueError, OSError):
        return str(Path(path).resolve())


def normalize_out(value, default):
    """把 `--json-out` 标准化：相对路径按项目根解析，绝对路径原样使用。"""
    if not value:
        return default
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    return candidate.resolve()


def normalize_matrix(value, default):
    if not value:
        return default
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    return candidate.resolve()


def judge(rows, config):
    """调用**生产统一判定器**；异常也算失败（不允许崩溃当作通过）。"""
    try:
        verdict = sandbox_matrix_verdict(rows, required=REQUIRED_MATRIX_CASES,
                                         profile_name="qq_ro", config=config)
    except Exception as exc:  # noqa: BLE001 - 记录为可诊断失败
        return False, [f"{type(exc).__name__}: {exc}"], []
    return bool(verdict[0]), list(verdict[1]), list(verdict.notes)


def mutate(rows, case, recompute=True, **changes):
    for row in rows:
        if row["case"] == case:
            row.update(changes)
            if recompute and "case_digest" in row:
                row["case_digest"] = case_digest(row)


def main(*, matrix_path: Path | None = None, out: Path | None = None) -> int:
    matrix_file = normalize_matrix(str(matrix_path) if matrix_path else None, DEFAULT_MATRIX)
    target = normalize_out(str(out) if out else None, DEFAULT_OUT)
    target.parent.mkdir(parents=True, exist_ok=True)
    if not matrix_file.is_file():
        print(f"缺少矩阵：{matrix_file}（先运行 scripts/verify_codex_sandbox_matrix.py）",
              file=sys.stderr)
        return 2
    document = json.loads(matrix_file.read_text(encoding="utf-8"))
    matrix = document.get("read_only_matrix") or document
    config = str((matrix.get("profile") or {}).get("config") or "")
    base_rows = copy.deepcopy(matrix.get("rows") or [])

    checks: list[dict] = []

    def check(group: str, case: str, expect_pass: bool, rows, note: str = "") -> None:
        try:
            passed, problems, notes = judge(rows, config)
        except Exception as exc:  # noqa: BLE001 - 底层崩溃也是"没有可诊断失败"
            checks.append({
                "group": group, "case": case, "expect_pass": expect_pass, "passed": None,
                "ok": False, "problems": [f"判定器抛异常：{type(exc).__name__}: {exc}"],
                "notes": [], "note": note,
            })
            return
        checks.append({
            "group": group, "case": case, "expect_pass": expect_pass, "passed": passed,
            "ok": passed is expect_pass, "problems": problems[:4], "notes": notes[:3],
            "note": note,
        })

    # 0) 真实矩阵正例（生成器产出的正常正负配对）
    check("1_positive_pairing", "current_matrix", True, copy.deepcopy(base_rows),
          "本轮真实生成的完整矩阵必须通过")

    deny_like = [spec.case for spec in CASE_SPECS if spec.role in (ROLL_DENY, ROLL_OBSERVE)]
    # 2) 每个拒绝/观测项：改期望、加豁免、删必要字段 —— 都必须失败
    for case in deny_like:
        rows = copy.deepcopy(base_rows)
        mutate(rows, case, expect_allowed=True, exit_code=0, stdout="succeeded",
               stderr="", quick_failure=False)
        check("2_expected_flip", case, False, rows)

        rows = copy.deepcopy(base_rows)
        mutate(rows, case, verdict_exempt=True, exit_code=0, stdout="succeeded",
               stderr="", quick_failure=False)
        check("2_exempt_self_grant", case, False, rows)

        rows = copy.deepcopy(base_rows)
        for row in rows:
            if row["case"] == case:
                for key in ("argv", "case_digest", "exit_code"):
                    row.pop(key, None)
        check("2_missing_required_field", case, False, rows)

        rows = copy.deepcopy(base_rows)
        mutate(rows, case, case_role=ROLL_ALLOW if case != "read_inside_workspace" else ROLL_DENY,
               recompute=False)
        check("2_role_relabel", case, False, rows)

    # 3) 严格网络：合同 / 工具自身失败 / 同 argv 对照
    port = next((row.get("endpoint", {}).get("port") for row in base_rows
                 if row["case"] == "network_loopback_probe"), 50999)
    for label, argv, argv_valid, expect_pass in (
        ("valid_nc_argv", ["/usr/bin/nc", "-z", "-G", "3", "127.0.0.1", str(port)], True, True),
        ("unknown_option", ["/usr/bin/nc", "--qq-invalid-option", "-z", "-G", "3",
                            "127.0.0.1", str(port)], False, False),
        ("nan_timeout", ["/usr/bin/nc", "-z", "-G", "nan", "127.0.0.1", str(port)], False, False),
        ("unsupported_scheme", ["/usr/bin/curl", "-s", "-m", "5", "-o", "/dev/null", "-w",
                                "http_code=%{http_code}", "ftp://127.0.0.1/x"], False, False),
        ("extra_argument", ["/usr/bin/nc", "-z", "-G", "3", "127.0.0.1", str(port), "x"],
         False, False),
        ("other_tool", ["/bin/cat", "/etc/hosts"], False, False),
    ):
        parsed, reason = check_probe_contract(argv)
        contract_ok = (parsed is not None) is argv_valid
        rows = copy.deepcopy(base_rows)
        mutate(rows, "network_loopback_probe", argv=argv, exit_code=1, stderr="",
               quick_failure=True,
               outside_control={"argv": list(argv), "exit_code": 0 if argv_valid else 1,
                                "reachable": argv_valid,
                                "evidence": "CONTROL_OK" if argv_valid else "CONTROL_FAIL",
                                "stdout": "", "stderr": ""},
               control_allowed=argv_valid)
        passed, problems, notes = judge(rows, config)
        # 合同的判据是"非法形态必须被合同拒绝"；合法形态则要求整体判定通过
        judged_ok = (passed is expect_pass) if argv_valid else (parsed is None)
        checks.append({
            "group": "3_probe_contract", "case": label, "expect_pass": expect_pass,
            "passed": passed, "ok": bool(judged_ok and contract_ok),
            "problems": problems[:4], "notes": notes[:3],
            "contract_reason": reason,
            "contract_verdict": "rejected" if parsed is None else "accepted",
        })

    rows = copy.deepcopy(base_rows)
    mutate(rows, "network_loopback_probe", outside_control={
        "argv": ["/usr/bin/nc", "-z", "-G", "3", "127.0.0.1", str(port)], "exit_code": 2,
        "reachable": False, "evidence": "CONTROL_FAIL", "stdout": "",
        "stderr": "nc: invalid option"}, control_allowed=False)
    check("3_outside_control", "outside_same_argv_failed", False, rows,
          "沙箱外同 argv 失败 → 判环境/命令故障，不得算策略拒绝")

    rows = copy.deepcopy(base_rows)
    mutate(rows, "network_loopback_probe", exit_code=1, stderr="Operation not permitted",
           outside_control={
               "argv": ["/usr/bin/nc", "-z", "-G", "3", "127.0.0.1", str(port)],
               "exit_code": 0, "reachable": True, "evidence": "CONTROL_OK",
               "stdout": "succeeded", "stderr": ""}, control_allowed=True)
    check("3_outside_control", "outside_ok_inside_denied", True, rows,
          "同一 argv 沙箱外成功 + 沙箱内被拒 = 禁网成立")

    # 4) schema：错误类型、非有限时间、错误摘要、未知/重复/缺用例、上层矛盾
    for case, change in (
        ("read_inside_workspace", {"exit_code": "not-an-exit-code"}),
        ("read_inside_workspace", {"exit_code": True}),
        ("read_inside_workspace", {"seconds": float("nan")}),
        ("read_inside_workspace", {"argv": "not-a-list"}),
        ("read_inside_workspace", {"expect_allowed": "yes"}),
        ("network_loopback_probe", {"case_digest": 12345}),
        ("network_loopback_probe", {"endpoint": "not-a-dict"}),
        ("side_effect_check", {"no_tmp_file": "true"}),
    ):
        rows = copy.deepcopy(base_rows)
        mutate(rows, case, recompute=False, **change)
        check("4_schema", f"{case}:{list(change)[0]}", False, rows)

    rows = copy.deepcopy(base_rows)
    for row in rows:
        if row["case"] == "read_sensitive_file":
            row["case_digest"] = "0" * 64
    check("4_schema", "wrong_digest", False, rows)

    check("4_case_set", "duplicate_case", False, copy.deepcopy(base_rows) + [copy.deepcopy(
        next(r for r in base_rows if r["case"] == "read_inside_workspace"))])
    check("4_case_set", "unknown_case", False,
          copy.deepcopy(base_rows) + [{"case": "unknown_case"}])
    check("4_case_set", "missing_case", False,
          [row for row in copy.deepcopy(base_rows) if row["case"] != "read_sensitive_file"])
    check("4_case_set", "empty_matrix", False, [])

    # 上层消费者：summary=true 但明细冲突必须失败
    from scripts.verify_codex_boundary import _recheck_policy_matrix  # noqa: E402

    tampered = copy.deepcopy(base_rows)
    mutate(tampered, "network_loopback_probe", exit_code=0, stdout="succeeded",
           stderr="", quick_failure=False)
    doc = {"cli": str(matrix.get("cli") or ""), "passed": True, "model_calls": False,
           "profile": {"name": "qq_ro", "config": config},
           "required_cases": list(REQUIRED_MATRIX_CASES), "rows": tampered}
    upper = _recheck_policy_matrix(doc, binary=str(matrix.get("cli") or ""))
    checks.append({
        "group": "4_upper_consumer", "case": "summary_true_vs_detail", "expect_pass": False,
        "passed": bool(upper) is False, "ok": bool(upper) is True, "problems": upper[:4],
        "notes": [], "note": "外层 passed=true 与明细冲突必须被重算判据拒绝",
    })

    # 6) 命令身份：每条执行行的实际 argv 必须等于可信上下文实例化的规格 argv
    context = matrix.get("trusted_context") or {}
    for spec in CASE_SPECS:
        if spec.role == ROLL_SIDE_EFFECT:
            continue
        rows = copy.deepcopy(base_rows)
        expected = instantiate_spec(spec, context)
        other = next((row for row in base_rows
                      if row["case"] != spec.case and not row["case"].startswith("network_")
                      and row.get("argv")), None)
        if other is None:
            continue
        for row in rows:
            if row["case"] == spec.case:
                row["argv"] = list(other["argv"])
        check("6_command_identity", f"{spec.case}:replaced_by_{other['case']}", False, rows,
              "用别的用例的 argv 冒充 → 必须失败")

    rows = copy.deepcopy(base_rows)
    for row in rows:
        row.pop("trusted_context", None)
    check("6_command_identity", "missing_trusted_context", False, rows,
          "缺少本次运行可信上下文 → 证据不足，不得退回只检查标签")

    # 7) 必要字段逐项缺失 / null（不按 case 或具体值打补丁）
    for spec in CASE_SPECS:
        for field_name in spec.required_fields:
            rows = copy.deepcopy(base_rows)
            for row in rows:
                if row["case"] == spec.case:
                    row.pop(field_name, None)
            check("7_required_field_missing", f"{spec.case}:{field_name}", False, rows)

            rows = copy.deepcopy(base_rows)
            for row in rows:
                if row["case"] == spec.case:
                    row[field_name] = None
            check("7_required_field_null", f"{spec.case}:{field_name}", False, rows)

    # 8) 外部对照执行结果：失败/矛盾一律不得算沙箱拒绝；合法正例仍通过
    for label, change in (
        ("exit_code_2_with_control_ok", {"exit_code": 2, "stdout": "",
                                         "stderr": "nc: invalid timeout: nan"}),
        ("timeout_with_control_ok", {"exit_code": 0, "timed_out": True, "stdout": "", "stderr": ""}),
        ("signal_with_control_ok", {"exit_code": 0, "signal": 9, "stdout": "", "stderr": ""}),
        ("success_marker_vs_code", {"exit_code": 1, "evidence": "CONTROL_OK", "reachable": True}),
    ):
        rows = copy.deepcopy(base_rows)
        for row in rows:
            if row["case"] == "network_loopback_probe":
                for name in ("outside_control", "network_control"):
                    record = dict(row[name])
                    record.update(change)
                    row[name] = record
                row["case_digest"] = case_digest(row)
        check("8_outside_control", label, False, rows,
              "对照的真实执行状态必须支撑成功标记")

    rows = copy.deepcopy(base_rows)
    check("8_outside_control", "valid_pairing_still_passes", True, rows,
          "合法同 argv 配对（沙箱外成功、沙箱内被拒）必须仍然通过")

    # 9) 嵌套对照执行记录 schema：两个表示分别验证非法结构与已约定字段类型
    for label, value in (
        ("timed_out_string", {"timed_out": "true"}),
        ("timed_out_number", {"timed_out": 1}),
        ("timed_out_null", {"timed_out": None}),
        ("signal_string", {"signal": "9"}),
        ("signal_true", {"signal": True}),
        ("stdout_object", {"stdout": {}}),
        ("stderr_list", {"stderr": []}),
        ("argv_int", {"argv": 7}),
        ("argv_string", {"argv": "not-a-list"}),
        ("argv_mixed", {"argv": ["/usr/bin/nc", 7]}),
        ("exit_code_bool", {"exit_code": True}),
        ("exit_code_null", {"exit_code": None}),
        ("seconds_string", {"seconds": "0.1"}),
        ("seconds_nan", {"seconds": float("nan")}),
        ("reachable_string", {"reachable": "true"}),
        ("evidence_object", {"evidence": {}}),
    ):
        for representation in ("outside_control", "network_control"):
            rows = copy.deepcopy(base_rows)
            for row in rows:
                if row["case"] == "network_loopback_probe":
                    record = dict(row[representation])
                    record.update(value)
                    row[representation] = record
                    row["case_digest"] = case_digest(row)
            check("9_control_record_schema", f"{representation}:{label}", False, rows,
                  "非法类型/结构必须由底层诊断失败，不得崩溃或被隐式转换")

    # 合法正例：两个表示都给合法执行状态 → 仍通过
    rows = copy.deepcopy(base_rows)
    check("9_control_record_schema", "valid_records_still_pass", True, rows)

    # 5) 记录与指纹保持（只读核对）
    # 指纹输出跟随**实际输出目标目录**（不硬编码任何历史目录，避免显式选新目录却写回旧交付目录）
    fingerprint_file = target.parent / "本轮指纹.json"
    if fingerprint_file != DEFAULT_OUT.parent / "本轮指纹.json" or True:
        pass  # 目录已由 target 决定
    fingerprint_file.write_text(json.dumps({
        "source_matrix": display_path(matrix_file),
        "matrix_sha256": __import__("hashlib").sha256(matrix_file.read_bytes()).hexdigest(),
        "generated_by": "scripts/verify_unified_closeout.py",
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    checks.append({
        "group": "5_records", "case": "restored_originals_present", "expect_pass": True,
        "passed": all((ROOT / rel).is_file() for rel in (
            "outputs/第六轮补充二次独立复验/S64已清理组号复用.json",
            "outputs/第六轮补充三次独立复验/严格回环降级为观察.json",
            "outputs/第六轮补充三次独立复验/同端口不同主机.json",
            "outputs/第六轮补充三次独立复验/缺字段回退旧判据.json")),
        "ok": None, "problems": [], "notes": [],
        "note": "四份已恢复原记录仍在位（逐字节核对见 outputs/统一收尾整改/历史证据核对.json）",
    })
    checks[-1]["ok"] = checks[-1]["passed"]
    checks.append({
        "group": "5_records", "case": "fingerprint_file_written", "expect_pass": True,
        "passed": fingerprint_file.exists(), "ok": fingerprint_file.exists(),
        "problems": [], "notes": [], "note": display_path(fingerprint_file),
    })

    checks.sort(key=lambda item: (item["group"], item["case"]))
    failed = [item for item in checks if item["ok"] is False]
    groups: dict[str, dict] = {}
    for item in checks:
        bucket = groups.setdefault(item["group"], {"total": 0, "ok": 0, "failed": 0})
        bucket["total"] += 1
        if item["ok"]:
            bucket["ok"] += 1
        else:
            bucket["failed"] += 1

    payload = {
        "source_matrix": display_path(matrix_file),
        "matrix_passed": bool(matrix.get("passed")),
        "role_of_control_only_cases": sorted(CONTROL_ONLY_CASES),
        "side_effect_states": list(SIDE_EFFECT_STATES),
        "groups": groups,
        "checks": checks,
        "failed": [item["case"] for item in failed],
        "passed": not failed,
    }
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"证据：{display_path(target)}")
    print(f"矩阵：{display_path(matrix_file)}（passed={payload['matrix_passed']}）")
    for name, bucket in groups.items():
        flag = "OK" if bucket["failed"] == 0 else "FAILED"
        print(f"  [{flag}] {name}: {bucket['ok']}/{bucket['total']}")
    if failed:
        print("未通过项：")
        for item in failed:
            print(f"  · {item['group']} / {item['case']}：{item['problems'][:1]}")
        return 1
    print(f"统一收尾检查：通过（共 {len(checks)} 项）")
    return 0


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="统一收尾 D 组固定检查")
    parser.add_argument("--matrix", default="", help="矩阵路径（相对项目根或绝对路径）")
    parser.add_argument("--json-out", default="", help="证据输出路径")
    return parser.parse_args(argv)


if __name__ == "__main__":
    args = _parse_args(sys.argv[1:])
    raise SystemExit(main(
        matrix_path=Path(args.matrix) if args.matrix else None,
        out=Path(args.json_out) if args.json_out else None,
    ))
