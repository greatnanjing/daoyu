"""daoyu-notify CLI（M5A 通知入口）：任意 shell / cron / Claude Code hooks 推微信。

    daoyu-notify <标题> [正文…]                  # 正文多段空格拼接
    daoyu-notify --hook stop|notification        # stdin 读 Claude Code hooks JSON

纯单向推送（不建任务、不进会话）。DB/白名单解析：env DAOYU_DB +
DAOYU_WHITELIST（逗号分隔；测试/多实例用）齐备则不读 config.json；否则
load_config() 按包定位 repo。失败 stderr 一行 + exit 1，不静默。"""
import argparse
import json
import os
import sys

from common.notify import PREFIX_ASK, PREFIX_DONE, PREFIX_NOTIFY, push_notification

_HOOK_PREFIX = {"stop": PREFIX_DONE, "notification": PREFIX_ASK}


def _resolve_targets() -> tuple[str, list[str]]:
    db_env = os.environ.get("DAOYU_DB", "")
    wl_env = os.environ.get("DAOYU_WHITELIST", "")
    if db_env and wl_env:   # 齐备即完全不碰 config.json（测试机无实例配置也可用）
        return db_env, [u.strip() for u in wl_env.split(",") if u.strip()]
    from common.config import load_config
    cfg = load_config()
    db = db_env or str(cfg.db_path)
    wl = ([u.strip() for u in wl_env.split(",") if u.strip()] if wl_env
          else sorted(cfg.whitelist))
    return db, wl


def _from_hook(event: str, raw: str) -> tuple[str, str]:
    """hooks stdin JSON → (标题, 正文)。解析失败/字段缺席降级容错——通知失败
    不该阻塞宿主会话流。字段名以真机实测为准，缺席即省略对应行。"""
    try:
        data = json.loads(raw)
    except ValueError:
        return "终端事件", raw.strip()[:200]
    if event == "stop":
        cwd = data.get("cwd")
        return "终端任务完成", f"📁 {cwd}" if isinstance(cwd, str) else ""
    if event == "notification":
        msg = data.get("message")
        return "Claude 等待确认", str(msg) if msg else ""
    return "终端事件", ""


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="daoyu-notify", description="推送通知到微信（经刀鱼出站通道）")
    p.add_argument("title", nargs="?", help="通知标题")
    p.add_argument("body", nargs="*", help="正文（多段空格拼接）")
    p.add_argument("--hook", choices=sorted(_HOOK_PREFIX),
                   help="从 stdin 读 Claude Code hooks JSON 并按事件格式化")
    args = p.parse_args(argv)

    if args.hook:
        title, body = _from_hook(args.hook, sys.stdin.read())
        source = f"hook:{args.hook}"
        prefix = _HOOK_PREFIX[args.hook]
    else:
        if not args.title:
            p.error("缺少标题（或用 --hook）")
        title, body = args.title, " ".join(args.body)
        source, prefix = "cli", PREFIX_NOTIFY

    try:
        db_path, users = _resolve_targets()
        from common.db import Database
        db = Database(db_path)
        db.ensure_schema()
        n = push_notification(db._conn, users, title, body,
                              source=source, prefix=prefix)
    except Exception as e:   # DB 不可达/配置缺失：stderr 一行，不静默
        print(f"daoyu-notify 失败: {e!r}", file=sys.stderr)
        return 1
    print(f"已推送 {n} 位用户")
    return 0


if __name__ == "__main__":
    sys.exit(main())
