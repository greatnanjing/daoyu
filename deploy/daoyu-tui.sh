#!/usr/bin/env bash
# 刀鱼终端会话入口：与微信 bot 同一 CLAUDE_CONFIG_DIR、同一凭据链（宿主
# ~/.claude/settings.json 动态优先、secrets.env 兜底——key/模型映射会变，跟随
# 宿主）。聊完退出后微信发 /adopt 即可收养该会话交叉接续。
# 服务器 shell 已知坑：死代理（127.0.0.1:7897 未监听）不清掉则瞬间
# ConnectionRefused——这里无条件 unset。
# 自检：DAOYU_TUI_DRYRUN=1 打印解析后的环境不启动 claude。
set -e
cd "$(dirname "$0")/.."
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY
set -a; source claude/secrets.env; set +a
# 动态层：白名单 ANTHROPIC_*/API_TIMEOUT_MS，token/api-key 形态二选一去重
# （与 runner 的 merge_claude_secrets 同语义）
eval "$(python3 - <<'PY'
import json, os, shlex
try:
    env = (json.load(open(os.path.expanduser("~/.claude/settings.json"))) or {}).get("env") or {}
except Exception:
    env = {}
pick = {k: v for k, v in env.items() if isinstance(k, str) and isinstance(v, str)
        and (k.startswith("ANTHROPIC_") or k == "API_TIMEOUT_MS")}
if "ANTHROPIC_AUTH_TOKEN" in pick:
    print("unset ANTHROPIC_API_KEY")
if "ANTHROPIC_API_KEY" in pick:
    print("unset ANTHROPIC_AUTH_TOKEN")
for k, v in pick.items():
    print(f"export {k}={shlex.quote(v)}")
PY
)"
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
# 自定义 API key 确认弹窗答过 No 会进 rejected（单用户环境，env key 就是要用的）
r = d.setdefault("customApiKeyResponses", {"approved": [], "rejected": []})
if r.get("rejected"):
    r["approved"] = sorted(set(r.get("approved", [])) | set(r["rejected"]))
    r["rejected"] = []
    json.dump(d, open(p, "w"), indent=2)
PY
if [ "${DAOYU_TUI_DRYRUN:-0}" = "1" ]; then
    echo "cwd=$PWD"
    echo "CLAUDE_CONFIG_DIR=$CLAUDE_CONFIG_DIR"
    echo "ANTHROPIC_BASE_URL=$ANTHROPIC_BASE_URL"
    echo "ANTHROPIC_AUTH_TOKEN=${ANTHROPIC_AUTH_TOKEN:0:8}... / ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY:0:8}..."
    echo "DEFAULT_OPUS_MODEL=$ANTHROPIC_DEFAULT_OPUS_MODEL"
    exit 0
fi
# 版本回显：刀鱼的实测假设锚定 EXPECTED_CLAUDE_VERSION（worker/version.py），
# 漂移时 flag/输出形态行为可能失真——每次进 TUI 顺手可见
echo "claude $(claude --version 2>/dev/null || echo '?')"
exec claude "$@"
