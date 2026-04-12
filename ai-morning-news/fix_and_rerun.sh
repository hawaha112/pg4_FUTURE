#!/bin/bash
# 修复 git 冲突 + 重新运行流水线

set -e
cd "$(dirname "$0")"

echo "🔧 修复 output 目录 git 冲突..."
cd output
# 中止之前失败的 rebase
git rebase --abort 2>/dev/null || true
# 强制同步远程
git fetch origin main
git reset --hard origin/main
echo "✅ output 目录已同步到远程最新状态"

cd ..

echo ""
echo "🚀 重新执行每日流水线..."
# 清空之前的去重记录中的今天的条目，让新文章可以重新分析
bash run_daily.sh
echo "✅ 执行完毕"
