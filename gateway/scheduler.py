"""M4 主动服务调度器：日报（daily）+ 巡检（patrol）。

scheduler_loop 每分钟整分对齐醒来、现读 cron_jobs 表决定动作（/cron 改表
即时生效）；日报/巡检判定纯 Python 零 token 成本，推送经 db.enqueue 落
outbox（发白名单全部用户），异常时建 Claude 分析任务挂 ops 话题（固定
UUID，分析历史上下文延续）。scheduler 绝不直接调 iLink、不自己跑 Claude。
"""
import json
import time
from collections import deque
from pathlib import Path

from common.models import CronJob

# ops 话题固定 UUID（合法 hex 形态；ensure_ops_session 建行，/delete 删了会重建）
OPS_UUID = "0da0f00d-0f00-4000-8000-00000000000d"


def _today_ts(hhmm: str, now: int) -> int:
    """now 当日 hhmm 时刻的 epoch（localtime 构造，口径同 db.local_midnight_ts）。"""
    h, m = hhmm.split(":")
    lt = time.localtime(now)
    return int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, int(h), int(m),
                            0, 0, 0, -1)))


def due_daily(job: CronJob, now: int) -> bool:
    """到点且今日未跑（last_run_at < 今日时刻）——防同一天重复推送。"""
    if not job.enabled or not job.time_of_day:
        return False
    ts = _today_ts(job.time_of_day, now)
    return ts <= now and (job.last_run_at or 0) < ts


def due_patrol(job: CronJob, now: int) -> bool:
    """距上次运行满间隔；从未跑（None）立即 due——首轮建立基线。"""
    if not job.enabled or not job.interval_min:
        return False
    return now - (job.last_run_at or 0) >= job.interval_min * 60


def next_run_time(job: CronJob, now: int) -> int | None:
    """/cron 列表呈现用；禁用返回 None。"""
    if not job.enabled:
        return None
    if job.name == "daily" and job.time_of_day:
        ts = _today_ts(job.time_of_day, now)
        if ts <= now or (job.last_run_at or 0) >= ts:
            ts += 86400
        return ts
    if job.interval_min:
        return (job.last_run_at or now) + job.interval_min * 60
    return None


def _broadcast(db, cfg, text: str) -> None:
    """发白名单全部用户（outbound._alert_all 同构：同步 enqueue 落 outbox，
    投递由出站循环 0.5s 轮询接管；whitelist 缺席（测试替身）静默跳过）。"""
    for user in sorted(getattr(cfg, "whitelist", None) or ()):
        db.enqueue(None, user, text)


def ensure_ops_session(db, cfg) -> int:
    """ops 话题（固定 UUID）：分析任务的挂靠点——历史聚一处、Claude 有先前
    分析上下文。无白名单（异常配置/测试）兜底本地用户名。"""
    s = db.get_session_by_uuid(OPS_UUID)
    if s is not None:
        return s.id
    user = min(cfg.whitelist) if getattr(cfg, "whitelist", None) else "ops@local"
    return db.create_fixed_session(user, cfg.default_cwd, OPS_UUID).id


def _media_mb(cfg) -> float:
    base = Path(getattr(cfg, "repo_root", ".")) / "data" / "media"
    try:
        total = sum(f.stat().st_size for f in base.rglob("*") if f.is_file())
    except OSError:
        return 0.0
    return total / 1048576.0


def collect_daily_data(db, cfg, now: int, sample: dict) -> dict:
    """日报三板块数据（统计窗口 = 昨日全天；生产 CST 无夏令时，-86400 无漂移）。"""
    lt = time.localtime(now)
    day_end = int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))
    day_start = day_end - 86400
    return {
        "date": time.strftime("%Y-%m-%d", time.localtime(day_start)),
        "tasks": db.daily_task_stats(day_start, day_end),
        "cost_usd": db.daily_cost(day_start, day_end),
        "cpu": sample.get("cpu", 0.0), "mem": sample.get("mem", 0.0),
        "disks": sample.get("disks", {}), "boot_days": sample.get("boot_days", 0.0),
        "sent": db.outbox_sent_count(day_start, day_end),
        "backlog": db.queue_depth(),
        "dead_outbox": db.dead_letter_count(),
        "online": bool(db.get_state("bot_token")),
        "media_mb": _media_mb(cfg),
    }


def render_daily_report(data: dict) -> str:
    t = data["tasks"]
    disk = " / ".join(f"{p} {v:.0f}%" for p, v in sorted(data["disks"].items())) or "—"
    lines = [
        f"🌅 刀鱼日报 {data['date']}",
        f"📊 任务：昨日 {t['total']} 个（成功 {t['done']} / 取消 {t['canceled']}"
        f" / 死信 {t['dead']}），费用 ${data['cost_usd']:.2f}",
        f"🖥 服务器：CPU {data['cpu']:.0f}% / 内存 {data['mem']:.0f}% / 磁盘 {disk}，"
        f"已运行 {data['boot_days']:.1f} 天",
        f"🐟 刀鱼：出站 {data['sent']} 条 / 队列 {data['backlog']} / "
        f"死信 {data['dead_outbox']} / "
        f"{'连接正常' if data['online'] else '⚠️ 连接未建立'} / "
        f"media {data['media_mb']:.0f}MB",
    ]
    return "\n".join(lines)


def daily_anomalies(data: dict, cron_cfg: dict) -> list[str]:
    """异常升级判定（spec §4）：死信新增 / 健康快照超阈 / 队列积压 / 掉线。"""
    out = []
    if data["tasks"]["dead"] > 0:
        out.append(f"昨日新增死信任务 {data['tasks']['dead']} 个")
    for path, pct in sorted(data["disks"].items()):
        if pct > cron_cfg["disk_threshold_pct"]:
            out.append(f"磁盘 {path} {pct:.0f}%（阈值 "
                       f"{cron_cfg['disk_threshold_pct']}%）")
    if data["cpu"] > cron_cfg["cpu_threshold_pct"]:
        out.append(f"CPU {data['cpu']:.0f}%（阈值 {cron_cfg['cpu_threshold_pct']}%）")
    if data["mem"] > cron_cfg["mem_threshold_pct"]:
        out.append(f"内存 {data['mem']:.0f}%（阈值 {cron_cfg['mem_threshold_pct']}%）")
    if data["backlog"] > cron_cfg["queue_backlog_warn"]:
        out.append(f"队列积压 {data['backlog']}（预警 {cron_cfg['queue_backlog_warn']}）")
    if not data["online"]:
        out.append("iLink token 缺失（连接未建立）")
    return out


def run_daily(db, cfg, now: int, sample: dict) -> str:
    """日报主流程：收集→模板→推送；异常时追加分析任务（挂 ops 话题）。
    模板先推、分析后到——分析失败日报照样在。"""
    data = collect_daily_data(db, cfg, now, sample)
    text = render_daily_report(data)
    anomalies = daily_anomalies(data, cfg.cron)
    if not anomalies:
        _broadcast(db, cfg, text)
        return "正常，推送 1 条"
    text += "\n⏳ 检测到异常，分析进行中…"
    prompt = ("刀鱼巡检系统自动任务：昨日运行数据存在异常，请分析原因并给出"
              "简要结论与建议。\n\n异常项：\n- " + "\n- ".join(anomalies) +
              "\n\n数据：" + json.dumps(
                  {"tasks": data["tasks"], "cost_usd": data["cost_usd"],
                   "backlog": data["backlog"], "dead_outbox": data["dead_outbox"],
                   "media_mb": round(data["media_mb"], 1)}, ensure_ascii=False) +
              "\n可执行只读命令查看 data/daoyu.db（audit_log/tasks 表）辅助分析，"
              "结论一屏以内。")
    sid = ensure_ops_session(db, cfg)
    db.create_task(None, sid, prompt, kind="chat")
    _broadcast(db, cfg, text)
    db.audit("cron_daily", f"anomalies={len(anomalies)}")
    return f"异常 {len(anomalies)} 项，已推送并建分析任务"


def check_patrol(db, cfg, now: int, sample: dict, cpu_win, mem_win) -> list[dict]:
    """巡检判定（纯函数式，异常项列表）：磁盘 / CPU / 内存持续超载 /
    队列积压 / iLink token / 证书。死信不查——M2 已有即时告警专责，
    巡检不双通道重复。"""
    c = cfg.cron
    alerts = []
    for path, pct in sorted(sample.get("disks", {}).items()):
        if pct > c["disk_threshold_pct"]:
            alerts.append({"key": f"disk:{path}", "title": "磁盘",
                           "lines": [f"{path} 分区 {pct:.0f}%（阈值 "
                                     f"{c['disk_threshold_pct']}%）"]})
    n = c["load_sustain_min"]
    cpu_recent = list(cpu_win)[-n:]
    if len(cpu_recent) >= n and all(v > c["cpu_threshold_pct"] for v in cpu_recent):
        alerts.append({"key": "cpu", "title": "CPU",
                       "lines": [f"持续 {c['cpu_threshold_pct']}%+ 达 {n} 分钟"]})
    mem_recent = list(mem_win)[-n:]
    if len(mem_recent) >= n and all(v > c["mem_threshold_pct"] for v in mem_recent):
        alerts.append({"key": "mem", "title": "内存",
                       "lines": [f"持续 {c['mem_threshold_pct']}%+ 达 {n} 分钟"]})
    backlog = len(db.active_tasks())
    if backlog > c["queue_backlog_warn"]:
        alerts.append({"key": "queue", "title": "队列",
                       "lines": [f"积压 {backlog} 个任务（预警 "
                                 f"{c['queue_backlog_warn']}）"]})
    if not db.get_state("bot_token"):
        alerts.append({"key": "ilink_token", "title": "连接",
                       "lines": ["iLink token 缺失（连接未建立）"]})
    alerts += check_certs(cfg, now)
    return alerts


def check_certs(cfg, now: int) -> list[dict]:
    """cert_paths 下 *.pem 读 NotAfter；剩余 < cert_warn_days 告警。
    路径不存在/非证书文件跳过（Windows 开发机、privkey.pem 均不误报炸）。"""
    from cryptography import x509
    import datetime
    c = cfg.cron
    alerts = []
    for base in c.get("cert_paths", []):
        basep = Path(base)
        if not basep.is_dir():
            continue
        for pem in sorted(basep.rglob("*.pem")):
            try:
                cert = x509.load_pem_x509_certificate(pem.read_bytes())
                days_left = (cert.not_valid_after_utc
                             - datetime.datetime.fromtimestamp(
                                 now, datetime.timezone.utc)).days
            except (ValueError, OSError):
                continue
            if days_left < c["cert_warn_days"]:
                alerts.append({"key": f"cert:{pem}", "title": "证书",
                               "lines": [f"{pem} 剩余 {days_left} 天（预警 "
                                         f"{c['cert_warn_days']} 天）"]})
    return alerts


def silenced(db, key: str, silence_s: int, now: int) -> bool:
    """同类异常静默期内（alert_silence_h）不重报——防重复告警重复建任务烧钱；
    过期后仍异常会再报一次（防「告一次永远沉默」）。"""
    ts = db.get_state(f"cron_alert:{key}")
    return bool(ts and ts.isdigit() and now - int(ts) < silence_s)


def mark_alert(db, key: str, now: int) -> None:
    db.set_state(f"cron_alert:{key}", str(now))


def run_patrol(db, cfg, now: int, sample: dict, cpu_win, mem_win) -> str:
    """巡检主流程：判定 → 静默期过滤 → 告警推送 + 合并建一个分析任务。
    正常轮次零 Claude 调用（零成本原则）。"""
    alerts = check_patrol(db, cfg, now, sample, cpu_win, mem_win)
    if not alerts:
        return "正常"
    silence_s = int(cfg.cron["alert_silence_h"]) * 3600
    fresh = [a for a in alerts if not silenced(db, a["key"], silence_s, now)]
    if not fresh:
        return f"{len(alerts)} 项异常均在静默期内"
    lines = [f"⚠️ 巡检告警（{len(fresh)} 项）"]
    for a in fresh:
        lines += [f"[{a['title']}] {ln}" for ln in a["lines"]]
    detail = "\n".join(f"[{a['title']}] " + "；".join(a["lines"]) for a in fresh)
    prompt = ("刀鱼巡检系统自动任务：巡检发现以下异常，请分析原因并给出简要"
              "结论与建议。\n\n" + detail +
              "\n可执行只读命令（df/ps/日志/data/daoyu.db）辅助分析，结论一屏以内。")
    sid = ensure_ops_session(db, cfg)
    tid = db.create_task(None, sid, prompt, kind="chat")
    for a in fresh:
        mark_alert(db, a["key"], now)
    lines.append(f"⏳ 已建分析任务 #{tid}，结论稍后推送")
    _broadcast(db, cfg, "\n".join(lines))
    db.audit("cron_patrol", f"alerts={len(fresh)}")
    return f"告警 {len(fresh)} 项，已推送并建分析任务 #{tid}"
