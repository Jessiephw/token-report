#!/usr/bin/env python3
"""
掃描 ~/.claude/projects/*.jsonl，產出每月 / 每專案的 Claude token 使用報告。

輸出：
- data/sessions.csv     每個 session 一列
- data/monthly.csv      月份 × 專案 彙總
- data/monthly.json     同上 JSON 版（給 Notion / Sheet 推送用）

時間欄位：
- wall_minutes    第一則到最後一則訊息的 wall-clock 分鐘
- active_minutes  訊息間隔 <= 5 分鐘的累計分鐘
"""

from __future__ import annotations
import csv
import json
import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Iterable

# ---- 設定 ----
HOME = Path.home()
LOG_ROOT = HOME / ".claude" / "projects"
OUT_DIR = Path(__file__).parent.parent / "data"
OUT_DIR.mkdir(parents=True, exist_ok=True)

ACTIVE_GAP_MIN = 5  # 訊息間隔超過此值不計入 active time
TZ_TPE = timezone(timedelta(hours=int(os.environ.get("TZ_OFFSET_HOURS", 8))))

# 你的月訂閱費（USD）。透過環境變數 CLAUDE_SUB_USD 覆寫，預設 100。
SUBSCRIPTION = {
    "name": os.environ.get("CLAUDE_SUBSCRIPTION", "Claude Subscription"),
    "monthly_usd": float(os.environ.get("CLAUDE_SUB_USD", 100.0)),
}

# 可選：你的子專案根目錄（用 YYYYMMDD_xxx/ 命名的資料夾）。
# 設了 SUBPROJECTS_ROOT 環境變數後會啟用子專案歸類；沒設就只用 cwd 直接歸類。
_SUBPROJ_ENV = os.environ.get("SUBPROJECTS_ROOT", "").strip()
SUBPROJECTS_ROOT = Path(_SUBPROJ_ENV) if _SUBPROJ_ENV else None

# 每百萬 token 的價格（USD）— 來自 LiteLLM 公開定價（截至 2026-04）
# 對齊 ccusage 的計算結果
PRICING = {
    # 數值對齊 ccusage 使用的 LiteLLM 價格表
    "claude-opus-4-7": {
        "input": 5.0,
        "output": 25.0,
        "cache_read": 0.50,
        "cache_creation": 6.25,
    },
    "claude-sonnet-4-6": {
        "input": 3.0,
        "output": 15.0,
        "cache_read": 0.30,
        "cache_creation": 3.75,
    },
    "claude-haiku-4-5-20251001": {
        "input": 1.0,
        "output": 5.0,
        "cache_read": 0.10,
        "cache_creation": 1.25,
    },
}


def calc_cost(model: str, usage: dict) -> float:
    p = PRICING.get(model)
    if not p:
        return 0.0
    return (
        usage.get("input_tokens", 0) * p["input"]
        + usage.get("output_tokens", 0) * p["output"]
        + usage.get("cache_read_input_tokens", 0) * p["cache_read"]
        + usage.get("cache_creation_input_tokens", 0) * p["cache_creation"]
    ) / 1_000_000


def parse_ts(s: str) -> datetime:
    # 例：2026-04-24T09:24:26.771Z
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


PROJECT_NAME_RE = re.compile(r"^\d{8}_")

# 領域資料夾：無日期前綴的常駐分類夾，本身可當成「umbrella 專案」記帳。
# 環境變數 DOMAIN_FOLDERS 用逗號分隔多個，例：DOMAIN_FOLDERS="00_ADMIN,00_DOCS"
DOMAIN_FOLDERS = {f for f in os.environ.get("DOMAIN_FOLDERS", "").split(",") if f.strip()}


def list_subproject_names() -> list[str]:
    """列出所有 YYYYMMDD_ 開頭的正式專案資料夾（遞迴，支援母 / 子層巢狀）。"""
    if not SUBPROJECTS_ROOT or not SUBPROJECTS_ROOT.exists():
        return []
    names: set[str] = set()
    for p in SUBPROJECTS_ROOT.rglob("*"):
        if p.is_dir() and PROJECT_NAME_RE.match(p.name):
            names.add(p.name)
    return sorted(names)


SUBPROJECTS = list_subproject_names()
# 用於子專案匹配的 regex（每個資料夾名）
SUBPROJ_PATTERNS = {name: re.compile(re.escape(name)) for name in SUBPROJECTS}


CROSS_PROJECT_THRESHOLD = 3
CROSS_PROJECT_DOMINANCE = 2.0


def _is_cross_project(counts: dict[str, int]) -> bool:
    """提到 N+ 個專案，且 top1/top2 < dominance → 視為跨專案 session（歸 general）。"""
    if len(counts) < CROSS_PROJECT_THRESHOLD:
        return False
    sorted_vals = sorted(counts.values(), reverse=True)
    if sorted_vals[1] <= 0:
        return False
    return sorted_vals[0] / sorted_vals[1] < CROSS_PROJECT_DOMINANCE


def detect_subproject(text_blob: str) -> str | None:
    """從 session 內容（user/assistant text）找最常被提到的子專案資料夾名。
    跨專案討論（3+ 個專案、最高/第二高 < 2x）回傳 None，讓上層歸 (general)。"""
    if not text_blob:
        return None
    counts = {}
    for name, pat in SUBPROJ_PATTERNS.items():
        n = len(pat.findall(text_blob))
        if n:
            counts[name] = n
    if not counts:
        return None
    if _is_cross_project(counts):
        return None
    return max(counts.items(), key=lambda kv: kv[1])[0]


WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}


def _deepest_project_segment(rel_path: str, is_file: bool = False) -> str | None:
    """從 02_進行中專案 之下的相對路徑找最深層 YYYYMMDD_xxx **目錄**段。
    例：'00_TAESCO/20260424_cmvp-management-system/foo.gs'
        → 回 '20260424_cmvp-management-system'（最深目錄）。
    若母 / 子都是 YYYYMMDD_，子優先（比母深）。

    找不到 YYYYMMDD_ 段時，退回 DOMAIN_FOLDERS（如 00_TAESCO/）作為 umbrella，
    讓 '00_TAESCO/sop-handbook/x.md' 這種共用資源也能歸到 00_TAESCO/cost-time.md。

    is_file=True：rel_path 含檔名，最後一段是檔名不算目錄段，會跳過避免誤匹配
    像 '20260428_xxx.csv' 這種檔名前綴。"""
    parts = rel_path.split("/")
    if is_file and parts:
        parts = parts[:-1]
    for part in reversed(parts):
        if PROJECT_NAME_RE.match(part):
            return PROJECT_RENAME_ALIASES.get(part, part)
    for part in parts:
        if part in DOMAIN_FOLDERS:
            return part
    return None


def detect_subproject_from_writes(file_paths: list[str]) -> str | None:
    """從 session 的 Write/Edit 工具目標路徑找該 session 主要在動哪個子專案。
    跨專案 session（動到 3+ 個專案、最高/第二高 < 2x）回傳 None。"""
    if not file_paths:
        return None
    counts: dict[str, int] = defaultdict(int)
    for p in file_paths:
        if p.startswith(PROJECTS_PREFIX):
            rel = p[len(PROJECTS_PREFIX):]
            seg = _deepest_project_segment(rel, is_file=True)
            if seg:
                counts[seg] += 1
    if not counts:
        return None
    if _is_cross_project(dict(counts)):
        return None
    return max(counts.items(), key=lambda kv: kv[1])[0]


# 專案改名／搬遷後的對應（舊名 → 新名）。
# 如果你的子專案資料夾改過名/搬過位置，把舊→新對應加進來，避免歷史 session 散落。
# 範例：LEGACY_PROJECT_ALIASES = {"my-old-folder": "20260101_my-project"}
LEGACY_PROJECT_ALIASES: dict[str, str] = {}
PROJECT_RENAME_ALIASES: dict[str, str] = {}

# 你的 workspace 根目錄（cwd 落這之內會嘗試做子專案歸類）。
# 預設由 SUBPROJECTS_ROOT 推斷其父目錄，可用環境變數 WORKSPACE_ROOT 覆寫。
WORKSPACE_ROOT = os.environ.get("WORKSPACE_ROOT", "").rstrip("/")
if not WORKSPACE_ROOT and SUBPROJECTS_ROOT:
    WORKSPACE_ROOT = str(SUBPROJECTS_ROOT.parent)
PROJECTS_PREFIX = f"{SUBPROJECTS_ROOT}/" if SUBPROJECTS_ROOT else ""
PROJECT_DIR_RE = re.compile(r"^(\d{8}_[^/]+)")


def cwd_to_project(cwd: str | None) -> tuple[str, str]:
    """
    根據 cwd 決定 (project, source_tag)。
    source_tag = 'cwd' 表示 cwd 直接在專案資料夾內，不需內容比對。
    source_tag = 'workspace' 表示在 workspace root，需要內容比對才能歸類子專案。
    source_tag = 'other' 表示其他位置，不做內容比對。
    """
    if not cwd:
        return "(unknown)", "other"

    # 1) cwd 已經進到 SUBPROJECTS_ROOT/xxx 內 → 找最深層 YYYYMMDD_xxx 段
    if PROJECTS_PREFIX and cwd.startswith(PROJECTS_PREFIX):
        rel = cwd[len(PROJECTS_PREFIX):]
        seg = _deepest_project_segment(rel)
        if seg:
            return seg, "cwd"

    # 2) cwd 是 workspace 根目錄 → 之後用內容比對歸子專案
    if WORKSPACE_ROOT and cwd == WORKSPACE_ROOT:
        return Path(WORKSPACE_ROOT).name or "workspace", "workspace"

    # 3) workspace 下的其他路徑 — 用第一層子目錄當 project 名（或套用 alias）
    if WORKSPACE_ROOT and cwd.startswith(WORKSPACE_ROOT + "/"):
        rel = cwd[len(WORKSPACE_ROOT) + 1:]
        first_seg = rel.split("/", 1)[0]
        if first_seg in LEGACY_PROJECT_ALIASES:
            return LEGACY_PROJECT_ALIASES[first_seg], "cwd"
        return f"{Path(WORKSPACE_ROOT).name}/{first_seg}", "cwd"

    # 4) 外部位置
    if cwd == str(HOME):
        return "(Home)", "other"
    if cwd in ("/", "/private/tmp", "/tmp"):
        return "(misc)", "other"
    return Path(cwd).name or cwd, "other"


def extract_text(message: dict) -> str:
    """從 message.content 抽純文字，給子專案內容比對用。"""
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks = []
        for c in content:
            if not isinstance(c, dict):
                continue
            if c.get("type") == "text" and c.get("text"):
                chunks.append(c["text"])
            elif c.get("type") == "tool_use":
                # tool_use 的 input 可能含檔案路徑
                inp = c.get("input")
                if isinstance(inp, dict):
                    chunks.append(json.dumps(inp, ensure_ascii=False))
            elif c.get("type") == "tool_result":
                ct = c.get("content")
                if isinstance(ct, str):
                    chunks.append(ct[:2000])
                elif isinstance(ct, list):
                    for cc in ct:
                        if isinstance(cc, dict) and cc.get("type") == "text":
                            chunks.append(cc.get("text", "")[:2000])
        return "\n".join(chunks)
    return ""


def process_session(jsonl_path: Path) -> dict | None:
    """處理一個 session JSONL，回傳彙總 dict。"""
    timestamps: list[datetime] = []
    tokens_by_model: dict[str, dict[str, int]] = defaultdict(
        lambda: {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}
    )
    text_chunks: list[str] = []
    write_paths: list[str] = []  # Write/Edit 工具目標路徑，用來判斷 session 主專案
    cwd_seen: list[str] = []
    user_msg_count = 0
    assistant_msg_count = 0
    git_branch = None
    seen_message_ids: set[str] = set()  # 去重避免 resume / 重啟造成重算
    first_user_text: str | None = None  # 人類輸入的第一句，當 session summary

    try:
        with jsonl_path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = d.get("type")
                if "timestamp" in d:
                    try:
                        timestamps.append(parse_ts(d["timestamp"]))
                    except Exception:
                        pass
                if d.get("cwd"):
                    cwd_seen.append(d["cwd"])
                if d.get("gitBranch") and not git_branch:
                    git_branch = d["gitBranch"]

                if t == "user":
                    user_msg_count += 1
                    text_chunks.append(extract_text(d.get("message", {})))
                    if first_user_text is None and not d.get("isMeta"):
                        mc = (d.get("message") or {}).get("content")
                        if isinstance(mc, str):
                            s = mc.strip()
                            # 過濾系統包裝（<command-name>、<system-reminder>、<local-command-stdout> 等）
                            if s and not s.startswith("<"):
                                first_user_text = " ".join(s.split())[:120]
                elif t == "assistant":
                    assistant_msg_count += 1
                    msg = d.get("message", {}) or {}
                    text_chunks.append(extract_text(msg))
                    # 收 Write/Edit 工具目標路徑用於 session 主專案判斷
                    content = msg.get("content")
                    if isinstance(content, list):
                        for c in content:
                            if isinstance(c, dict) and c.get("type") == "tool_use" and c.get("name") in WRITE_TOOLS:
                                fp = (c.get("input") or {}).get("file_path") or ""
                                if fp:
                                    write_paths.append(fp)
                    usage = msg.get("usage") or {}
                    model = msg.get("model") or "unknown"
                    # 排除合成訊息（不計費）與重複（resume/重啟造成）
                    if not usage or model == "<synthetic>":
                        continue
                    msg_id = msg.get("id") or d.get("requestId")
                    if msg_id and msg_id in seen_message_ids:
                        continue
                    if msg_id:
                        seen_message_ids.add(msg_id)
                    tokens_by_model[model]["input"] += usage.get("input_tokens", 0)
                    tokens_by_model[model]["output"] += usage.get("output_tokens", 0)
                    tokens_by_model[model]["cache_read"] += usage.get("cache_read_input_tokens", 0)
                    tokens_by_model[model]["cache_creation"] += usage.get(
                        "cache_creation_input_tokens", 0
                    )
    except Exception as e:
        print(f"  跳過 {jsonl_path.name}: {e}")
        return None

    if not timestamps:
        return None

    timestamps.sort()
    wall_seconds = (timestamps[-1] - timestamps[0]).total_seconds()
    active_seconds = 0.0
    gap_threshold = ACTIVE_GAP_MIN * 60
    for prev, cur in zip(timestamps, timestamps[1:]):
        gap = (cur - prev).total_seconds()
        if gap <= gap_threshold:
            active_seconds += gap

    total_cost = 0.0
    total_input = total_output = total_cache_read = total_cache_creation = 0
    models_used = []
    for model, tk in tokens_by_model.items():
        usage_dict = {
            "input_tokens": tk["input"],
            "output_tokens": tk["output"],
            "cache_read_input_tokens": tk["cache_read"],
            "cache_creation_input_tokens": tk["cache_creation"],
        }
        total_cost += calc_cost(model, usage_dict)
        total_input += tk["input"]
        total_output += tk["output"]
        total_cache_read += tk["cache_read"]
        total_cache_creation += tk["cache_creation"]
        models_used.append(model)

    cwd = Counter(cwd_seen).most_common(1)[0][0] if cwd_seen else None
    project, source_tag = cwd_to_project(cwd)
    text_blob = "\n".join(text_chunks)

    # 先檢查 session 內容是否跨多個專案（無視 cwd）
    text_counts: dict[str, int] = {}
    for name, pat in SUBPROJ_PATTERNS.items():
        n = len(pat.findall(text_blob))
        if n:
            text_counts[name] = n
    if text_counts and _is_cross_project(text_counts):
        # 跨專案 session（例：workspace 維運、安全清理、跨多個專案的討論）→ 歸 (general)
        project = "claude-workspace (general)"
    elif source_tag == "workspace":
        # cwd = workspace 根，且內容沒跨專案：用 write/text 找主要專案
        matched = detect_subproject_from_writes(write_paths)
        if not matched:
            matched = detect_subproject(text_blob)
        if matched:
            project = matched
        else:
            project = "claude-workspace (general)"
    # 否則：cwd 直接命中某子專案、且內容也沒跨專案 → 維持 cwd 判斷

    started_local = timestamps[0].astimezone(TZ_TPE)
    ended_local = timestamps[-1].astimezone(TZ_TPE)

    return {
        "session_id": jsonl_path.stem,
        "started_at": started_local.isoformat(timespec="seconds"),
        "ended_at": ended_local.isoformat(timespec="seconds"),
        "date": started_local.strftime("%Y-%m-%d"),
        "month": started_local.strftime("%Y-%m"),
        "project": project,
        "cwd": cwd or "",
        "git_branch": git_branch or "",
        "models": ", ".join(sorted(set(models_used))),
        "user_messages": user_msg_count,
        "assistant_messages": assistant_msg_count,
        "input_tokens": total_input,
        "output_tokens": total_output,
        "cache_read_tokens": total_cache_read,
        "cache_creation_tokens": total_cache_creation,
        "total_tokens": total_input + total_output + total_cache_read + total_cache_creation,
        "cost_usd": round(total_cost, 4),
        "wall_minutes": round(wall_seconds / 60, 1),
        "active_minutes": round(active_seconds / 60, 1),
        "summary": first_user_text or "",
    }


def main():
    if not LOG_ROOT.exists():
        raise SystemExit(f"找不到 {LOG_ROOT}")

    sessions: list[dict] = []
    for proj_dir in sorted(LOG_ROOT.iterdir()):
        if not proj_dir.is_dir():
            continue
        jsonls = sorted(proj_dir.glob("*.jsonl"))
        print(f"[{proj_dir.name}] {len(jsonls)} sessions")
        for jl in jsonls:
            row = process_session(jl)
            if row:
                sessions.append(row)

    if not sessions:
        print("沒有資料")
        return

    # 寫 sessions.csv
    sessions_csv = OUT_DIR / "sessions.csv"
    fields = list(sessions[0].keys())
    with sessions_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(sessions)
    print(f"\n寫入 {sessions_csv} ({len(sessions)} sessions)")

    # 月份 × 專案 彙總
    agg: dict[tuple, dict] = defaultdict(
        lambda: {
            "sessions": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
            "total_tokens": 0,
            "cost_usd": 0.0,
            "wall_minutes": 0.0,
            "active_minutes": 0.0,
            "user_messages": 0,
            "assistant_messages": 0,
            "models": set(),
        }
    )
    for s in sessions:
        key = (s["month"], s["project"])
        a = agg[key]
        a["sessions"] += 1
        a["input_tokens"] += s["input_tokens"]
        a["output_tokens"] += s["output_tokens"]
        a["cache_read_tokens"] += s["cache_read_tokens"]
        a["cache_creation_tokens"] += s["cache_creation_tokens"]
        a["total_tokens"] += s["total_tokens"]
        a["cost_usd"] += s["cost_usd"]
        a["wall_minutes"] += s["wall_minutes"]
        a["active_minutes"] += s["active_minutes"]
        a["user_messages"] += s["user_messages"]
        a["assistant_messages"] += s["assistant_messages"]
        for m in s["models"].split(", "):
            if m:
                a["models"].add(m)

    monthly_rows = []
    for (month, project), v in sorted(agg.items()):
        monthly_rows.append(
            {
                "month": month,
                "project": project,
                "sessions": v["sessions"],
                "user_messages": v["user_messages"],
                "assistant_messages": v["assistant_messages"],
                "input_tokens": v["input_tokens"],
                "output_tokens": v["output_tokens"],
                "cache_read_tokens": v["cache_read_tokens"],
                "cache_creation_tokens": v["cache_creation_tokens"],
                "total_tokens": v["total_tokens"],
                "cost_usd": round(v["cost_usd"], 2),
                "wall_hours": round(v["wall_minutes"] / 60, 2),
                "active_hours": round(v["active_minutes"] / 60, 2),
                "models": ", ".join(sorted(v["models"])),
            }
        )

    monthly_csv = OUT_DIR / "monthly.csv"
    with monthly_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(monthly_rows[0].keys()))
        w.writeheader()
        w.writerows(monthly_rows)
    print(f"寫入 {monthly_csv} ({len(monthly_rows)} 列)")

    monthly_json = OUT_DIR / "monthly.json"
    with monthly_json.open("w", encoding="utf-8") as f:
        json.dump(monthly_rows, f, ensure_ascii=False, indent=2)
    print(f"寫入 {monthly_json}")

    # 每月總計（跨月趨勢用）
    month_totals: dict[str, dict] = defaultdict(
        lambda: {
            "sessions": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
            "total_tokens": 0,
            "cost_usd": 0.0,
            "wall_hours": 0.0,
            "active_hours": 0.0,
            "projects": set(),
            "models": set(),
        }
    )
    for r in monthly_rows:
        m = month_totals[r["month"]]
        m["sessions"] += r["sessions"]
        m["input_tokens"] += r["input_tokens"]
        m["output_tokens"] += r["output_tokens"]
        m["cache_read_tokens"] += r["cache_read_tokens"]
        m["cache_creation_tokens"] += r["cache_creation_tokens"]
        m["total_tokens"] += r["total_tokens"]
        m["cost_usd"] += r["cost_usd"]
        m["wall_hours"] += r["wall_hours"]
        m["active_hours"] += r["active_hours"]
        m["projects"].add(r["project"])
        for mm in r["models"].split(", "):
            if mm:
                m["models"].add(mm)

    sub_cost = SUBSCRIPTION["monthly_usd"]

    def value_verdict(api_cost: float) -> str:
        """根據 API 等價花費對比訂閱費用，給出建議。"""
        ratio = api_cost / sub_cost if sub_cost > 0 else 0
        if ratio >= 2.0:
            return f"超值（{ratio:.1f}x）— 月費賺翻"
        if ratio >= 1.2:
            return f"划算（{ratio:.1f}x）— 訂閱比 API 便宜"
        if ratio >= 0.8:
            return f"打平（{ratio:.2f}x）— 月費剛好"
        if ratio >= 0.4:
            return f"偏貴（{ratio:.2f}x）— 可考慮降方案"
        return f"超用不到（{ratio:.2f}x）— 強烈建議降方案"

    totals_rows = []
    for month, v in sorted(month_totals.items()):
        api_cost = round(v["cost_usd"], 2)
        ratio = round(api_cost / sub_cost, 2) if sub_cost > 0 else 0
        totals_rows.append(
            {
                "month": month,
                "projects": len(v["projects"]),
                "sessions": v["sessions"],
                "input_tokens": v["input_tokens"],
                "output_tokens": v["output_tokens"],
                "cache_read_tokens": v["cache_read_tokens"],
                "cache_creation_tokens": v["cache_creation_tokens"],
                "total_tokens": v["total_tokens"],
                "cost_usd": api_cost,
                "wall_hours": round(v["wall_hours"], 2),
                "active_hours": round(v["active_hours"], 2),
                "cost_per_active_hour": round(v["cost_usd"] / v["active_hours"], 2) if v["active_hours"] > 0 else 0,
                "subscription": SUBSCRIPTION["name"],
                "subscription_usd": sub_cost,
                "value_ratio": ratio,
                "verdict": value_verdict(api_cost),
                "models": ", ".join(sorted(v["models"])),
            }
        )

    totals_csv = OUT_DIR / "monthly-totals.csv"
    with totals_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(totals_rows[0].keys()))
        w.writeheader()
        w.writerows(totals_rows)
    print(f"寫入 {totals_csv} ({len(totals_rows)} 列)")

    totals_json = OUT_DIR / "monthly-totals.json"
    with totals_json.open("w", encoding="utf-8") as f:
        json.dump(totals_rows, f, ensure_ascii=False, indent=2)

    # 總覽
    total_cost = sum(r["cost_usd"] for r in monthly_rows)
    total_active = sum(r["active_hours"] for r in monthly_rows)
    total_wall = sum(r["wall_hours"] for r in monthly_rows)
    print(f"\n=== 總覽 ===")
    print(f"  Sessions:    {len(sessions)}")
    print(f"  USD 花費:    ${total_cost:.2f}")
    print(f"  Wall hours:  {total_wall:.1f} 小時")
    print(f"  Active hours: {total_active:.1f} 小時")
    print(f"\n=== 每月總計（跨月趨勢） ===")
    for r in totals_rows:
        print(f"  {r['month']}  ${r['cost_usd']:>7.2f}  活躍 {r['active_hours']:>5.1f}h  {r['sessions']:>3} sessions ({r['projects']} 專案)")

    print(f"\n=== 月費 vs API 等價（{SUBSCRIPTION['name']} ${sub_cost:.0f}/mo） ===")
    for r in totals_rows:
        print(f"  {r['month']}  API ${r['cost_usd']:>7.2f}  vs 月費 ${sub_cost:.0f}  →  {r['verdict']}")


if __name__ == "__main__":
    main()
