"""HTTP 接入层：统一 /api/v1（与 QQ 共用同一套服务与权限）。

本包不在 `__init__` 里导入子模块，避免与 app.routing 形成导入环。
"""

__all__: list[str] = []
