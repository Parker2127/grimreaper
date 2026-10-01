"""A small driver for OpenAI Agents API sessions whose tools run locally.

The session runs in OpenAI's Managed Agents service with no sandbox (`environment: none`).
Whenever the agent calls one of our function tools, the session pauses in `requires_action`.
We run the tool here, on the user's machine, with the user's AWS credentials, and submit the result.
AWS credentials never leave this process.

Note: the Agents API's built-in subagents don't support function tools, so GrimReaper uses a single agent.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

DEFAULT_MODEL = "gpt-6-astra"
TERMINAL_TURN = {"completed", "failed", "cancelled"}


class AgentRunError(RuntimeError):
    pass


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    fn: Callable[[dict], Any]

    @property
    def spec(self) -> dict:
        return {"type": "function", "name": self.name, "description": self.description, "parameters": self.parameters}


@dataclass
class Progress:
    tool_calls: int = 0
    last_tool: str = ""
    tools_used: Counter = field(default_factory=Counter)
    seconds: float = 0.0
    done: bool = False


def _run_tool(tools: dict[str, Tool], call) -> dict:
    result = {"type": "agent.session.input.tool_result", "turn_id": call.turn_id, "call_id": call.call_id}
    tool = tools.get(call.name)
    if tool is None:
        return result | {"success": False, "error": f"unknown tool {call.name}"}
    try:
        args = json.loads(call.arguments) if isinstance(call.arguments, str) else dict(call.arguments or {})
        output = tool.fn(args)
        return result | {"success": True, "output": output if isinstance(output, str) else json.dumps(output, default=str)}
    except ClientError as e:  # give the model the AWS error code so it can adapt (e.g. AccessDenied)
        err = e.response.get("Error", {})
        return result | {"success": False, "error": f"AWS {err.get('Code', 'error')}: {err.get('Message', '')}"[:500]}
    except (BotoCoreError, ValueError, KeyError) as e:
        return result | {"success": False, "error": f"{type(e).__name__}: {e}"[:500]}
    except Exception as e:  # never leak a traceback (or anything secret in it) to the model
        return result | {"success": False, "error": f"tool failed ({type(e).__name__})"}


def run_session(
    client,
    *,
    instructions: str,
    task: str,
    tools: list[Tool],
    output_schema: dict | None = None,
    model: str = DEFAULT_MODEL,
    timeout_s: float = 600,
    poll_s: float = 1.0,
    on_progress: Callable[[Progress], None] = lambda _: None,
) -> str:
    """Run one task to completion and return the agent's final answer text."""
    by_name = {t.name: t for t in tools}
    agent: dict[str, Any] = {"model": model, "instructions": instructions, "tools": [t.spec for t in tools]}
    if output_schema:
        agent["text"] = {"format": {"type": "json_schema", "schema": output_schema}}

    sessions = client.beta.agents.sessions
    session = sessions.create(environment={"type": "none"}, agent=agent, input=task, metadata={"app": "grimreaper"})
    progress = Progress()
    handled: set[tuple[str, str]] = set()
    started = time.monotonic()
    finished = False
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            while True:
                progress.seconds = time.monotonic() - started
                if progress.seconds > timeout_s:
                    raise AgentRunError(f"the agent didn't finish within {timeout_s:.0f}s")
                current = sessions.retrieve(session.id)
                if current.status == "failed":
                    raise AgentRunError(f"session failed: {current.error}")

                if current.status == "requires_action":
                    calls = [
                        a for a in current.required_actions
                        if a.type == "function_call" and (a.turn_id, a.call_id) not in handled
                    ]
                    if not calls:
                        kinds = sorted({a.type for a in current.required_actions})
                        raise AgentRunError(f"session needs an action GrimReaper can't provide: {kinds}")
                    # The model can request several tools at once; run them concurrently.
                    results = list(pool.map(lambda c: _run_tool(by_name, c), calls))
                    sessions.events.create(session.id, events=results)
                    handled.update((c.turn_id, c.call_id) for c in calls)
                    progress.tool_calls += len(calls)
                    progress.last_tool = calls[-1].name
                    progress.tools_used.update(c.name for c in calls)
                    on_progress(progress)
                    continue

                turn = _root_turn(sessions, session.id)
                if turn is not None and turn.status in TERMINAL_TURN and current.status == "idle":
                    if turn.status != "completed":
                        detail = f"{turn.error.code}: {turn.error.message}" if turn.error else turn.status
                        raise AgentRunError(f"agent turn {turn.status}: {detail}")
                    answer = _final_answer(sessions, session.id, turn.id)
                    finished = True
                    progress.done = True
                    on_progress(progress)
                    return answer

                on_progress(progress)
                time.sleep(poll_s)
    finally:
        _cleanup(sessions, session.id, cancel=not finished)


def _cleanup(sessions, session_id: str, cancel: bool) -> None:
    """Delete the session. A session that is still running must be cancelled first, or delete is refused."""
    if cancel:
        try:
            sessions.events.create(session_id, events=[{"type": "agent.session.input.cancel"}])
        except Exception:
            pass
    for delay in ((0, 2, 5, 10) if cancel else (0,)):
        time.sleep(delay)
        try:
            sessions.delete(session_id)
            return
        except Exception:
            continue


def _root_turn(sessions, session_id: str):
    for turn in sessions.turns.list(session_id, order="asc"):
        if turn.subagent_id is None:
            return turn
    return None


def _final_answer(sessions, session_id: str, turn_id: str) -> str:
    fallback = None
    for item in sessions.items.list(session_id, order="desc"):
        if item.type != "message" or getattr(item, "role", None) != "assistant" or item.turn_id != turn_id:
            continue
        text = "".join(part.text for part in item.content if getattr(part, "text", None))
        if getattr(item, "phase", None) == "final_answer":
            return text
        fallback = fallback or text
    if fallback is None:
        raise AgentRunError("the agent finished without a final answer")
    return fallback
