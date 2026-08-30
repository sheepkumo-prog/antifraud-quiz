#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
題庫自動生成 Pipeline
====================================================================
每週執行一次：
  1. 抓取內政部警政署 165 打詐儀錶板的詐騙手法清單
  2. 與資料庫快照比對，找出新增或內容有變動的手法
  3. 對每個變動的手法呼叫 Claude API 生成題目草稿
  4. 以 status='draft' 寫入 items 表，等待人工審核

設計上刻意不自動上架。依 Chauhan 等人（2025）之項目分析結果，
生成式模型產出的情境式選擇題在誘答選項有效性上明顯遜於人工編製，
故本流程僅產生草稿，品質把關由研究者於審核後台完成。

環境變數（放在 GitHub Secrets，不要寫進程式碼）：
  SUPABASE_URL           你的專案網址
  SUPABASE_SERVICE_KEY   service_role 金鑰（可繞過 RLS，務必保密）
  ANTHROPIC_API_KEY      Claude API 金鑰
選用：
  MAX_METHODS_PER_RUN    單次最多處理幾個手法，預設 3（控制成本）
  ITEMS_PER_METHOD       每個手法生成幾題，預設 6
  DRY_RUN                設為 1 時只印出結果不寫入資料庫
====================================================================
"""

import os
import re
import sys
import json
import time
import hashlib
import datetime as dt
from typing import Any

import requests

# --------------------------------------------------------------------
# 設定
# --------------------------------------------------------------------
DASHBOARD_API = "https://165dashboard.tw/CIB_DWS_API/api/FraudMethod/GetTodayFraudMethodList"
MODEL = "claude-sonnet-5"

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

MAX_METHODS = int(os.environ.get("MAX_METHODS_PER_RUN", "3"))
ITEMS_PER_METHOD = int(os.environ.get("ITEMS_PER_METHOD", "6"))
DRY_RUN = os.environ.get("DRY_RUN", "") == "1"

UA = "AntiFraudQuizBot/1.0 (academic research; weekly fetch)"

# 四大情境類型，依于小軒（2011）之分類
SCENARIOS = ["動之以情", "誘之以利", "恫之以嚇", "社會系統的信賴"]


def log(msg: str) -> None:
    print(f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


# --------------------------------------------------------------------
# Supabase REST 薄封裝
# --------------------------------------------------------------------
def sb(method: str, path: str, **kw) -> Any:
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": kw.pop("prefer", "return=representation"),
    }
    r = requests.request(method, url, headers=headers, timeout=30, **kw)
    if r.status_code >= 400:
        raise RuntimeError(f"Supabase {method} {path} -> {r.status_code}: {r.text[:400]}")
    return r.json() if r.text else None


# --------------------------------------------------------------------
# 1. 抓取儀錶板
# --------------------------------------------------------------------
def fetch_methods() -> list[dict]:
    log("抓取 165 打詐儀錶板手法清單…")
    r = requests.get(DASHBOARD_API, headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    data = r.json()
    if not data.get("isSuccess"):
        raise RuntimeError(f"儀錶板回傳失敗：{data.get('message')}")
    methods = data.get("body") or []
    log(f"取得 {len(methods)} 種手法")
    return methods


def content_hash(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()


# --------------------------------------------------------------------
# 2. 比對出新增或變動的手法
# --------------------------------------------------------------------
def diff_and_upsert(methods: list[dict]) -> list[dict]:
    existing = {m["method_id"]: m for m in (sb("GET", "fraud_methods?select=*") or [])}
    changed = []
    now = dt.datetime.now(dt.timezone.utc).isoformat()

    for m in methods:
        mid = m.get("Id")
        desc = (m.get("Description") or "").strip()
        h = content_hash(desc)
        old = existing.get(mid)

        row = {
            "method_id": mid,
            "name": m.get("Name"),
            "description": desc,
            "content_hash": h,
            "case_study_id": str(m.get("CaseStudyId") or ""),
            "updated_at": now,
        }

        if old is None:
            log(f"  新增手法：{row['name']}")
            changed.append(row)
        elif old.get("content_hash") != h:
            log(f"  話術有變動：{row['name']}")
            changed.append(row)

        if not DRY_RUN:
            sb("POST", "fraud_methods", json=row,
               prefer="resolution=merge-duplicates,return=minimal")

    return changed


# --------------------------------------------------------------------
# 3. 呼叫 Claude 生成題目
# --------------------------------------------------------------------
PROMPT = """你正在協助一項防詐教育研究，任務是依據臺灣警政署公布的真實詐騙話術，編寫防詐練習題。

# 來源資料
詐騙手法名稱：{name}
該手法的實際話術清單（來自內政部警政署 165 打詐儀錶板）：
{description}

# 編寫要求

請產出 {n} 題，題型分配如下：
- 是非選擇題 2 題（type = "tf"）：測驗對該手法基本特徵的辨識
- 陷阱題 2 題（type = "trap"）：題幹中刻意植入詐騙者慣用的話術線索，測驗學習者是否會被誘導
- 情境模擬題 {n_sim} 題（type = "sim"）：以三幕式的多輪訊息往返還原完整詐騙鋪陳歷程

每題都必須標註所屬情境類型，從以下四類擇一：動之以情、誘之以利、恫之以嚇、社會系統的信賴。

陷阱題另須標註所植入的話術技巧，從以下五種擇一或組合：超額報酬、權威身分、社會共識、急迫感、回報義務。

每題都要附一個模擬介面（media），從以下四種擇一：
- line：假通訊軟體對話，欄位 kind/title/time/msgs（msgs 為字串陣列）
- sms：假簡訊，欄位 kind/from/text，可選 link
- app：假應用程式畫面，欄位 kind/name/time/total/change/rows（rows 為 [名稱, 數值] 的陣列）/btn
- doc：假公文，欄位 kind/seal/title/lines（字串陣列）/body

# 內容規範

1. 所有機構名稱、網址、人名一律虛構，不得使用真實銀行、平台或政府機關的商標。假網址不可為真實可連線位址。
2. 選項為三個，只有一個正確答案，另外兩個誘答選項必須是真實的人會犯的錯誤判斷，不可明顯荒謬。
3. 解析要說明「為什麼」，指出可辨識的線索，而非只說「這是詐騙」。
4. 用語口語、避免金融與法律專業術語，一般民眾看得懂。
5. 題幹聚焦於被害者端的辨識線索與應對步驟，不要寫成可供實際操作的詐騙腳本。

# 輸出格式

只輸出 JSON 陣列，不要有任何說明文字或 markdown 標記。每個元素的結構：

{{
  "type": "tf",
  "scenario": "誘之以利",
  "tactic": "超額報酬",
  "level": 2,
  "stem": "題幹",
  "options": ["選項一", "選項二", "選項三"],
  "answer": 1,
  "explain": "解析",
  "media": {{"kind": "sms", "from": "+886 9XX-XXX-XXX", "text": "..."}},
  "source_tactic_no": 3
}}

情境模擬題（type = "sim"）改用這個結構，不需要 stem / options / answer / explain / media：

{{
  "type": "sim",
  "scenario": "動之以情",
  "level": 3,
  "title": "情境標題",
  "rounds": [
    {{"media": {{...}}, "q": "這一幕的提問", "options": [...], "answer": 0, "explain": "解析"}},
    {{...}}, {{...}}
  ],
  "source_tactic_no": 5
}}

source_tactic_no 請填該題所依據的話術在上面清單中的編號。"""


def generate_items(method: dict) -> list[dict]:
    n = ITEMS_PER_METHOD
    n_sim = max(1, n - 4)
    body = {
        "model": MODEL,
        "max_tokens": 8000,
        "messages": [{
            "role": "user",
            "content": PROMPT.format(
                name=method["name"],
                description=method["description"][:4000],
                n=n, n_sim=n_sim,
            ),
        }],
    }
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json=body, timeout=180,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"Claude API {r.status_code}: {r.text[:400]}")

    text = "".join(b.get("text", "") for b in r.json().get("content", []))
    text = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", text.strip())
    try:
        items = json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"回傳非合法 JSON：{e}\n前 300 字：{text[:300]}")
    if not isinstance(items, list):
        raise RuntimeError("回傳不是陣列")
    return items


# --------------------------------------------------------------------
# 4. 驗證與寫入
# --------------------------------------------------------------------
def validate(it: dict) -> list[str]:
    """回傳問題清單，空清單代表通過基本檢核"""
    errs = []
    if it.get("type") not in ("tf", "trap", "sim"):
        errs.append("type 不合法")
    if it.get("scenario") not in SCENARIOS:
        errs.append("scenario 不在四大類型中")

    if it.get("type") == "sim":
        rounds = it.get("rounds")
        if not isinstance(rounds, list) or not rounds:
            errs.append("sim 缺少 rounds")
        else:
            for i, rd in enumerate(rounds, 1):
                opts = rd.get("options")
                if not isinstance(opts, list) or len(opts) < 2:
                    errs.append(f"第 {i} 幕選項不足")
                elif not isinstance(rd.get("answer"), int) or not (0 <= rd["answer"] < len(opts)):
                    errs.append(f"第 {i} 幕 answer 超出範圍")
                if not rd.get("explain"):
                    errs.append(f"第 {i} 幕缺少解析")
    else:
        opts = it.get("options")
        if not isinstance(opts, list) or len(opts) < 2:
            errs.append("選項不足")
        elif not isinstance(it.get("answer"), int) or not (0 <= it["answer"] < len(opts)):
            errs.append("answer 超出範圍")
        if not it.get("stem"):
            errs.append("缺少題幹")
        if not it.get("explain"):
            errs.append("缺少解析")
    return errs


def insert_items(method: dict, items: list[dict], run_date: str) -> int:
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    rows, seq = [], 0
    for it in items:
        errs = validate(it)
        if errs:
            log(f"    略過一題（{'；'.join(errs)}）")
            continue
        seq += 1
        rows.append({
            "id": f"auto-{run_date}-{method['method_id']:03d}-{seq:02d}",
            "type": it["type"],
            "scenario": it["scenario"],
            "tactic": it.get("tactic"),
            "level": int(it.get("level") or 2),
            "title": it.get("title"),
            "stem": it.get("stem"),
            "options": it.get("options"),
            "answer": it.get("answer"),
            "explain": it.get("explain"),
            "rounds": it.get("rounds"),
            "media": it.get("media"),
            "status": "draft",
            "source_method_id": method["method_id"],
            "source_tactic_no": it.get("source_tactic_no"),
            "source_note": f"165 打詐儀錶板「{method['name']}」第 {it.get('source_tactic_no','?')} 條話術，抓取日期 {run_date}",
            "generated_at": now,
            "generated_by": MODEL,
        })
    if not rows:
        return 0
    if DRY_RUN:
        print(json.dumps(rows, ensure_ascii=False, indent=2)[:3000])
        return len(rows)
    sb("POST", "items", json=rows, prefer="resolution=ignore-duplicates,return=minimal")
    return len(rows)


# --------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------
def main() -> int:
    for k in ("SUPABASE_URL", "SUPABASE_SERVICE_KEY", "ANTHROPIC_API_KEY"):
        if not os.environ.get(k):
            log(f"缺少環境變數 {k}")
            return 1

    run_date = dt.date.today().strftime("%Y%m%d")
    total, status, note = 0, "success", ""

    try:
        methods = fetch_methods()
        changed = diff_and_upsert(methods)
        log(f"需要生成題目的手法：{len(changed)} 個")

        for m in changed[:MAX_METHODS]:
            log(f"  生成中：{m['name']}")
            try:
                items = generate_items(m)
                n = insert_items(m, items, run_date)
                total += n
                log(f"    寫入 {n} 題草稿")
                if not DRY_RUN:
                    sb("PATCH", f"fraud_methods?method_id=eq.{m['method_id']}",
                       json={"processed_at": dt.datetime.now(dt.timezone.utc).isoformat()},
                       prefer="return=minimal")
                time.sleep(2)
            except Exception as e:                       # 單一手法失敗不中斷整體
                status = "partial"
                note += f"{m['name']}: {e}\n"
                log(f"    失敗：{e}")

        if len(changed) > MAX_METHODS:
            note += f"另有 {len(changed) - MAX_METHODS} 個手法待下次處理\n"

    except Exception as e:
        status, note = "failed", str(e)
        log(f"執行失敗：{e}")

    if not DRY_RUN:
        try:
            sb("POST", "sync_log", prefer="return=minimal", json={
                "methods_fetched": len(methods) if "methods" in dir() else 0,
                "methods_changed": len(changed) if "changed" in dir() else 0,
                "items_generated": total,
                "status": status,
                "note": note[:2000] or None,
            })
        except Exception as e:
            log(f"寫入 sync_log 失敗：{e}")

    log(f"完成，共產生 {total} 題草稿，狀態 {status}")
    print(f"::notice::本次產生 {total} 題草稿，狀態 {status}")
    return 0 if status != "failed" else 1


if __name__ == "__main__":
    sys.exit(main())
