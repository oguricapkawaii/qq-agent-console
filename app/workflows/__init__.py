"""多 Agent 协作（/team，支持 text/read/edit/exec 步骤）。

模块边界：
- models：冻结计划、步骤与状态模型、拓扑校验纯函数；
- parser：/team 子命令解析（公共选项与 /task 共用），保留正文原文，不执行 Shell；
- store：复用项目 SQLite 连接的参数化持久化（可重复迁移）；
- executor：单步执行（隔离上下文、统一走 TaskService 的权限判定、限额、归档）；
- runner：DAG 调度、并发、失败传播、取消、超时、重试轮次；
- notifier：状态通知的脱敏与分段发送；
- service / handler：权限、配额、能力与目录冻结、命令回执。
"""

from app.workflows.executor import ExecutionOutcome, StepContext, StepExecutor
from app.workflows.handler import TeamCommandHandler
from app.workflows.notifier import Notifier
from app.workflows.runner import WorkflowRunner
from app.workflows.service import WorkflowService
from app.workflows.store import WorkflowStore

__all__ = [
    "ExecutionOutcome",
    "Notifier",
    "StepContext",
    "StepExecutor",
    "TeamCommandHandler",
    "WorkflowRunner",
    "WorkflowService",
    "WorkflowStore",
]
