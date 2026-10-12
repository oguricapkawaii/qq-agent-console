#!/usr/bin/env python
"""重新生成第三轮整改的证据文件（验收方可原样重跑）。

用法：
    .venv/bin/python scripts/collect_round3_evidence.py

产出（覆盖写）：
    outputs/验收复现/第三轮整改结果.txt   完整命令输出快照（含每条命令的 returncode）
    outputs/验收复现/第三轮源码指纹.json  本轮源码/测试指纹 + 结果摘要 + skip 计数

退出码（可用于自动放行判断）：
    0 = 全部必需测试通过，且没有 skip；
    1 = 有必需测试失败/出错，或必需测试被 skip（**跳过不等于隔离通过**）；
    2 = 环境问题（找不到解释器 / 指纹命令失败）。

设计原则：只运行本地离线测试，不调用真实模型、不发送 QQ 消息、不改生产配置。
沙箱用例需要 macOS 的 `sandbox-exec`；不可用时它们会 skip——脚本会把 skip 记为未通过原因，
而不是把"跳过"当成"隔离通过"。
"""

from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "outputs/验收复现/第三轮整改结果.txt"
FINGERPRINTS = ROOT / "outputs/验收复现/第三轮源码指纹.json"
PYTHON = str(ROOT / ".venv/bin/python")
FINGERPRINT_CMD = (
    "find {root} -name '*.py' -not -path '*__pycache__*' | sort "
    "| xargs shasum -a 256 | shasum -a 256"
)

# 必需测试集：(标题, argv, 说明)。任何一条失败/出错/skip 都会让脚本非零退出。
REQUIRED: tuple[tuple[str, list[str], str], ...] = (
    (
        "完整回归",
        [PYTHON, "-m", "pytest", "-q"],
        "tests/ 全部离线用例（`testpaths = [\"tests\"]`；含 team、HTTP、agent、沙箱专项）",
    ),
    (
        "第一二轮 Codex 独立复现（断言未修改）",
        [PYTHON, "-m", "pytest", "-q",
         "outputs/验收复现/test_independent_acceptance.py",
         "outputs/验收复现/test_round2_boundaries.py",
         "--tb=short"],
        "第一轮 16 项 + 第二轮 8 项",
    ),
    (
        "第三轮 Codex 独立复现（断言未修改）",
        [PYTHON, "-m", "pytest", "-q",
         "outputs/验收复现/test_round3_independent.py", "--tb=short"],
        "T01/T02/T03 的 4 项反例",
    ),
    (
        "本轮新增边界与正向用例",
        [PYTHON, "-m", "pytest", "-q", "tests/test_round3_boundaries.py", "-v"],
        "R01–R06 负向 + 正向 + 守门用例",
    ),
    (
        "team 集成用例（真实 team 运行）",
        [PYTHON, "-m", "pytest", "-q", "tests/test_team_exec_boundaries.py", "-v"],
        "父 exec/子只读、用户 text 覆盖、protected exec、部分读取 retry",
    ),
)

_SUMMARY = re.compile(r"(\d+)\s+(passed|failed|error|errors|skipped|deselected)")


def run(argv: list[str]) -> tuple[int, str, dict[str, int]]:
    """运行一条命令，返回 (returncode, 合并输出, pytest 计数)。"""
    completed = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, check=False)
    output = (completed.stdout or "") + (completed.stderr or "")
    counts: dict[str, int] = {}
    for line in output.splitlines():
        if not line.strip():
            continue
        if not any(word in line for word in ("passed", "failed", "error", "skipped")):
            continue
        for number, kind in _SUMMARY.findall(line):
            counts[kind.rstrip("s")] = int(number)
    return completed.returncode, output, counts


def fingerprint(root: str) -> str:
    out = subprocess.run(
        FINGERPRINT_CMD.format(root=root), shell=True, cwd=ROOT,
        capture_output=True, text=True, check=True,
    )
    return out.stdout.split()[0]


def main() -> int:
    if not Path(PYTHON).is_file():
        print(f"找不到解释器：{PYTHON}", file=sys.stderr)
        return 2
    status = run([PYTHON, "-c",
                  "from app.security.sandbox import self_check; print(self_check())"])[1].strip()
    sandbox_ok = "可用" in status

    body = [
        "第三轮整改测试结果（R01–R06 + T01–T03）",
        f"生成时间：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "生成脚本：scripts/collect_round3_evidence.py"
        "（可原样重跑；必需测试失败或被 skip 时非零退出）",
        f"解释器：{sys.version.split()[0]}  平台：{os.uname().sysname} {os.uname().machine}",
        f"sandbox-exec 探测：{status}",
        "",
        "说明：本文件保存每条命令的**退出码与完整输出**。`skipped` 表示该用例在当前平台",
        "未真正运行（例如没有 Seatbelt）；脚本会把它算作未通过原因——跳过不等于隔离通过。",
    ]
    summary: dict[str, object] = {}
    failures: list[str] = []
    for title, argv, note in REQUIRED:
        code, output, counts = run(argv)
        skipped = int(counts.get("skipped", 0))
        failed = int(counts.get("failed", 0)) + int(counts.get("error", 0))
        body += [
            "",
            "=" * 72,
            f"$ {' '.join(argv)}",
            f"# {title}（{note}）→ exit={code}"
            + (f"；failed/error={failed}" if failed else "")
            + (f"；skipped={skipped}" if skipped else ""),
            "=" * 72,
            output.rstrip(),
        ]
        summary[title] = {
            "exit_code": code,
            "passed": int(counts.get("passed", 0)),
            "failed": failed,
            "skipped": skipped,
            "command": " ".join(argv),
        }
        if code != 0 or failed:
            failures.append(f"{title}：exit={code}，failed/error={failed}")
        if skipped:
            failures.append(f"{title}：有 {skipped} 项被 skip（未真正运行，不能算隔离通过）")

    body += [
        "",
        "=" * 72,
        "[沙箱策略自证] 实际生成的 Seatbelt profile（放行/拒绝规则）",
        "=" * 72,
        run([PYTHON, "-c", _PROFILE_SNIPPET])[1].rstrip(),
        "",
        "=" * 72,
        "[敏感清单] 与 broker 共用的 seatbelt_patterns()",
        "=" * 72,
        run([PYTHON, "-c",
             "from app.security.paths import seatbelt_patterns\n"
             "print(len(seatbelt_patterns()))\n"
             "print('\\n'.join(seatbelt_patterns()))"])[1].rstrip(),
    ]
    if not sandbox_ok:
        failures.append(f"本机 sandbox-exec 不可用：{status}")

    body += [
        "",
        "=" * 72,
        "[结论]",
        "=" * 72,
        ("全部必需测试通过，且没有 skip。" if not failures
         else "存在未通过项：\n" + "\n".join(f"· {item}" for item in failures)),
    ]
    RESULTS.write_text("\n".join(body).rstrip() + "\n", encoding="utf-8")

    data = {
        "生成时间": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "说明": (
            "第三轮整改（R01-R06 + T01-T03）结束时的源码指纹与结果摘要；"
            "与 outputs/统一控制台测试报告.md、outputs/统一控制台最终架构.md、"
            "outputs/统一控制台实施记录.md 头部一致"
        ),
        "命令": FINGERPRINT_CMD.format(root="<dir>"),
        "app_fingerprint": fingerprint("app"),
        "tests_fingerprint": fingerprint("tests"),
        "sandbox_available": sandbox_ok,
        "sandbox_status": status,
        "结果": summary,
        "未通过项": failures,
        "结论": "通过" if not failures else "存在未通过项，见 未通过项",
    }
    FINGERPRINTS.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    print(json.dumps(data, ensure_ascii=False, indent=2))
    if failures:
        print("未通过项：\n" + "\n".join(f"· {item}" for item in failures), file=sys.stderr)
        return 1
    return 0


_PROFILE_SNIPPET = """
import os, sys, tempfile
from pathlib import Path
from app.security.sandbox import prepare_plan, write_profile
d = Path(os.path.realpath(tempfile.mkdtemp()))
root = d / 'workspace'; root.mkdir()
tmp = d / 'tmp'; tmp.mkdir()
readonly = root / 'readonly'; readonly.mkdir()
plan = prepare_plan(workspace=root, temp_dir=tmp, executable=sys.executable,
                    protected=(root / 'protected',),
                    path_rules=((readonly, True, False),))
profile = write_profile(plan, tmp)
lines = profile.read_text().splitlines()
print(f'profile 行数：{len(lines)}')
print('--- 读取放行（含解释器最小例外）---')
for line in lines:
    if 'allow file-read-data (subpath' in line:
        print(' ', line)
print('--- 读取拒绝（受保护/只读子树，不含敏感正则）---')
for line in lines:
    if 'deny file-read-data (subpath' in line:
        print(' ', line)
print('--- 写入放行/收回 ---')
for line in lines:
    if 'allow file-write* (subpath' in line or 'deny file-write* (subpath' in line:
        print(' ', line)
print('--- 末行 ---')
print(' ', lines[-1])
"""


if __name__ == "__main__":
    raise SystemExit(main())
