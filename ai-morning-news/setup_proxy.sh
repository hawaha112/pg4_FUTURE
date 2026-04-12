#!/bin/bash
# ============================================================
# 一键安装 claude-max-api-proxy
# 前提：已安装 Node.js (>=18) 和 Claude Code CLI 并已登录
# ============================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROXY_DIR="$SCRIPT_DIR/claude-max-api-proxy"

echo "🔍 检查依赖..."

# 检查 node
if ! command -v node &>/dev/null; then
    echo "❌ 未找到 Node.js，请先安装: https://nodejs.org"
    exit 1
fi
NODE_VER=$(node -v | sed 's/v//' | cut -d. -f1)
if [ "$NODE_VER" -lt 18 ]; then
    echo "❌ Node.js 版本过低（需要 >=18，当前 $(node -v)）"
    exit 1
fi
echo "  ✅ Node.js $(node -v)"

# 检查 npm
if ! command -v npm &>/dev/null; then
    echo "❌ 未找到 npm"
    exit 1
fi
echo "  ✅ npm $(npm -v)"

# 检查 Claude Code CLI
if ! command -v claude &>/dev/null; then
    echo "⏳ 安装 Claude Code CLI..."
    npm install -g @anthropic-ai/claude-code
fi
echo "  ✅ Claude Code CLI"

# 检查 Claude 登录状态
echo ""
echo "🔐 检查 Claude 登录状态..."
if claude auth status 2>&1 | grep -qi "not logged in\|error\|unauthorized"; then
    echo "⏳ 请在弹出的浏览器中登录你的 Anthropic 账号..."
    claude auth login
fi
echo "  ✅ Claude 已登录"

# 克隆/更新代理
echo ""
if [ -d "$PROXY_DIR" ]; then
    echo "🔄 更新 claude-max-api-proxy..."
    cd "$PROXY_DIR"
    git pull --ff-only 2>/dev/null || true
else
    echo "📥 克隆 claude-max-api-proxy..."
    git clone https://github.com/theserverlessdev/claude-max-api-proxy.git "$PROXY_DIR"
    cd "$PROXY_DIR"
fi

# 安装依赖并构建
echo "📦 安装依赖..."
npm install --silent

echo "🔨 构建..."
npm run build --silent

echo ""
echo "============================================"
echo "✅ 安装完成！"
echo ""
echo "代理已安装到: $PROXY_DIR"
echo "run_daily.sh 会在每次运行时自动启停代理"
echo ""
echo "手动测试: node $PROXY_DIR/dist/server/standalone.js"
echo "然后访问: curl http://localhost:3456/health"
echo "============================================"
