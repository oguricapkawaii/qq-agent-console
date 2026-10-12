> 当前总览：[项目总架构说明](outputs/项目总架构说明.md)；日常操作：[更新后的操作指南](outputs/操作指南.md)。文档更新于2026-10-12，历史整改数字保留原时点含义。

> 当前完整验收与部署状态以 [统一控制台最终交付矩阵](outputs/统一控制台最终交付矩阵.md) 为准；下文历史测试数字保留原时点含义。

# QQ 群 + 统一 HTTP API 的多模型控制台

以 QQ 群为控制台、同时对外提供统一 HTTP API 的多模型调度器：群里只用**一个** OneBot 机器人账号，
通过**一套统一口令**把任务路由到不同逻辑 Agent（deepseek / zcode / codex / gpt / glm / 自定义），
统一管理「文本 / 只读 / 读写 / 受控命令执行」四种权限、目录授权、目录锁与多 Agent 团队协作。

> ⚠️ **旧口令已停用，且没有兼容执行层。** `/glm`、`/deepseek`、`/zcode`、`/gpt`、`/codex`、`/all`、`/read`、
> `/write`、`/models`、`/set`、`/status`、`/clear`、`/approve`、`/whitelist` 现在只返回一条迁移提示：
> **不会读写文件、不会调用模型、不会修改配置**（对照表见 2.5）。命令与参数以注册表为准，见
> [命令清单.md](outputs/命令清单.md)（由 [scripts/render_commands.py](scripts/render_commands.py) 生成）。

使用者指南：[outputs/操作指南.md](outputs/操作指南.md)；设计规格：统一架构.md（本机归档，未上传）
（方案快照，实现以 `app/` 为准）。本 README 面向部署与二次开发，**如实区分「代码声明」「离线验证」与「真实环境已验证」**。

---

## 1. 快速开始

要求 Python 3.12+（本项目未使用系统 Python 3.9）：

```bash
# 安装（方式一：uv，推荐，无需 Homebrew/sudo；方式二：已有 python3.12）
curl -LsSf https://astral.sh/uv/install.sh | sh
uv python install 3.12 && uv venv .venv --python 3.12 && uv pip install --python .venv/bin/python -e ".[dev]"
# python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"

# 最小 .env（占位符，真实密钥不要提交）：cp .env.example .env 后填写
#   QQ_ALLOWED_USER_IDS=<你的QQ号>    允许调用模型的用户；留空 = 所有人只能 /help
#   QQ_ALLOWED_GROUP_IDS=<你的群号>   允许响应的群；留空 = 所有群静默
#   DEEPSEEK_API_KEY=<密钥>           至少一个后端的凭据（按需）
#   TRANSPORT=onebot_ws / ONEBOT_WS_URL=ws://127.0.0.1:3001

# 启动（结构化配置：CONFIG_FILE=config.yaml .venv/bin/python -m app.main）
.venv/bin/python -m app.main    # 等价：.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8080
curl -s http://127.0.0.1:8080/healthz      # {"status":"ok"}
curl -s http://127.0.0.1:8080/status | head -c 400
```

群里发第一条命令：`/help`（未授权用户唯一可用）→ `/agent list`（后端能力，✅/❌ = 是否已配置）
→ `/task deepseek -- 解释这段需求`。启动时会执行**可重复**的 schema 迁移、把未完成任务标记为 `interrupted`
（绝不自动重跑），并打印配置迁移/冲突说明（同样出现在 `/status` 的 `workspaces.notes` 与 `config_notes`）。

---

## 2. 统一命令模型

### 2.1 命令族

| 命令 | 用途 |
|---|---|
| `/task <agent> [选项] -- <任务>` | 统一任务入口：文本 / 只读 / 读写 / 受控命令执行 |
| `/task all -- <问题>` | 已配置 Agent 并发独立回答（**仅文本**，不接受任何选项） |
| `/task status\|result\|cancel <任务ID>` | 查询、取结果、取消；`status` 省略 ID 时列出本群最近 10 个任务 |
| `/workspace find\|use\|status\|clear\|select` | 查找、绑定、查看、清除目录；确认模糊查找候选 |
| `/agent list\|models\|set\|status\|clear` | 后端能力、真实模型目录、保存默认、状态、清上下文 |
| `/team create\|add\|remove\|plan\|run\|status\|result\|cancel\|retry` | 多 Agent 协作（DAG、冻结计划、后台调度） |
| `/confirm <确认码>` | 确认一项**已被明确列出内容**的受控操作（一次性、有 TTL） |
| `/access add\|remove\|list` | 本群成员授权管理（**仅部署配置中的管理员**） |
| `/command list\|show <名称>`、`/help [命令族]` | 发现口令、参数与示例，不调用模型 |

`/help`、`/command`、`/api/v1/commands` 与命令清单来自同一份注册表（`app/commands/registry.py`、`spec.py`）。

### 2.2 公共选项（`/task` 与 `/team add` 完全相同）

| 选项 | 说明 |
|---|---|
| `--backend <后端ID>` | 指定执行后端；默认取该 Agent 的默认后端 |
| `--model <模型>` | 指定模型；必须存在于该后端的**真实目录**中，否则直接报错 |
| `--effort <强度\|default>` | 推理强度；`default` 表示模型默认 |
| `--mode text\|read\|edit\|exec` | 执行权限（见 2.3） |
| `--dir "路径"` | 绝对路径，或 `/workspace` 绑定后写 `.` |
| `--find "关键词"` | 在授权搜索根内查找目录，返回候选后需你选择 |
| `--after a,b` | **仅 `/team add`**：本步的直接依赖步骤 ID |

支持 `--key value`、`--key=value` 与引号包裹的含空格/中文路径；未知选项、重复选项、缺值、非法枚举、
`--dir` 与 `--find` 同时出现一律拒绝并给出用法提示。

### 2.3 四种模式与默认推导

有效能力 = **你的目录授权 ∩ 步骤声明 ∩ 后端真实能力 ∩ 全局策略**（`app/security/acl.py`）。

| 模式 | 能力 |
|---|---|
| `text` | 只输出文本，无任何文件/命令工具（默认） |
| `read` | 列目录、按关键词搜索、读取文本；**没有**写文件、Shell、额外联网工具 |
| `edit` | read + 创建/修改文件；记录差异、原始哈希与备份；哈希漂移拒绝覆盖 |
| `exec` | edit + **沙箱内**受控本地命令（平台隔离：写限任务目录、禁网、**读取默认拒绝**、受保护路径不可读写；再叠加白名单 + 参数数组 + 超时/输出上限 + 进程组清理） |

默认推导（`app/commands/options.py` / `WorkspaceService.resolve_selector`）：无目录选择器且无 `--mode` → **text**
（已绑定目录也不开工具）；有 `--dir`/`--find` 但省略 `--mode` → **read**；显式 `read|edit|exec` 但不给目录 →
用**本群 × 你**的绑定目录，没有绑定则拒绝（**不会**回落到进程工作目录）；`--mode text` 与 `--dir`/`--find`
同时出现 → 拒绝；`--dir .` 只表示绑定过的目录。**不从正文推断权限**（正文写“修复/改成”也不会升级到 edit）；
后端不支持所请求模式时明确返回 `capability_not_supported`，**不会**偷偷换后端或降级。

### 2.4 `--` 语义

选项写在**首个独立 `--`** 之前（`--` 前后都是空白或行首/行尾才算独立）；`--` 之后的正文**原样保留**：
引号、空行、中文、以 `--` 开头的内容都不再被解释；没有独立 `--` 时正文视为空，要求正文的命令
（如 `/team add`）会明确报错而不是猜。

### 2.5 旧口令 → 新用法（只给迁移提示）

| 旧口令 | 现在请用 |
|---|---|
| `/glm`、`/deepseek`、`/zcode`、`/gpt`、`/codex` `[选项] <问题>` | `/task <agent> [选项] -- <问题>` |
| `/all <问题>` | `/task all -- <问题>`（仅文本并发，不承担团队协作） |
| `/read <agent> <路径>` | `/task <agent> --dir "路径" --mode read -- <问题>` |
| `/write` + `/approve` | `/task <agent> --dir "路径" --mode edit -- <要求修改的内容>`（确认码用 `/confirm`） |
| `/models`、`/set` | `/agent models <agent>`、`/agent set <agent> [--model 模型] [--effort 强度]` |
| `/status`、`/clear <agent>` | `/agent status [agent]`、`/agent clear <agent>`（服务健康检查仍是 HTTP `/status`） |
| `/approve <码>`、`/whitelist add\|remove\|list` | `/confirm <确认码>`、`/access add\|remove\|list` |

迁移提示原文：`⚠️ 旧口令 /<名字> 已停用，且没有兼容执行层（不会读写文件、不会调用模型）。`
完全未知的斜杠命令继续**静默忽略**（普通聊天一律不触发模型）。

---

## 3. 目录与权限

### 3.1 授权根（唯一的目录权限来源）

```yaml
workspaces:                      # CONFIG_FILE=config.yaml；示例见 config.example.yaml
  roots:
    - path: /Users/example/Documents/示例项目   # 必须是已存在目录，否则启动直接报错
      label: 示例项目
      modes: [read]                     # 普通授权成员可用模式（默认 [read]）
      admin_modes: [read, edit, exec]   # 管理员可用模式（默认 [read, edit]）
      searchable: true                  # 是否参与 /workspace find（默认 true）
      users: {10001: [read, edit]}      # 按 QQ 号的更细授权（覆盖 modes）
  find: {max_depth: 4, max_candidates: 8, max_entries: 20000, max_seconds: 2.0, selection_ttl_seconds: 300}
  exclude_dirs: [node_modules, .git, .venv, dist, build]
  edit_enabled: true                    # 全局开关（exec_enabled 同理）：关闭后不放行 edit/exec
```

`QQ_ALLOWED_USER_IDS` 中的用户是**部署配置管理员**（可执行 `/access`）；`/access add` 授权的是动态成员，
动态成员**永远不能**提升为管理员。判定顺序：群在名单内 → 用户在名单内 → 路径落在某个授权根内（多命中取最具体的）
→ 该根的模式覆盖请求模式 → 全局开关 → 后端能力。成员默认只有 text；read/edit/exec 必须显式授权。

### 3.2 受保护范围与路径判定

默认受保护：**机器人项目目录**、`data/`、`runtime/`、`.env`、`backups/`（`app/workspaces/configurator.py`），
可用 `protected_paths` 追加；另有默认敏感清单（`.env*`、`id_rsa`/`id_ed25519`、`credentials*`、`secrets.yaml`、
`.netrc`、`.pgpass`、`.git-credentials`、`session.db`，后缀 `.pem/.key/.p12/.pfx/.jks/.keystore`，
目录 `.ssh/.aws/.gnupg/.codex/.zcode/.config/.git`）。路径一律先规范化：`canonical()` 用 `realpath` 解析并对
不存在的尾部保留最接近的存在祖先（`app/security/paths.py`），**不用字符串 `startswith`**，因此符号链接逃逸、
相邻同前缀目录、`a/../b` 在解析阶段就被拒绝；不支持 `~` 展开。

### 3.3 模糊查找与选择确认

`/workspace find 记账项目` 只查找并绑定（不自动创建任务）；`/workspace select AB12CD34 1` 选择候选
（确认码 8 位十六进制大写，默认 300 秒有效）；`/workspace status | use "/绝对路径" | clear` 查看、直接绑定、清除。

- `find` 只在**授权搜索根**内按目录名匹配（不读文件内容、不扫描整机），受 `max_depth / max_candidates /
  max_entries / max_seconds` 与 `exclude_dirs` 限制、不跟随符号链接目录；`README/.git/pyproject.toml`
  等标志文件只做**排序加权**，不做语义猜测。
- **只有一个候选也必须由本人 select**；确认码一次性，绑定「类型 + 群 + 发起人 + 请求/草稿版本 + 候选快照 + TTL」，
  重放、过期、跨群、跨用户、候选被删除都拒绝（`app/security/confirmations.py`）。
- 任务内 `--find` 的确认关联**原请求**：选择成功后原任务只提交一次，选择前不调用模型、不改动文件；
  独立 `find` 只绑定目录。绑定写入 `workspace_bindings`（群 × 用户），**使用前复查授权**，撤权后视为无绑定；
  team 步骤用 `--find` 时只把规范路径冻结进计划。

### 3.4 目录锁（全入口互斥）

`WorkspaceCoordinator` 覆盖单任务、`/task all` 子任务、team 步骤、broker 写入与原生 harness 调用：
**同目录或父子目录的任务整段独占**（含 read），无关目录可并行；超时由 `workspaces.lock_timeout_seconds`
（`WORKSPACE_LOCK_TIMEOUT_SECONDS`，默认 30 秒）控制，超时报 `目标目录正被其他任务占用，请稍后重试：…`；
正常返回、异常、取消都释放锁。**单 worker 限制（硬性）**：跨进程锁尚未实现，因此
`workspaces.max_workers`（或 `WORKSPACE_MAX_WORKERS` / `server.workspace_max_workers`）只允许 **1**；
声明 >1、或声明 `cross_process_lock: true`，都会在启动时直接报错——不接受用布尔开关冒充多进程安全。

### 3.5 公共文件 broker（read/edit/exec 的统一实现）

文本型 API 后端与 harness 后端都通过 broker 获得目录能力：模型**只返回数据**（JSON 动作），服务端逐动作
严格校验后才真正读写（`app/workspaces/broker.py` + `app/backends/broker_loop.py`），例如
`{"actions": [{"action": "read", "path": "src/app.py", "max_bytes": 65536}]}`，结束时用 `{"final": "…"}`。

- 白名单动作：`list`(path/depth 1–3)、`read`(path/max_bytes)、`search`(query/path/max_results)、
  `propose_changes`(changes[]，每项 `path`+`content` 或 `delete=true`，可带 `expected_sha256`，一次 ≤20 个文件)、
  `run_command`(argv **数组**/cwd/timeout，仅 exec 模式)、`final`(answer)；其他名字一律拒绝。
  `list/search/read` 共用同一套安全打开：从**授权根的目录 fd** 出发逐级 `O_NOFOLLOW|O_DIRECTORY` 打开，
  之后所有 `stat/open/scandir/mkdir/rename/unlink` 都用 `dir_fd` 相对形式（`app/security/safeopen.py`）——
  父目录在检查之后被替换成符号链接也不会把操作引到工作区之外；
  **每一个后代路径**（`list` 的每一项、`search` 的每个候选文件）在打开前都重新做最具体目录授权、受保护与敏感复查，
  父目录授权不覆盖更具体的子目录授权，撤权在下一个动作立即生效；**符号链接一律不参与 list/search**；
  `exec` 的命令同样受这套授权约束（按**当前用户**在子目录上的实际读写能力生成沙箱限制），
  而解释器运行时（venv + 标准库）在受保护项目目录内也会被精确地只读放行，因此不需要 `-S` 之类绕行；
  搜索的每次读取都计入累计字节预算并受单文件上限、条目预算约束。
  `propose_changes` **只登记**变更，落盘由 `commit()` 在持目录锁时执行。
- 只接受工作目录内的**相对路径**：绝对路径、`..`、`~`、越界、敏感路径一律拒绝；含 NUL 的文件按二进制拒读；
- `read` 返回两个哈希：`sha256` 是**返回内容**的摘要（`max_bytes` 截断时只是片段），`version_sha256` 是与内容
  同一次安全读取得到的**全文件**版本；只有 `version_sha256` 用于"读后被改"判定，因此部分读取之后修改文件不会
  被误报为漂移，而文件真的变化时仍然拒绝覆盖。超过 8MiB 的文件会明确失败（避免用片段冒充版本），可改用 `search`；
  **read 模式下写动作是硬拒绝**（不是“提示模型不要写”），`run_command` 只在 `exec` 模式开放。
- 预算（超出即任务明确失败，不静默截断，`commands.broker` 可配）：动作数 40、轮数 8、累计读取 256 KiB、
  单文件 128 KiB、列目录 200 项、搜索 40 命中。
- 坏 JSON / 未知动作 / 越界 / 只读写 / 命令被拒 → `fatal`，任务失败（`bad_actions`、`broker_denied`、
  `budget_exceeded`）；文件不存在、目标是目录、二进制、哈希冲突等**可恢复**错误回灌给模型继续处理。
  模型文本不能冒充已发生的修改。

### 3.6 变更记录（哈希 / 备份 / 部分失败）

每个变更在**规划时就冻结**存在性与 `before_hash`（不依赖模型是否提供 `expected_sha256`），提交前再次复核：
漂移记为 `conflict` 且**不写入**；新建目标在提交前出现同样拒绝。覆盖/删除前把原文件备份到受控目录
`workspaces.backup_dir/<task_id>/`（默认 `runtime/state/backups/`，备份名是"相对路径把 `/` 换成 `__`"，同名再追加 `.001`…）；备份根 → 任务目录 → 备份文件名的**整条父链**
都从根 fd 逐级无跟随打开，任何一层是符号链接（含 `<备份根>/<任务ID>` 被换成链接）就**取消该文件的覆盖**并记失败，
备份文件本身用 `O_CREAT|O_EXCL|O_NOFOLLOW` 唯一命名创建，**从不覆盖已有文件**。
单文件用「同目录临时文件 + `os.replace(src_dir_fd, dst_dir_fd)`」原子替换（不按绝对路径字符串操作）；
**多文件提交不是整体原子操作**，逐文件记录 `applied / failed / conflict / skipped`，回执形如
`🔧 文件变更：· 修改 README.md [已应用] 8eee60df→7ddfe2a9，备份 README.md.bak`。
**只要有关键文件未写入，任务就是失败**（`commit_failed`），不会把模型的 final 当成落盘成功。
失败或取消**不回滚**已落盘内容；用 `/task result <任务ID>` 查看清单与备份路径后复制回原路径（覆盖前确认目标未被再次修改）。

---

## 4. 后端与接入

### 4.1 Provider / Backend / Agent 三层

**Provider** = API 协议与鉴权；**Backend** = 可执行后端（API adapter 或 harness 执行器）；**Agent** =
用户选择的逻辑入口（`/task <agent>` 的名字），持默认后端与允许的后端集合。`BackendDescriptor`
（`app/backends/descriptor.py`）字段：`id / agent / adapter_type / display_name / enabled / configured /
models / model_efforts / default_model / native_modes / broker_modes / isolation / exec_policy /
credential_env / unavailable_reason / notes`；`capabilities()` = `text` ∪ `native_modes` ∪ (`broker_modes` ∩
{read, edit})，配置了 ExecPolicy 时 broker 额外提供 exec。构造时自检：没有可验证命令白名单不许声明 broker exec；
`isolation: none` 不许声明原生文件/执行能力。状态探测**不产生模型调用**，业务层不再有 `name == "codex"` 分支。

### 4.2 adapter 列表（`app/backends/builtin.py`）

| adapter | 对应后端 | 隔离 | 原生能力 | broker 能力 |
|---|---|---|---|---|
| `openai_compatible` | GPT / DeepSeek API / GLM / 任意 OpenAI 兼容网关 | broker | 无 | read/edit（+exec 由策略提供） |
| `deepseek_harness` | 本机 DeepSeek Harness（`dsh` headless） | broker | 无 | read/edit |
| `codex_cli` | 本机 Codex CLI | sandbox | read/edit（需 `native_tools`） | read/edit |
| `zcode_stdio` | 本机 ZCode app-server | sandbox | read/edit（需 `native_tools`） | read/edit |
| `mock_harness` | 离线示例（[mock_adapter.py](app/backends/mock_adapter.py)） | broker | 无 | read/edit |

OpenAI 兼容后端的默认端点：GPT `https://api.openai.com/v1`、DeepSeek `https://api.deepseek.com/v1`、
GLM `https://open.bigmodel.cn/api/paas/v4`（vLLM / one-api 等兼容网关改 `base_url` 即可，密钥只写 `.env`）。

`native_tools` 是**部署级显式开关**，默认关闭：即使适配器声明了原生能力，网关也只在「步骤声明该模式 +
`isolation == sandbox` + 后端配置 `native_tools: true`」同时成立时才走原生工具，否则一律走公共 broker；
原生模式执行前后对工作区做**有界快照**（≤2000 条目）以如实报告变更。ZCode 原生读工具没有门禁，默认走 broker。

### 4.3 只改配置新增同协议模型 / 新增协议 harness

```yaml
backends:
  corp_llm: {adapter: openai_compatible, agent: corp, display_name: 公司内部 LLM, enabled: true,
             credential_env: CORP_LLM_KEY, default_model: corp-1,
             base_url: https://llm.example.internal/v1, model_options: {corp-1: [low, high]}}
agents:
  corp: {display_name: 公司 LLM, default_backend: corp_llm}
```

同协议模型/后端**不需要改任何 Python 代码**（`credential_env` 只写环境变量名，真实值放 `.env`/环境变量）；
后端 id 重复、`default_backend` 指向未定义后端、`agents.*` 含未知字段都会在启动时报错。新协议 harness 只需：
写一个 `AgentAdapter` 子类（声明 `adapter_type / isolation / native_modes / broker_modes`，实现 `chat`）+
调用 `register_adapter("my_adapter", factory)`——Dispatcher、TaskService、team 调度都不用改；最小可运行示例见
[app/backends/mock_adapter.py](app/backends/mock_adapter.py)（配置 `adapter: mock_harness` + `responses:`）。

### 4.4 本机 DeepSeek Harness 与 ZCode 的既有安装路径

沿用本机已安装版本（当前部署的实际路径，不是“必须如此”）：`DEEPSEEK_BACKEND=api|harness`；harness 用
`DEEPSEEK_HARNESS_BINARY=/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh`、
`DEEPSEEK_HARNESS_BASE_URL=https://api.deepseek.com/anthropic`、`DEEPSEEK_HARNESS_WORKDIR=.`；ZCode 用
`ZCODE_ENABLED=true`、`ZCODE_MODEL=glm-5.3`、`ZCODE_CATALOG=data/zcode_models.json`、
`ZCODE_NODE=/Applications/DeepSeek Harness.app/Contents/Resources/runtime/primary-runtime/dependencies/node/bin/node`、
`ZCODE_ENTRY=/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs`、
`ZCODE_BUILTIN=/Applications/ZCode.app/Contents/Resources/config/provider/zcode-builtin.json`；
`ZCODE_WORKSPACE_ROOT` 留空表示无原生文件工具（旧字段会被保守迁移，见 7.3）。Codex 沿用已有 ChatGPT 登录
（`CODEX_ENABLED` / `CODEX_BINARY` / `CODEX_WORKDIR`；`CODEX_ALLOWED_ROOTS` 是旧字段，迁移为管理员
read/edit/exec 授权）。所有执行器固定 argv、stdin 传问题、有输出上限与超时/取消清理进程组；DeepSeek harness 与
ZCode 使用本机应用与凭据（**凭据解析的已知问题见第 12 节**）。

### 4.5 Codex 适配器的受控 HTTP/SSE 连接（只作用于 `codex_cli`）

**观测原因**（`evidence/WebSocket握手失败.json`）：本机 `codex-cli 0.162.0-alpha.17.2` 的
Responses WebSocket 在 TLS 握手阶段 EOF，CLI 永远收不到 `turn.completed`，真实 DAG 的 codex
exec 步骤只能得到 `codex_failed`。父进程随后用**同一 ChatGPT 端点、同一现有 OpenAI 登录、同一模型
（`gpt-6.1-sol`）与同一权限 profile** 做了同源 HTTPS/SSE 诊断并真实通过
（`evidence/HTTP同源诊断成功.json`、`evidence/官方连接配置来源.md`）。

**实现**：`CodexAdapter.build_argv()` 在每次调用（broker/text、原生 read/edit、原生 exec）的 argv 里
追加六个 `-c` 覆盖，声明一个**自定义** provider（`app/agents/codex.py` 的 `http_provider_config()`）：

| 配置 | 值 | 作用 |
|---|---|---|
| `model_provider` | `"qqbot_http"` | 使用自定义 provider，不覆盖内置 `openai`（CLI 拒绝覆盖） |
| `model_providers.qqbot_http.name` | `"QQBot HTTP"` | 仅本机器人可见的名字 |
| `model_providers.qqbot_http.base_url` | `"https://chatgpt.com/backend-api/codex"` | 与已实测成功路径同源，HTTPS |
| `model_providers.qqbot_http.requires_openai_auth` | `true` | 沿用现有 OpenAI 登录，不新增 `env_key`/明文令牌 |
| `model_providers.qqbot_http.supports_websockets` | `false` | 关闭握手 EOF 的 Responses WebSocket |
| `model_providers.qqbot_http.wire_api` | `"responses"` | HTTPS/SSE 的 Responses 语义 |

边界与不变项：`--ignore-user-config` 始终在 argv 中，配置只存在于**本次调用**，不读也不写用户
`~/.codex/config.toml`；模型、账号、TLS 校验与 `qq_ro`/`qq_files` 权限 profile、`approval_policy`、
能力探测闸门全部不变；deepseek/zcode 等其它后端不生成该配置块。`http_provider_blockers()` 是合同检查：
只要 argv 里出现 provider 配置，就拒绝 false 认证、`env_key`、明文令牌、关闭 TLS 校验、非 https、
`supports_websockets=true` 或覆盖内置 `openai` 的写法；`_sandbox_ready()` 在启动前用它兜底。

---

## 5. 团队协作（`/team`）

### 5.1 子命令

| 子命令 | 说明 |
|---|---|
| `create <目标>` | 新建草稿，返回团队 ID（形如 `t7a91c20d`） |
| `add <团队ID> <步骤ID> <agent> [选项] -- <本步任务>` | 添加或**完整替换**一步；同 ID 重发连旧定义与依赖一起替换，并让相关旧确认失效 |
| `remove <团队ID> <步骤ID>` | 删除草稿步骤；仍有下游依赖时拒绝，要求先调整这些步骤的 `--after` |
| `plan <团队ID>` | 显示每步后端/模型/强度/权限/目录/依赖、并行层次、同目录互斥与阻塞原因 |
| `run <团队ID>` | 校验后冻结计划并排入后台执行；重复 run 不新建轮次 |
| `status [团队ID]` | 查看进度；省略 ID 时列出自己在本群最近 10 个任务 |
| `result <团队ID> [步骤ID]` | 取当前轮次结果，按计划顺序而非完成顺序返回 |
| `cancel <团队ID>` | 取消草稿或活动轮次；已完成结果保留，文件变更不回滚；重复 cancel 不报错 |
| `retry <团队ID>` | 仅 failed/interrupted 可用；开启新轮次，复用成功步骤结果，只重跑失败/受阻步骤 |

ID 规则：团队 ID `^t[0-9a-f]{8}$`，步骤 ID `^[a-z][a-z0-9_-]{0,31}$`。

### 5.2 每步权限、目录与冻结语义

`add` 的公共选项与 `/task` 完全一致，只多 `--after`：

```text
/team create 修复项目问题并验证
/team add <团队ID> inspect deepseek --mode read --dir "/Users/example/Documents/你的项目" -- 分析问题并提出最小改动
/team add <团队ID> fix     zcode    --after inspect --mode edit --dir "/Users/example/Documents/你的项目" -- 根据上游结论修改文件
/team add <团队ID> verify  codex    --after fix     --mode exec --dir "/Users/example/Documents/你的项目" -- 执行配置允许的测试
```

- 不写 `mode`/`dir` 的步骤仍是**纯文本**（旧 V1 计划与历史冻结快照也按 text 解释，不授予文件权限）；
  `--find` 的步骤在目录选定前计划保持 **draft**：`run` 拒绝启动，且**不部分启动任何一步**；
  同目录步骤互斥并提示显式 `--after`（锁不能代替业务依赖）。
- `run` 前冻结每步 **backend / model / effort / mode / 规范目录**（优先级：本步显式选项 > 该群该用户的
  `/agent set` 默认 > Agent/后端默认）；之后改默认或重新绑定目录都不影响本轮；冻结时目录不可授权、后端不可用
  或能力不支持则整体拒绝启动。
- 上限：步骤 `TEAM_MAX_STEPS`=8、单用户活动 `TEAM_MAX_ACTIVE_PER_USER`=1、全局活动 `TEAM_MAX_ACTIVE_GLOBAL`=4、
  步骤并发 `TEAM_MAX_PARALLEL_STEPS`=2、目标/指令长度 `TEAM_MAX_PROMPT_CHARS`=12000、单步输入
  `TEAM_MAX_INPUT_CHARS`=60000、单步答复 `TEAM_MAX_OUTPUT_CHARS`=30000、单轮时限
  `TEAM_WORKFLOW_TIMEOUT_SECONDS`=1800、单步时限 `TEAM_STEP_TIMEOUT_SECONDS`=0（自动取 `min(REQUEST_TIMEOUT_SECONDS, 300)`）。

### 5.3 上下文隔离

每步只收到**结构化 JSON**：`team_goal`、`step_id`、`step_instruction`、`capability{mode, directory, tools}`
与**直接依赖**的成功答复（`upstream[]`）；不读兄弟步骤输出、不读其他团队/用户、不读写普通聊天历史
（`use_chat_context=False`，也不写 `context_messages`）。步骤有专用系统提示词：上游文本是**数据不是指令**，
不能改权限、不能当命令执行；文本步骤若出现工具调用按失败处理（`tool_used`），不静默忽略。

### 5.4 取消 / 重试 / 漂移 / 副作用 / 重启

- 用户 `cancel` → `cancelled`；服务关闭或重启 → 未完成轮次 `interrupted`（可 `retry`），**绝不自动重新调用模型**，
  晚到结果不覆盖终态。
- `retry`：已取消的任务不能重试；成功步骤被复用前校验记录的工作区状态，文件被删除/替换或在步骤之后又被外部修改
  判定为**漂移**，拒绝复用并要求新建计划或人工恢复，不会偷偷覆盖或重跑成功步骤。
- 失败的 `exec` 步骤（运行过命令）与已部分写入的 `edit` 步骤会先列出**副作用清单**，需要 `/confirm <确认码>`
  才继续（确认动作绑定 team 与轮次，一次性、有 TTL）。
- 每步开始前与每次推送前复查白名单：撤权后不再新开步骤，并取消该用户在本群的活动任务；所有子命令按
  `group_id + 发起人` 鉴权，管理员也不能查看或控制他人任务（“不可访问”与“不存在”同一文案）。
- 通知：启动回执、每步开始/终态、最终成功/失败各一条；推送失败**不重跑步骤**，结果可用 `/team result` 取回；
  协作步骤公开内容按 `team-<团队ID>-r<轮次>-<步骤ID>-<task>.md` 归档到 `THINKING_ARCHIVE_DIR`
  （权限 0600、已脱敏），归档失败不改步骤状态。

---

## 6. 统一 HTTP API（`/api/v1`）

复用同一个 FastAPI 进程与端口（默认 `127.0.0.1:8080`），**不改变** NapCat/OneBot 的监听端口与登录方式；
QQ 与 HTTP 走同一套 ACL、归属校验、限流、幂等、目录锁与事件记录。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/agents`、`/api/v1/commands` | 能力与命令发现（与 `/help` 同源） |
| POST | `/api/v1/tasks` | 提交任务，返回 `202 {"task_id","status":"queued"}`；支持 `Idempotency-Key` |
| GET / POST | `/api/v1/tasks/{id}`、`/{id}/result`、`/{id}/cancel` | 状态；结果 + 文件变更清单；取消 |
| POST | `/api/v1/workspaces/find`、`/api/v1/workspaces/select` | 与 `/workspace` 同一确认服务 |
| POST | `/api/v1/confirmations/{code}` | 与 `/confirm` 同一确认服务（目录选择码需走 workspaces/select） |
| POST | `/api/v1/teams`；PUT/DELETE `/teams/{id}/steps/{step}` | 创建团队草稿（201）；添加/替换、删除步骤 |
| GET | `/api/v1/teams/{id}/plan`、`/teams/{id}`、`/teams/{id}/result` | 计划、状态、结果 |
| POST | `/api/v1/teams/{id}/run`、`/cancel`、`/retry` | 生命周期动作 |

**Token 身份映射**（YAML 只写环境变量名，真实值放 `.env`）：

```yaml
api:
  tokens:
    - {name: local-admin, token_env: QQ_CONSOLE_API_TOKEN, user_id: 123456789,
       group_ids: [987654321], scopes: [task, team, workspace], admin: true}
```

```bash
export QQ_CONSOLE_API_TOKEN=<自行生成的随机值>
curl -s -H "Authorization: Bearer $QQ_CONSOLE_API_TOKEN" http://127.0.0.1:8080/api/v1/agents | head -c 400
curl -s -X POST http://127.0.0.1:8080/api/v1/tasks -H "Authorization: Bearer $QQ_CONSOLE_API_TOKEN" \
  -H "Content-Type: application/json" -H "Idempotency-Key: demo-1" \
  -d '{"agent":"deepseek","prompt":"看一下目录结构","dir":"/Users/example/Documents/你的项目","mode":"read"}'
```

约定：缺失/错误 Token → `401`（`missing_token`/`invalid_token`）；scope 不足或群不在 Token 允许列表 → `403`；
**未配置任何 Token 时 `/api/v1` 返回 `503`（不会无鉴权开放）**；正文里的 `user_id`/`admin`/`group_id` 一律忽略，
以 Token 映射为准；任务不存在与不归本人返回同一个 `404 task_not_found`；GET 不返回绝对路径与密钥。
`Idempotency-Key` 在「群 × 用户」维度去重（重复提交返回同一 `task_id` 且 `idempotent: true`）；目录未选定时
`POST /tasks` 返回 `status: awaiting_selection` + `selection_code` + `candidates`。

`/healthz` 返回 `{"status":"ok"}`（**不是**旧 QQ 口令）；`/status` 保留为服务状态接口，含 `schema_version /
transport / agents / backends（native_modes、broker_modes、capabilities、isolation）/ commands / presets /
workspaces{roots, notes, max_workers, cross_process_lock, active_locks} / config_notes / pending_confirmations /
api_v1_enabled / limits`。

---

## 7. 配置参考

### 7.1 双轨与优先级

`.env` 由 pydantic-settings 读取；`CONFIG_FILE=config.yaml` 指定 YAML，**YAML 会以 `os.environ.setdefault` 注入**。
因此实际生效顺序是：**真实环境变量 > CONFIG_FILE 的 YAML > `.env`**——这与[操作指南](outputs/操作指南.md) 与
[config.example.yaml](config.example.yaml) 注释里写的“真实环境变量 > .env > YAML”不同（见第 12 节）。
结构化配置（`backends/agents/workspaces/commands/api/team/limits`）建议只写在 YAML，也可用 `CONSOLE_BACKENDS` /
`CONSOLE_AGENTS` / `CONSOLE_WORKSPACES` / `CONSOLE_COMMANDS` / `CONSOLE_API` 传整段 JSON 临时覆盖。

### 7.2 常用配置项

| 键（YAML / 环境变量） | 默认 | 说明 |
|---|---|---|
| `qq.allowed_user_ids` / `QQ_ALLOWED_USER_IDS` | 空 | 允许调用模型的用户（也是管理员）；留空只能 `/help` |
| `qq.allowed_group_ids` / `QQ_ALLOWED_GROUP_IDS` | 空 | 允许响应的群；留空所有群静默 |
| `qq.ignore_user_ids` / `QQ_IGNORE_USER_IDS` | 空 | 显式屏蔽（如群内其他机器人） |
| `storage.database_path` / `DATABASE_PATH` | `data/bot.db` | SQLite 路径 |
| `transport.*` / `TRANSPORT`、`ONEBOT_*` | `onebot_ws`、`ws://127.0.0.1:3001` | OneBot 接入（9.1） |
| `server.host`/`port`、`HTTP_LISTEN_HOST`/`PORT` | `127.0.0.1:8080` | 本服务监听（`/status`、HTTP 模式事件上报） |
| `workspaces.*`（roots / find / edit_enabled / exec_enabled / max_workers / lock_timeout_seconds） | `[]` / 深度 4·候选 8·条目 20000·2 秒·TTL 300 秒 / `true` / `1` / `30` | 目录授权与查找预算、全局开关、worker 数（>1 必须提供跨进程锁）、锁超时 |
| `commands.exec.*` / `commands.broker.*` / `commands.presets` | 见 7.4–7.5、3.5 | 受控命令、broker 预算、自定义口令 |
| `api.tokens` / `CONSOLE_API` | `[]` | HTTP API Token 映射（第 6 节） |
| `limits.request_timeout_seconds` / `max_context_turns` / `max_message_length` | `60` / `8` / `1500` | 调用超时、上下文轮数、单条消息上限（超出自动分段） |
| `limits.max_thinking_chars` / `thinking_archive_dir` | `800` / `runtime/state/thinking` | 群内推理摘要上限（`0` = 不发，归档不受影响）；归档目录（留空不归档） |
| `limits.shared_agent_context` | `false` | 同一「群 × 用户」共享线程（9.5） |
| `limits.rate_limit_per_minute` / `approval_ttl_seconds` | `6` / `300` | 每用户每分钟调用上限；确认码默认 TTL |
| `team.*` / `TEAM_*` | 见 5.2 | 协作上限与超时 |
| `logging.level` / `LOG_LEVEL` | `INFO` | 日志级别 |

完整清单见 [.env.example](.env.example) 与 [config.example.yaml](config.example.yaml)。

### 7.3 旧式目录字段的保守迁移

`FILE_READ_ROOT`、`FILE_WRITE_ROOT`、`CODEX_ALLOWED_ROOTS`、`ZCODE_WORKSPACE_ROOT` 仍可识别，
但未配置 `workspaces.roots` 时按**保守规则**迁移（`app/workspaces/configurator.py`）：

| 旧字段 | 迁移后成员权限 | 迁移后管理员权限 |
|---|---|---|
| `FILE_READ_ROOT` | **无**（旧 `/read` 是管理员命令，迁移不扩大给成员） | read |
| `FILE_WRITE_ROOT` | 无 | read / edit |
| `CODEX_ALLOWED_ROOTS` | 无 | read / edit / exec |
| `ZCODE_WORKSPACE_ROOT` | 无 | read / edit |

**只读根绝不会升级为可写根**；差异逐条打印在启动日志与 `/status` 的 `workspaces.notes`。新旧同时存在时
**不静默选择**：`workspaces.roots` 生效，被忽略的旧字段逐条列出（两套权限不会悄悄并存）。旧式 per-agent 字段
（`GLM_*`、`DEEPSEEK_*`、`ZCODE_*`、`CODEX_*`）同样迁移为等价后端并输出冲突说明，建议迁移后删除旧字段。

### 7.4 exec 白名单与 presets

```yaml
commands:
  exec:
    enabled: true
    # exec 必须在可验证隔离内运行（macOS sandbox-exec）。
    # 默认策略：写限任务目录与任务私有临时目录；禁网；
    # 读取默认拒绝（只有系统运行库、解释器自身/venv 与任务目录可读），
    # 任意位置的 .env*、密钥/凭据文件与受保护路径（含工作区内部的 protected 子树）一律拒绝；
    # 平台不支持隔离时 exec 会被明确禁用；require_sandbox=false 仅供本地排查。
    require_sandbox: true
    allow_network: false
    allowlist: [pytest, python3, node, npm, pnpm, make, ls, cat, git, ruff, mypy]
    max_timeout_seconds: 300
    max_output_bytes: 262144
    allow_network_tools: false
  broker:
    max_actions: 40
    max_rounds: 8
    max_total_read_bytes: 262144
    max_file_bytes: 131072
    max_search_entries: 2000
    max_task_seconds: 300        # 多轮会话整体期限（含锁等待、所有轮次与工具）
  presets:
    review:    {description: 用 deepseek 只读评审目录, agent: deepseek, mode: read,
                template: "请评审以下目录改动，重点关注兼容性：{task}"}
    summarize: {description: 纯文本总结（无工具）, agent: gpt, mode: text, template: "请总结：{task}"}
```

执行器用**参数数组**启动（`shell=False`，没有 shell 注入面）；工作目录必须在本次任务授权目录内，可执行文件必须在
白名单内；默认拒绝 `sh/bash/sudo/su`、`curl/wget/nc/ssh/scp/rsync`、`docker/kubectl/mount`、
`chmod/chown/dd/kill/launchctl/osascript` 等；参数中的越界路径或敏感文件名同样拒绝；环境只保留
`PATH/HOME/LANG/TMPDIR/…` 等安全键；超时按**进程组** SIGKILL 清理，输出截断到上限。未显式设置
`commands.exec.enabled: false` 时**受控 exec 默认可用**（走 broker）；**原生** exec 不在任何内置适配器上开启。
preset 只做文本替换（不 eval、不生成 Shell），固定 agent/mode/backend，默认只允许覆盖 `--dir`/`--find`
（`text` 模式连目录都不允许），展开后仍受调用者自身 ACL 限制；新增业务动作则是「一个 handler + 一个 `CommandSpec`」，
注册表在启动时检查 handler 是否实现、名字是否与命令名/保留名/旧口令冲突。

---

## 8. 系统架构

分层（自上而下，每层只依赖下一层）：

1. **接入**：`app/transports/`（OneBot v11 WS/HTTP）与 `app/api/v1`（HTTP + Token 身份映射）；
2. **命令**：`app/routing/dispatcher.py` + `app/commands/`（解析、鉴权、限流、迁移提示）；
3. **服务**：`app/tasks/service.py`（唯一执行入口）与 `app/workflows/`（DAG 调度、冻结计划、通知）；
4. **策略**：`app/security/`（ACL、路径边界、确认码、exec 策略、脱敏）与 `app/workspaces/`（授权根、绑定、模糊查找、目录锁、broker、变更记录）；
5. **执行**：`app/backends/`（BackendRegistry/Descriptor、ExecutionGateway、broker 动作循环）→ `app/agents/`（各协议适配器）；
6. **存储**：`app/storage/context_store.py` 与 `app/tasks/store.py`、`app/workflows/store.py`（同一条 SQLite 连接）。

| 目录 | 职责 |
|---|---|
| `app/commands/` | 命令与选项的声明式注册表：`/help`、`/command`、HTTP discovery、命令清单的唯一来源 |
| `app/routing/` | `dispatcher.py`：命令路由、鉴权、限流、迁移提示、回复分段与脱敏（不再实现文件读写） |
| `app/workspaces/` | 授权根配置与迁移、绑定、模糊查找、全入口目录锁、公共文件 broker、变更记录 |
| `app/backends/` | `descriptor/protocol/registry/factory/builtin/gateway/broker_loop`：三层模型、能力描述、执行网关、动作循环 |
| `app/tasks/` | TaskService（单任务/广播/team 步骤唯一入口）与 TaskStore（tasks/task_events/workspace_bindings/file_changes） |
| `app/security/` | ACL、路径边界与敏感清单、**根 fd 安全打开（`safeopen.py`）**、确认码、exec 策略与 **Seatbelt 沙箱（`sandbox.py`）**、限流、脱敏、文本消毒、鉴权 |
| `app/workflows/` | `/team`：`models/parser/store/executor/runner/notifier/service/handler`（6 张 team_* 表） |
| `app/api/`、`app/agents/`、`app/storage/`、`app/transports/` | HTTP 端点与身份映射；适配器与系统提示词；SQLite 上下文/偏好/白名单；OneBot 传输 |
| `scripts/`、`tests/` | 生成命令清单的脚本；离线 pytest 回归测试（假适配器 + 临时目录） |

设计原则：Transport、Backend adapter、Dispatcher、Storage、Security 相互解耦；任务、目录锁、确认码、
能力判定全部收敛到 `TaskService`，`/task`、`/task all`、`/team` 与 HTTP 共用同一条执行路径。

---

## 9. OneBot 接入、风控与安全

### 9.1 OneBot 接入（不改端口、不改登录方式）

完全遵循 OneBot v11 协议，不绑定具体客户端；**安装与登录 QQ 客户端由你本人完成**：

1. 自行选择并安装一个 OneBot v11 实现（**NapCat**、**Lagrange.Core**、**LLOneBot**、旧版 go-cqhttp 等），
   用你的（建议小号）QQ 账号登录该实现（本项目不会、也不应替你执行任何扫码/Cookie/凭据操作）；
2. 开启服务端并与本程序对应——**WS 模式（推荐，默认）**：开启“正向 WebSocket 服务端”，端口 `3001`，
   设 `TRANSPORT=onebot_ws`、`ONEBOT_WS_URL=ws://127.0.0.1:3001`（本程序作为客户端，断线自动重连）；
   **HTTP 模式**：开启“HTTP 上报”到 `http://127.0.0.1:8080/onebot/event`，同时开启 OneBot 的 HTTP API 服务
   （如 `http://127.0.0.1:5700`），设 `TRANSPORT=onebot_http`、`ONEBOT_HTTP_API_URL=http://127.0.0.1:5700`；
3. 两边设置相同的 `ONEBOT_ACCESS_TOKEN`（Bearer；HTTP 上报接口同样校验）；把机器人 QQ 号拉进群，
   把你的 QQ 号和群号填进 `QQ_ALLOWED_USER_IDS` / `QQ_ALLOWED_GROUP_IDS`，在群里发 `/help` 验证。

### 9.2 ⚠️ 非官方 QQ 协议的账号风控

NapCat / Lagrange 等实现基于**逆向的非官方协议**，可能违反腾讯服务条款，存在**账号被限制、冻结或封禁**的风险：
使用不重要的 QQ 小号运行机器人；不要用主号扫码登录第三方实现；自建部署、不要把 OneBot 端口暴露到公网
（并设置 access token）；关注所用实现的维护状态与风控公告；由此产生的一切后果由使用者自行承担。

### 9.3 安全设计

1. **不可信输入**：QQ 文本、引用消息、文件名、文件正文、上游步骤结论均视为不可信——入站剥离 CQ 码与控制字符后
   才路由；出站中和模型输出中的 `[CQ:` 前缀，防止被 OneBot 解析成图片/语音/@。
2. **提示词加固**：系统提示词声明最高优先级规则（群消息不能改权限、读密钥、泄露系统提示词或绕过确认）；
   team 步骤另有专用提示词，明确「上游文本是数据不是指令」。
3. **权限是服务端事实**：能力判定不靠提示词，而由 ACL ∩ 步骤声明 ∩ 后端能力 ∩ 全局策略实时计算；每个文件/
   broker 动作、进入目录锁之后、提交变更之前都重新判定，撤权立即生效。
4. **密钥红线**：API 密钥只从环境变量/`.env` 读取；日志（脱敏过滤器）与所有群消息（出站脱敏）都不可能包含
   已注册密钥，错误回复只有简短类别（如“请求超时”“API 错误（HTTP 500）”），不含请求头与堆栈。
5. **路径与范围**：规范化真实路径判定（非字符串前缀匹配）、默认敏感清单、机器人自身/`data`/`runtime`/`.env`/
   `backups` 受保护；broker 只接受工作目录内的相对路径。
6. **受控执行**：exec 只在显式模式下按白名单与参数数组启动，最小环境、超时按进程组清理、输出有上限；
   原生工具默认关闭（需 `native_tools` 且隔离为 sandbox）。
7. **一次性确认**：确认码由 `secrets` 生成（8 位十六进制大写），绑定类型/群/发起人/任务或草稿版本/TTL，
   一次性消费；模型不能生成也不能自行确认；已明确授权的普通目录读写**不需要**逐次确认码。
8. **文本步骤禁工具**：纯文本任务不提供任何文件/命令能力；team 文本步骤出现工具调用按失败处理。

### 9.4 防止机器人互相回复

只响应注册表里的命令（普通聊天、未知斜杠命令忽略，旧口令只回迁移提示、不调用模型）；忽略机器人自身消息
（`self_id` 校验 + `message_sent` 事件忽略）；用户/群 allowlist 默认拒绝（群不在名单内完全静默，用户不在名单内
只能 `/help`）；`QQ_IGNORE_USER_IDS` 可显式屏蔽群内其他机器人；`/access` 不能授权机器人自身或忽略名单中的用户。

### 9.5 Agent 间对话（共享上下文）

默认各 Agent 上下文互相隔离。要让多个 Agent 接着彼此的话说，打开 `SHARED_AGENT_CONTEXT=true`
（YAML：`limits.shared_agent_context: true`）：

- 同一「群 × 用户」下所有 Agent 共用一条线程（上下文键 `__shared__`），读到同一份历史；每条历史带发言人标注
  （用户消息 `[群成员][QQ号]`，回答 `[Agent][显示名]`）；
- `/task all` 由并发改为**串行**，后一个 Agent 能看到前一个的回答；`/agent clear <任一 Agent>` 清空整条共享线程；
  群与用户之间**仍然隔离**；
- 注意：共享线程意味着调用某个 Agent 会把该线程最近若干轮一并发给它的提供方，请按群隐私预期决定是否开启；
  轮数仍受 `MAX_CONTEXT_TURNS` 限制。team 步骤**不使用**这条线程（见 5.3）。

---

## 10. 测试与验证

```bash
.venv/bin/python -m pytest -q
```

测试全部**离线**运行（无需网络与 QQ 账号），使用 FakeAgent / RecordingTransport / 临时数据库与临时目录，覆盖
命令注册与解析、旧口令只回迁移提示、ACL 与路径边界、目录锁与父子目录互斥、模糊查找与选择确认、broker 动作协议
与预算、变更哈希与备份、exec 策略拒绝、后端能力与 `capability_not_supported`、`/task` 各模式、`/team` 冻结/
DAG/取消/重试/漂移/副作用确认、HTTP API 鉴权与归属隔离、脱敏与分段等。

**最新状态（2026-10-12 协议补丁后）**：完整回归 **866 passed / 1 skipped / 0 failed**
（跳过项为隔离副本不含 `.env`，来源 `evidence/已完成部署与验收.md`）。

**2026-10-12 连接层收尾补丁（本副本）**：两处必要改动——(1) Codex 适配器 argv 增加受控
HTTP/SSE provider（见 4.5）；(2) `scripts/verify_final_delivery.py` 的 `scaffold_exec_instruction()`
改用解释器命令名（`python3`/`python`，不再把 `/Users/.../Documents/...` 下的绝对解释器路径交给模型），
`run_verification()` 用 `async with interpreter_on_path()` 在本验证进程内把 `sys.executable` 所在目录
前置到 `PATH`，正常结束/异常/取消都会恢复原值，因此 exec 步骤里的 `python3 -m pytest` 仍是同一个项目
venv，且不放松任何工具权限。新增合同测试 `tests/test_codex_http_provider.py`、
`tests/test_final_delivery_interpreter.py`；上一轮独立回归为 885 passed / 1 skipped，本轮完整回归须在
项目 venv 内重跑（**本副本不含 venv 依赖，未在此副本执行全量 pytest**，如实记录见
`evidence/连接层收尾补丁.md`）。统一最终验证入口：

```bash
# 离线全量：完整 pytest + 真实 create_app/lifespan + 端口 + 受控 OneBot 模拟 + 备份/回退演练
.venv/bin/python scripts/verify_final_delivery.py --json-out outputs/验收复现/统一最终验证.json

# 真实调用（显式授权 + 预算；预算=真实 adapter 调用次数上限、按次硬计数，临时库/目录/假 QQ，不发真实消息）
.venv/bin/python scripts/verify_final_delivery.py --run-real --budget 30 \
  --real-json-out outputs/验收复现/统一最终验证-真实调用.json
```

结论与逐项口径以 `outputs/统一控制台最终交付矩阵.md` 为单一权威表；`outputs/统一控制台测试报告.md`
中的 719/657/624 等为历轮快照。

**离线全绿不等于真实模型或 QQ 链路已验证**：真实 QQ 上的新版统一口令、完整三后端真实 DAG 与生产部署
仍需独立授权执行（见最终交付矩阵 §3/§4）。

| 层次 | 手段 | 能证明什么 |
|---|---|---|
| 离线 | `pytest` + 假适配器 + 临时目录 | 权限、锁、协议、状态机、变更记录等**代码行为** |
| 真实后端 | 真实凭据在**临时目录**跑 `/task <agent> --dir <临时目录> --mode read\|edit\|exec -- …` | 该后端与凭据是否真的可用 |
| QQ 链路 | 白名单用户在真实群里发命令 | OneBot 接入、收发与风控现状 |

报告归档位置：`outputs/统一控制台测试报告.md`（实施/测试方产出）与 `outputs/统一控制台Codex验收报告.md`
（验收方独立产出）。实施记录见 `outputs/统一控制台实施记录.md`，验收对应表见 `outputs/统一控制台交付清单.md`。

---

## 11. 备份与回滚

- 本次实施前的源码快照：`backups/pre-unified-console-20261010-151327/`，含 `app/`、`tests/`、配置示例、
  `.env` 副本与数据库一致性备份 `bot.db.consistent-backup`。
- 数据库备份必须用 SQLite backup API（不能只复制运行中的 `bot.db` 而漏掉 WAL）：

  ```bash
  python3 - <<'PY'
  import sqlite3
  src = sqlite3.connect("file:data/bot.db?mode=ro", uri=True)
  dst = sqlite3.connect("data/bot.db.backup")
  with dst:
      src.backup(dst)
  dst.close(); src.close()
  PY
  ```

- 回滚：① 源码用上面的快照覆盖 `app/`、`tests/` 与配置示例；② 配置恢复 `config.yaml` / `.env` 到快照副本
  （schema 迁移只新增表/列，回滚代码不需要删表）；③ 数据库先停服务，再用一致性备份恢复 `data/bot.db`
  （恢复前确认不会覆盖用户后续改动）。详细步骤见[操作指南](outputs/操作指南.md) 第 10 节；实施记录预期归档在
  `outputs/统一控制台实施记录.md`（截至本次改写该文件尚未出现在 `outputs/` 中）。

---

## 12. 已知限制与文档差异（如实记录）

**能力与验证边界：**

- **目录锁只在单进程内有效**：跨进程锁**未实现**，`max_workers` 只允许 1；声明 >1 或
  `cross_process_lock: true` 都会在启动时报错。
- **exec 依赖平台隔离**：在 macOS 上用 Seatbelt（`sandbox-exec`）强制"写限任务目录、禁网、**读取默认拒绝**、
  受保护路径不可读写"；读取只放行系统运行库、解释器自身（含 `.venv`）与任务目录，任意位置的 `.env*`、密钥与凭据
  都会被拒绝。平台没有等价隔离时 **exec 被明确禁用**，不会退回无隔离执行（详见 `app/security/sandbox.py`）。
  两个已知折中：① Seatbelt 的多分支正则组会静默失配，因此敏感规则"一形态一条规则"，新增敏感名必须同步改
  `app/security/paths.py`；② 目录元数据仍需可读，exec 内的 `ls` 能看到敏感文件的**名字**（内容读不到）。
- **文件打开不按路径字符串**：read/list/search/备份/原子替换/删除统一走根 fd + 逐级 `O_NOFOLLOW` 打开
  （`app/security/safeopen.py`），父目录在检查之后被替换也不会越界；父目录授权不覆盖更具体的子目录授权。
- **单文件读取上限**：超过 8MiB 的文件 `read` 明确失败（避免用片段摘要冒充全文件版本），可改用 `search`。
- **原生工具默认关闭且当前不可放行**：Codex/ZCode 的原生 read/edit 实测未达可用条件
  （Codex 原生 read 启动成功但读不到文件，且 read/edit 共用可写 profile），因此
  `native_modes` 已清空——能力发现不会再声明它们，生产 `native_tools` 保持 `false`。
  文件读写统一走公共 broker（read/edit/exec 都可用）。
- **Codex CLI 能力探测**：适配器只下发本机 CLI 真实存在的能力开关，并把未识别的名字与
  脱敏后的 stderr 摘要写进诊断；不会再出现"CLI 因未知 feature flag 直接退出、只显示 codex_failed"。
- **ZCode 默认强度**：带工具的模式默认 `low`（`max` 实测在 60s 级预算下 edit/exec 超时），
  可用 `backends.zcode.mode_efforts` 或 `/task zcode --effort max …` 覆盖。
- **ZCode 原生读取边界待复核**：ZCode 协议对读类工具没有门禁（实测 workspace 外读取不触发权限请求），默认走
  broker；启用 ZCode 原生工具前必须完成真实环境的隔离复核。
- **真实后端已验证的部分**（2026-10-12 协议补丁后更新）：DeepSeek Harness / ZCode / Codex 三个真实后端
  的 **QQ text** 均已验；DeepSeek `deepseek-flash` 的 text/read/edit/exec、ZCode `glm-5.3` 的
  text/read/edit/exec/team、跨模型 team（DeepSeek read → ZCode edit）、本机 TCP HTTP 鉴权/幂等/正文
  已在临时工作区/库 + 假 QQ 下实测。依据：`evidence/已完成部署与验收.md`。
  **仍未验证**：完整三后端 read→edit→Codex exec 一条真实 DAG、真实 `create_app`/lifespan HTTP 入口、
  cancel/retry/重启 interrupted/确认失效的真装配演练、一致性备份恢复与代码回退演练（生产库/生产部署均未演练）、
  原生工具、OpenAI 兼容后端的真实调用。统一验证入口 `scripts/verify_final_delivery.py`
  （离线默认；真实调用必须 `--run-real --budget N`），口径见 `outputs/统一控制台最终交付矩阵.md`。
- **离线验证的边界**：其余结论仍来自离线验证（假适配器 + 临时目录），未擅自扩大调用范围；
  未改动生产配置；`/status` 返回 `ok` 不等于 QQ 已登录或模型可用。
- **确认码在内存中**：重启后待确认/待选择记录失效，未完成任务标记 `interrupted`，需显式重试。
- **不承诺原子回滚**：多文件提交逐文件记录状态，取消或失败都可能留下部分写入，恢复靠受控备份目录
  （`runtime/state/backups/<task_id>/`）里的备份文件；exec 命令直接产生的改动通过前后快照记录（2000 文件上限）。

**本轮核对发现并已修复的问题（保留记录，便于验收复查）：**

1. **凭据来源**（已修复）：后端工厂原先只看进程环境变量，而 `.env` 由 pydantic-settings 读取、不会写回
   `os.environ`，导致"密钥只在 `.env`"的部署被判为未配置。现在有两道保障：
   ① `load_settings()` 会把 `.env` 载入进程环境（不覆盖真实环境变量）；
   ② `BackendConfig.credentials` 由 `Settings` 注入，`BackendConfig.env()` 的顺序是
   **进程环境变量 > `.env`（Settings）> YAML**。回归用例：
   `tests/test_config_migration.py::test_credentials_from_settings_are_used_when_env_missing` 等 3 条。
2. **配置优先级**（已修复）：`.env` 现在先于 YAML 注入 `os.environ`，实际行为与文档一致：
   **真实环境变量 > `.env` > YAML**（YAML 只对未设置的键生效）。
3. **`/agent set --backend` 持久化**（已修复）：`agent_preferences` 新增 `backend` 列（可重复迁移，旧库缺列自动补齐），
   `TaskService.resolve_selection` 的后端优先级为 **本次 `--backend` > 保存的后端 > Agent 默认后端**。
4. **测试集**：见第 10 节；该轮历史值为 **624 passed**，最新为 **866 passed / 1 skipped**（协议补丁后，
   见第 10 节与 `evidence/已完成部署与验收.md`；329 条改造前用例的等价迁移用例仍全部保留）。
5. **历史代码清理**（已完成）：`app/routing/commands.py`、`app/security/file_access.py`、
   `app/security/tool_policy.py` 已删除；`app/routing/__init__.py` 只导出 `Dispatcher`；
   `.env.example` 中 `/read`·`/write` 的旧注释已改写为统一控制台说明。
6. `.env` 与 `config.example.yaml` 仍保留旧式目录字段（`FILE_READ_ROOT` 等），仅用于 7.3 的保守迁移；
   新部署请直接使用 `workspaces.roots`。
7. `outputs/统一控制台实施记录.md`、`outputs/统一控制台测试报告.md`、`outputs/统一控制台交付清单.md` 已在本轮生成；
   `outputs/统一控制台Codex验收报告.md` 由验收方独立产出。
