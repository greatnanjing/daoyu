"""假 claude CLI：读 stdin 收 prompt，逐行回放 FAKE_CLAUDE_SCRIPT 指向的 NDJSON 文件。

真实 claude CLI（Node）恒以 UTF-8 收发；Windows 管道默认本地码页（cp936），
故这里显式 reconfigure，保证测试环境与生产行为一致。

FAKE_CLAUDE_ARGS_LOG：把收到的 argv 写盘（若含 --mcp-config，连同其指向文件的
内容快照）——runner 的临时 mcp config 在任务结束即删，只能由子进程侧快照供断言。
FAKE_CLAUDE_EXIT_CODE：回放结束后以该码退出（模拟失败路径）。
"""
import json
import os
import sys
import time


def main():
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    prompt = sys.stdin.read()          # 必须消费 stdin（真实 claude 也从 stdin 读）
    script = os.environ["FAKE_CLAUDE_SCRIPT"]
    # 把收到的 prompt 存下来供测试断言
    with open(os.environ["FAKE_CLAUDE_STDIN_LOG"], "w", encoding="utf-8") as f:
        f.write(prompt)
    args_log = os.environ.get("FAKE_CLAUDE_ARGS_LOG")
    if args_log:
        rest = sys.argv[1:]
        info: dict = {"argv": rest}
        if "--mcp-config" in rest:
            i = rest.index("--mcp-config")
            try:
                with open(rest[i + 1], encoding="utf-8") as mf:
                    info["mcp_config"] = json.load(mf)
            except OSError:
                info["mcp_config"] = None
        with open(args_log, "w", encoding="utf-8") as f:
            json.dump(info, f, ensure_ascii=False)
    if os.environ.get("FAKE_CLAUDE_STDERR"):
        sys.stderr.write(os.environ["FAKE_CLAUDE_STDERR"])
    with open(script, encoding="utf-8") as f:
        for line in f:
            sys.stdout.write(line)
            sys.stdout.flush()
            time.sleep(float(os.environ.get("FAKE_CLAUDE_DELAY", "0.01")))
    code = os.environ.get("FAKE_CLAUDE_EXIT_CODE")
    if code:
        sys.exit(int(code))


if __name__ == "__main__":
    main()
