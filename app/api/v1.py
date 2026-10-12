"""统一 HTTP API（/api/v1）。

与 QQ 共用同一套 ACL、TaskService、确认服务、幂等键与目录锁：
- 必须携带 Bearer Token；Token 在配置中映射到可信的群/用户身份与 scope；
- 请求正文中的 user_id/admin/group 伪造一律忽略（以 Token 映射为准）；
- 任务不存在与不归本人返回同一安全错误；
- 列表默认不返回绝对路径与密钥。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, Body, Header, HTTPException, Request, status

from app.routing.qq_dedup import (
    HTTP_KEY_SCHEME,
    http_namespaced_key,
)
from app.tasks.submission import (
    DuplicateSubmission,
    PendingSelection,
    SubmissionError,
    options_from_payload,
)

logger = logging.getLogger("qqbot.api")

MIN_SCOPE_TASK = "task"
MIN_SCOPE_TEAM = "team"
MIN_SCOPE_WORKSPACE = "workspace"


class ApiIdentity:
    """Token 映射出的可信身份。"""

    __slots__ = ("name", "user_id", "group_ids", "scopes", "admin")

    def __init__(self, *, name: str, user_id: int, group_ids: tuple[int, ...],
                 scopes: frozenset[str], admin: bool = False) -> None:
        self.name = name
        self.user_id = user_id
        self.group_ids = group_ids
        self.scopes = scopes
        self.admin = admin

    def allows_scope(self, scope: str) -> bool:
        """F08：scope 必须显式授予；没有列出 scope 的 Token 只能做只读发现。"""
        return scope in self.scopes

    def group(self, requested: Any) -> int:
        """校验请求中的群归属；不在 Token 允许列表内一律拒绝。"""
        if not self.group_ids:
            raise HTTPException(status_code=403, detail="token_scope_denied")
        if requested is None:
            return self.group_ids[0]
        try:
            group_id = int(requested)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="invalid_group") from None
        if group_id not in self.group_ids:
            raise HTTPException(status_code=403, detail="token_scope_denied")
        return group_id


def build_identity_table(config: Any) -> dict[str, ApiIdentity]:
    """从配置构建 token -> 身份映射。"""
    import os

    raw = config if isinstance(config, dict) else {}
    tokens = raw.get("tokens") or []
    table: dict[str, ApiIdentity] = {}
    if not isinstance(tokens, list):
        raise ValueError("api.tokens 必须是列表")
    for index, item in enumerate(tokens):
        if not isinstance(item, dict):
            raise ValueError(f"api.tokens[{index}] 必须是映射")
        token = str(item.get("token") or "")
        env_name = str(item.get("token_env") or "")
        if not token and env_name:
            token = os.environ.get(env_name, "")
        if not token:
            raise ValueError(
                f"api.tokens[{index}] 需要 token_env（推荐）或 token；未找到凭据时拒绝启动。"
            )
        user_raw = item.get("user_id")
        if user_raw is None:
            raise ValueError(f"api.tokens[{index}] 必须声明 user_id（可信身份）")
        groups = item.get("group_ids")
        table[token] = ApiIdentity(
            name=str(item.get("name") or f"token-{index}"),
            user_id=int(user_raw),
            group_ids=tuple(int(value) for value in groups) if groups else (),
            scopes=frozenset(str(value) for value in (item.get("scopes") or ())),
            admin=bool(item.get("admin") or False),
        )
    return table


def create_router(app_state_getter) -> APIRouter:
    router = APIRouter(prefix="/api/v1")

    def state(request: Request):
        return app_state_getter(request.app)

    def require_scope(identity: ApiIdentity, scope: str) -> None:
        """F08：所有修改/确认端点都必须校验 scope，低权限 Token 不能做高权限动作。"""
        if not identity.allows_scope(scope):
            raise HTTPException(status_code=403, detail="scope_denied")

    def authenticate(request: Request, authorization: str | None) -> ApiIdentity:
        table: dict[str, ApiIdentity] = getattr(request.app.state, "api_tokens", {})
        if not table:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="api_disabled"
            )
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(status_code=401, detail="missing_token")
        token = authorization.split(" ", 1)[1].strip()
        identity = table.get(token)
        if identity is None:
            raise HTTPException(status_code=401, detail="invalid_token")
        return identity

    # ---------- 发现 ----------

    @router.get("/agents")
    async def list_agents(request: Request, authorization: str | None = Header(default=None)):
        authenticate(request, authorization)
        registry = state(request).dispatcher.registry
        return {
            "agents": registry.agent_discovery(),
            "backends": registry.discovery(),
        }

    @router.get("/commands")
    async def list_commands(request: Request, authorization: str | None = Header(default=None)):
        authenticate(request, authorization)
        dispatcher = state(request).dispatcher
        return {
            "commands": dispatcher.commands.discovery(),
            "presets": [preset.discovery() for preset in dispatcher.presets.values()],
        }

    # ---------- 任务 ----------

    @router.post("/tasks", status_code=202)
    async def create_task(
        request: Request,
        payload: dict = Body(...),
        authorization: str | None = Header(default=None),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ):
        identity = authenticate(request, authorization)
        require_scope(identity, MIN_SCOPE_TASK)
        dispatcher = state(request).dispatcher
        tasks = dispatcher.tasks
        group_id = identity.group(payload.get("group_id"))
        agent = str(payload.get("agent") or "")
        prompt = str(payload.get("prompt") or "")
        if not agent or not prompt:
            raise HTTPException(status_code=400, detail="agent_and_prompt_required")
        # 与 QQ 完全相同的选项解析与冲突校验（F11/§5）
        try:
            options = options_from_payload({**payload, "prompt": prompt}).with_body(prompt)
        except SubmissionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        # F2：HTTP 新键必须带**入口命名空间**（`v1|http|<raw>`）并记录 key_scheme='http:v1'。
        # 查询分两步，且都限定 entry="http"：
        #   1) 新方案记录（key_scheme='http:v1'）——必须**真实逻辑键**确属本次 raw key
        #      （`find_owned_by_idempotency` 与原子登记同一候选顺序 + 同一归属校验）：
        #      raw=A 不会命中 raw=A#2 的基键 `v1|http|A#2`，反之亦然；命中即返回既有任务。
        #   2) 升级前的旧原始键记录（key_scheme IS NULL）——同键重放仍命中旧行（兼容），
        #      字面等于新键的原始 key 不会命中新记录（方案不同，互不冒名）。
        # 不清数据、不改 DB 版本号。
        http_key = http_namespaced_key(idempotency_key) if idempotency_key else None
        if idempotency_key:
            existing = tasks._store.find_owned_by_idempotency(  # noqa: SLF001
                entry="http", group_id=group_id, user_id=identity.user_id,
                idempotency_key=http_key, key_scheme=HTTP_KEY_SCHEME,
            )
            if existing is not None:
                return {
                    "task_id": existing["task_id"], "status": existing["status"],
                    "idempotent": True,
                }
            legacy = tasks._store.find_by_idempotency(  # noqa: SLF001
                group_id=group_id, user_id=identity.user_id, key=idempotency_key,
                entry="http", key_scheme=None,
            )
            if legacy is not None:
                return {
                    "task_id": legacy["task_id"], "status": legacy["status"],
                    "idempotent": True,
                }
        # F1：把命名空间键交给 prepare()——登记在**目录选择/确认副作用之前**完成，
        # PreparedTask 与 PendingSelection 共用同一次原子裁决；这里不再重复登记。
        try:
            prepared = dispatcher.submission.prepare(
                entry="http", group_id=group_id, user_id=identity.user_id, agent=agent,
                options=options, idempotency_key=http_key, key_scheme=HTTP_KEY_SCHEME,
            )
        except SubmissionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        if isinstance(prepared, DuplicateSubmission):
            return {
                "task_id": prepared.task_id, "status": prepared.status, "idempotent": True,
            }
        if isinstance(prepared, PendingSelection):
            return {
                "task_id": prepared.task_id, "status": "awaiting_selection",
                "selection_code": prepared.code,
                "candidates": list(prepared.candidates),
                "message": prepared.message,
            }
        import dataclasses

        if http_key:
            # 带 Idempotency-Key：登记已在 prepare() 内完成（同一把键只登记一次），
            # 这里沿用同一 task_id，绝不二次登记。
            task_id = prepared.request.task_id or tasks._store.new_id()  # noqa: SLF001
            task_request = dataclasses.replace(prepared.request, task_id=task_id)
            spec = dataclasses.replace(prepared.spec, task_id=task_id)
        else:
            # 无 Idempotency-Key：**不做任何去重决策**（保持原契约："无键 = 每次新建"）。
            task_id = tasks._store.new_id()  # noqa: SLF001
            task_request = dataclasses.replace(prepared.request, task_id=task_id)
            spec = dataclasses.replace(prepared.spec, task_id=task_id)
            tasks._store.create_task(  # noqa: SLF001
                entry="http", group_id=group_id, user_id=identity.user_id, agent=agent,
                prompt=task_request.prompt, mode=task_request.mode, backend=spec.backend_id,
                model=spec.model, effort=spec.effort, effort_set=task_request.effort_set,
                workspace=str(task_request.workspace) if task_request.workspace else None,
                status="queued", idempotency_key=None, task_id=task_id,
            )
        background = asyncio.create_task(
            tasks.run(task_request, spec=spec, history=tasks.history_for(task_request)),
            name=f"http-task-{task_id}",
        )
        state(request).background.add(background)
        background.add_done_callback(state(request).background.discard)
        return {"task_id": task_id, "status": "queued"}

    @router.get("/tasks/{task_id}")
    async def get_task(
        task_id: str, request: Request, authorization: str | None = Header(default=None)
    ):
        identity = authenticate(request, authorization)
        require_scope(identity, MIN_SCOPE_TASK)
        record = _own_task(request, identity, task_id)
        return _public_task(record)

    @router.get("/tasks/{task_id}/result")
    async def get_task_result(
        task_id: str, request: Request, authorization: str | None = Header(default=None)
    ):
        identity = authenticate(request, authorization)
        require_scope(identity, MIN_SCOPE_TASK)
        record = _own_task(request, identity, task_id)
        changes = state(request).dispatcher.tasks._store.list_changes(task_id=task_id)  # noqa: SLF001
        return {
            "task_id": task_id,
            "status": record["status"],
            "content": record["result_text"] or "",
            "error": record["error"],
            # V01：操作类别与逐动作回执，让调用方不必解析自然语言判断"是否真的改了文件"
            "operation": record.get("operation") or "",
            "action_counts": _decode_action_counts(record.get("action_counts")),
            "changes": [
                {
                    "path": item["path"], "kind": item["kind"], "status": item["status"],
                    "before_hash": item["before_hash"], "after_hash": item["after_hash"],
                    "bytes": item["bytes"], "error": item["error"],
                }
                for item in changes
            ],
        }

    @router.post("/tasks/{task_id}/cancel")
    async def cancel_task(
        task_id: str, request: Request, authorization: str | None = Header(default=None)
    ):
        identity = authenticate(request, authorization)
        require_scope(identity, MIN_SCOPE_TASK)
        record = _own_task(request, identity, task_id)
        # §5：使用任务自身（已授权）的 group_id，而不是 group_ids[0]
        text = await state(request).dispatcher.tasks.cancel(
            task_id=task_id, group_id=record["group_id"], user_id=identity.user_id,
        )
        return {"task_id": task_id, "message": text}

    # ---------- 目录 ----------

    @router.post("/workspaces/find")
    async def workspaces_find(
        request: Request, payload: dict = Body(...),
        authorization: str | None = Header(default=None),
    ):
        identity = authenticate(request, authorization)
        require_scope(identity, MIN_SCOPE_WORKSPACE)
        dispatcher = state(request).dispatcher
        group_id = identity.group(payload.get("group_id"))
        keyword = str(payload.get("keyword") or "").strip()
        if not keyword:
            raise HTTPException(status_code=400, detail="keyword_required")
        from app.commands.options import PublicOptions

        options = PublicOptions(find=keyword, mode="read")
        resolution = dispatcher.workspaces.resolve_selector(
            group_id=group_id, user_id=identity.user_id, options=options, mode="read",
            purpose="workspace",
        )
        return {
            "selection_code": resolution.selection_code,
            "candidates": [str(item.path) for item in resolution.candidates],
            "message": resolution.message,
        }

    @router.post("/workspaces/select")
    async def workspaces_select(
        request: Request, payload: dict = Body(...),
        authorization: str | None = Header(default=None),
    ):
        identity = authenticate(request, authorization)
        require_scope(identity, MIN_SCOPE_WORKSPACE)
        group_id = identity.group(payload.get("group_id"))
        code = str(payload.get("code") or "").strip()
        index = payload.get("index")
        if not code or not isinstance(index, int):
            raise HTTPException(status_code=400, detail="code_and_index_required")
        dispatcher = state(request).dispatcher
        outcome = dispatcher.workspaces.select(
            group_id=group_id, user_id=identity.user_id, code=code, index=index
        )
        if not outcome.ok:
            raise HTTPException(status_code=400, detail=outcome.message)
        # F11：与 QQ 相同——选定后按原 purpose 续跑任务或回填 team 草稿
        if outcome.resume_task_id:
            require_scope(identity, MIN_SCOPE_TASK)
            background = asyncio.create_task(
                dispatcher.submission.resume(
                    task_id=outcome.resume_task_id, group_id=group_id,
                    user_id=identity.user_id, workspace=outcome.workspace,
                ),
                name=f"http-resume-{outcome.resume_task_id}",
            )
            state(request).background.add(background)
            background.add_done_callback(state(request).background.discard)
            return {
                "workspace": str(outcome.workspace), "message": outcome.message,
                "resumed_task_id": outcome.resume_task_id,
            }
        if outcome.team_id:
            require_scope(identity, MIN_SCOPE_TEAM)
            if dispatcher.team is None:
                raise HTTPException(status_code=400, detail="team_disabled")
            from app.models import InboundMessage

            message = InboundMessage(
                group_id=group_id, user_id=identity.user_id, self_id=0, text="/workspace select"
            )
            text = await dispatcher.team.on_workspace_selected(outcome, message)
            return {"workspace": str(outcome.workspace), "team_id": outcome.team_id,
                    "message": text}
        return {"workspace": str(outcome.workspace), "message": outcome.message}

    # ---------- 确认 ----------

    @router.post("/confirmations/{code}")
    async def confirm(
        code: str, request: Request, payload: dict = Body(default={}),
        authorization: str | None = Header(default=None),
    ):
        identity = authenticate(request, authorization)
        group_id = identity.group(payload.get("group_id"))
        confirmations = state(request).dispatcher.confirmations
        pending = confirmations.peek(code)
        if pending is None:
            raise HTTPException(status_code=404, detail="confirmation_not_found")
        if pending.kind == "workspace.select":
            raise HTTPException(status_code=400, detail="use_workspaces_select")
        require_scope(identity, MIN_SCOPE_TEAM if pending.kind == "action.confirm" else MIN_SCOPE_TASK)
        outcome, record = confirmations.consume(
            kind=pending.kind, user_id=identity.user_id, code=code, group_id=group_id
        )
        if record is None:
            raise HTTPException(status_code=400, detail=outcome.value)
        action = record.action
        if action is not None and action.kind == "team.retry":
            team_id = str(action.payload.get("team_id") or "")
            handler = state(request).dispatcher.team
            if handler is None:
                raise HTTPException(status_code=400, detail="team_disabled")
            from app.models import InboundMessage

            message = InboundMessage(
                group_id=group_id, user_id=identity.user_id, self_id=0, text="/confirm"
            )
            text = await state(request).dispatcher._handle_team_confirmation(  # noqa: SLF001
                record, message
            )
            return {"team_id": team_id, "message": text}
        return {"code": code, "summary": record.summary, "kind": record.kind}

    # ---------- 团队 ----------

    @router.post("/teams", status_code=201)
    async def create_team(
        request: Request, payload: dict = Body(...),
        authorization: str | None = Header(default=None),
    ):
        identity = authenticate(request, authorization)
        require_scope(identity, MIN_SCOPE_TEAM)
        goal = str(payload.get("goal") or "").strip()
        if not goal:
            raise HTTPException(status_code=400, detail="goal_required")
        group_id = identity.group(payload.get("group_id"))
        dispatcher = state(request).dispatcher
        if dispatcher.team is None:
            raise HTTPException(status_code=400, detail="team_disabled")
        store = dispatcher.team._service._store  # noqa: SLF001
        record = store.create_draft(
            group_id=group_id, owner_user_id=identity.user_id, goal=goal
        )
        return {"team_id": record.team_id, "goal": record.goal, "status": record.status}

    @router.put("/teams/{team_id}/steps/{step_id}")
    async def upsert_step(
        team_id: str, step_id: str, request: Request, payload: dict = Body(...),
        authorization: str | None = Header(default=None),
    ):
        identity = authenticate(request, authorization)
        require_scope(identity, MIN_SCOPE_TEAM)
        group_id = identity.group(payload.get("group_id"))
        from app.models import InboundMessage

        parts = [
            "/team", "add", team_id, step_id, str(payload.get("agent") or ""),
        ]
        for name in ("backend", "model", "effort", "mode"):
            if payload.get(name):
                parts += [f"--{name}", str(payload[name])]
        if payload.get("dir"):
            parts += ["--dir", f'"{payload["dir"]}"']
        if payload.get("find"):
            parts += ["--find", f'"{payload["find"]}"']
        if payload.get("after"):
            after = payload["after"]
            values = after if isinstance(after, list) else [after]
            parts += ["--after", ",".join(str(item) for item in values)]
        prompt = str(payload.get("prompt") or "")
        if not prompt:
            raise HTTPException(status_code=400, detail="prompt_required")
        message = InboundMessage(
            group_id=group_id, user_id=identity.user_id, self_id=0,
            text=" ".join(parts) + " -- " + prompt,
        )
        text = await _run_team_command(request, message)
        return {"team_id": team_id, "step_id": step_id, "message": text}

    @router.delete("/teams/{team_id}/steps/{step_id}")
    async def delete_step(
        team_id: str, step_id: str, request: Request, payload: dict = Body(default={}),
        authorization: str | None = Header(default=None),
    ):
        identity = authenticate(request, authorization)
        require_scope(identity, MIN_SCOPE_TEAM)
        group_id = identity.group(payload.get("group_id"))
        from app.models import InboundMessage

        message = InboundMessage(
            group_id=group_id, user_id=identity.user_id, self_id=0,
            text=f"/team remove {team_id} {step_id}",
        )
        text = await _run_team_command(request, message)
        return {"team_id": team_id, "step_id": step_id, "message": text}

    @router.get("/teams/{team_id}/plan")
    async def team_plan(
        team_id: str, request: Request, authorization: str | None = Header(default=None)
    ):
        identity = authenticate(request, authorization)
        require_scope(identity, MIN_SCOPE_TEAM)
        group_id = identity.group(request.query_params.get("group_id"))
        from app.models import InboundMessage

        message = InboundMessage(
            group_id=group_id, user_id=identity.user_id, self_id=0, text=f"/team plan {team_id}"
        )
        return {"team_id": team_id, "plan": await _run_team_command(request, message)}

    @router.get("/teams/{team_id}")
    async def team_status(
        team_id: str, request: Request, authorization: str | None = Header(default=None)
    ):
        identity = authenticate(request, authorization)
        require_scope(identity, MIN_SCOPE_TEAM)
        group_id = identity.group(request.query_params.get("group_id"))
        from app.models import InboundMessage

        message = InboundMessage(
            group_id=group_id, user_id=identity.user_id, self_id=0, text=f"/team status {team_id}"
        )
        return {"team_id": team_id, "status": await _run_team_command(request, message)}

    @router.get("/teams/{team_id}/result")
    async def team_result(
        team_id: str, request: Request, authorization: str | None = Header(default=None)
    ):
        identity = authenticate(request, authorization)
        require_scope(identity, MIN_SCOPE_TEAM)
        group_id = identity.group(request.query_params.get("group_id"))
        from app.models import InboundMessage

        message = InboundMessage(
            group_id=group_id, user_id=identity.user_id, self_id=0, text=f"/team result {team_id}"
        )
        return {"team_id": team_id, "result": await _run_team_command(request, message)}

    @router.post("/teams/{team_id}/{action}")
    async def team_action(
        team_id: str, action: str, request: Request, payload: dict = Body(default={}),
        authorization: str | None = Header(default=None),
    ):
        identity = authenticate(request, authorization)
        require_scope(identity, MIN_SCOPE_TEAM)
        if action not in {"run", "cancel", "retry"}:
            raise HTTPException(status_code=404, detail="unknown_action")
        group_id = identity.group(payload.get("group_id"))
        from app.models import InboundMessage

        message = InboundMessage(
            group_id=group_id, user_id=identity.user_id, self_id=0,
            text=f"/team {action} {team_id}",
        )
        return {"team_id": team_id, "action": action, "message": await _run_team_command(request, message)}

    # ---------- 辅助 ----------

    async def _run_team_command(request: Request, message) -> str:
        dispatcher = state(request).dispatcher
        if dispatcher.team is None:
            raise HTTPException(status_code=400, detail="team_disabled")
        parsed = dispatcher.commands.parse(message.text)
        if parsed is None or isinstance(parsed, str):
            raise HTTPException(status_code=400, detail="invalid_command")
        try:
            return await dispatcher.team.handle_text(parsed, message)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None

    def _own_task(request: Request, identity: ApiIdentity, task_id: str) -> dict:
        tasks = state(request).dispatcher.tasks
        record = tasks._store.get_task(task_id)  # noqa: SLF001
        if record is None or record["user_id"] != identity.user_id or (
            identity.group_ids and record["group_id"] not in identity.group_ids
        ):
            # 不存在与不归本人在文案上不可区分
            raise HTTPException(status_code=404, detail="task_not_found")
        return record

    def _public_task(record: dict) -> dict:
        return {
            "task_id": record["task_id"],
            "status": record["status"],
            "entry": record["entry"],
            "agent": record["agent"],
            "backend": record["backend"],
            "model": record["model"],
            "effort": record["effort"],
            "mode": record["mode"],
            "error": record["error"],
            "duration_ms": record["duration_ms"],
            "changed_files": record["changed_files"],
            # V01：操作类别与逐动作回执（与 QQ 展示、team 判定同源）
            "operation": record.get("operation") or "",
            "action_counts": _decode_action_counts(record.get("action_counts")),
            "created_at": record["created_at"],
            "updated_at": record["updated_at"],
            "summary": record["result_summary"],
            "team_id": record["team_id"],
            "step_id": record["step_id"],
        }

    return router


def _decode_action_counts(raw) -> dict[str, int]:
    """把库里的 action_counts 文本还原为 dict；为空/损坏时返回空 dict。"""
    if not raw:
        return {}
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return {}
    if not isinstance(value, dict):
        return {}
    return {str(key): int(item) for key, item in value.items()
            if isinstance(item, (int, float))}


def _opt(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
