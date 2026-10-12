"""后端层：Provider / Backend / Agent 三层分离与统一执行协议。

模块边界：
- descriptor：BackendDescriptor（原生能力、broker 能力、隔离等级）；
- factory：适配器工厂注册表（新协议 harness 的唯一接入点）；
- builtin / mock_adapter：内置适配器工厂与"新协议 harness"可运行示例；
- registry：从配置构建后端与逻辑 Agent（含旧式配置迁移与冲突说明）；
- protocol：TaskRequest / ExecutionSpec / ExecutionResult 统一协议；
- broker_loop：公共文件 broker 的动作循环；
- gateway：ExecutionGateway（选择适配器、校验能力、执行 broker 或已验证原生工具）。

本包不在 `__init__` 里导入子模块，避免与 app.agents / app.workspaces 形成导入环。
"""

__all__: list[str] = []
