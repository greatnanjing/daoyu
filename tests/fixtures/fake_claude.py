"""假 claude CLI：读 stdin 收 prompt，逐行回放 FAKE_CLAUDE_SCRIPT 指向的 NDJSON 文件。

真实 claude CLI（Node）恒以 UTF-8 收发；Windows 管道默认本地码页（cp936），
故这里显式 reconfigure，保证测试环境与生产行为一致。
"""
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
    if os.environ.get("FAKE_CLAUDE_STDERR"):
        sys.stderr.write(os.environ["FAKE_CLAUDE_STDERR"])
    with open(script, encoding="utf-8") as f:
        for line in f:
            sys.stdout.write(line)
            sys.stdout.flush()
            time.sleep(float(os.environ.get("FAKE_CLAUDE_DELAY", "0.01")))


if __name__ == "__main__":
    main()
