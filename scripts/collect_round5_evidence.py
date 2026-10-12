#!/usr/bin/env python
"""本轮（L01–L04）整改的可重跑证据：每条命令的 argv、exit code、完整输出与计数。

用法：
    .venv/bin/python scripts/collect_round5_evidence.py            # 只跑离线/可离线部分
    .venv/bin/python scripts/collect_round5_evidence.py --live     # 追加真实模型验证（花钱）

产出（覆盖写）：
    outputs/验收复现/第五轮整改结果.txt
    outputs/验收复现/第五轮源码指纹.json

退出码：
    0 = 全部必需项通过且没有 skip；
    1 = 有必需项失败/出错，或必需项被 skip（**跳过不等于隔离通过**）；
    2 = 环境问题（解释器或指纹命令失败）。

说明：
- `--live` 会执行 `scripts/verify_codex_boundary.py --run`、`scripts/verify_http_live.py --run`、
  `scripts/verify_zcode_live.py --run`，产生**真实模型调用**（不发送 QQ 消息、不改生产配置）；
  它们各自的 JSON 证据单独落盘，本文件只记录 argv/exit code/摘要。
- 不记录任何密钥或业务文件内容。
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "outputs/验收复现/第五轮整改结果.txt"
FINGERPRINTS = ROOT / "outputs/验收复现/第五轮源码指纹.json"
PYTHON = str(ROOT / ".venv/bin/python")
FINGERPRINT_CMD = (
    "find {root} -name '*.py' -not -path '*__pycache__*' | sort "
    "| xargs shasum -a 256 | shasum -a 256"
)

OFFLINE_REQUIRED: tuple[tuple[str, list[str], str], ...] = (
    (
        "完整回归（tests/）",
        [PYTHON, "-m", "pytest", "-q"],
        "pyproject.toml 的 testpaths=[tests]；含 team/HTTP/沙箱/Codex 能力探测",
    ),
    (
        "三轮 Codex 独立复现（断言未改）",
        [PYTHON, "-m", "pytest", "-q", "outputs/验收复现/", "--tb=short"],
        "16 + 8 + 4 = 28 项",
    ),
    (
        "L02 真实联调失败复现（断言未改）",
        [PYTHON, "-m", "pytest", "-q", "outputs/验收复现/test_http_result_content.py", "--tb=short"],
        "成功任务的 HTTP 结果正文必须等于假适配器标记",
    ),
    (
        "L02 持久化与各模式正文回归",
        [PYTHON, "-m", "pytest", "-q", "tests/test_http_result_content.py", "-v"],
        "存储 round-trip、重开数据库、text/read/edit/exec、鉴权、幂等",
    ),
    (
        "本轮隔离端到端（新版口令/确认/审批/跨模型 team）",
        [PYTHON, "-m", "pytest", "-q", "tests/test_isolated_e2e_console.py", "-v"],
        "录制传输 + 临时库/目录，不连接真实 QQ",
    ),
    (
        "Codex 能力探测与 argv 策略（离线）",
        [PYTHON, "-m", "pytest", "-q", "tests/test_codex_selection.py", "-v"],
        "未知 feature flag 绝不进入 argv；失败诊断脱敏有界",
    ),
)

LIVE_REQUIRED: tuple[tuple[str, list[str], str], ...] = (
    (
        "L01 真实 Codex 边界（真实模型）",
        [PYTHON, "scripts/verify_codex_boundary.py", "--run", "--effort", "low", "--full"],
        "argv 被真实 CLI 接受 + broker 模式无越界写/无命令副作用 + broker read/edit/exec 正向",
    ),
    (
        "L02 真实本机 TCP HTTP 正文（真实模型）",
        [PYTHON, "scripts/verify_http_live.py", "--run", "--agent", "deepseek"],
        "结果接口 content 等于真实答复且与库内 result_text 一致",
    ),
    (
        "L04 真实 ZCode 强度/预算/取消（真实模型）",
        [PYTHON, "scripts/verify_zcode_live.py", "--run"],
        "默认强度可用 + 预算耗尽即停 + 无孤儿进程",
    ),
)

_SUMMARY = re.compile(r"(\d+)\s+(passed|failed|error|errors|skipped|deselected)")


def run(argv: list[str]) -> tuple[int, str, dict[str, int]]:
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


def main(*, live: bool) -> int:
    if not Path(PYTHON).is_file():
        print(f"找不到解释器：{PYTHON}", file=sys.stderr)
        return 2
    sandbox = run([PYTHON, "-c",
                   "from app.security.sandbox import self_check; print(self_check())"])[1].strip()
    body = [
        "第五轮整改测试结果（L01–L04）",
        f"生成时间：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "生成脚本：scripts/collect_round5_evidence.py"
        "（必需项失败或被 skip 时非零退出）",
        f"解释器：{sys.version.split()[0]}  平台：{os.uname().sysname} {os.uname().machine}",
        f"sandbox-exec：{sandbox}",
        f"真实模型验证：{'包含（--live）' if live else '未包含（加 --live 才执行）'}",
        "",
        "说明：本文件保存每条命令的 argv、exit code 与完整输出；`skipped` 计为未通过原因，",
        "跳过不等于隔离/能力通过。真实模型脚本的详细 JSON 证据在本目录各自的文件里。",
    ]
    summary: dict[str, object] = {}
    failures: list[str] = []
    suite = list(OFFLINE_REQUIRED) + (list(LIVE_REQUIRED) if live else [])
    for title, argv, note in suite:
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
            output.rstrip()[-20000:],
        ]
        summary[title] = {
            "exit_code": code, "passed": int(counts.get("passed", 0)),
            "failed": failed, "skipped": skipped, "command": " ".join(argv),
        }
        if code != 0 or failed:
            failures.append(f"{title}：exit={code}，failed/error={failed}")
        if skipped:
            failures.append(f"{title}：有 {skipped} 项被 skip（未真正运行，不能算通过）")

    body += [
        "",
        "=" * 72,
        "[结论]",
        "=" * 72,
        ("全部必需项通过，且没有 skip。" if not failures
         else "存在未通过项：\n" + "\n".join(f"· {item}" for item in failures)),
    ]
    RESULTS.write_text("\n".join(body).rstrip() + "\n", encoding="utf-8")

    data = {
        "生成时间": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "说明": "第五轮整改（L01–L04）源码/测试指纹与结果摘要",
        "命令": FINGERPRINT_CMD.format(root="<dir>"),
        "app_fingerprint": fingerprint("app"),
        "tests_fingerprint": fingerprint("tests"),
        "sandbox_status": sandbox,
        "live_runs_included": live,
        "结果": summary,
        "未通过项": failures,
        "结论": "通过" if not failures else "存在未通过项，见 未通过项",
        "真实模型验证证据": {
            "codex_boundary": "outputs/验收复现/Codex真实边界验证.json",
            "http_content": "outputs/验收复现/真实HTTP结果正文验证.json",
            "zcode_budget": "outputs/验收复现/真实ZCode强度与预算验证.json",
        },
    }
    FINGERPRINTS.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    print(json.dumps(data, ensure_ascii=False, indent=2))
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
