#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

today="$(date +%Y%m%d)"
input="${1:-${PORTFOLIO_CSV:-${HOME}/Documents/投资表_数据表_表格.csv}}"

python3 tools/portfolio_tracker.py update \
  --input "${input}" \
  --output "data/portfolio/holdings_latest.csv" \
  --report "reports/持仓分析-${today}.md"
