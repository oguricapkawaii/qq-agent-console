"""`/team` 解析器测试（统一公共选项契约）。

覆盖：
- 固定子命令与 ID 校验；
- `add` 与 `/task` 共用一份公共选项解析：--backend/--model/--effort/--mode/--dir/--find
  以及 team 专属的 --after；
- 正文原文保留（中文、引号、换行、以 -- 开头的内容都不再被解释）；
- 重复/未知/缺值/非法枚举、`--dir`+`--find` 冲突、`--mode text`+`--dir` 一律拒绝；
- 解析结果不执行任何 Shell，也不授予任何权限（只返回数据结构）。
"""

from __future__ import annotations

import pytest

from app.workflows.parser import parse_team_command, split_body

TEAM = "t7a91c20d"


def parse(args: str, **kwargs):
    return parse_team_command(args, max_prompt_chars=kwargs.pop("max_prompt_chars", 12000), **kwargs)


def test_create_keeps_goal_text():
    command = parse("create 为我的网站制定上线计划")
    assert command.action == "create"
    assert command.goal == "为我的网站制定上线计划"


def test_create_rejects_empty_and_overlong():
    with pytest.raises(ValueError):
        parse("create")
    with pytest.raises(ValueError):
        parse("create " + "长" * 11, max_prompt_chars=10)


def test_unknown_subcommand_and_empty_args():
    with pytest.raises(ValueError):
        parse("")
    with pytest.raises(ValueError):
        parse("launch " + TEAM)


# ---------- add：公共选项（与 /task 同一份解析器） ----------


def test_add_full_options():
    command = parse(
        f"add {TEAM} research deepseek --after a,b --backend b1 --model deepseek-chat "
        f"--effort high --mode read --dir \"/tmp/项目 一\" -- 分析需求"
    )
    assert (command.team_id, command.step_id, command.agent) == (TEAM, "research", "deepseek")
    assert command.after == ("a", "b")
    assert command.backend == "b1"
    assert command.model == "deepseek-chat"
    assert command.effort == "high"
    assert command.effort_set is True
    assert command.mode == "read"
    assert command.directory == "/tmp/项目 一"
    assert command.find is None
    assert command.instruction == "分析需求"
    assert command.has_capability_request is True


def test_add_default_is_text_without_capability_request():
    command = parse(f"add {TEAM} step glm -- 纯文本任务")
    assert command.mode is None and command.directory is None and command.find is None
    assert command.backend is None
    # 未声明能力 = 纯文本；解析器本身不授予任何权限
    assert command.has_capability_request is False


def test_add_find_and_backend_are_parsed_but_not_executed():
    command = parse(f"add {TEAM} step glm --backend alt --find \"记账 项目\" -- 看看")
    assert command.backend == "alt"
    assert command.find == "记账 项目"
    assert command.directory is None
    assert command.mode is None  # 由 service 依规格推导为 read


def test_add_mode_default_and_equals_form():
    command = parse(f"add {TEAM} step glm --mode=exec --model=glm-4 --effort=low --dir=/tmp -- 任务")
    assert command.mode == "exec"
    assert command.model == "glm-4"
    assert command.effort == "low"
    assert command.directory == "/tmp"


def test_add_preserves_body_newlines_quotes_and_dashes():
    body = '第一行 "引号" 与 \'单引号\'\n第二行\n-- 不是选项\n/team add 也不是命令\n--mode exec 也不是选项'
    command = parse(f"add {TEAM} final deepseek -- {body}")
    assert command.instruction == body
    # 正文里的能力声明绝不被解释成选项
    assert command.mode is None and command.directory is None


def test_split_body_uses_first_standalone_separator():
    header, body = split_body(f"{TEAM} step glm --effort high -- 正文 -- 继续")
    assert header.strip() == f"{TEAM} step glm --effort high"
    # 只切第一处独立 `--`；正文里的 `--` 原样保留
    assert body.strip() == "正文 -- 继续"
    assert "-- 继续" in body


def test_add_effort_default_is_explicit():
    command = parse(f"add {TEAM} step glm --effort default -- 任务")
    assert command.effort_set is True
    assert command.effort is None


def test_add_without_effort_is_not_explicit():
    command = parse(f"add {TEAM} step glm -- 任务")
    assert command.effort_set is False


def test_add_after_deduplicates_but_keeps_order():
    command = parse(f"add {TEAM} final glm --after b,a,b -- 汇总")
    assert command.after == ("b", "a")


def test_add_after_accepts_full_width_comma():
    command = parse(f"add {TEAM} final glm --after b，a -- 汇总")
    assert command.after == ("b", "a")


def test_add_rejects_overlong_body():
    with pytest.raises(ValueError):
        parse(f"add {TEAM} step glm -- " + "长" * 11, max_prompt_chars=10)


@pytest.mark.parametrize(
    "args",
    [
        f"add {TEAM} step glm 缺少分隔符",
        f"add {TEAM} step glm --workspace -- 任务",
        f"add {TEAM} step glm --shell -- 任务",
        f"add {TEAM} step glm --unknown x -- 任务",
        f"add {TEAM} step glm --model a --model b -- 任务",
        f"add {TEAM} step glm --dir /a --dir /b -- 任务",
        # 冲突与语义矛盾：text 模式不接受目录，dir/find 只能二选一
        f"add {TEAM} step glm --dir /a --find b -- 任务",
        f"add {TEAM} step glm --mode text --dir /a -- 任务",
        f"add {TEAM} step glm --mode text --find b -- 任务",
        # 非法枚举
        f"add {TEAM} step glm --mode bogus -- 任务",
        # 缺值 / 空值 / 未闭合引号
        f"add {TEAM} step glm --dir -- 任务",
        f"add {TEAM} step glm --dir \"\" -- 任务",
        f"add {TEAM} step glm --model -- 任务",
        f"add {TEAM} step glm --dir \"未闭合 -- 任务",
        f"add {TEAM} step glm --after -- 任务",
        f"add {TEAM} step glm --after a,,b -- 任务",
        f"add {TEAM} step glm --after \"\" -- 任务",
        # ID / 结构错误
        f"add {TEAM} Step glm -- 任务",
        f"add bad-id step glm -- 任务",
        f"add {TEAM} 1step glm -- 任务",
        f"add {TEAM} step glm -- ",
        f"add {TEAM} step -- 任务",
        f"add {TEAM} {TEAM} glm -- 任务",
        "add",
    ],
)
def test_add_rejects_invalid_input(args):
    with pytest.raises(ValueError):
        parse(args)


def test_add_after_items_are_split_deduped_and_validated():
    """--after 做切分、去重与**步骤 ID 形态校验**（'BAD' 这类大写名在解析期就被拒绝）。"""
    command = parse(f"add {TEAM} final glm --after ok,ok,step-2 -- 汇总")
    assert command.after == ("ok", "step-2")
    with pytest.raises(ValueError) as excinfo:
        parse(f"add {TEAM} final glm --after BAD -- 汇总")
    assert "--after" in str(excinfo.value) and "BAD" in str(excinfo.value)


@pytest.mark.parametrize(
    "args,needle",
    [
        (f"add {TEAM} step glm --unknown x -- 任务", "--unknown"),
        (f"add {TEAM} step glm --model a --model b -- 任务", "重复"),
        (f"add {TEAM} step glm --dir -- 任务", "缺少值"),
        (f"add {TEAM} step glm --dir /a --find b -- 任务", "只能二选一"),
        (f"add {TEAM} step glm --mode text --dir /a -- 任务", "--mode text"),
        (f"add {TEAM} step glm --mode bogus -- 任务", "text|read|edit|exec"),
    ],
)
def test_add_error_messages_are_actionable(args, needle):
    with pytest.raises(ValueError) as excinfo:
        parse(args)
    assert needle in str(excinfo.value)


# ---------- remove / 生命周期 / result ----------


def test_remove_parsing_and_validation():
    command = parse(f"remove {TEAM} research")
    assert (command.action, command.team_id, command.step_id) == ("remove", TEAM, "research")
    for bad in ("remove", f"remove {TEAM}", f"remove {TEAM} A", f"remove bad-id research"):
        with pytest.raises(ValueError):
            parse(bad)


def test_lifecycle_subcommands_need_valid_team_id():
    for action in ("plan", "run", "cancel", "retry"):
        command = parse(f"{action} {TEAM}")
        assert command.action == action and command.team_id == TEAM
        with pytest.raises(ValueError):
            parse(action)
        with pytest.raises(ValueError):
            parse(f"{action} not-a-team")
        with pytest.raises(ValueError):
            parse(f"{action} {TEAM} extra")


def test_status_without_id_lists_recent():
    command = parse("status")
    assert command.action == "status" and command.team_id == ""
    assert parse(f"status {TEAM}").team_id == TEAM


def test_result_with_optional_step():
    command = parse(f"result {TEAM}")
    assert command.action == "result" and command.result_step == ""
    command = parse(f"result {TEAM} final")
    assert command.result_step == "final"
    for bad in ("result", f"result {TEAM} a b", f"result {TEAM} A"):
        with pytest.raises(ValueError):
            parse(bad)


def test_usage_mentions_capability_options():
    # 缺少 `--` 分隔符或未知子命令时回执完整用法（含能力选项），便于用户自助修正
    with pytest.raises(ValueError) as excinfo:
        parse(f"add {TEAM} step glm 缺少分隔符")
    text = str(excinfo.value)
    assert "--mode" in text and "--dir" in text and "--after" in text
    with pytest.raises(ValueError) as excinfo:
        parse("frobnicate")
    assert "--mode" in str(excinfo.value)
