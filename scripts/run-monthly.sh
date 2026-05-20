#!/usr/bin/env bash
# 月度更新：跑 extract.py 產生最新資料，輸出簡要摘要

set -e
cd "$(dirname "$0")/.."

echo "=== 跑 extract.py 抽資料 ==="
python3 scripts/extract.py

echo ""
echo "=== 摘要 ==="
python3 -c "
import json
with open('data/monthly.json') as f:
    rows = json.load(f)
rows.sort(key=lambda r: -r['cost_usd'])
total_cost = sum(r['cost_usd'] for r in rows)
total_active = sum(r['active_hours'] for r in rows)
total_sessions = sum(r['sessions'] for r in rows)
print(f'總計  \${total_cost:.2f} USD｜活躍 {total_active:.1f}h｜{total_sessions} sessions')
print()
print('Top 5 by cost:')
for r in rows[:5]:
    label = f'{r[\"project\"]}/{r[\"subproject\"]}' if r.get('subproject') else r['project']
    print(f'  {label:50s} \${r[\"cost_usd\"]:>6.2f}  活躍 {r[\"active_hours\"]:>4.1f}h')
"

echo ""
echo "資料已更新：data/monthly.csv  data/sessions.csv  data/monthly.json"
