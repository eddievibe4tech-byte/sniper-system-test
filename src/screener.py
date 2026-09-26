"""
台股海選引擎 (Screener) - 方案 C 混合版
動態條件調整 + 空結果容錯
"""
import json
import os
import time
from datetime import datetime, timedelta
from typing import List, Dict, Optional
from finmind_client import FinMindClient

# 嚴格條件（多頭市場）
STRICT_RULES = {
    "revenue_yoy_min": 15.0,
    "inst_buy_days_min": 3,
    "price_above_ma20": True,
    "max_price_checks": 60,
    "sleep_seconds": 0.5,
}

# 放寬條件（震盪/空頭市場）
RELAXED_RULES = {
    "revenue_yoy_min": 10.0,
    "inst_buy_days_min": 2,
    "price_above_ma20": False,
    "max_price_checks": 60,
    "sleep_seconds": 0.5,
}


def _latest_rev_month(today):
    """月營收約於次月 10 日公佈 → 取最近已公佈月份"""
    y, m = today.year, today.month - (1 if today.day >= 10 else 2)
    while m <= 0:
        m += 12
        y -= 1
    return y, m


def fetch_month_revenue(finmind, y, m):
    """取得指定月份的營收數據

    🔴 修正（#90）：抓取失敗回傳 None（不再用 `or []` 吞掉失敗），
    讓上層能區分「FinMind 拒答」與「真的沒有資料」。
    """
    data = finmind._make_request('TaiwanStockMonthRevenue', '',
                                 start_date=f"{y}-{m:02d}-01",
                                 end_date=f"{y}-{m:02d}-31")
    if data is None:
        return None
    return {r['stock_id']: r['revenue'] for r in data if r.get('revenue')}


def fetch_inst_streak(finmind, codes, lookback):
    """逐日批量抓全市場投信買賣超，回傳各檔連買天數"""
    days, d = [], datetime.now()
    while len(days) < lookback:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            days.append(d.strftime('%Y-%m-%d'))
    
    streak, stopped = {c: 0 for c in codes}, set()
    for day in days:
        data = finmind._make_request('TaiwanStockInstitutionalInvestorsBuySell', '',
                                     start_date=day, end_date=day) or []
        net = {r['stock_id']: r.get('Investment_Trust_net', 0) for r in data}
        for c in codes:
            if c in stopped:
                continue
            if net.get(c, 0) > 0:
                streak[c] += 1
            else:
                stopped.add(c)
        time.sleep(0.3)
    return streak


def get_all_taiwan_stocks(finmind: FinMindClient) -> List[Dict]:
    """
    取得全市場股票清單（過濾掉 ETF 與權證）
    """
    try:
        data = finmind.get_stock_list() or []
    except Exception as e:
        print(f"⚠️ 獲取股票清單失敗：{e}")
        data = []
    
    stocks = []
    for item in data:
        stock_id = item.get('stock_id', '')
        stock_name = item.get('stock_name', '')
        industry = item.get('industry_category', '未知')
        
        if len(stock_id) == 4 and stock_id.isdigit():
            stocks.append({
                'stock_id': stock_id,
                'stock_name': stock_name,
                'industry': industry
            })
    
    # P1-1 防禦：若股票數量異常少，發出警告並使用預設清單
    if len(stocks) < 500:
        print(f"⚠️ 警告：股票宇宙只有 {len(stocks)} 檔（正常應 >1700），使用預設活躍股清單...")
        fallback_codes = ["2330", "2317", "2382", "2308", "2454", "2881", "2882", "3711", "3017", "1504", 
                          "1519", "2303", "2412", "2002", "2884", "2885", "2886", "2890", "2891", "2892"]
        stocks = [{'stock_id': code, 'stock_name': code, 'industry': '預設'} for code in fallback_codes]
    
    print(f"✅ 載入 {len(stocks)} 檔台股")
    return stocks


def _screen_with_rules(finmind: FinMindClient, rules: Dict, info: Dict) -> List[Dict]:
    """使用指定規則進行海選"""
    print(f"🔍 啟動海選引擎（營收>{rules['revenue_yoy_min']}%, 投信>{rules['inst_buy_days_min']}天，MA20={rules['price_above_ma20']}）...")
    
    # 關卡 1：營收 YoY
    today = datetime.now()
    y, m = _latest_rev_month(today)
    rev_now = fetch_month_revenue(finmind, y, m)
    rev_past = fetch_month_revenue(finmind, y - 1, m)
    # 🔴 修正（#90）：抓取失敗要大聲失敗，不要偽裝成「市場太弱」
    if rev_now is None or rev_past is None:
        raise RuntimeError("FinMind 批量營收抓取失敗（400？檢查 data_id 參數）")
    yoy = {c: (rev_now[c] - rev_past[c]) / rev_past[c] * 100
           for c in rev_now if rev_past.get(c)}
    pool = [c for c, v in yoy.items() if v >= rules["revenue_yoy_min"]]
    print(f"  關卡 1 營收 YoY>{rules['revenue_yoy_min']}%：剩 {len(pool)} 檔")

    # 關卡 2：投信連買
    streak = fetch_inst_streak(finmind, pool, rules.get("inst_lookback_days", 10))
    pool = [c for c in pool if streak[c] >= rules["inst_buy_days_min"]]
    print(f"  關卡 2 投信連買>={rules['inst_buy_days_min']}天：剩 {len(pool)} 檔")

    # 關卡 3：股價 vs MA20（可選）— P0-2 修正：使用 _get_raw_prices 直接取價格陣列
    pool.sort(key=lambda c: (yoy[c], streak[c]), reverse=True)
    candidates = []
    for code in pool[:rules["max_price_checks"]]:
        try:
            prices = finmind._get_raw_prices(code, days=60)
            if len(prices) < 20:
                continue
            
            ma20 = sum(prices[-20:]) / 20
            current = prices[-1]
            
            if rules["price_above_ma20"] and current < ma20:
                continue
            
            s = info.get(code, {})
            candidates.append({
                "code": code,
                "name": s.get('stock_name', code),
                "industry": s.get('industry', '未知'),
                "revenue_yoy": round(yoy[code], 2),
                "inst_buy_days": streak[code],
                "current_price": round(current, 2),
                "ma20": round(ma20, 2),
                "screened_at": datetime.now().strftime('%Y-%m-%d'),
            })
        except Exception as e:
            print(f"  ⚠️ {code} 股價檢查失敗：{e}")
        finally:
            time.sleep(rules["sleep_seconds"])

    candidates.sort(key=lambda x: (x['revenue_yoy'], x['inst_buy_days']), reverse=True)
    return candidates


def run_weekly_screener(finmind: FinMindClient) -> List[Dict]:
    """方案 C 混合版：動態條件 + 空結果容錯

    🔴 修正（#90）：抓取失敗（RuntimeError）時記錄 error 欄位並非零退出，
    讓 workflow 紅燈、Actions 通知；不再把「FinMind 拒答」偽裝成「市場太弱」。
    rules_applied 改為誠實標籤：STRICT / RELAXED (auto) / NONE (genuine zero)。
    """
    info = {s['stock_id']: s for s in get_all_taiwan_stocks(finmind)}

    try:
        # 第一輪：嚴格條件
        candidates = _screen_with_rules(finmind, STRICT_RULES, info)
        rules_used = "STRICT"

        # 如果 0 檔，自動放寬條件重跑
        if not candidates:
            print("⚠️ 嚴格條件無候選股，自動放寬條件重跑...")
            candidates = _screen_with_rules(finmind, RELAXED_RULES, info)
            rules_used = "RELAXED (auto)"

            if candidates:
                print(f"✅ 放寬條件後找到 {len(candidates)} 檔候選股")
    except RuntimeError as e:
        # 抓取失敗 → 誠實記錄錯誤並讓 workflow 紅燈
        print(f"❌ 海選資料抓取失敗：{e}")
        save_screener_results([], rules_used="ERROR", error=str(e))
        raise SystemExit(1)

    # 真的 0 檔（數據正常但無符合者）→ 接受空結果（不中斷 workflow）
    if not candidates:
        print("⚠️ 本週真的無符合條件的候選股（數據正常，genuine zero）")
        save_screener_results([], rules_used="NONE (genuine zero)")
        return []

    top = candidates[:15]
    save_screener_results(top, rules_used=rules_used)
    print(f"✅ 海選完成：{len(top)} 檔候選")
    return top


def save_screener_results(candidates: List[Dict], rules_used: str = "UNKNOWN",
                          error: Optional[str] = None) -> None:
    data_dir = os.path.join(os.path.dirname(__file__), '..', 'data')
    os.makedirs(data_dir, exist_ok=True)
    output_path = os.path.join(data_dir, 'screener_candidates.json')

    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump({
            "candidates": candidates,
            "updated_at": datetime.now().isoformat(),
            "rules_applied": rules_used,
            "error": error,
        }, f, ensure_ascii=False, indent=2)

    print(f"📁 結果已儲存至：{output_path}")


if __name__ == "__main__":
    token = os.getenv('FINMIND_TOKEN', '')
    if not token:
        print("⚠️ 警告：未設定 FINMIND_TOKEN")
        exit(1)
    
    client = FinMindClient(token=token)
    candidates = run_weekly_screener(client)
    
    print("\n🎯 本週 Alpha 候選股 Top 5:")
    for i, stock in enumerate(candidates[:5], 1):
        print(f"  {i}. {stock['code']} {stock['name']} - 營收 {stock['revenue_yoy']}%, 投信 {stock['inst_buy_days']} 天")
