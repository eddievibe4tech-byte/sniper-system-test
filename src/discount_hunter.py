"""
🎯 特價獵人引擎 (Discount Hunter) - 重構優化版

支援：
1. 修正基準價錨定（採用前日收盤價或近期高點，徹底解決「自我參照死鎖」無法觸發特價的 Bug）
2. 區分台美股幣別 (TWD / USD)，預算改為每檔分配額度（budget_per_target）
3. 採用 Dataclass 結構化資料，遵循 SRP 將「資料取得／策略計算／結果輸出」分層，提升型別安全與可測試性

監控池調整（長期總報酬視角）：
- 00940（元大台灣價值高息）→ 006208（富邦台50）：市值型、經理費 0.10%、
  總費用率約 0.23~0.25%，具備汰弱留強自動調適機制，資產累積階段顯著優於高息策略型 ETF。
"""
import json
import logging
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Dict, List

import pandas as pd
import yfinance as yf

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

TZ = timezone(timedelta(hours=8))
DATA_DIR = Path(__file__).resolve().parent.parent / "data"


class AnchorType(Enum):
    PREV_CLOSE = "prev_close"   # 適用單日拉回特價（相比昨收）
    RECENT_HIGH = "recent_high"  # 適用波段拉回特價（相比 20 日高點）


@dataclass
class TargetConfig:
    symbol: str
    name: str
    currency: str
    small_discount: float  # 例如 0.03 (3%)
    big_discount: float    # 例如 0.06 (6%)
    anchor_type: AnchorType = AnchorType.PREV_CLOSE


@dataclass
class MarketData:
    current_price: float
    anchor_price: float
    change_pct: float  # (現價 - 基準價) / 基準價


@dataclass
class HunterResult:
    symbol: str
    name: str
    currency: str
    current_price: float
    anchor_price: float
    change_pct: float
    status: str
    action: str
    small_zone: List[float]
    big_zone: List[float]
    first_batch_budget: float
    hunt_reserve_budget: float


# 監控清單配置（已修正為真實標的；00940 → 006208 富邦台50，長期複利優先）
HUNT_TARGETS: List[TargetConfig] = [
    TargetConfig("VOO", "S&P 500 ETF", "USD", 0.02, 0.05, AnchorType.PREV_CLOSE),
    TargetConfig("JPM", "摩根大通", "USD", 0.03, 0.07, AnchorType.PREV_CLOSE),
    TargetConfig("EOG", "能源探勘", "USD", 0.03, 0.08, AnchorType.PREV_CLOSE),
    # 🆕 修正：改用真正的台灣市值前 50 大龍頭 ETF
    TargetConfig("0050.TW", "元大台灣50", "TWD", 0.015, 0.035, AnchorType.PREV_CLOSE),
    # 🆕 修正：006208 顯著優於 00940——市值型、經理費 0.10%、總費用率更低、複利動能最大化
    TargetConfig("006208.TW", "富邦台50", "TWD", 0.015, 0.035, AnchorType.PREV_CLOSE),
]


class MarketDataFetcher:
    """負責行情抓取與資料清洗"""

    @staticmethod
    def fetch_market_data(targets: List[TargetConfig]) -> Dict[str, MarketData]:
        symbols = [t.symbol for t in targets]
        results = {}

        try:
            # 抓取 1 個月資料，確保計算 20 日高點或昨收價時數據充足
            df = yf.download(
                symbols,
                period="1mo",
                group_by="ticker",
                progress=False,
                threads=True,
                auto_adjust=True,
            )

            for target in targets:
                sym = target.symbol
                try:
                    target_df = df[sym].dropna() if isinstance(df.columns, pd.MultiIndex) else df.dropna()
                    if target_df.empty or len(target_df) < 2:
                        logger.warning(f"[{sym}] 歷史數據不足，跳過")
                        continue

                    current_price = round(float(target_df["Close"].iloc[-1]), 2)

                    # 決定基準價錨點
                    if target.anchor_type == AnchorType.RECENT_HIGH:
                        anchor_price = round(float(target_df["High"].tail(20).max()), 2)
                    else:
                        # 預設：前一交易日收盤價
                        anchor_price = round(float(target_df["Close"].iloc[-2]), 2)

                    change_pct = round((current_price - anchor_price) / anchor_price * 100, 2)

                    results[sym] = MarketData(
                        current_price=current_price,
                        anchor_price=anchor_price,
                        change_pct=change_pct,
                    )
                except Exception as e:
                    logger.warning(f"[{sym}] 解析失敗: {e}")

        except Exception as e:
            logger.error(f"批次下載價格失敗: {e}")

        return results


class DiscountHunterStrategy:
    """特價策略引擎核心"""

    @staticmethod
    def evaluate(target: TargetConfig, data: MarketData, single_budget: float) -> HunterResult:
        anchor = data.anchor_price
        curr = data.current_price

        # 正確邏輯：以「基準價」推算特價目標價位（而非現價自我參照）
        small_target_price = round(anchor * (1 - target.small_discount), 2)
        big_target_price = round(anchor * (1 - target.big_discount), 2)

        small_zone = [round(small_target_price * 0.995, 2), round(small_target_price * 1.005, 2)]
        big_zone = [round(big_target_price * 0.99, 2), round(big_target_price * 1.01, 2)]

        # 判定是否落入或低於特價點位
        if curr <= big_zone[1]:
            status = "🟢 已達大特價"
            action = "動用 70-80% 資金單筆加碼"
        elif curr <= small_zone[1]:
            status = "🟡 已達小特價"
            action = "動用 20-30% 資金分批買入"
        elif (curr - small_zone[1]) / curr * 100 < 1.0:
            status = "⚠️ 接近特價"
            action = "準備資金，預先掛限價單"
        else:
            status = "⏳ 尚未特價"
            action = "繼續定期定額或維持觀望"

        # 二八資金分配 (25% 先行，75% 儲備)
        first_batch = round(single_budget * 0.25, 2)
        hunt_reserve = round(single_budget * 0.75, 2)

        return HunterResult(
            symbol=target.symbol.replace(".TW", ""),
            name=target.name,
            currency=target.currency,
            current_price=curr,
            anchor_price=anchor,
            change_pct=data.change_pct,
            status=status,
            action=action,
            small_zone=small_zone,
            big_zone=big_zone,
            first_batch_budget=first_batch,
            hunt_reserve_budget=hunt_reserve,
        )


def run_discount_hunter(budget_per_target: float = None):
    """執行特價獵人引擎

    預算優先級：函式參數 > 環境變數 HUNT_BUDGET > 預設 10000
    （語意為「單一標的的配置額度」，避免台美股跨幣別總預算失真）
    """
    logger.info("🎯 啟動特價獵人引擎...")

    if budget_per_target is None:
        budget_per_target = float(os.getenv("HUNT_BUDGET", "10000"))
    logger.info(f"每檔標的配置額度: {budget_per_target:,.0f}（各標的依自身幣別計）")

    market_data_map = MarketDataFetcher.fetch_market_data(HUNT_TARGETS)
    results: List[HunterResult] = []

    for target in HUNT_TARGETS:
        data = market_data_map.get(target.symbol)
        if not data:
            continue
        eval_result = DiscountHunterStrategy.evaluate(target, data, budget_per_target)
        results.append(eval_result)

    # 排序：已達特價 > 接近特價 > 尚未特價；同狀態下依跌幅排序
    status_order = {"🟢 已達大特價": 0, "🟡 已達小特價": 1, "⚠️ 接近特價": 2, "⏳ 尚未特價": 3}
    results.sort(key=lambda x: (status_order.get(x.status, 4), x.change_pct))

    output = {
        "updated_at": datetime.now(TZ).isoformat(),
        "strategy_note": "二八分批法：25% 底倉參與，75% 留作特價狩獵金",
        "targets": [asdict(r) for r in results],
    }

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_file = DATA_DIR / "discount_hunter.json"
    out_file.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"✅ 特價獵人執行完畢，結果已寫入 {out_file}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="🎯 特價獵人引擎 (Discount Hunter)")
    parser.add_argument("--budget", type=float, default=None,
                        help="每檔標的配置額度（優先於環境變數 HUNT_BUDGET，預設 10000）")
    args = parser.parse_args()
    run_discount_hunter(budget_per_target=args.budget)
