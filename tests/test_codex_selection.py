import asyncio
import json
import signal
from pathlib import Path
import pytest
from app.agents.codex import (
    BROKER_DISABLE_CANDIDATES,
    READ_ONLY_PROFILE,
    broker_session_view,
    sealed_read_only_permission_config,
    CodexAdapter,
    CodexCapabilities,
    CodexEvents,
    parse_features,
    permission_config,
    read_only_permission_config,
    redact_text,
)
from app.models import ChatMessage, InvocationOptions


def adapter(tmp_path):
    cache = tmp_path / 'models.json'
    cache.write_text(json.dumps({'models': [{'slug': 'test-model', 'supported_reasoning_levels': [{'effort': 'high'}]}]}))
    agent = CodexAdapter(True, binary='true', workdir=str(tmp_path), cache_path=str(cache), allowed_roots=[str(tmp_path)])
    agent._login_present = True
    return agent


def caps(features, version='codex-cli test', error=''):
    """构造一个已探测（或探测失败）的能力结果，避免测试真的去跑 CLI。"""
    return CodexCapabilities(
        binary='true', version=version, features=frozenset(features),
        probed=not error, error=error,
    )


def probed_adapter(tmp_path, features=None):
    agent = adapter(tmp_path)
    agent._capabilities = caps(BROKER_DISABLE_CANDIDATES if features is None else features)
    return agent


class Writer:
    def __init__(self): self.data = b''
    def write(self, data): self.data += data
    async def drain(self): pass
    def close(self): pass


class Process:
    pid = 123456
    # 真实路径由 spawn_owned() 标记；这里显式声明"该组归本次调用所有"，
    # 以便测试走到 killpg 分支（R64 的回收逻辑）。
    _qq_owned_group = True

    def __init__(self, events=(), finished=True, stderr=b''):
        self.returncode = 0 if finished else None
        self.stdin = Writer()
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        for event in events:
            self.stdout.feed_data((json.dumps(event)+'\n').encode())
        if finished: self.stdout.feed_eof()
        if stderr:
            self.stderr.feed_data(stderr)
        self.stderr.feed_eof()
    async def wait(self):
        self.returncode = self.returncode if self.returncode is not None else -9
        return self.returncode


def record_killpg(killed: list) -> None:
    """替换 harness_process.killpg：记录"真实杀组"与"存活探测"两类调用。

    R64 之后 kill_process 会先 killpg(SIGKILL) 再在很短窗口内探测组是否清空，
    因此断言应针对"实际发送的信号"，而不是所有调用次数。
    """

    def fake(pgid, sig):
        if sig == 0:
            killed.append(f"probe:{pgid}")
        else:
            killed.append(f"kill:{pgid}:{sig}")

    return fake


def argv_config(args, key: str) -> str:
    """从 argv 里取出实际下发的配置串（完整 `-c key=value` 实参）。"""
    for index, item in enumerate(args):
        if item == "-c" and index + 1 < len(args) and str(args[index + 1]).startswith(key + "="):
            return str(args[index + 1])
    return ""


async def test_fixed_argv_and_stdin(monkeypatch, tmp_path):
    agent = probed_adapter(tmp_path); captured = {}
    process = Process([{'type':'item.completed','item':{'type':'agent_message','text':'answer'}}, {'type':'turn.completed'}])
    async def spawn(*args, **kwargs):
        captured.update(args=args, kwargs=kwargs); return process
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    reply = await agent.chat_with_options([ChatMessage('user', '$(touch BAD); --sandbox danger-full-access')], timeout=1, options=InvocationOptions('test-model','high'))
    assert reply.content == 'answer'
    assert captured['args'][captured['args'].index('--model')+1] == 'test-model'
    assert 'model_reasoning_effort="high"' in captured['args']
    assert b'$(touch BAD)' in process.stdin.data
    assert not any('touch BAD' in arg for arg in captured['args'])
    # S61：broker/text 的 cwd 是**空的会话隔离目录**，不是真实项目目录
    assert captured['kwargs']['cwd'] == str(broker_session_view(str(tmp_path)))
    assert not captured['kwargs']['cwd'].startswith(str(tmp_path))
    assert '--ignore-user-config' in captured['args'] and '--json' in captured['args']
    # V03/S61：封边只读 profile —— 真实工作区不进入可读根，只允许会话隔离目录
    assert not agent.sandbox_policy_blockers(captured['args'])
    assert f'default_permissions="{READ_ONLY_PROFILE}"' in captured['args']
    assert ':workspace_roots"="deny' in argv_config(
        captured['args'], f"permissions.{READ_ONLY_PROFILE}")
    assert 'permissions.qq_files=' not in " ".join(str(a) for a in captured['args'])
    assert '--sandbox' not in captured['args']
    assert 'OPENAI_API_KEY' not in captured['kwargs']['env']


async def test_timeout_kills_process_group(monkeypatch,tmp_path):
    agent=probed_adapter(tmp_path); killed=[]; process=Process(finished=False)
    async def spawn(*args,**kwargs): return process
    monkeypatch.setattr(asyncio,'create_subprocess_exec',spawn)
    monkeypatch.setattr('app.agents.harness_process.killpg', record_killpg(killed))
    reply=await agent.chat_with_options([ChatMessage('user','hi')],timeout=.01,options=InvocationOptions('test-model'))
    assert reply.error=='timeout'
    assert [item for item in killed if item.startswith('kill:')] == [
        f'kill:123456:{signal.SIGKILL}']


async def test_cancellation_cleans_process(monkeypatch,tmp_path):
    agent=probed_adapter(tmp_path); killed=[]; process=Process(finished=False)
    async def spawn(*args,**kwargs): return process
    monkeypatch.setattr(asyncio,'create_subprocess_exec',spawn)
    monkeypatch.setattr('app.agents.harness_process.killpg', record_killpg(killed))
    task=asyncio.create_task(agent.chat([],timeout=30)); await asyncio.sleep(.01); task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert [item for item in killed if item.startswith('kill:')] == [
        f'kill:123456:{signal.SIGKILL}']


async def test_unsupported_options_never_spawn(tmp_path,monkeypatch):
    agent=adapter(tmp_path)
    async def spawn(*args,**kwargs): pytest.fail('must not spawn')
    monkeypatch.setattr(asyncio,'create_subprocess_exec',spawn)
    for option in [InvocationOptions('test-model','ultra'),      # 强度不在真实目录中
                   InvocationOptions('test-model',mode='exec'),  # 后端不支持 exec
                   InvocationOptions('test-model',mode='read')]: # 文件模式缺目录
        with pytest.raises(ValueError): await agent.chat_with_options([],timeout=1,options=option)


async def test_workspace_profile_and_public_summaries(monkeypatch,tmp_path):
    agent=probed_adapter(tmp_path); captured={}
    agent.runtime_options={'native_tools': True}   # F02：原生工具必须显式开启
    process=Process([{'type':'item.completed','item':{'type':'reasoning','text':'公开摘要'}},
        {'type':'item.completed','item':{'type':'file_change','changes':[{'path':'a.txt','kind':'update'}],'status':'completed'}},
        {'type':'item.completed','item':{'type':'agent_message','text':'done'}},{'type':'turn.completed'}])
    async def spawn(*args,**kwargs): captured.update(args=args,kwargs=kwargs); return process
    monkeypatch.setattr(asyncio,'create_subprocess_exec',spawn)
    reply=await agent.chat_with_options([],timeout=1,options=InvocationOptions(
        'test-model',directory=str(tmp_path),mode='read',workspace_tools=True))
    assert reply.thinking=='公开摘要' and 'a.txt' in reply.tool_log[0]
    args=captured['args']
    # V03：原生 read/edit **不再**加载可写 profile（qq_files 只留给原生 exec）
    assert '--sandbox' not in args and 'permissions.qq_files=' not in " ".join(str(a) for a in args)
    assert f'default_permissions="{READ_ONLY_PROFILE}"' in args
    assert argv_config(args, f"permissions.{READ_ONLY_PROFILE}") == \
        read_only_permission_config(Path(captured['kwargs']['cwd']))
    assert not agent.sandbox_policy_blockers(args)
    # native read：可读工作区，但 shell/exec 仍被显式禁用
    assert 'shell_tool' in args and 'unified_exec' in args
    assert captured['kwargs']['cwd']==str(tmp_path)


async def test_broker_mode_disables_native_tools(monkeypatch,tmp_path):
    """F02/V03：公共 broker 模式是硬禁原生工具的文本调用（只读限根、无写 profile）。"""
    agent=probed_adapter(tmp_path); captured={}
    agent.runtime_options={'native_tools': True}
    process=Process([{'type':'item.completed','item':{'type':'agent_message','text':'done'}},
                     {'type':'turn.completed'}])
    async def spawn(*args,**kwargs): captured.update(args=args,kwargs=kwargs); return process
    monkeypatch.setattr(asyncio,'create_subprocess_exec',spawn)
    await agent.chat_with_options([],timeout=1,options=InvocationOptions(
        'test-model',directory=str(tmp_path),mode='read',workspace_tools=False))
    args=captured['args']
    # 文档声称的机制必须真的在 argv 里：只读限根 profile
    assert f'default_permissions="{READ_ONLY_PROFILE}"' in args
    broker_config = argv_config(args, f"permissions.{READ_ONLY_PROFILE}")
    # S61：封边 —— 真实工作区被 deny，只有会话隔离目录可读
    assert ':workspace_roots"="deny' in broker_config
    assert broker_config == sealed_read_only_permission_config(
        str(broker_session_view(str(tmp_path))), denied_roots=(str(tmp_path),))
    # 可写 profile 与 legacy 只读沙箱都不得出现（后者是配置错误 / 会顶掉 profile）
    assert 'permissions.qq_files=' not in " ".join(str(item) for item in args)
    assert '--sandbox' not in args
    assert not any('"."="write"' in str(item) for item in args)
    assert 'shell_tool' in args and 'unified_exec' in args


async def test_probe_failure_refuses_to_start(monkeypatch,tmp_path):
    """V03：能力探测失败时不允许在"未验证的版本"上调用，必须拒绝启动。"""
    agent = adapter(tmp_path)
    agent._capabilities = caps(frozenset(), error='features list 退出码 1')
    async def spawn(*args, **kwargs): pytest.fail('探测失败时不得启动 CLI')
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    reply = await agent.chat_with_options([], timeout=1, options=InvocationOptions('test-model'))
    assert reply.error == 'codex_failed'
    assert '探测失败' in reply.diagnostic and '拒绝' in reply.diagnostic


async def test_sandbox_gate_rejects_incomplete_argv(tmp_path):
    """V03：关键安全开关缺失/与 --sandbox 同传时，策略闸门必须报错。"""
    agent = probed_adapter(tmp_path)
    good, _ = agent.build_argv(
        options=InvocationOptions('test-model'), native=False, directory=str(tmp_path),
    )
    assert agent.sandbox_policy_blockers(good) == []
    assert argv_config(good, f"permissions.{READ_ONLY_PROFILE}")
    assert agent.sandbox_policy_blockers(good + ['--sandbox', 'read-only'])
    for missing in ('default_permissions="qq_ro"', 'permissions.qq_ro={',
                    'approval_policy="never"', 'shell_environment_policy.inherit="none"',
                    'web_search="disabled"'):
        stripped = [item for item in good if not item.startswith(missing)]
        assert len(stripped) < len(good), missing
        assert agent.sandbox_policy_blockers(stripped), missing


def test_events_ignore_unpublished_content():
    events=CodexEvents()
    events.accept({'type':'item.completed','item':{'type':'reasoning','text':'summary','raw_content':'must not archive'}})
    events.accept({'type':'item.completed','item':{'type':'command_execution','command':'cat a','aggregated_output':'secret output','status':'completed'}})
    reply=events.reply(); assert reply.thinking=='summary'
    assert 'must not archive' not in str(reply) and 'secret output' not in str(reply)


def test_directory_quotes_and_symlink_escape(tmp_path):
    from app.commands.options import parse_task_arguments
    from app.commands.registry import CommandRegistry
    parsed=parse_task_arguments('--dir "/some/folder with spaces" --effort high -- 修改文件',
                                spec=CommandRegistry().get('task'))
    assert parsed.directory=='/some/folder with spaces' and parsed.effort=='high' and parsed.body=='修改文件'
    agent=adapter(tmp_path)
    # 相对路径、.. 与凭据/系统目录仍由后端本地兜底拒绝
    with pytest.raises(ValueError): agent.workspace('relative/dir')
    with pytest.raises(ValueError): agent.workspace(str(tmp_path/'../x'))
    with pytest.raises(ValueError): agent.workspace(str(tmp_path/'runtime'))
    with pytest.raises(ValueError): agent.workspace(str(Path.home()))
    # 符号链接被解析为真实路径：目录边界由 ACL 在规范路径上统一判定，
    # 因此 agent.workspace 返回的永远是可以直接拿去判定授权的真实目录。
    (tmp_path/'escape').symlink_to('/etc',target_is_directory=True)
    assert agent.workspace(str(tmp_path/'escape'))==str(Path('/etc').resolve())


# ---------- L01：能力探测与失败诊断 ----------


def test_parse_features_reads_real_cli_table() -> None:
    """`codex features list` 的表格输出必须能被解析（名字在第一列）。"""
    text = (
        "agent_message_board                      under development  false\n"
        "shell_tool                               stable             true\n"
        "file_write                               removed            false\n"
    )
    assert parse_features(text) == frozenset({'agent_message_board', 'shell_tool', 'file_write'})
    assert parse_features('') == frozenset()


async def test_unknown_feature_flags_are_never_sent(monkeypatch, tmp_path):
    """L01 主用例：CLI 不认识的能力名不得进入 argv（否则 CLI 直接退出）。"""
    agent = probed_adapter(tmp_path, features={'shell_tool', 'apps'})
    captured = {}
    process = Process([{'type':'item.completed','item':{'type':'agent_message','text':'ok'}},
                       {'type':'turn.completed'}])
    async def spawn(*args, **kwargs): captured.update(args=args, kwargs=kwargs); return process
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    reply = await agent.chat_with_options([], timeout=1, options=InvocationOptions('test-model'))
    assert reply.error is None
    args = captured['args']
    # 认识的才下发
    assert args[args.index('--disable')+1] == 'shell_tool'
    assert args.count('--disable') == 2
    # 本机 CLI 不存在的名字（历史缺陷里的三个）绝不出现在 argv
    for bogus in ('file_write', 'apply_patch', 'edit_files'):
        assert bogus not in args, bogus
    assert '未下发' in agent.last_diagnostic


async def test_probe_failure_never_invents_feature_switches(monkeypatch, tmp_path):
    """探测失败时"只下发跨版本稳定的命令类开关"这条策略仍在（由闸门用例覆盖启动拒绝）。

    这里直接验证策略函数本身：`_candidate_features` 与 `_ALWAYS_SAFE_DISABLE` 的组合
    不会把本机 CLI 不认识的三个历史名字（file_write/apply_patch/edit_files）下发。
    """
    from app.agents.codex import _ALWAYS_SAFE_DISABLE

    agent = probed_adapter(tmp_path, features=set(_ALWAYS_SAFE_DISABLE))
    argv, unknown = agent.build_argv(
        options=InvocationOptions('test-model'), native=False, directory=str(tmp_path),
    )
    disabled = [argv[i + 1] for i, item in enumerate(argv) if item == '--disable']
    assert disabled == ['shell_tool', 'unified_exec']
    for bogus in ('file_write', 'apply_patch', 'edit_files'):
        assert bogus not in argv, bogus
    assert set(unknown) == set(BROKER_DISABLE_CANDIDATES) - set(_ALWAYS_SAFE_DISABLE)
    assert not agent.sandbox_policy_blockers(argv)


async def test_cli_failure_reports_redacted_stderr(monkeypatch, tmp_path):
    """CLI 失败时把 stderr 摘要（脱敏、有界）带回错误详情，而不是只给 codex_failed。"""
    agent = probed_adapter(tmp_path)
    process = Process(
        [{'type':'item.completed','item':{'type':'error','message':'Unknown feature flag: file_write'}}],
        stderr=b'Error: Unknown feature flag: file_write\napi_key=sk-shouldNotLeak1234\n',
    )
    process.returncode = 1
    async def spawn(*args, **kwargs): return process
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    reply = await agent.chat_with_options([], timeout=1, options=InvocationOptions('test-model'))
    assert reply.error == 'codex_failed'
    assert 'Unknown feature flag' in reply.diagnostic
    assert 'sk-shouldNotLeak' not in reply.diagnostic
    assert 'REDACTED' in reply.diagnostic


def test_capability_report_is_actionable() -> None:
    agent = CodexAdapter(False, binary=None)
    report = agent.capability_report()
    assert report['probed'] is False and report['error']
    assert '探测失败' in report['note']


def test_redact_text_removes_secret_shapes() -> None:
    out = redact_text('token=abcdef0123456789 and sk-abcdefgh12345678')
    assert 'abcdef0123456789' not in out and 'sk-abcdefgh' not in out


# ---------- L04：ZCode 模式化默认强度（离线策略断言） ----------


def synthetic_zcode_catalog(tmp_path: Path) -> Path:
    """写入 schema 有效的合成模型目录，避免依赖生产 data/zcode_models.json。"""
    catalog = tmp_path / "zcode_models.json"
    catalog.write_text(json.dumps({
        "glm-5.3": {
            "model_id": "fixture-model-id",
            "efforts": ["low", "high", "max"],
            "default_effort": "max",
        }
    }), encoding="utf-8")
    return catalog


def test_zcode_mode_efforts_default_to_low_for_tool_modes(tmp_path) -> None:
    """带工具的模式默认 low（max 实测超时），纯文本保留模型目录默认。"""
    from app.agents.zcode import ZCodeAdapter

    catalog = synthetic_zcode_catalog(tmp_path)
    agent = ZCodeAdapter(api_key="", base_url="", enabled=False, catalog=str(catalog))
    assert agent.model_name in agent._catalog  # noqa: SLF001 - 测试内部状态
    assert agent.default_effort_for("read", agent.model_name) == "low"
    assert agent.default_effort_for("edit", agent.model_name) == "low"
    assert agent.default_effort_for("exec", agent.model_name) == "low"
    # 纯文本不被降级：仍用目录里的 default_effort
    assert agent.default_effort_for("text", agent.model_name) == (
        agent._catalog[agent.model_name]["default_effort"]  # noqa: SLF001
    )
    # 合成目录确实被读取（生产目录不参与）
    assert agent.default_effort_for("text", agent.model_name) == "max"


def test_zcode_mode_efforts_can_be_overridden(tmp_path) -> None:
    from app.agents.zcode import ZCodeAdapter

    catalog = synthetic_zcode_catalog(tmp_path)
    agent = ZCodeAdapter(
        api_key="", base_url="", enabled=False, catalog=str(catalog),
        mode_efforts={"edit": "high", "text": "low"},
    )
    assert agent.default_effort_for("edit", agent.model_name) == "high"
    assert agent.default_effort_for("text", agent.model_name) == "low"
    # 未覆盖的模式仍是策略默认
    assert agent.default_effort_for("read", agent.model_name) == "low"
    # 合成目录确实被读取（生产目录不参与）
    assert agent.model_options == {"glm-5.3": ("low", "high", "max")}
