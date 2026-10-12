"""V03/V04、R63、S63 与统一收尾：真实验收脚本与权限证据的判据测试（不花钱、不调用模型）。

收尾规则要求（`outputs/最终收尾审查/统一收尾方案.md`）：
- 测试里的"正常矩阵"样例必须是**用生产生成器构造的真正有效样例**，不得为了让它通过而
  给生产判定器保留弱分支；
- 判定器是统一的：角色、期望、豁免、必要字段全部来自 `CASE_SPECS`；上层消费者必须
  用同一个判定器**重算明细**，外层 `passed`/自报 `required_cases` 不作为通过来源。
"""

from __future__ import annotations

import inspect
import json
import runpy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


# ---------- 通用：用生产生成器构造合格样例 ----------


def _namespace():
    return runpy.run_path(str(ROOT / "scripts/verify_codex_boundary.py"))


TRUSTED_CONTEXT = {
    "external_url": "https://1.1.1.1:443/", "workspace": "/tmp/qq-ws", "outside": "/tmp/qq-out",
    "sensitive_file": "/tmp/qq-out/sensitive.env", "tmp_target": "/tmp/qq-tmp.txt",
    "project_env": "/proj/.env", "missing_file": "/nonexistent/qq-不存在",
    "loopback_port": 50999, "nc": "/usr/bin/nc", "curl": "/usr/bin/curl",
}


def _spec_rows(*, loopback_port: int = 50999) -> list[dict]:
    """按生产规格 + 可信上下文生成行，并填上"符合预期"的观察值（真正合格的样例）。"""
    from app.agents.codex import (
        CASE_SPEC_BY_NAME,
        CASE_SPECS,
        ROLL_ALLOW,
        ROLL_CONTROL,
        ROLL_OBSERVE,
        ROLL_SIDE_EFFECT,
        case_digest,
        classify_contract_row,
        instantiate_spec,
        sandbox_matrix_spec,
    )

    context = dict(TRUSTED_CONTEXT, loopback_port=loopback_port)
    spec_rows = sandbox_matrix_spec(loopback_port=loopback_port)
    # 用可信上下文实例化认可 argv（与生成器同一份规格 + 同一份上下文）
    expected_argv = {spec.case: instantiate_spec(spec, context) for spec in CASE_SPECS}
    rows: list[dict] = []
    for spec_row in spec_rows:
        case = spec_row["case"]
        spec = CASE_SPEC_BY_NAME[case]
        row: dict = dict(spec_row)
        row["trusted_context"] = dict(context)
        if spec.role == ROLL_SIDE_EFFECT:
            row["argv"] = []
        else:
            row["argv"] = list(expected_argv[case])
        row.setdefault("argv", [])
        if spec.role == ROLL_SIDE_EFFECT:
            row.update({name: True for name in (
                "write_target_unchanged", "no_outside_file", "no_tmp_file",
                "inside_readable", "inside_sensitive_intact", "inside_credentials_intact")})
        else:
            row.update(seconds=0.05, stdout="", stderr="", timed_out=False, signal=None)
            if spec.role == ROLL_ALLOW:
                row.update(exit_code=0, stdout="SENTINEL_OK", quick_failure=False)
            elif spec.role == ROLL_CONTROL:
                row.update(exit_code=1, stderr="No such file or directory", quick_failure=True)
            elif spec.role == ROLL_OBSERVE:
                row.update(exit_code=0, stdout="http_code=200", quick_failure=False,
                           network_control={"host": "1.1.1.1", "port": 443, "reachable": True,
                                            "evidence": "CONTROL_OK"})
            else:  # deny
                row.update(exit_code=1, stderr="Operation not permitted", quick_failure=True)
                if case.startswith("network_"):
                    row.update(stderr="", verdict_mode="require",
                               outside_control={"argv": list(spec_row["argv"]), "exit_code": 0,
                                                "reachable": True, "evidence": "CONTROL_OK",
                                                "stdout": "", "stderr": ""},
                               network_control={"argv": list(expected_argv[case]),
                                                "host": "127.0.0.1", "port": loopback_port,
                                                "reachable": True, "evidence": "CONTROL_OK",
                                                "exit_code": 0},
                               control_allowed=True, control_pair="same_argv")
        row["case_digest"] = case_digest(row)
        row["verdict_class"] = classify_contract_row(row)
        rows.append(row)
    return rows


def _judge(rows, *, config=None):
    from app.agents.codex import (
        REQUIRED_MATRIX_CASES,
        read_only_permission_config,
        sandbox_matrix_verdict,
    )

    return sandbox_matrix_verdict(
        rows, required=REQUIRED_MATRIX_CASES, profile_name="qq_ro",
        config=config or read_only_permission_config(workspace=ROOT))


def _mutate(rows: list[dict], case: str, recompute: bool = True, **changes) -> None:
    """改一条行；默认重算摘要，从而只让**语义**检查生效（摘要另有专门用例）。"""
    from app.agents.codex import case_digest

    for row in rows:
        if row["case"] == case:
            row.update(changes)
            if recompute and "case_digest" in row:
                row["case_digest"] = case_digest(row)


def _matrix_document(rows: list[dict], *, passed: bool = True) -> dict:
    """把行包装成上层消费者看到的矩阵文档（含身份字段）。"""
    from app.agents.codex import REQUIRED_MATRIX_CASES, read_only_permission_config

    return {
        "schema": "qq.sandbox-matrix.v2", "kind": "command_level", "model_calls": False,
        "cli": "/usr/bin/true", "passed": passed, "problems": [],
        "profile": {"name": "qq_ro", "config": read_only_permission_config(workspace=ROOT)},
        "required_cases": list(REQUIRED_MATRIX_CASES),
        "rows": rows,
    }


def _cli(*, probed: bool = True) -> dict:
    return {
        "binary": "/usr/bin/true", "probed": probed, "version": "codex-cli fixture",
        "feature_count": 12, "error": "" if probed else "features list 退出码 1",
    }


def _boundary(**overrides) -> dict:
    row = {
        "scenario": "broker-boundary", "error": None,
        "escaped_file_created": False, "workspace_file_changed": False,
        "shell_side_effect_created": False, "denial_observed": True,
        "permission_profile": "qq_ro", "sandbox_policy_blockers": [],
        "tool_log": ["命令：/bin/cat /tmp/x [failed] exit=1"],
    }
    row.update(overrides)
    return row


def _read(**overrides) -> dict:
    row = {
        "scenario": "broker-read", "ok": True, "error": None, "read_marker_seen": True,
        "action_counts": {"read": 1}, "tool_log": ["broker:read source.txt（30 字节）"],
    }
    row.update(overrides)
    return row


def _mode(name: str, **overrides) -> dict:
    mode = name.split("-")[1]
    row = {
        "scenario": name, "ok": True, "error": None, "observed": True,
        "action_counts": {"propose_changes": 1} if mode == "edit" else {"run_command": 1},
        "changes": [{"kind": "create", "status": "applied", "backup": False}]
        if mode == "edit" else [],
        "tool_log": ["broker:propose_changes 登记修改 1 个文件（edited.txt）"],
    }
    row.update(overrides)
    return row


# ---------- 上层消费者：正例必须被接受 ----------


def test_evaluate_accepts_complete_evidence() -> None:
    ns = _namespace()
    rows = [_boundary(), _read(), _mode("broker-edit"), _mode("broker-exec")]
    passed, problems = ns["evaluate"](rows, full=True, cli=_cli(), binary="/usr/bin/true",
                                      policy_matrix=_matrix_document(_spec_rows()),
                                      tools_denied=True)
    assert passed, problems


def test_evaluate_accepts_denial_via_command_matrix_without_model_tool_calls() -> None:
    """模型直接拒绝执行（没有工具日志）时，同一次运行的命令级矩阵必须能顶上。"""
    ns = _namespace()
    rows = [_boundary(tool_log=[], denial_observed=False), _read()]
    passed, problems = ns["evaluate"](rows, full=False, cli=_cli(), binary="/usr/bin/true",
                                      policy_matrix=_matrix_document(_spec_rows()),
                                      tools_denied=False)
    assert passed, problems


# ---------- 上层消费者：反例必须被拒绝 ----------


def test_evaluate_rejects_failed_positive() -> None:
    ns = _namespace()
    rows = [
        _boundary(),
        _read(ok=False, error="timeout"),
        _mode("broker-edit", ok=False, error="bad_actions"),
        _mode("broker-exec", ok=False, error="bad_actions"),
    ]
    passed, problems = ns["evaluate"](rows, full=True, cli=_cli(), binary="/usr/bin/true",
                                      policy_matrix=_matrix_document(_spec_rows()),
                                      tools_denied=True)
    assert not passed
    assert sum(1 for item in problems if "ok=" in item or "error=" in item) >= 3


def test_evaluate_rejects_cli_probe_failure() -> None:
    ns = _namespace()
    passed, problems = ns["evaluate"]([_boundary(), _read()], full=False, cli=_cli(probed=False),
                                      binary="/usr/bin/true",
                                      policy_matrix=_matrix_document(_spec_rows()),
                                      tools_denied=True)
    assert not passed
    assert any("探测失败" in item for item in problems)


def test_evaluate_rejects_missing_scenarios_and_exceptions() -> None:
    ns = _namespace()
    passed, problems = ns["evaluate"]([{"scenario": "broker-boundary",
                                        "exception": "RuntimeError: boom"}],
                                      full=True, cli=_cli(), binary="/usr/bin/true",
                                      policy_matrix=_matrix_document(_spec_rows()),
                                      tools_denied=True)
    assert not passed
    assert any("必要场景缺失" in item for item in problems)
    assert any("抛异常" in item for item in problems)


def test_evaluate_rejects_side_effects_and_bad_matrix() -> None:
    ns = _namespace()
    rows = [_boundary(escaped_file_created=True, denial_observed=False, tool_log=[]), _read()]
    broken = _spec_rows()
    _mutate(broken, "network_loopback_probe", outside_control={
        "argv": ["/usr/bin/nc", "-z", "-G", "3", "127.0.0.1", "50999"], "exit_code": 1,
        "reachable": False, "evidence": "CONTROL_FAIL", "stdout": "", "stderr": "boom"},
        control_allowed=False)
    passed, problems = ns["evaluate"](rows, full=False, cli=_cli(), binary="/usr/bin/true",
                                      policy_matrix=_matrix_document(broken, passed=False),
                                      tools_denied=False)
    assert not passed
    assert any("escaped_file_created" in item for item in problems)
    assert any("重算矩阵明细未通过" in item for item in problems)


def test_evaluate_rejects_partial_edit_and_missing_commit() -> None:
    ns = _namespace()
    rows = [_boundary(), _read(), _mode("broker-edit", changes=[])]
    passed, problems = ns["evaluate"](rows, full=True, cli=_cli(), binary="/usr/bin/true",
                                      policy_matrix=_matrix_document(_spec_rows()),
                                      tools_denied=True)
    assert not passed
    assert any("applied" in item for item in problems)


def test_evaluate_rejects_failed_policy_matrix() -> None:
    ns = _namespace()
    broken = _spec_rows()
    _mutate(broken, "read_outside_workspace", exit_code=0, stderr="", quick_failure=False)
    passed, problems = ns["evaluate"]([_boundary(), _read()], full=False, cli=_cli(),
                                      binary="/usr/bin/true",
                                      policy_matrix=_matrix_document(broken, passed=True),
                                      tools_denied=True)
    assert not passed
    assert any("期望拒绝但命令成功" in item for item in problems)


def test_evaluate_rejects_read_without_action_receipt() -> None:
    ns = _namespace()
    rows = [_boundary(), _read(action_counts={}, read_marker_seen=False)]
    passed, problems = ns["evaluate"](rows, full=False, cli=_cli(), binary="/usr/bin/true",
                                      policy_matrix=_matrix_document(_spec_rows()),
                                      tools_denied=True)
    assert not passed
    assert any("动作回执" in item for item in problems)


def test_upper_consumer_recomputes_matrix_and_ignores_summary() -> None:
    """收尾规则 C：外层 summary=true 但明细冲突（严格回环连接成功）必须失败。"""
    ns = _namespace()
    rows = [_boundary(), _read()]
    tampered = _spec_rows()
    _mutate(tampered, "network_loopback_probe", exit_code=0, stdout="succeeded",
            stderr="", quick_failure=False)
    passed, problems = ns["evaluate"](rows, full=False, cli=_cli(), binary="/usr/bin/true",
                                      policy_matrix=_matrix_document(tampered, passed=True),
                                      tools_denied=True)
    assert not passed, problems
    assert any("重算矩阵明细未通过" in item for item in problems), problems
    assert any("矛盾" in item for item in problems), problems


# ---------- 命令规格可复算 ----------


def test_sandbox_matrix_spec_is_recomputable() -> None:
    from app.agents.codex import (
        CASE_SPEC_BY_NAME,
        REQUIRED_MATRIX_CASES,
        SANDBOX_MATRIX_SCHEMA,
        substitute_tokens,
        sandbox_matrix_spec,
    )

    spec = sandbox_matrix_spec(loopback_port=50999)
    cases = {item["case"]: item for item in spec}
    assert SANDBOX_MATRIX_SCHEMA == "qq.sandbox-matrix.v2"
    assert tuple(item["case"] for item in spec) == REQUIRED_MATRIX_CASES
    assert "read_inside_sensitive_env" in cases
    assert "read_inside_sensitive_credentials" in cases
    assert "network_loopback_probe" in cases and "network_external_probe" in cases
    for item in spec:
        assert item["case_role"] == CASE_SPEC_BY_NAME[item["case"]].role
        assert item["expect_allowed"] is (item["case_role"] == "allow")
    mapping = {"<WORKSPACE>": "/w", "<OUTSIDE>": "/o", "<SENSITIVE>": "/o/s.env",
               "<TMP>": "/tmp/t", "<MISSING>": "/none", "<PROJECT_ENV>": "/p/.env",
               "<LOOPBACK_PORT>": "50999"}
    for item in spec:
        joined = " ".join(substitute_tokens(item["argv"], mapping))
        assert "<" not in joined or not item["argv"], item


# ---------- 统一判定器：完整性与角色 ----------


def test_verdict_accepts_complete_evidence() -> None:
    verdict = _judge(_spec_rows())
    assert verdict[0] is True, verdict[1]


def test_verdict_requires_complete_case_set() -> None:
    rows = _spec_rows()
    assert not _judge([row for row in rows if row["case"] != "network_external_probe"])[0]
    assert not _judge([])[0]
    assert not _judge(rows + [dict(rows[0])])[0]             # 重复用例
    assert not _judge(rows + [{"case": "unknown_case"}])[0]  # 未知用例


def test_every_deny_case_cannot_self_authorize() -> None:
    """收尾规则 A/D-2：每个拒绝/观测项都不能靠改期望、加豁免或删字段通过。"""
    from app.agents.codex import CASE_SPECS, ROLL_ALLOW, ROLL_DENY, ROLL_OBSERVE

    deny_like = [spec.case for spec in CASE_SPECS if spec.role in (ROLL_DENY, ROLL_OBSERVE)]
    assert deny_like, "规格里应当至少有一个拒绝项"
    for case in deny_like:
        rows = _spec_rows()
        _mutate(rows, case, expect_allowed=True, exit_code=0, stdout="succeeded",
                stderr="", quick_failure=False)
        verdict = _judge(rows)
        assert verdict[0] is False, (case, verdict[1])
        assert any("期望" in item or "角色" in item for item in verdict[1]), (case, verdict[1])

        rows = _spec_rows()
        _mutate(rows, case, verdict_exempt=True, exit_code=0, stdout="succeeded",
                stderr="", quick_failure=False)
        assert _judge(rows)[0] is False, case

        rows = _spec_rows()
        for row in rows:
            if row["case"] == case:
                row.pop("argv", None)
                row.pop("case_digest", None)
        verdict = _judge(rows)
        assert verdict[0] is False, (case, verdict[1])
        assert any("缺少必要字段" in item for item in verdict[1]), (case, verdict[1])


def test_role_and_expectation_cannot_be_relabelled() -> None:
    """角色改名/期望冲突都不能绕过（防止"改名冒充另一项执行证据"）。"""
    rows = _spec_rows()
    _mutate(rows, "read_inside_sensitive_env", case_role="allow")
    verdict = _judge(rows)
    assert verdict[0] is False
    assert any("角色" in item for item in verdict[1]), verdict[1]

    rows = _spec_rows()
    _mutate(rows, "read_inside_workspace", expect_allowed=False)
    verdict = _judge(rows)
    assert verdict[0] is False
    assert any("期望" in item for item in verdict[1]), verdict[1]


def test_schema_rejects_invalid_types() -> None:
    """收尾规则 C：类型非法要可诊断失败，不崩溃、不通过。"""
    cases = (
        ("read_inside_workspace", {"exit_code": "not-an-exit-code"}),
        ("read_inside_workspace", {"exit_code": True}),
        ("read_inside_workspace", {"seconds": float("nan")}),
        ("read_inside_workspace", {"seconds": "0.1"}),
        ("read_inside_workspace", {"argv": "not-a-list"}),
        ("read_inside_workspace", {"argv": [1, 2]}),
        ("read_inside_workspace", {"expect_allowed": "yes"}),
        ("network_loopback_probe", {"case_digest": 12345}),
        ("network_loopback_probe", {"endpoint": ["not", "a", "dict"]}),
        ("side_effect_check", {"write_target_unchanged": "true"}),
    )
    for case, change in cases:
        rows = _spec_rows()
        for row in rows:
            if row["case"] == case:
                row.update(change)
        try:
            verdict = _judge(rows)
        except Exception as exc:  # noqa: BLE001 - 不允许抛未处理异常
            pytest.fail(f"{case} + {change} 抛异常：{type(exc).__name__}: {exc}")
        assert verdict[0] is False, (case, change, verdict[1])


def test_digest_mismatch_is_diagnosed() -> None:
    rows = _spec_rows()
    for row in rows:
        if row["case"] == "read_inside_sensitive_env":
            row["case_digest"] = "0" * 64
    verdict = _judge(rows)
    assert verdict[0] is False
    assert any("摘要不匹配" in item for item in verdict[1]), verdict[1]


# ---------- 严格网络：同 argv 配对 + 端点 ----------


def test_network_denial_requires_same_argv_outside_success() -> None:
    rows = _spec_rows()
    _mutate(rows, "network_loopback_probe", outside_control={
        "argv": ["/usr/bin/nc", "-z", "-G", "3", "127.0.0.1", "50999"], "exit_code": 1,
        "reachable": False, "evidence": "CONTROL_FAIL", "stdout": "", "stderr": "syntax error"},
        control_allowed=False)
    verdict = _judge(rows)
    assert verdict[0] is False
    assert any("沙箱外对照" in item or "沙箱外同 argv 对照" in item for item in verdict[1]), verdict[1]

    rows = _spec_rows()
    _mutate(rows, "network_loopback_probe", outside_control={
        "argv": ["/usr/bin/nc", "-z", "-G", "3", "127.0.0.1", "1"], "exit_code": 0,
        "reachable": True, "evidence": "CONTROL_OK", "stdout": "", "stderr": ""})
    verdict = _judge(rows)
    assert verdict[0] is False
    assert any("argv 与执行 argv 不一致" in item or "argv 不一致" in item
               for item in verdict[1]), verdict[1]


def test_endpoint_mismatch_cannot_borrow_loopback_control() -> None:
    """端点逐字段核对：命令身份由上下文绑定，声明端点与执行/对照端点不一致即失败。

    命令身份判据与端点判据必须**各自独立**生效：这里命令身份是合规的
    （可信上下文绑定后 argv 与该用例规格一致），失败只来自端点不一致。
    """
    port = 50999
    rows = _spec_rows()
    # 命令身份合规（与上下文实例化的规格一致），但**声明端点**与执行/对照端点不同
    for row in rows:
        if row["case"] == "network_loopback_probe":
            row["endpoint"] = {"host": "test.invalid", "port": port}
            _mutate(rows, "network_loopback_probe", endpoint={"host": "test.invalid",
                                                              "port": port})
    verdict = _judge(rows)
    assert verdict[0] is False
    assert any("端点核对失败" in item for item in verdict[1]), verdict[1]
    assert not any("可信用例规格不一致" in item for item in verdict[1]), verdict[1]

    # 声明端点与**对照**端点不一致：同样失败
    rows = _spec_rows()
    for row in rows:
        if row["case"] == "network_loopback_probe":
            row["network_control"] = {"argv": row["argv"], "host": "127.0.0.1",
                                      "port": port + 1, "reachable": True,
                                      "evidence": "CONTROL_OK", "exit_code": 0}
            _mutate(rows, "network_loopback_probe",
                    network_control={"argv": row["argv"], "host": "127.0.0.1", "port": port + 1,
                                     "reachable": True, "evidence": "CONTROL_OK",
                                     "exit_code": 0})
    verdict = _judge(rows)
    assert verdict[0] is False
    assert any("端点核对失败" in item for item in verdict[1]), verdict[1]

    # 非法端口：明确失败（合同或端点判据任一命中）且不抛异常
    rows = _spec_rows()
    bad = ["/usr/bin/curl", "-s", "-m", "5", "-o", "/dev/null", "-w",
           "http_code=%{http_code} time_total=%{time_total}", "http://127.0.0.1:not-a-port/"]
    for row in rows:
        if row["case"] == "network_loopback_probe":
            row["argv"] = bad
            row["outside_control"] = {"argv": list(bad), "exit_code": 0, "reachable": True,
                                      "evidence": "CONTROL_OK", "stdout": "", "stderr": ""}
    try:
        verdict = _judge(rows)
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"非法端口导致异常：{type(exc).__name__}: {exc}")
    assert verdict[0] is False
    assert any("可信探针合同" in item or "端点核对失败" in item
               or "可信用例规格不一致" in item for item in verdict[1]), verdict[1]


def test_command_identity_is_bound_to_trusted_context() -> None:
    """命令身份：替换成别的用例的 argv（即使重算摘要）必须失败；合法动态路径仍可用。"""
    from app.agents.codex import instantiate_spec, CASE_SPEC_BY_NAME

    rows = _spec_rows()
    other = next(row for row in rows if row["case"] == "read_inside_workspace")["argv"]
    _mutate(rows, "read_inside_sensitive_env", argv=list(other))
    verdict = _judge(rows)
    assert verdict[0] is False
    assert any("可信用例规格不一致" in item for item in verdict[1]), verdict[1]

    # 缺少可信上下文 → 证据不足（不得退回只检查标签）
    rows = _spec_rows()
    for row in rows:
        row.pop("trusted_context", None)
    verdict = _judge(rows)
    assert verdict[0] is False
    assert any("可信上下文" in item for item in verdict[1]), verdict[1]

    # 合法动态路径必须保持可用：换一份**上下文**（本次临时目录）并同步 argv → 通过
    rows = _spec_rows()
    other_context = dict(TRUSTED_CONTEXT, workspace="/tmp/qq-ws2", outside="/tmp/qq-out2",
                         sensitive_file="/tmp/qq-out2/sensitive.env", loopback_port=51999)
    for row in rows:
        if row["case"] == "side_effect_check":
            row["trusted_context"] = dict(other_context)
            continue
        spec = CASE_SPEC_BY_NAME[row["case"]]
        row["trusted_context"] = dict(other_context)
        row["argv"] = instantiate_spec(spec, other_context)
    for row in rows:
        if row["case"] == "network_loopback_probe":
            row["endpoint"] = {"host": "127.0.0.1", "port": 51999}
            row["outside_control"] = {"argv": row["argv"], "exit_code": 0, "reachable": True,
                                      "evidence": "CONTROL_OK", "stdout": "succeeded",
                                      "stderr": ""}
            row["network_control"] = {"argv": row["argv"], "host": "127.0.0.1", "port": 51999,
                                      "reachable": True, "evidence": "CONTROL_OK",
                                      "exit_code": 0}
            row["control_allowed"] = True
    from app.agents.codex import case_digest
    for row in rows:
        row["case_digest"] = case_digest(row)
    verdict = _judge(rows)
    assert verdict[0] is True, verdict[1]


def test_probe_command_contract_is_closed() -> None:
    """未知选项、缺值、多余参数、其它工具都必须在端点核对阶段失败。"""
    port = 50999
    for bad_argv in (
        ["/usr/bin/nc", "--qq-invalid-option", "-z", "-G", "3", "127.0.0.1", str(port)],
        ["/usr/bin/nc", "-z", "-G", "3", "127.0.0.1"],
        ["/usr/bin/nc", "-z", "-G", "3", "127.0.0.1", str(port), "extra"],
        ["/bin/cat", "/etc/hosts"],
        ["/bin/sh", "-c", "true"],
    ):
        rows = _spec_rows()
        _mutate(rows, "network_loopback_probe", argv=bad_argv, exit_code=1,
                stderr="", quick_failure=True,
                outside_control={"argv": list(bad_argv), "exit_code": 1, "reachable": False,
                                 "evidence": "CONTROL_FAIL", "stdout": "", "stderr": ""},
                control_allowed=False)
        verdict = _judge(rows)
        assert verdict[0] is False, (bad_argv, verdict[1])
        assert any("可信探针合同" in item or "端点核对失败" in item
                   or "可信用例规格不一致" in item for item in verdict[1]), \
            (bad_argv, verdict[1])


def test_command_self_failure_is_not_policy_denial() -> None:
    """命令自身错误（真实的未知选项）不能被算作禁网。"""
    from app.agents.codex import check_probe_contract

    bad = ["/usr/bin/nc", "--qq-invalid-option", "-z", "-G", "3", "127.0.0.1", "50999"]
    endpoint, reason = check_probe_contract(bad)
    assert endpoint is None and reason
    rows = _spec_rows()
    _mutate(rows, "network_loopback_probe", argv=bad, exit_code=1, stderr="", quick_failure=True)
    assert _judge(rows)[0] is False


def test_external_observation_never_proves_network_denial() -> None:
    rows = _spec_rows()
    _mutate(rows, "network_external_probe", exit_code=6, stdout="http_code=000",
            stderr="curl: (6) Could not resolve host", quick_failure=False,
            control_allowed=False,
            network_control={"host": "1.1.1.1", "port": 443, "reachable": False,
                             "evidence": "CONTROL_FAIL", "exit_code": 1})
    verdict = _judge(rows)
    assert verdict[0] is True, verdict[1]
    assert any("辅助观测" in item for item in verdict.notes), verdict.notes


def test_external_cannot_be_promoted_to_strict() -> None:
    rows = _spec_rows()
    _mutate(rows, "network_external_probe", verdict_mode="require")
    verdict = _judge(rows)
    assert verdict[0] is False
    assert any("observe" in item for item in verdict[1]), verdict[1]


# ---------- 控制项与副作用 ----------


def test_control_only_case_is_the_declared_one() -> None:
    """只有规格声明的控制项可用控制判据；其它拒绝项加豁免即失败。"""
    from app.agents.codex import CONTROL_ONLY_CASES

    assert CONTROL_ONLY_CASES == frozenset({"read_missing_path_control"})
    rows = _spec_rows()
    _mutate(rows, "read_missing_path_control", exit_code=1)
    assert _judge(rows)[0] is True
    rows = _spec_rows()
    _mutate(rows, "read_missing_path_control", exit_code="x")
    assert _judge(rows)[0] is False


def test_side_effect_states_are_checked() -> None:
    for state in ("write_target_unchanged", "no_outside_file", "no_tmp_file",
                  "inside_readable", "inside_sensitive_intact", "inside_credentials_intact"):
        rows = _spec_rows()
        _mutate(rows, "side_effect_check", recompute=False, **{state: False})
        verdict = _judge(rows)
        assert verdict[0] is False, (state, verdict[1])
        assert any(state in item for item in verdict[1]), (state, verdict[1])


# ---------- CLI 分类与环境 ----------


def test_cli_failure_is_not_permission_denial() -> None:
    from app.agents.codex import classify_contract_row

    assert classify_contract_row(
        {"case": "write_denied", "expect_allowed": False, "exit_code": 101,
         "stderr": "CLI panic", "timed_out": False}) == "cli_failure"
    assert classify_contract_row(
        {"case": "write_denied", "expect_allowed": False, "exit_code": 2,
         "stderr": "Error: filesystem path `.env` must be absolute"}) == "cli_failure"
    assert classify_contract_row(
        {"case": "write_denied", "expect_allowed": False, "exit_code": -9,
         "stderr": ""}) == "signal_exit"
    assert classify_contract_row(
        {"case": "write_denied", "expect_allowed": False, "exit_code": None,
         "stderr": ""}) == "missing_exit_code"
    assert classify_contract_row(
        {"case": "write_denied", "expect_allowed": False, "exit_code": None,
         "stderr": "timeout", "timed_out": True}) == "timeout"
    assert classify_contract_row(
        {"case": "write_denied", "expect_allowed": False, "exit_code": 1,
         "stderr": ""}) == "denied_without_evidence"
    assert classify_contract_row(
        {"case": "write_denied", "expect_allowed": False, "exit_code": 1,
         "stderr": "cat: /x: Operation not permitted"}) == "denied_ok"


def test_cli_env_is_allowlisted_and_drops_invalid_utf8(monkeypatch) -> None:
    from app.agents.codex import CLI_ENV_ALLOWLIST, sanitized_cli_env

    monkeypatch.setenv("QQ_SUPER_SECRET_TOKEN", "must-not-leak")
    bad = "bad" + chr(0xDCFF) + chr(0xDCFE) + "sequence"
    monkeypatch.setenv("_", bad)
    env = sanitized_cli_env()
    assert "QQ_SUPER_SECRET_TOKEN" not in env
    assert "_" not in env
    assert set(env) <= set(CLI_ENV_ALLOWLIST)
    assert env.get("LANG")
    env = sanitized_cli_env({"EXTRA_OK": "值", "EXTRA_BAD": "x" + chr(0xDCFF)})
    assert env["EXTRA_OK"] == "值" and "EXTRA_BAD" not in env


# ---------- 脚本与归档保护 ----------


def test_live_scripts_are_importable_and_expose_out_argument() -> None:
    for name in ("verify_codex_boundary.py", "verify_zcode_live.py", "verify_http_live.py"):
        namespace = runpy.run_path(str(ROOT / "scripts" / name))
        signature = inspect.signature(namespace["main"])
        assert "out" in signature.parameters, name
        assert "DEFAULT_OUT" in namespace, name


def test_http_live_verdict_separates_transport_and_compliance() -> None:
    text = (ROOT / "scripts/verify_http_live.py").read_text(encoding="utf-8")
    assert '"scope": "传输/持久化判据（不含模型是否服从标记指令）"' in text
    assert 'rows["compliance"]' in text
    assert '"passed": not problems' in text


def test_live_scripts_refuse_to_overwrite_archived_evidence_under_pytest(tmp_path) -> None:
    for name, default_name in (
        ("verify_codex_boundary.py", "DEFAULT_OUT"),
        ("verify_zcode_live.py", "DEFAULT_OUT"),
        ("verify_http_live.py", "DEFAULT_OUT"),
    ):
        namespace = runpy.run_path(str(ROOT / "scripts" / name))
        default = namespace[default_name]
        assert namespace["_under_pytest"]() is True, name
        assert namespace["_resolve_out"](None, default, name=name) is None, name
        explicit = tmp_path / f"{name}.json"
        assert namespace["_resolve_out"](explicit, default, name=name) == explicit, name
        assert str(default).startswith(str(ROOT / "outputs")), (name, default)


def test_matrix_script_default_output_is_current_round() -> None:
    """默认输出必须落在**当前**整改目录，不能覆盖旧归档。"""
    text = (ROOT / "scripts/verify_codex_sandbox_matrix.py").read_text(encoding="utf-8")
    assert "outputs/嵌套对照schema收尾/" in text, "默认输出未指向当前整改目录"
    s63 = (ROOT / "scripts/verify_s63_network_and_env.py").read_text(encoding="utf-8")
    assert "outputs/嵌套对照schema收尾" in s63
    closeout = (ROOT / "scripts/verify_unified_closeout.py").read_text(encoding="utf-8")
    assert "outputs/嵌套对照schema收尾" in closeout


def test_archived_round5_evidence_is_preserved() -> None:
    path = ROOT / "outputs/验收复现/Codex真实边界验证.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert "verdict" not in payload, "第五轮证据被本轮结构覆盖了"
    assert payload.get("full") is True
    assert [row.get("scenario") for row in payload["results"]] == [
        "broker-boundary", "broker-read", "broker-edit", "broker-exec",
    ]
    assert payload["cli"].get("probed") is True
