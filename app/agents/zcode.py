"""安装版 ZCode stdio 协议适配器，独立HOME和provider，不改桌面配置。"""
from __future__ import annotations
import asyncio
import json
import os
import shutil
import tempfile
from pathlib import Path
from app.agents.base import AgentAdapter
from app.agents.harness_process import (
    MAX_OUTPUT_BYTES, conversation_prompt, kill_process, launch_env, mark_owned_group,
)
from app.models import AgentReply, InvocationOptions

DEFAULT_NODE = '/Applications/DeepSeek Harness.app/Contents/Resources/runtime/primary-runtime/dependencies/node/bin/node'
DEFAULT_ENTRY = '/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs'
DEFAULT_BUILTIN = '/Applications/ZCode.app/Contents/Resources/config/provider/zcode-builtin.json'
PROVIDER = 'bigmodel-standard-api'

# 会话累计读取上限。session/read 每次返回全量快照（空会话也有约20KB样板），
# 0.5秒轮询一次长任务会累计几十MB，因此不能复用 harness_process 的单次2MiB上限；
# 单行超长仍由 create_subprocess_exec 的 limit=MAX_OUTPUT_BYTES 防护。
MAX_SESSION_BYTES = 64 * 1024 * 1024

# 文件任务模式的工具白名单：只有读写类工具，不含 Bash/JS/Web 等执行与网络工具。
FILE_TOOLS = ('Read', 'Write', 'Edit', 'Glob', 'Grep')

# 本机器人项目目录（含 .env 密钥、QQ 白名单与数据库）：任何情况下都不允许工具写入。
PROTECTED_DIR = Path(__file__).resolve().parents[2]

# 权限请求 input 中的路径字段（只校验这些，避免把正文里恰好以 / 开头的字符串误判成路径）。
_PATH_KEYS = ('file_path', 'filePath', 'path', 'directory', 'dir')


class ProtocolError(Exception):
    def __init__(self, kind='harness_failed'):
        self.kind = kind


def personal_config(api_key: str, base_url: str, model_ids: list[str]) -> dict:
    return {'schemaVersion': 1, 'config': {
        'providerConfigRules': {'providerRules': [{
            'providerId': PROVIDER, 'templateId': PROVIDER, 'enabled': True,
            'config': {'group': 'standard-personal', 'personalModelIds': model_ids,
                'modelOrder': model_ids, 'access': {'type': 'api-key', 'apiKey': api_key},
                'api': {'type': 'openai-chat-completions', 'baseUrl': base_url}}}]},
        'modelConfigRules': {'providerModelRules': [], 'manualProviderModelRules': []}}}


def prepare_home(home: Path, api_key: str, base_url: str, model_ids: list[str]) -> Path:
    config_dir = home / '.zcode' / 'cli'
    config_dir.mkdir(parents=True)
    (config_dir / 'config.json').write_text(json.dumps({
        'plugins': {'enabled': False}, 'skills': {'enabled': False},
        'features': {'subagent': False, 'memory': False, 'mcp': False, 'skill': False},
        'hooks': {}, 'storage': {'dir': str(home), 'sessionDbPath': str(home / 'session.db')}}))
    provider = home / 'provider.json'
    # 文件创建时即0600；临时目录由tempfile设为0700。
    with os.fdopen(os.open(provider, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as output:
        json.dump(personal_config(api_key, base_url, model_ids), output)
    return provider


class ZCodeProtocol:
    def __init__(self, proc, approver=None):
        self.proc = proc
        self.sequence = 0
        self.received_bytes = 0
        # approver(params) -> (allowed, reason)：文件任务模式下自动审批 interaction/requestPermission。
        self.approver = approver
        self.permission_log: list[str] = []

    async def write(self, value):
        self.proc.stdin.write((json.dumps(value, ensure_ascii=False) + '\n').encode())
        await self.proc.stdin.drain()

    async def request(self, method, params):
        self.sequence += 1
        identifier = self.sequence
        await self.write({'id': identifier, 'method': method, 'params': params})
        while True:
            raw = await self.proc.stdout.readline()
            if not raw:
                raise ProtocolError()
            self.received_bytes += len(raw)
            if self.received_bytes > MAX_SESSION_BYTES:
                raise ProtocolError('output_limit')
            try:
                message = json.loads(raw)
            except ValueError:
                raise ProtocolError('parse') from None
            if not isinstance(message, dict):
                raise ProtocolError('parse')
            if 'method' in message and 'id' in message:
                if message['method'] == 'session/requestRuntimePreferences':
                    await self.write({'id': message['id'], 'result': {
                        'nativeSearchEnhancementsEnabled': False, 'memoryEnabled': False,
                        'askUserQuestionAutoResolutionEnabled': False}})
                elif message['method'] == 'interaction/requestPermission' and self.approver is not None:
                    params = message.get('params') if isinstance(message.get('params'), dict) else {}
                    allowed, reason = self.approver(params)
                    self.permission_log.append(
                        f"{params.get('toolName')} {'allow' if allowed else 'deny:' + reason}")
                    decision = {'decision': 'allow'} if allowed else {
                        'decision': 'deny', 'reason': reason or 'QQ harness 拒绝了该操作'}
                    await self.write({'id': message['id'], 'result': decision})
                    # 拒绝不终止任务：模型会收到拒绝原因并调整行为继续作答。
                else:
                    # 不能代替QQ控制者批准权限或回答模型的提问。
                    await self.write({'id': message['id'], 'error': {
                        'code': -32000, 'message': 'QQ harness interaction is not enabled'}})
                    raise ProtocolError('harness_approval_required')
            elif message.get('id') == identifier:
                if 'error' in message:
                    raise ProtocolError()
                if 'result' not in message:
                    raise ProtocolError('parse')
                return message['result']


def assistant_text(snapshot: dict) -> str:
    for message in reversed(snapshot.get('messages', [])):
        if message.get('info', {}).get('role') == 'assistant':
            return '\n'.join(part.get('text', '') for part in message.get('parts', [])
                             if part.get('type') == 'text').strip()
    return ''


def reasoning_text(snapshot: dict) -> str:
    """提取最后一条 assistant 消息的 reasoning 部分（模型思考过程）。"""
    for message in reversed(snapshot.get('messages', [])):
        if message.get('info', {}).get('role') == 'assistant':
            return '\n'.join(part.get('text', '') for part in message.get('parts', [])
                             if part.get('type') == 'reasoning').strip()
    return ''


def tool_log(snapshot: dict, root: Path | None = None) -> list[str]:
    """提取会话全部工具调用摘要；标记越出工作目录的操作（审计用）。"""
    entries: list[str] = []
    for message in snapshot.get('messages', []):
        for part in message.get('parts', []):
            if part.get('type') != 'tool':
                continue
            state = part.get('state') if isinstance(part.get('state'), dict) else {}
            inputs = state.get('input') if isinstance(state.get('input'), dict) else {}
            target = next((str(inputs[key]) for key in _PATH_KEYS
                           if isinstance(inputs.get(key), str)), '')
            note = ''
            if root is not None and target.startswith('/'):
                resolved = Path(target)
                if resolved == PROTECTED_DIR or PROTECTED_DIR in resolved.parents:
                    note = ' ⚠️受保护目录'
                elif resolved != root and root not in resolved.parents:
                    note = ' ⚠️工作目录外'
            entries.append(f"{part.get('tool', '?')} {target} [{state.get('status', '?')}]{note}".strip())
    return entries


def path_within(target: Path, root: Path) -> bool:
    resolved = target.expanduser().resolve()
    return resolved == root or root in resolved.parents


def make_approver(root: Path):
    """构造文件任务模式的自动审批器：工作目录内放行，越界或触碰受保护目录一律拒绝。"""

    def approve(params: dict) -> tuple[bool, str]:
        tool = params.get('toolName')
        if tool not in FILE_TOOLS:
            return False, '工具不在允许列表中'
        inputs = params.get('input') if isinstance(params.get('input'), dict) else {}
        paths = [inputs[key] for key in _PATH_KEYS if isinstance(inputs.get(key), str)]
        if tool in {'Write', 'Edit'} and not any(key in inputs for key in ('file_path', 'filePath')):
            return False, '缺少目标文件路径'
        for raw in paths:
            resolved = Path(raw).expanduser()
            if resolved == PROTECTED_DIR or PROTECTED_DIR in resolved.parents:
                return False, '目标在受保护目录内，已拒绝'
            if not path_within(resolved, root):
                return False, '目标不在工作目录内，已拒绝'
        return True, ''
    return approve


class ZCodeAdapter(AgentAdapter):
    adapter_type = 'zcode_stdio'
    # toolAllowlist 只开 Read/Write/Edit/Glob/Grep；无 Bash/Web，因此没有原生 exec
    isolation = 'sandbox'
    # L03：ZCode 原生路径本轮未开启，未验证的读取边界仍未复核 → 不计入可用能力
    native_modes = frozenset()
    broker_modes = frozenset({'read', 'edit'})
    notes = (
        'native_read_edit: 未验证/不可放行（本轮未开启原生路径）；'
        '原生工具白名单仅读写类（Read/Write/Edit/Glob/Grep），exec 由公共 broker 提供。',
    )
    name = 'zcode'
    display_name = 'ZCode'
    status_note = '尚未配置：需要安装版ZCode、Node、GLM密钥及模型目录。'

    # L04：推理强度直接决定**单次调用耗时**，而调用耗时受任务 deadline 约束。
    # 实测（2026-10-10，独立临时工作区）：默认 max 下 edit/exec 两次各约 60s 超时；
    # 显式 low 时 edit 15.98s、exec 29.23s 通过，text/read 在默认强度下通过。
    # 因此**带工具的模式默认用 low**（可被 --effort 或 mode_efforts 覆盖），
    # 纯文本保留模型目录的 default_effort，避免"默认配置必然超时"。
    DEFAULT_MODE_EFFORTS: dict[str, str] = {'text': '', 'read': 'low', 'edit': 'low', 'exec': 'low'}

    def __init__(self, *, api_key: str, base_url: str, enabled=False, node=DEFAULT_NODE,
                 entry=DEFAULT_ENTRY, builtin=DEFAULT_BUILTIN, model='glm-5.3',
                 catalog='data/zcode_models.json', workspace_root='', mode_efforts=None):
        self._api_key = api_key.strip()
        self.base_url = base_url
        self.enabled = enabled
        self.node = shutil.which(node)
        self.entry = entry
        self.builtin = builtin
        self.model_name = model
        self.workspace_root = workspace_root.strip()
        try:
            self._catalog = json.loads(Path(catalog).read_text())
            if not isinstance(self._catalog, dict):
                raise ValueError()
            for name, spec in self._catalog.items():
                if not isinstance(name, str) or not isinstance(spec.get('model_id'), str) or not isinstance(spec.get('efforts'), list):
                    raise ValueError()
        except (OSError, ValueError, TypeError, AttributeError):
            self._catalog = {}
        self._slots = asyncio.Semaphore(1)
        # 每个模式的默认强度（空字符串=用模型目录的 default_effort）
        self.mode_efforts: dict[str, str] = dict(self.DEFAULT_MODE_EFFORTS)
        for key, value in (mode_efforts or {}).items():
            self.mode_efforts[str(key)] = str(value or '')
        self.last_diagnostic = ''

    def default_effort_for(self, mode: str, model: str) -> str:
        """本次调用实际使用的推理强度（显式 --effort 优先，其次模式默认，最后目录默认）。"""
        configured = self.mode_efforts.get(mode or 'text', '')
        if configured:
            return configured
        return str((self._catalog.get(model) or {}).get('default_effort') or '')

    @property
    def model_options(self):
        return {name: tuple(spec['efforts']) for name, spec in self._catalog.items()}

    @property
    def configured(self):
        return bool(self.enabled and self.node and self._api_key and Path(self.entry).is_file()
                    and Path(self.builtin).is_file() and self.model_name in self._catalog)

    async def chat(self, messages, *, timeout):
        return await self.chat_with_options(messages, timeout=timeout, options=InvocationOptions(self.model_name))

    async def chat_with_options(self, messages, *, timeout, options):
        self.validate_options(options)
        if not self.configured:
            return AgentReply(error='not_configured')
        try:
            async with asyncio.timeout(timeout):
                async with self._slots:
                    return await self._run(messages, options)
        except TimeoutError:
            return AgentReply(error='timeout')
        except ProtocolError as exc:
            return AgentReply(error=exc.kind)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return AgentReply(error='harness_failed')

    def _workspace(self, requested: str | None = None) -> Path | None:
        """原生文件模式的工作目录。

        F02 合同：只有 `workspace_tools=True` 的调用才会走到这里（broker/文本模式传 None）；
        优先使用本次任务冻结的规范目录，其次才是适配器配置的 workspace_root。
        """
        candidate = (requested or "").strip() or self.workspace_root
        if not candidate:
            return None
        root = Path(candidate).expanduser()
        return root if root.is_dir() else None

    async def _run(self, messages, options):
        root = self._workspace(options.directory) if options.workspace_tools else None
        with tempfile.TemporaryDirectory(prefix='qq-zcode-') as directory:
            home = Path(directory)
            provider = prepare_home(home, self._api_key, self.base_url,
                                    [spec['model_id'] for spec in self._catalog.values()])
            env = launch_env()
            env.update(HOME=directory, ZCODE_DATA_BASE_DIR=directory, ZCODE_STORAGE_DIR=directory,
                ZCODE_SESSION_DB_PATH=str(home / 'session.db'),
                ZCODE_BUILTIN_PROVIDER_CONFIG_FILE=self.builtin,
                ZCODE_PERSONAL_PROVIDER_CONFIG_FILE=str(provider))
            proc = None
            try:
                proc = mark_owned_group(await asyncio.create_subprocess_exec(
                    self.node, self.entry, 'app-server',
                    env=env, cwd=directory, start_new_session=True, limit=MAX_OUTPUT_BYTES,
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL))
                rpc = ZCodeProtocol(proc, approver=make_approver(root) if root is not None else None)
                selected = {'providerId': PROVIDER, 'modelId': self._catalog[options.model]['model_id']}
                # 默认也明确选择runtime目录记录的defaultLevel，避免回退到另一模型。
                effort = options.effort or self.default_effort_for(
                    options.mode or 'text', options.model)
                if effort:
                    selected['options'] = {'reasoningLevel': effort}
                create_params = {
                    'workspace': {'workspacePath': str(root) if root is not None else directory,
                                  'workspaceKey': 'qq-harness'},
                    'model': selected, 'titleGenerationEnabled': False, 'mcpServers': [],
                    # 文件模式只开读写类工具；文本模式移除全部工具。
                    'toolAllowlist': list(FILE_TOOLS) if root is not None else ['__qq_no_tools__'],
                    'dynamicWorkflowEnabled': False}
                snapshot = await rpc.request('session/create', create_params)
                session = snapshot['session']['sessionId']
                current = snapshot['settings']['model'].get('current', {})
                if current.get('providerId') != PROVIDER or current.get('modelId') != selected['modelId']:
                    raise ProtocolError('selection_mismatch')
                if root is None:
                    # 纯文本模式保持 plan；文件任务需要默认 build 模式才能写盘。
                    await rpc.request('session/setMode', {'sessionId': session, 'mode': 'plan'})
                if effort:
                    changed = await rpc.request('session/setThoughtLevel', {
                        'sessionId': session, 'thoughtLevel': effort, 'persistAsWorkspaceLastUsed': False})
                    # 某版本返回snapshot包裹；实际验证成功后才发送任务。
                    value = changed.get('snapshot', changed)
                    if value.get('settings', {}).get('thoughtLevel', {}).get('current') != effort:
                        raise ProtocolError('selection_mismatch')
                content = conversation_prompt(messages)
                if root is not None:
                    # 读类工具在协议层没有门禁，只能靠指令约束 + 事后审计标记。
                    content = (f'工作目录：{root}。你可以使用文件工具完成用户任务，'
                               f'文件读写应限定在该目录内；不要访问本机器人自身的项目目录。\n' + content)
                await rpc.request('session/send', {'sessionId': session,
                    'modelSelection': selected, 'content': content})
                while True:
                    snapshot = await rpc.request('session/read', {'sessionId': session})
                    state = snapshot.get('projection', {}).get('status')
                    if root is None and (snapshot.get('projection', {}).get('pendingPermissions')
                                         or state == 'waiting'):
                        raise ProtocolError('harness_approval_required')
                    if state == 'error':
                        raise ProtocolError()
                    answer = assistant_text(snapshot)
                    if state in {'idle', 'completed'} and answer:
                        # L04：如实回填本次实际使用的强度（否则任务记录会显示"未指定"）
                        return AgentReply(content=answer, thinking=reasoning_text(snapshot),
                                          tool_log=tool_log(snapshot, root),
                                          resolved_effort=effort or None)
                    await asyncio.sleep(.5)
            finally:
                await kill_process(proc)
