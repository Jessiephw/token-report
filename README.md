# token-report

> 每天追蹤你的 Claude AI 花費 — 用了多少 token、各專案分布、訂閱划不划算。
> Built for solo workers who want to know where every dollar of AI cost goes.

掃描 `~/.claude/projects/` 下所有 Claude Code 對話 log，統計每月、每專案的：
- USD 花費（對齊 LiteLLM / ccusage 公開定價）
- Token 使用量（input / output / cache）
- 活躍時間（訊息間隔 ≤ 5 分鐘的累計）
- Wall-clock 時間（session 第一則到最後一則訊息）
- Session 數、模型分佈

支援模型：Claude Opus 4.7 / Sonnet 4.6 / Haiku 4.5（要加新模型改 `PRICING` dict）。

---

## 快速開始

### 1. 下載

```bash
git clone https://github.com/Jessiephw/token-report.git ~/token-report
cd ~/token-report
```

**不熟 git？** 從網頁版下載 ZIP 也可以：
1. 打開 https://github.com/Jessiephw/token-report
2. 點右上綠色 **Code** 按鈕 → **Download ZIP**
3. 解壓後把 `token-report-main` 資料夾搬到 Home 並改名為 `token-report`（最終要在 `~/token-report/`）
4. Terminal 跑 `cd ~/token-report`，後面步驟一致

### 2. （可選）設定你的 workspace 路徑

如果你有把 Claude Code 對話用「workspace + 子專案資料夾」結構管理（例如 `~/Desktop/my-workspace/projects/20260101_xxx/`），可以設環境變數讓 token-report 自動把 session 歸類到子專案：

```bash
export WORKSPACE_ROOT="$HOME/Desktop/my-workspace"
export SUBPROJECTS_ROOT="$WORKSPACE_ROOT/projects"
export CLAUDE_SUB_USD="100"   # 你的月訂閱費 USD（預設 100）
```

沒設這些變數也能跑 — 只是不會做子專案細分歸類，每個 session 用 cwd 當專案名。

### 3. 跑掃描

```bash
python3 scripts/extract.py
```

產出 3 個檔案在 `data/`：

| 檔案 | 看什麼 |
|---|---|
| `monthly.csv` | 每月 × 每專案花費明細 |
| `monthly-totals.csv` | 每月總計 + value ratio |
| `sessions.csv` | 每場對話一列（最細） |

### 4. 打開 Dashboard 看視覺化

跑這兩個指令 — 一個開瀏覽器 dashboard、一個開 Finder 資料夾：

```bash
open ~/token-report/dashboard.html   # 瀏覽器打開 dashboard
open ~/token-report/data/            # Finder 打開資料夾，看到 3 個 csv
```

從 Finder 把 **data 資料夾內 3 個 .csv**（monthly.csv / monthly-totals.csv / sessions.csv）直接**拖到 dashboard 視窗左側的「上傳區」**（虛線框），圖表就會跑出來：

- 月花費折線圖 vs 訂閱費
- 各專案佔比圓餅圖
- Token 類型分佈（Input / Output / Cache）
- 每月 value ratio（API 等價 ÷ 訂閱費）
- 所有 session 明細可過濾

書卷暖色系設計，可直接在瀏覽器看／截圖分享。

---

## 自動排程（macOS launchd）

範本 plist 在 `examples/com.user.token-report.plist`。

複製到 `~/Library/LaunchAgents/` 後**改兩個 `<絕對路徑>`**（用 `pwd` 在 token-report 目錄拿到完整路徑）：

```bash
cd ~/token-report
pwd
# 例如輸出：/Users/yourname/token-report
```

把 plist 內的 `<絕對路徑>` 替換成 `pwd` 輸出的字串，再：

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.user.token-report.plist
```

每月 1 號 09:00 會自動跑掃描。

---

## 定價

| 模型 | Input | Output | Cache read | Cache creation |
|---|---|---|---|---|
| Opus 4.7 | $5 | $25 | $0.50 | $6.25 |
| Sonnet 4.6 | $3 | $15 | $0.30 | $3.75 |
| Haiku 4.5 | $1 | $5 | $0.10 | $1.25 |

（USD per million tokens；對齊 LiteLLM 公開定價，與 ccusage 約 3-4% 誤差）

**未來新模型怎麼加？** Anthropic 之後出新模型（例如 Opus 5）的話，打開 `scripts/extract.py`，找到最上方的 `PRICING` 表，照著現有格式多加一行：

```python
"claude-opus-5": {"input": 5.0, "output": 25.0, "cache_read": 0.50, "cache_creation": 6.25},
```

4 個價格從 Anthropic 官方定價頁拿。沒加新模型的話，掃到那筆 cost 會算成 $0。

---

## License

MIT
