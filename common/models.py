"""数据模型：与 TRD §6 五表一一对应，外加运行时 state KV（不在 TRD 内，为'一切先落盘'所需）。"""
from dataclasses import dataclass


@dataclass
class InboundMessage:
    msg_id: str
    from_user: str
    text: str
    context_token: str
    received_at: int
    state: str = "received"  # received / queued
    id: int | None = None


@dataclass
class SessionBinding:
    id: int
    wechat_user: str
    cwd: str
    claude_uuid: str
    policy: str
    created_at: int
    last_active_at: int


@dataclass
class Task:
    id: int
    message_id: int | None
    session_id: int
    prompt: str
    kind: str          # chat / command
    state: str         # pending / running / done / failed / dead / canceled
    attempts: int
    max_attempts: int
    created_at: int
    updated_at: int
    claude_bg_id: str | None = None  # --bg 任务 id（M2 长任务）


@dataclass
class OutboxItem:
    id: int
    task_id: int | None
    to_user: str
    text: str
    seq: int
    state: str         # pending / sent / failed / dead
    attempts: int
    max_attempts: int
    last_error: str | None
    created_at: int


@dataclass
class Budget:
    max_turns: int = 50
    max_usd: float = 5.0
