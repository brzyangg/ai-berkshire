---
name: portfolio-book
description: 用自然语言管理本地私有投资组合账本。用户描述买入、卖出、基金赎回、分红、利息、入金、出金、换汇、现金或证券转仓、交易纠错时使用；用户询问当前持仓、成本、净资产、总盈亏、现金池、资金池、交易历史或要求基于已保存账本做持仓分析时也使用。通过 tools/portfolio_book.py 执行，不直接手改 ledger 或持仓 CSV。
---

## Codex adapter note

This skill is generated from `skills/portfolio-book.md` so Claude Code and Codex users share one canonical workflow.

- Treat `$ARGUMENTS` as the user's request in the current Codex thread.
- When the source mentions Claude-only surfaces such as Task, Agent, WebSearch, Bash, Read, or Write, use the closest Codex capability available in this session: subagents when available, web search when needed, shell commands for local tools, and normal file edits for workspace files.
- Use shared project tools from `tools/` in this repository. Prefer running commands from the repository root with paths like `python3 tools/financial_rigor.py ...`; if the current thread starts outside the repo, locate the actual checkout path first instead of assuming a fixed home-directory path.
- Before starting research, run the `date` command to confirm today's date; treat it as the baseline for "latest" data and state the data cutoff date in the report header. Never assume the current date from training data.
- Preserve the research quality rules from `AGENTS.md`: cross-check financial data, use exact arithmetic tools for valuation/math, and clearly label uncertainty and source gaps.

# 组合账本自然语言管理

把用户的自然语言转换成可核验的账本操作，并以本地私有账本作为持仓数量、
成本、现金和交易历史的唯一事实来源。

## 数据位置

- 账本：`local/portfolio/ledger.csv`，追加式记录，Git 已忽略。
- 最新估值：`local/portfolio/valuation_latest.csv`。
- 最新汇总：`local/portfolio/summary_latest.json`。
- 净值历史：`local/portfolio/nav_history.csv`。
- 兼容持仓：`local/portfolio/current_holdings.csv`，并按本地配置同步旧 CSV。
- 操作程序：`tools/portfolio_book.py`。

从仓库根目录执行命令。若当前线程不在仓库中，先定位实际 checkout，不要假设
固定路径。不得把 `local/portfolio/` 中的私有金额提交到 Git。

## 意图路由

| 用户意图 | 动作 |
|---|---|
| 买入、卖出、清仓、基金赎回 | `trade` |
| 入金、出金、分红、利息、单独费用 | `cash` |
| 同币种现金跨账户移动 | `transfer` |
| 证券原股转仓 | `position-transfer` |
| 换汇 | `fx` |
| 录错、撤销上一笔记录 | 查 `history` 后用 `void` |
| 当前净值、盈亏、现金、成本、资金池 | 需要最新数据时先 `update`，再 `show` |
| 最近交易、卖出历史 | `history` |
| 持仓复盘、仓位分析、调仓研究 | 刷新账本后进入 `portfolio-review` 工作流 |

## 写账前解析

### 交易

从用户描述中解析：

- 买入或卖出；
- 账户；
- 标的名称和代码；
- 市场与币种；
- 数量；
- 成交价格，或场外基金的净回款；
- 手续费；
- 成交日期；
- 备注。

以下字段不得猜测：买卖方向、数量、成交价格/净回款、币种。缺少时一次性询问
全部缺失项。账户只有在该币种当前仅有一个账户时才可从账本推断；否则询问。
代码、市场和币种可以从 `data/portfolio_aliases.json` 中确定。

日期未给出且用户使用“今天、刚刚、刚才”等表达时采用今天。补录历史交易必须
使用用户给出的日期。手续费未提供时可按 0 记录，但备注必须写“手续费未提供”。

### 现金事件

至少需要事件类型、账户、币种和金额。分红或利息应记录来源标的。入金与卖出
回款不是一回事：卖出自动增加现金，不得再重复记录入金。

### 汇率

- 当日 CNY 事件自动使用 1。
- 当日 USD/HKD 事件省略 `--fx-to-cny`，由程序自动获取并核验。
- 历史 USD/HKD 事件必须提供交易日 `--fx-to-cny`。
- 不得用今天的汇率回填历史交易。

## 命令映射

### 买入

用户：“今天用美股账户买了 5 股 QQQM，成交价 300 美元，手续费 0.35。”

```bash
python3 tools/portfolio_book.py trade \
  --side buy --account US --name QQQM --code QQQM \
  --market US --currency USD --quantity 5 --price 300 \
  --fee 0.35 --note "用户提供的交易说明"
```

### 卖出

```bash
python3 tools/portfolio_book.py trade \
  --side sell --account US --name QQQ --code QQQ \
  --market US --currency USD --quantity 2 --price 750 \
  --fee 0.35
```

场外基金只知道最终回款时使用 `--net-amount`，不要反推后再当成正式成交价：

```bash
python3 tools/portfolio_book.py trade \
  --side sell --account CN --name 医疗基金 --code 012323 \
  --market CN --currency CNY --quantity 25060 \
  --net-amount 23515 --note "场外基金净赎回款"
```

### 现金

```bash
python3 tools/portfolio_book.py cash \
  --type deposit --account CN --currency CNY --amount 10000

python3 tools/portfolio_book.py cash \
  --type dividend --account US --currency USD \
  --name SGOV --amount 18.20
```

`--type` 仅使用：`deposit`、`withdraw`、`dividend`、`interest`、`fee`。

### 转账、转仓与换汇

```bash
python3 tools/portfolio_book.py transfer \
  --from-account TIGER --to-account IBKR \
  --currency HKD --amount 10000 --fx-to-cny 0.86

python3 tools/portfolio_book.py position-transfer \
  --from-account TIGER --to-account IBKR \
  --name 腾讯 --code 00700 --market HK --currency HKD \
  --quantity 200 --fee 500 --fx-to-cny 0.86

python3 tools/portfolio_book.py fx \
  --account IBKR --from-currency HKD --to-currency USD \
  --from-amount 7800 --to-amount 1000 \
  --fx-to-cny 0.86 --counter-fx-to-cny 6.71
```

证券转仓保留原数量和成本，不得记录成卖出再买入。

### 纠错

先运行：

```bash
python3 tools/portfolio_book.py history --limit 20
```

确定目标 `event_id` 后：

```bash
python3 tools/portfolio_book.py void \
  --event-id EVENT_ID --note "冲销原因"
```

若“上一笔”存在歧义，必须让用户确认。不得直接删除或修改账本历史行。

## 执行与核验

写操作按以下顺序执行：

1. 解析并检查必填字段。
2. 运行对应命令。不要使用 `eval`，对自由文本参数安全引用。
3. 命令失败时不修改其他文件，向用户说明缺失字段或现金/持仓不足。
4. 运行 `python3 tools/portfolio_book.py history --limit 5`，确认新事件存在。
5. 运行 `python3 tools/portfolio_book.py update` 刷新行情、汇率和净值。
6. 运行 `python3 tools/portfolio_book.py show`，核对数量、成本、现金和总资产。
7. 回答时报告 event_id、现金变化、持仓变化和已实现盈亏；未提供手续费等假设
   必须明确写出。

`update` 联网失败不代表记账失败。此时保留已经成功追加的事件，说明最新估值未
刷新，不得拿旧 `show` 输出冒充交易后的新净值。

## 查询与持仓分析

### 快速查询

- 用户问“现在持仓/净值/现金是多少”：运行 `update` 后 `show`。
- 用户明确只想看上次结果或不联网：直接 `show`，同时报告快照时间。
- 用户问交易历史：运行 `history`；卖出历史加 `--trades-only`。

### 组合复盘

用户要求持仓 review、仓位建议、现金部署或风险分析时：

1. 运行 `date` 确认研究日期。
2. 运行 `portfolio_book.py update`。
3. 读取 `summary_latest.json`、`valuation_latest.csv`、`nav_history.csv`；需要已实现
   盈亏或资金来源时再读取 `ledger.csv` 或运行 `history`。
4. 账本数据控制数量、平均成本、账户、现金和交易历史。不得使用旧报告覆盖账本。
5. 再按 `portfolio-review` 与 `financial-data` 的研究规则验证行情、估值、基本面
   和调仓判断。账本行情可用于组合展示，但决策级金融数字仍需独立核验。

输出至少包含：组合净资产、总盈亏、已实现/未实现盈亏、三币种现金池、现金管理
资产、可调配资金池、主要仓位、集中度和数据截止时间。

## 安全规则

- `ledger.csv` 是事实源，`current_holdings.csv` 和 Documents CSV 都是派生文件。
- 不直接手改或覆盖账本；纠错只能追加 `void`。
- 不把卖出和入金重复计入现金。
- 不在缺价格或缺汇率时计算完整净资产；沿用程序的“不完整”状态。
- 不提交 `local/portfolio/`、个人金额、账户明细或券商信息到公开仓库。
- 本工具不下单，只记录用户已经确认发生的交易。
