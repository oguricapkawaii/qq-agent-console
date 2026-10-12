#!/usr/bin/env python
"""第六轮整改（V01–V04）的可重跑证据。

用法：
    .venv/bin/python scripts/collect_round6_evidence.py            # 离线 + 本机沙箱矩阵
    .venv/bin/python scripts/collect_round6_evidence.py --live     # 追加真实模型验证（花钱）

产出（**新文件，不覆盖第五轮证据**）：
    outputs/第六轮整改/第六轮整改结果.txt          每条命令的 argv/exit code/摘要
    outputs/第六轮整改/完整输出/<序号>-<名称>.stdout.txt   每条命令的**完整** stdout
    outputs/第六轮整改/完整输出/<序号>-<名称>.stderr.txt   每条命令的**完整** stderr
    outputs/第六轮整改/第六轮源码指纹.json           app/tests/scripts/config+docs 指纹

退出码：
    0 = 全部必需项通过且没有 skip；
    1 = 有必需项失败/出错、被 skip，或证据判定不足（**跳过不等于通过**）；
    2 = 环境问题（解释器/CLI 缺失、指纹命令失败）。

纪律（对应 V04 与 §6 附项）：
- 每条命令的完整输出独立保存并提供路径与 sha256，摘要才允许截断；
- 指纹覆盖 scripts/config/docs，而不只是 app/tests；
- 真实脚本的 JSON 证据必须存在、`verdict.passed` 为 true，否则本文件判失败；
- 不记录任何密钥或业务文件内容。
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "outputs/嵌套对照schema收尾"
RESULTS = OUT_DIR / "第六轮整改结果.txt"
FINGERPRINTS = OUT_DIR / "第六轮源码指纹.json"
FULL_DIR = OUT_DIR / "完整输出"
PYTHON = str(ROOT / ".venv/bin/python")

# 指纹覆盖范围（§6 附项：scripts/config/docs 必须纳入；本轮交付说明与独立反例也纳入）
FINGERPRINT_TARGETS: dict[str, tuple[str, ...]] = {
    "app": ("app",),
    "tests": ("tests",),
    "scripts": ("scripts",),
    "config_and_docs": ("config.example.yaml", ".env.example", "pyproject.toml", "README.md"),
    # 独立验收报告 §"已接受项与文档问题"：指纹必须覆盖**全部交付文档**，
    # 而不只是 README/配置示例。
    "delivery_docs": (
        "outputs/统一控制台最终架构.md",
        "outputs/统一控制台实施记录.md",
        "outputs/操作指南.md",
        "outputs/统一控制台测试报告.md",
        "outputs/统一控制台交付清单.md",
        "outputs/命令清单.md",
        "outputs/DeepSeek第六轮定向整改交付说明.md",
        "outputs/DeepSeek第六轮复验补充整改交付说明.md",
        "outputs/DeepSeek第六轮补充复验整改交付说明.md",
        "outputs/DeepSeek第六轮补充二次复验整改交付说明.md",
        "outputs/DeepSeek第六轮补充三次复验整改交付说明.md",
        "outputs/DeepSeek第六轮补充四次复验整改交付说明.md",
        "outputs/DeepSeek统一收尾整改交付说明.md",
        "outputs/DeepSeek统一收尾剩余整改交付说明.md",
        "outputs/DeepSeek嵌套对照schema收尾交付说明.md",
        "outputs/第五轮独立验收/test_round5_independent.py",
        "outputs/第六轮独立验收/test_round6_independent.py",
        "outputs/嵌套对照schema收尾/嵌套对照副本/",
        "outputs/嵌套对照schema收尾/固定清单剩余副本/",
        "outputs/嵌套对照schema收尾/补充验收副本/",
        "outputs/嵌套对照schema收尾/上轮反例副本/",
        "outputs/嵌套对照schema收尾/语义反例副本/",
        "outputs/嵌套对照schema收尾/权限矩阵.json",
    ),
}
FINGERPRINT_SUFFIXES = (".py", ".yaml", ".yml", ".toml", ".sh", ".md")

OFFLINE_REQUIRED: tuple[tuple[str, list[str], str], ...] = (
    (
        "完整回归（tests/）",
        [PYTHON, "-m", "pytest", "-q"],
        "pyproject.toml 的 testpaths=[tests]",
    ),
    (
        "第五轮独立反例（断言未改，预期全绿）",
        [PYTHON, "-m", "pytest", "-q", "outputs/第五轮独立验收/test_round5_independent.py",
         "--tb=short"],
        "V01–V04 四条独立反例必须全部转为通过",
    ),
    (
        "嵌套对照schema收尾固定检查（241 项确定性判据）",
        [PYTHON, "scripts/verify_unified_closeout.py"], "六类收尾条件一次跑完",
    ),
    (
        "第六轮补充四次严格探针反例（从副本运行，不写原目录）",
        [PYTHON, "-m", "pytest", "-q",
         "outputs/嵌套对照schema收尾/严格探针副本/", "--tb=short"],
        "可信规格固定期望拒绝 + 探针命令合同封闭 + 非法 URL/端口不崩溃（断言未改）",
    ),
    (
        "第六轮补充三次语义反例（从副本运行，不写原目录）",
        [PYTHON, "-m", "pytest", "-q",
         "outputs/嵌套对照schema收尾/语义反例副本/", "--tb=short"],
        "严格回环不可降级 + 端点逐字段核对 + 缺字段即失败（断言未改）",
    ),
    (
        "第六轮补充二次独立复验（副本，不写原目录）",
        [PYTHON, "-m", "pytest", "-q",
         "outputs/嵌套对照schema收尾/上轮反例副本/",
        "outputs/嵌套对照schema收尾/语义反例副本/", "--tb=short"],
        "S63 端点对照 + S64 组号复用，六条必须通过",
    ),
    (
        "第六轮补充反例（验收方文件，断言未改）",
        [PYTHON, "-m", "pytest", "-q",
         "outputs/嵌套对照schema收尾/补充验收副本/",
        "outputs/嵌套对照schema收尾/上轮反例副本/",
        "outputs/嵌套对照schema收尾/语义反例副本/", "--tb=short"],
        "S61 敏感边界 + S64 所有权 + S63 网络判定，四条必须通过",
    ),
    (
        "第六轮独立反例（验收方文件，断言未改）",
        [PYTHON, "-m", "pytest", "-q", "outputs/第六轮独立验收/test_round6_independent.py",
         "--tb=short"],
        "R61–R64 五条独立反例必须全部转为通过",
    ),
    (
        "第六轮新增定向测试",
        [PYTHON, "-m", "pytest", "-q", "tests/test_operation_result_contract.py",
         "tests/test_stderr_and_cleanup.py", "tests/test_live_verdict_contract.py",
         "--tb=short"],
        "R61 根内敏感拒绝 + R62 模式化判定 + R63 矩阵/环境/判据 + R64 进程组回收",
    ),
    (
        "上轮+本轮独立反例与历史复现（一起重跑）",
        [PYTHON, "-m", "pytest", "-q", "outputs/嵌套对照schema收尾/上轮反例副本/",
         "outputs/嵌套对照schema收尾/语义反例副本/",
         "outputs/嵌套对照schema收尾/补充验收副本/",
         "outputs/第六轮独立验收/", "outputs/第五轮独立验收/",
         "outputs/验收复现/", "--tb=short"],
        "共 48 项：6(二次复验) + 4(补充) + 5(第六轮) + 4(第五轮) + 29(历史)",
    ),
    (
        "历史独立复现（断言未改）",
        [PYTHON, "-m", "pytest", "-q", "outputs/验收复现/", "--tb=short"],
        "第一至第四轮的独立反例仍必须通过",
    ),
    (
        "本轮隔离端到端（含目录替换用例修正）",
        [PYTHON, "-m", "pytest", "-q", "tests/test_isolated_e2e_console.py", "-v"],
        "录制传输 + 临时库/目录，不连接真实 QQ",
    ),
    (
        "Codex 能力/权限策略（离线）",
        [PYTHON, "-m", "pytest", "-q", "tests/test_codex_selection.py", "-v"],
        "只读限根 profile、开关闸门、探测失败拒绝启动",
    ),
)

LOCAL_REQUIRED: tuple[tuple[str, list[str], str], ...] = (
    (
        "S63 端点绑定 + 非法 UTF-8 环境（不调用模型）",
        [PYTHON, "scripts/verify_s63_network_and_env.py",
         "--out-dir", str(OUT_DIR)],
        "同端点对照才成立；端点不一致/无对照/回环未被拒都必须失败；注入坏环境后矩阵仍通过",
    ),
    (
        "S61 封边会话视图证据（不调用模型）",
        [PYTHON, "scripts/verify_s61_sealed_view.py",
         "--json-out", str(OUT_DIR / "verify_s61_sealed_view.py.json")],
        "真实 CLI：深层/新增敏感文件不可读；扫描不完整即整根 deny；正负配对成立",
    ),
    (
        "V03 真实沙箱拒绝矩阵（不调用模型）",
        [PYTHON, "scripts/verify_codex_sandbox_matrix.py",
         "--json-out", str(OUT_DIR / "verify_codex_sandbox_matrix.py.json")],
        "真实 CLI 直接执行命令：合法读成功；越界/敏感读、写入、网络全部被拒",
    ),
)

LIVE_REQUIRED: tuple[tuple[str, list[str], str], ...] = (
    (
        "V03/V04 真实 Codex 边界（真实模型）",
        [PYTHON, "scripts/verify_codex_boundary.py", "--run", "--effort", "low", "--full",
         "--json-out", str(OUT_DIR / "verify_codex_boundary.py.json")],
        "CLI 探测成功 + 负向无副作用且有真实拒绝 + read/edit/exec 正向 ok=true 且有动作/提交证据",
    ),
    (
        "L02 真实本机 TCP HTTP 正文（真实模型）",
        [PYTHON, "scripts/verify_http_live.py", "--run", "--agent", "deepseek",
         "--json-out", str(OUT_DIR / "verify_http_live.py.json")],
        "结果接口 content 等于真实答复且与库内 result_text 一致",
    ),
    (
        "L04/V01 真实 ZCode 强度/预算/取消/编辑判定（真实模型）",
        [PYTHON, "scripts/verify_zcode_live.py", "--run",
         "--json-out", str(OUT_DIR / "verify_zcode_live.py.json")],
        "默认强度可用 + 预算耗尽即停 + 按 PID/进程组核对无残留 + 编辑必须真的落盘",
    ),
)

# 真实脚本对应的 JSON 证据：必须存在且 verdict.passed 为 true
LIVE_EVIDENCE = (
    "verify_s61_sealed_view.py.json",
    "verify_codex_boundary.py.json",
    "verify_zcode_live.py.json",
    "verify_http_live.py.json",
    "verify_codex_sandbox_matrix.py.json",
)

_SUMMARY = re.compile(r"(\d+)\s+(passed|failed|error|errors|skipped|deselected)")


def display_path(path) -> str:
    """项目内路径显示为相对路径；项目外显示绝对路径（不因 relative_to 抛异常）。"""
    try:
        return str(Path(path).resolve().relative_to(ROOT))
    except (ValueError, OSError):
        return str(Path(path).resolve())


def run(argv: list[str], *, index: int, name: str) -> tuple[int, str, dict[str, int]]:
    completed = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, check=False)
    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    # §6 附项：完整输出独立保存（不截断），摘要里只放尾部
    FULL_DIR.mkdir(parents=True, exist_ok=True)
    for stream, text in (("stdout", stdout), ("stderr", stderr)):
        target = FULL_DIR / f"{index:02d}-{name}.{stream}.txt"
        target.write_text(text, encoding="utf-8")
    output = stdout + stderr
    counts: dict[str, int] = {}
    for line in output.splitlines():
        if not line.strip() or not any(
            word in line for word in ("passed", "failed", "error", "skipped")
        ):
            continue
        for number, kind in _SUMMARY.findall(line):
            counts[kind.rstrip("s")] = int(number)
    return completed.returncode, output, counts


def _digest_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fingerprint(entries: tuple[str, ...]) -> dict:
    """按"排序后的相对路径 + 文件内容"计算总指纹（路径也进摘要，避免改名不算变更）。"""
    files: list[Path] = []
    for entry in entries:
        target = ROOT / entry
        if target.is_file():
            files.append(target)
        elif target.is_dir():
            files.extend(
                item for item in target.rglob("*")
                if item.is_file() and item.suffix in FINGERPRINT_SUFFIXES
                and "__pycache__" not in item.parts
            )
    overall = hashlib.sha256()
    per_file: dict[str, str] = {}
    for path in sorted(files):
        relative = display_path(path)
        digest = _digest_file(path)
        per_file[relative] = digest
        overall.update(relative.encode("utf-8"))
        overall.update(b"\0")
        overall.update(digest.encode("ascii"))
        overall.update(b"\n")
    return {"files": len(per_file), "sha256": overall.hexdigest(), "paths": per_file}


def _verdict_of(path: Path) -> tuple[bool, str]:
    """读取真实脚本的 JSON 证据并判断结论（不能只看脚本 exit code）。

    兼容三种证据结构（脚本演进过，旧证据不能因为结构不同就被判失败）：
    1. `verdict.passed` + `problems`（V04 边界脚本 / http 脚本 / 命令级矩阵）；
    2. `verdict.read_only_profile_enforced`（只读矩阵）；
    3. 平铺的 `verdict.{signal: bool}` 信号表 + 顶层 `passed`（ZCode 脚本的旧格式）。
    """
    if not path.is_file():
        return False, "证据文件不存在"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return False, f"证据无法解析：{type(exc).__name__}"
    if not isinstance(payload, dict):
        return False, "证据不是 JSON 对象"
    verdict = payload.get("verdict")
    problems: list[str] = []
    passed: bool | None = None
    if isinstance(verdict, dict):
        if isinstance(verdict.get("passed"), bool):
            passed = bool(verdict["passed"])
            problems = list(verdict.get("problems") or [])
        elif isinstance(verdict.get("read_only_profile_enforced"), bool):
            passed = bool(verdict["read_only_profile_enforced"])
            problems = list(verdict.get("read_only_problems") or [])
        else:
            signals = {key: value for key, value in verdict.items() if isinstance(value, bool)}
            if signals:
                failed = [key for key, value in signals.items() if not value]
                passed = not failed
                problems = [f"{key}=false" for key in failed]
    if passed is None:
        if isinstance(payload.get("passed"), bool):
            passed = bool(payload["passed"])
        else:
            return False, "证据缺少 verdict.passed（无法判定结论）"
    matrix = payload.get("sandbox_policy_matrix")
    if isinstance(matrix, dict) and matrix.get("passed") is not True:
        problems = problems + ["命令级权限矩阵未通过"]
        passed = False
    return bool(passed), ("；".join(str(item) for item in problems[:5]) if problems else "")


def main(*, live: bool) -> int:
    if not Path(PYTHON).is_file():
        print(f"找不到解释器：{PYTHON}", file=sys.stderr)
        return 2
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    sandbox = subprocess.run(
        [PYTHON, "-c", "from app.security.sandbox import self_check; print(self_check())"],
        cwd=ROOT, capture_output=True, text=True, check=False,
    ).stdout.strip()
    body = [
        "第六轮定向整改测试结果（V01–V04）",
        f"生成时间：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "生成脚本：scripts/collect_round6_evidence.py（必需项失败/被 skip/证据判定不足时非零退出）",
        f"解释器：{sys.version.split()[0]}  平台：{os.uname().sysname} {os.uname().machine}",
        f"sandbox-exec：{sandbox}",
        f"真实模型验证：{'包含（--live）' if live else '未包含（加 --live 才执行）'}",
        f"完整输出目录：{display_path(FULL_DIR)}",
        "",
        "说明：每条命令的完整 stdout/stderr 单独保存（路径见每条命令下方），本文件的摘要可能截断；",
        "`skipped` 记为未通过原因；真实脚本的 JSON 证据必须存在且 verdict 为通过。",
    ]
    summary: dict[str, object] = {}
    failures: list[str] = []
    evidence_rows: list[dict] = []
    suite = list(OFFLINE_REQUIRED) + list(LOCAL_REQUIRED) + (
        list(LIVE_REQUIRED) if live else []
    )
    for index, (title, argv, note) in enumerate(suite, start=1):
        slug = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff-]+", "", title.replace("/", "-"))
        code, output, counts = run(argv, index=index, name=slug)
        skipped = int(counts.get("skipped", 0))
        failed = int(counts.get("failed", 0)) + int(counts.get("error", 0))
        stdout_path = FULL_DIR / f"{index:02d}-{slug}.stdout.txt"
        stderr_path = FULL_DIR / f"{index:02d}-{slug}.stderr.txt"
        body += [
            "",
            "=" * 72,
            f"$ {' '.join(argv)}",
            f"# {title}（{note}）→ exit={code}"
            + (f"；failed/error={failed}" if failed else "")
            + (f"；skipped={skipped}" if skipped else ""),
            f"# 完整 stdout：{display_path(stdout_path)} "
            f"sha256={_digest_file(stdout_path)[:16]}…",
            f"# 完整 stderr：{display_path(stderr_path)} "
            f"sha256={_digest_file(stderr_path)[:16]}…",
            "=" * 72,
            output.rstrip()[-20000:],
        ]
        summary[title] = {
            "exit_code": code, "passed": int(counts.get("passed", 0)),
            "failed": failed, "skipped": skipped, "command": " ".join(argv),
            "stdout_path": display_path(stdout_path),
            "stderr_path": display_path(stderr_path),
            "stdout_sha256": _digest_file(stdout_path),
            "stderr_sha256": _digest_file(stderr_path),
            "note": note,
        }
        if code != 0 or failed:
            failures.append(f"{title}：exit={code}，failed/error={failed}")
        if skipped:
            failures.append(f"{title}：有 {skipped} 项被 skip（未真正运行，不能算通过）")

    if live:
        for name in LIVE_EVIDENCE:
            path = OUT_DIR / name
            passed, detail = _verdict_of(path)
            evidence_rows.append({
                "file": display_path(path), "passed": passed, "detail": detail,
                "sha256": _digest_file(path) if path.is_file() else "",
            })
            if not passed:
                failures.append(f"真实证据 {name} 判定未通过：{detail or '见文件'}")

    body += [
        "",
        "=" * 72,
        "[结论]",
        "=" * 72,
        ("全部必需项通过，且没有 skip，真实证据判定通过。" if not failures
         else "存在未通过项：\n" + "\n".join(f"· {item}" for item in failures)),
    ]
    RESULTS.write_text("\n".join(body).rstrip() + "\n", encoding="utf-8")

    data = {
        "生成时间": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "说明": "第六轮定向整改（V01–V04）交付物指纹与结果摘要",
        "完整输出目录": display_path(FULL_DIR),
        "指纹范围": {name: list(entries) for name, entries in FINGERPRINT_TARGETS.items()},
        "指纹": {name: fingerprint(entries) for name, entries in FINGERPRINT_TARGETS.items()},
        "sandbox_status": sandbox,
        "live_runs_included": live,
        "结果": summary,
        "真实证据": evidence_rows,
        "未通过项": failures,
        "结论": "通过" if not failures else "存在未通过项，见 未通过项",
    }
    FINGERPRINTS.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    print(json.dumps({
        "结论": data["结论"],
        "未通过项": failures,
        "指纹": {name: item["sha256"][:16] for name, item in data["指纹"].items()},
    }, ensure_ascii=False, indent=2))
    if failures:
        print("未通过项：\n" + "\n".join(f"· {item}" for item in failures), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true",
                        help="追加真实模型验证（产生费用；不发送 QQ 消息）")
    args = parser.parse_args()
    raise SystemExit(main(live=args.live))
