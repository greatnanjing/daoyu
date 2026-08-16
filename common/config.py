"""配置加载：gateway/config.json（实例，gitignore）+ claude/secrets.env（gitignore）。

Config 对象同时是 TaskRunner / OutboundLoop 的 config 契约（claude_bin / secrets /
repo_root / budget / throttle 四键全在），无需二次适配。
"""
import json
from dataclasses import dataclass, field
from pathlib import Path

from common.models import Budget

# TaskRunner（progress_window_s/page_char_limit）与 OutboundLoop
# （min_send_interval_s/page_char_limit/daily_send_limit）的契约键全在此给默认，
# 实例 config.json 缺键也不至于运行期 KeyError。
_DEFAULT_THROTTLE = {"min_send_interval_s": 1.0, "progress_window_s": 2.5,
                     "page_char_limit": 2000, "daily_send_limit": 500}
_DEFAULT_WORKER = {"concurrency": 3, "poll_interval_s": 0.5,
                   "bg_poll_s": 10, "bg_blocked_timeout_s": 1800}
_DEFAULT_RECONNECT = {"session_duration_s": 86400, "warning_before_s": 7200,
                      "reminder_interval_s": 1800, "force_before_s": 1800,
                      "qrcode_scan_timeout_s": 600}


@dataclass
class Config:
    repo_root: Path
    db_path: Path
    whitelist: set[str]
    default_cwd: str
    claude_bin: str | list[str]
    throttle: dict
    worker: dict
    reconnect: dict
    budget: Budget
    secrets: dict = field(default_factory=dict)


def load_config(repo_root: Path | None = None) -> Config:
    root = repo_root or Path(__file__).resolve().parents[1]
    cfg_path = root / "gateway" / "config.json"
    if not cfg_path.is_file():
        raise SystemExit(f"缺少配置文件 {cfg_path}（参考 gateway/config.example.json）")
    raw = json.loads(cfg_path.read_text(encoding="utf-8"))

    secrets = {}
    env_path = root / "claude" / "secrets.env"
    if env_path.is_file():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                secrets[k.strip()] = v.strip()

    throttle = dict(_DEFAULT_THROTTLE)
    throttle.update(raw.get("throttle") or {})
    worker = dict(_DEFAULT_WORKER)
    worker.update(raw.get("worker") or {})
    reconnect = dict(_DEFAULT_RECONNECT)
    reconnect.update(raw.get("reconnect") or {})

    budget_raw = raw.get("budget", {})
    try:
        budget = Budget(**budget_raw)
    except TypeError as e:
        raise SystemExit(
            f"gateway/config.json 的 budget 含未知键: {e}（合法键: max_turns, max_usd）")

    return Config(
        repo_root=root,
        db_path=root / "data" / "daoyu.db",
        whitelist=set(raw.get("whitelist", [])),
        default_cwd=raw.get("default_cwd", str(root)),
        claude_bin=raw.get("claude_bin", "claude"),
        throttle=throttle,
        worker=worker,
        reconnect=reconnect,
        budget=budget,
        secrets=secrets,
    )
