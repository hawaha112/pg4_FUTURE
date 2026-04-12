#!/bin/bash
# 完整测试：清除去重数据库 → 重新运行流水线

set -e
cd "$(dirname "$0")"

echo "🧹 临时备份并清除去重数据库..."
if [ -f "dedup.db" ]; then
    cp dedup.db dedup.db.bak
    rm -f dedup.db
    echo "  已备份 dedup.db → dedup.db.bak"
fi
if [ -f "history.json" ]; then
    cp history.json history.json.bak
    echo '[]' > history.json
    echo "  已备份 history.json → history.json.bak"
fi

echo ""
echo "🚀 执行完整流水线（含 LLM 分析）..."
bash run_daily.sh

echo ""
echo "✅ 完整测试结束！"
echo "   如需恢复去重历史：cp dedup.db.bak dedup.db"
