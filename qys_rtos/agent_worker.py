"""Run one AI task in a separate process, communicating over private pipes."""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

from pydantic_ai import CancellationToken
from pydantic_ai.messages import ModelMessagesTypeAdapter

from .agent_config import AgentConfig
from .agent_service import AgentSession
from .agent_workspace import AgentWorkspace


def run_worker() -> int:
    output_lock = threading.Lock()
    token = CancellationToken()
    workspace = None
    key = ""

    def emit(kind: str, **values) -> None:
        with output_lock:
            sys.stdout.write(json.dumps({"type": kind, **values}, ensure_ascii=True) + "\n")
            sys.stdout.flush()

    try:
        request = json.loads(sys.stdin.readline())
        key = str(request.get("api_key", ""))
        config = AgentConfig(str(request["base_url"]), str(request["model"]), key)
        workspace = AgentWorkspace(Path(request["project"]), Path(request["backup_root"]),
                                   on_change=lambda path: emit("file_changed", path=path),
                                   cancelled=lambda: token.cancelled)

        def watch_cancel() -> None:
            for line in sys.stdin:
                try:
                    if json.loads(line).get("type") == "cancel":
                        token.cancel()
                        break
                except (ValueError, AttributeError):
                    continue

        threading.Thread(target=watch_cancel, daemon=True).start()
        session = AgentSession(workspace, config=config, on_status=lambda message: emit("status", message=message))
        raw_history = request.get("history", "")
        if raw_history:
            session.history = ModelMessagesTypeAdapter.validate_json(raw_history)
        answer = session.run(str(request["prompt"]), build_output=str(request.get("build_output", "")),
                             cancellation_token=token)
        history = ModelMessagesTypeAdapter.dump_json(session.history).decode("utf-8")
        if len(history.encode("utf-8")) > 2_000_000:
            history = ""
            emit("status", message="会话历史过长，下一次任务将开始新对话。")
        with workspace.mutation_lock:
            pass
        emit("completed", answer=answer, history=history)
        return 0
    except BaseException as exc:
        if workspace is not None:
            with workspace.mutation_lock:
                pass
        if token.cancelled:
            message = "任务已停止。"
        else:
            message = str(exc).replace(key, "[密钥已隐藏]") if key else str(exc)
            message = message[:1500] or type(exc).__name__
        emit("error", message=message)
        return 1
