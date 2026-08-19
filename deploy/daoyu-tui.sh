#!/usr/bin/env bash
# 刀鱼终端会话入口：与微信 bot 同一 CLAUDE_CONFIG_DIR、同一凭据（secrets.env
# 现场注入——宿主 shell 没有 ANTHROPIC_*，直接 claude 会拿不到 key）。聊完退出
# 后微信发 /adopt 即可收养该会话交叉接续。
# 服务器 shell 已知坑：死代理（127.0.0.1:7897 未监听）不清掉则瞬间
# ConnectionRefused——这里无条件 unset。
# 自检：DAOYU_TUI_DRYRUN=1 打印解析后的环境不启动 claude。
set -e
cd "$(dirname "$0")/.."
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY
set -a; source claude/secrets.env; set +a
export CLAUDE_CONFIG_DIR="$PWD/data/claude-home"
mkdir -p "$CLAUDE_CONFIG_DIR"
# TUI 需要 hasCompletedOnboarding 标记；-p 无头路径从不写它（claude-home 由
# runner/daemon 建立），缺标记时 TUI 卡首跑引导。幂等补齐，失败不阻断启动。
python3 - <<'PY' 2>/dev/null || true
import json, os
p = os.path.join(os.environ["CLAUDE_CONFIG_DIR"], ".claude.json")
try:
    d = json.load(open(p))
except Exception:
    d = {}
if not d.get("hasCompletedOnboarding"):
    d["hasCompletedOnboarding"] = True
    json.dump(d, open(p, "w"), indent=2)
PY
if [ "${DAOYU_TUI_DRYRUN:-0}" = "1" ]; then
    echo "cwd=$PWD"
    echo "CLAUDE_CONFIG_DIR=$CLAUDE_CONFIG_DIR"
    echo "ANTHROPIC_BASE_URL=$ANTHROPIC_BASE_URL"
    echo "ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY:0:8}..."
    echo "onboarding=$(python3 -c "import json,os;print(json.load(open(os.environ['CLAUDE_CONFIG_DIR']+'/.claude.json')).get('hasCompletedOnboarding'))")"
    exit 0
fi
exec claude "$@"
