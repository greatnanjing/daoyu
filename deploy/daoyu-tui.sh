#!/usr/bin/env bash
# 刀鱼终端会话入口：与微信 bot 同一 CLAUDE_CONFIG_DIR、同一凭据（secrets.env
# 现场注入——宿主 shell 没有 ANTHROPIC_*，直接 claude 会拿不到 key）。聊完退出
# 后微信发 /adopt 即可收养该会话交叉接续。
# 服务器 shell 已知坑：死代理（127.0.0.1:7897 未监听）不清掉则瞬间
# ConnectionRefused——这里无条件 unset。
set -e
cd "$(dirname "$0")/.."                     # repo 根（secrets.env 与 claude-home 相对它）
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY
set -a; source claude/secrets.env; set +a   # 与 runner 注入子进程同一来源
export CLAUDE_CONFIG_DIR="$PWD/data/claude-home"
mkdir -p "$CLAUDE_CONFIG_DIR"
exec claude "$@"
