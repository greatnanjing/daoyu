"""claude -p --output-format stream-json 的 NDJSON 事件解析 + 进度节流。"""
import json
import time
from dataclasses import dataclass, field


@dataclass
class StreamEvent:
    type: str                                  # init / text / tool / result
    text: str = ""
    tool_name: str | None = None
    slash_commands: list[str] = field(default_factory=list)
    session_id: str | None = None
    cost_usd: float | None = None
    is_error: bool = False
    subtype: str | None = None                 # result 事件：success / error_max_turns /
                                               # error_max_budget_usd / error_during_execution


class StreamParser:
    """feed_line 喂入 stdout 的一行（含/不含换行均可），产出关注的事件，忽略其余。

    有状态：tool_use 块的 input_json_delta 增量追加到该块 tool 事件的 text
    （如 Bash 命令 JSON），供 worker 做进度提示。
    """

    def __init__(self) -> None:
        self._open_tools: dict[int, StreamEvent] = {}

    def feed_line(self, line: str) -> StreamEvent | None:
        line = line.strip()
        if not line:
            return None
        try:
            obj = json.loads(line)
        except ValueError:
            return None
        if not isinstance(obj, dict):
            return None

        t = obj.get("type")
        if t == "system" and obj.get("subtype") == "init":
            return StreamEvent(type="init", session_id=obj.get("session_id"),
                               slash_commands=list(obj.get("slash_commands") or []))
        if t == "stream_event":
            ev = obj.get("event") or {}
            et = ev.get("type")
            if et == "content_block_delta":
                delta = ev.get("delta") or {}
                if delta.get("type") == "text_delta":
                    return StreamEvent(type="text", text=delta.get("text", ""))
                if delta.get("type") == "input_json_delta":
                    tool = self._open_tools.get(ev.get("index"))
                    if tool is not None:
                        tool.text += delta.get("partial_json") or ""
                    return None
            elif et == "content_block_start":
                block = ev.get("content_block") or {}
                if block.get("type") == "tool_use":
                    event = StreamEvent(type="tool", tool_name=block.get("name"))
                    self._open_tools[ev.get("index")] = event
                    return event
            elif et == "content_block_stop":
                self._open_tools.pop(ev.get("index"), None)
            return None
        if t == "result":
            return StreamEvent(type="result", text=obj.get("result") or "",
                               cost_usd=obj.get("total_cost_usd"),
                               is_error=bool(obj.get("is_error")),
                               subtype=obj.get("subtype"))
        return None


class Throttle:
    """时间窗节流：窗口内抑制、出窗口放行；第一条永远立即放行。"""

    def __init__(self, interval_s: float = 2.5):
        self._interval = interval_s
        self._last = float("-inf")

    def allow(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        if now - self._last >= self._interval:
            self._last = now
            return True
        return False
