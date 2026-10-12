"""安装版 DeepSeek Harness headless：每任务独立 profile 和 model selection。"""
from __future__ import annotations
import asyncio
import json
import shutil
import tempfile
from pathlib import Path
from collections.abc import Sequence
import yaml
from app.agents.base import AgentAdapter
from app.agents.harness_process import conversation_prompt, launch_env, run_process
from app.models import AgentReply, ChatMessage, InvocationOptions

DEFAULT_DSH = '/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh'
# 已安装 DeepSeek Messages adapter 的模型目录及明确支持的强度。
DSH_MODELS = {'deepseek-flash': ('off', 'low', 'high', 'max'),
              'deepseek-v4-pro': ('off', 'low', 'high', 'max')}
DISABLED_ROWS = ('tool-plugin-manager', 'plugin-manager', 'config-editor', 'settings',
    'deepseek-account', 'llm-deepseek-account', 'session-title-llm', 'session-telemetry-otel',
    'tool-bash', 'tool-pwsh', 'tool-jobs', 'tool-fs', 'tool-fs-search', 'tool-skill',
    'tool-subagent-control', 'tool-subagent-list-agents', 'tool-subagent', 'tool-subagent-fork',
    'tool-workflow', 'tool-web', 'tool-todo', 'tool-goal', 'tool-ralph', 'agent-instructions',
    'skill-filesystem', 'goal-round-driver')


def parse_run_events(text: str) -> tuple[str, str]:
    """解析 headless `--json` 的事件流，返回（最终答复, 思考过程）。

    只认允许的字段，任何畸形行/未知事件一律忽略：事件流不属于可信输入，
    绝不能让原始行进入回复或上下文。
    """
    answer = ''
    thinking: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get('type')
        if not isinstance(kind, str):
            continue
        kind = kind.split('#', 1)[0]
        chunk = event.get('text')
        if kind == 'thinking' and isinstance(chunk, str):
            thinking.append(chunk)
        elif kind == 'final' and isinstance(chunk, str):
            answer = chunk
    return answer.strip(), '\n'.join(part.strip() for part in thinking if part.strip()).strip()


def task_patch(options: InvocationOptions, base_url: str) -> list[dict]:
    selection = {'provider': 'deepseek-official', 'model': options.model}
    if options.effort is not None:
        selection['reasoningEffort'] = options.effort
    return [
        {'id': 'agent-default-model', 'config': selection},
        {'id': 'llm-deepseek', 'config': {'baseURL': base_url, 'maxTokens': 8192}},
        {'id': 'sandbox-policy', 'config': {'mode': 'read-only'}},
        *({'id': row, 'disabled': True} for row in DISABLED_ROWS),
    ]


class DeepSeekHarnessAdapter(AgentAdapter):
    adapter_type = 'deepseek_harness'
    # 原生文件与 Shell 工具在 task_patch 中被逐项禁用；read/edit 走公共 broker
    isolation = 'broker'
    native_modes = frozenset()
    broker_modes = frozenset({'read', 'edit'})
    notes = ('Harness 原生 tool-fs/tool-bash 全部关闭，目录能力由公共 broker 提供。',)
    name = 'deepseek'
    display_name = 'DeepSeek Harness'
    status_note = '尚未配置：需要 DeepSeek Harness CLI 和本机 API 密钥。'

    def __init__(self, *, api_key: str, enabled: bool = True, binary: str = DEFAULT_DSH,
                 workdir: str = '.', model: str = 'deepseek-flash',
                 base_url: str = 'https://api.deepseek.com/anthropic',
                 model_options: dict | None = None) -> None:
        self._api_key = api_key.strip()
        self.enabled = enabled
        self.binary = shutil.which(binary)
        self.workdir = str(Path(workdir).resolve())
        self.model_name = model
        self.base_url = base_url
        self._slots = asyncio.Semaphore(1)
        self._model_options = {
            str(key): tuple(value) for key, value in (model_options or DSH_MODELS).items()
        }

    @property
    def model_options(self):
        return dict(self._model_options)

    @property
    def configured(self):
        return bool(self.enabled and self.binary and self._api_key
                    and self.model_name in self.model_options and Path(self.workdir).is_dir())

    async def chat(self, messages: Sequence[ChatMessage], *, timeout: float) -> AgentReply:
        return await self.chat_with_options(messages, timeout=timeout, options=InvocationOptions(self.model_name))

    async def chat_with_options(self, messages, *, timeout, options):
        self.validate_options(options)
        if not self.configured:
            return AgentReply(error='not_configured')
        try:
            async with asyncio.timeout(timeout):
                async with self._slots:
                    with tempfile.TemporaryDirectory(prefix='qq-dsh-') as directory:
                        patch = Path(directory) / 'task.yaml'
                        patch.write_text(yaml.safe_dump(task_patch(options, self.base_url), allow_unicode=True))
                        env = launch_env()
                        env.update(DSH_HOME=directory, DEEPSEEK_API_KEY=self._api_key,
                                   DSH_PERMISSION_MODE='read-only', DSH_TELEMETRY_MODE='OFF')
                        # --json 输出事件流（含 thinking），是取得思考过程的唯一通道；
                        # --patch 属于启动器选项，必须排在应用选项 --json 之前。
                        code, text = await run_process([self.binary, '--profile', 'headless', '--patch', str(patch),
                                                        '--json', '-'],
                            env=env, cwd=directory, stdin=conversation_prompt(messages))
                        if code != 0:
                            return AgentReply(error='harness_failed')
                        answer, thinking = parse_run_events(text)
                        if not answer:
                            return AgentReply(error='harness_failed')
                        return AgentReply(content=answer, thinking=thinking)
        except TimeoutError:
            return AgentReply(error='timeout')
        except (OSError, ValueError):
            return AgentReply(error='harness_failed')
