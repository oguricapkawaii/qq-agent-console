"""路由层：命令注册表驱动的调度。

命令语法与参数在 app/commands/ 声明；本包只负责把消息路由到对应处理器。
"""

from app.routing.dispatcher import Dispatcher

__all__ = ["Dispatcher"]
