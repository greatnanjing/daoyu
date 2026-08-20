"""M4 主动服务调度器：日报（daily）+ 巡检（patrol）。

scheduler_loop 每分钟整分对齐醒来、现读 cron_jobs 表决定动作（/cron 改表
即时生效）；日报/巡检判定纯 Python 零 token 成本，推送经 db.enqueue 落
outbox（发白名单全部用户），异常时建 Claude 分析任务挂 ops 话题（固定
UUID，分析历史上下文延续）。scheduler 绝不直接调 iLink、不自己跑 Claude。
"""
import time

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
