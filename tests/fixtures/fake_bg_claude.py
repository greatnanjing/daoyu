"""假 claude --bg：prompt 走 argv（非 stdin），stdout 首行 backgrounded → <id> + 任务卡。

真实 claude CLI（Node）恒 UTF-8；Windows 管道默认 cp936，显式 reconfigure，
保证 "→" 等非 ASCII 字符按 UTF-8 落管道（runner 侧 utf-8 errors=replace 解码）。
FAKE_BG_ARGS_LOG：把收到的 argv 写盘供断言（prompt 必须在 argv 里）。
FAKE_BG_ID：backgrounded 行里的 id（默认 ab12cd34）。
FAKE_BG_NO_ID=1：不输出 backgrounded 行（模拟 stdout 无 id 可解析）。
FAKE_BG_EXIT_CODE：输出后以该码退出（模拟启动失败）。
FAKE_BG_DELAY_MS：启动后先睡这么久再输出（模拟 daemon 慢，launch 期可被 /cancel）。
"""
import json
import os
import sys
import time


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    args_log = os.environ.get("FAKE_BG_ARGS_LOG")
    if args_log:
        with open(args_log, "w", encoding="utf-8") as f:
            json.dump({"argv": sys.argv[1:],
                       "claude_config_dir": os.environ.get("CLAUDE_CONFIG_DIR")},
                      f, ensure_ascii=False)
    delay_ms = os.environ.get("FAKE_BG_DELAY_MS")
    if delay_ms:
        time.sleep(int(delay_ms) / 1000.0)
    bg_id = os.environ.get("FAKE_BG_ID", "ab12cd34")
    if not os.environ.get("FAKE_BG_NO_ID"):
        print(f"backgrounded → {bg_id}", flush=True)
        print(f"  ✓ task {bg_id} 「跑个大活」 queued for execution", flush=True)
    code = os.environ.get("FAKE_BG_EXIT_CODE")
    if code:
        sys.stderr.write("bg failed: daemon unavailable\n")
        sys.exit(int(code))


if __name__ == "__main__":
    main()
