"""目录层：授权根解析、绑定、模糊查找、全入口目录锁、公共文件 broker。

模块边界：
- models：候选、解析结果与选择结果的纯数据；
- finder：有界的本地目录名称搜索（不扫描整机、不读文件内容）；
- coordinator：单任务/team/原生 harness 共用的目录互斥锁；
- changes：哈希、备份、冲突检测与逐文件部分失败记录；
- broker：模型可调用的结构化文件动作（list/read/search/propose_changes）；
- service：把 `--dir`/`--find`/绑定目录解析为规范路径，并管理候选确认。

注意：本包不在 `__init__` 里导入子模块，避免与 app.tasks / app.backends 之间
形成导入环。
"""

__all__: list[str] = []
