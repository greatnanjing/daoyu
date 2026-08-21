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
                     "page_char_limit": 2000, "daily_send_limit": 500,
                     "merge_window_s": 2.0}
_DEFAULT_WORKER = {"concurrency": 3, "poll_interval_s": 0.5,
                   "bg_poll_s": 10, "bg_blocked_timeout_s": 1800}
# session_duration_s 默认 30 天：实测（2026-08-19，本机实例 token 连续 ≥3.5 天
# 有效无 401）服务端 bot_token 长期存活，TRD 时代 "24h 过期" 假设被推翻——
# 主动续期周期过长无害（token 真死时 poll_loop 401 清 token 自动触发重扫）。
_DEFAULT_RECONNECT = {"session_duration_s": 2592000, "warning_before_s": 7200,
                      "reminder_interval_s": 1800, "force_before_s": 1800,
                      "qrcode_scan_timeout_s": 600, "silent_grace_s": 30}
# M4 主动服务阈值（scheduler 读取）。cert_paths 为列表不进 /config set 白名单
# （低频运维键，直接改文件）；数值键经 proxy.CONFIG_KEYS 开放微信 set。
_DEFAULT_CRON = {"disk_threshold_pct": 85, "cpu_threshold_pct": 90,
                 "mem_threshold_pct": 90, "load_sustain_min": 5,
                 "cert_warn_days": 14, "cert_paths": ["/etc/letsencrypt/live"],
                 "alert_silence_h": 6, "queue_backlog_warn": 20}
# M5A 通知 HTTP 入口（gateway/notify_http.py 读取）。低频运维键直接改文件，
# 不进 /config set 白名单（同 cert_paths 口径）。
_DEFAULT_NOTIFY = {"listen": "127.0.0.1:8417", "http_enabled": True}


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
    # data/media/inbound|outbound 的保留天数（过期 img-* 文件启动/日界时清理；
    # M3 审查追加项。不进 /config set 白名单——低频运维键，直接改文件）
    media_retention_days: float = 14.0
    # M4 主动服务阈值（/config set 可改数值键，重启生效）
    cron: dict = field(default_factory=lambda: dict(_DEFAULT_CRON))
    # M5A 通知 HTTP 入口（127.0.0.1 单路由 POST /notify）
    notify: dict = field(default_factory=lambda: dict(_DEFAULT_NOTIFY))


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
    cron = dict(_DEFAULT_CRON)
    cron.update(raw.get("cron") or {})
    notify = dict(_DEFAULT_NOTIFY)
    notify.update(raw.get("notify") or {})

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
        cron=cron,
        notify=notify,
        budget=budget,
        secrets=secrets,
        media_retention_days=float(raw.get("media_retention_days", 14.0)),
    )


# 宿主 settings.json env 块的动态凭据白名单：ANTHROPIC_* 全前缀（覆盖未来新增
# 的模型槽位变量）+ API_TIMEOUT_MS。用户在宿主 ~/.claude/settings.json 里维护
# key 与模型映射（会动态变动），刀鱼每个任务现场读取跟随；secrets.env 退化为
# 兜底层。只取凭据/模型键——permissions/plugins/defaultMode 等一概不碰，
# CLAUDE_CONFIG_DIR 隔离语义不变。
def host_claude_env(home: Path | None = None) -> dict:
    """宿主 ~/.claude/settings.json env 块的白名单子集（坏文件/缺席 → {}）。
    纯函数、home 参数化可测；任何解析异常静默回退兜底层。"""
    try:
        p = (home or Path.home()) / ".claude" / "settings.json"
        env = (json.loads(p.read_text(encoding="utf-8")) or {}).get("env") or {}
        return {k: v for k, v in env.items()
                if isinstance(k, str) and isinstance(v, str)
                and (k.startswith("ANTHROPIC_") or k == "API_TIMEOUT_MS")}
    except Exception:
        return {}


def merge_claude_secrets(fallback: dict, host: dict) -> dict:
    """兜底层（secrets.env）+ 动态层（宿主 settings.json）合并，动态层逐键优先。
    AUTH_TOKEN/API_KEY 二选一去重：两层各出一把时双头并发（Authorization 与
    x-api-key 各带不同 key）语义不明——以动态层声明的形态为准，剔除兜底层的
    另一形态。"""
    out = dict(fallback)
    if "ANTHROPIC_AUTH_TOKEN" in host:
        out.pop("ANTHROPIC_API_KEY", None)
    if "ANTHROPIC_API_KEY" in host:
        out.pop("ANTHROPIC_AUTH_TOKEN", None)
    out.update(host)
    return out
