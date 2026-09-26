#!/usr/bin/env python3
"""
一次性遷移腳本：標記 legacy telemetry 記錄（#88 Bug 3）

背景：9/13 早上之前的舊格式記錄沒有 entry_price，驗證四條件第 1 條永不成立，
這些記錄「永遠不會被驗證」，卻一直被前端計入「待驗證」，導致數字虛高（136 → 實際可驗證筆數）。

用法：
    python scripts/migrate_legacy.py            # 預設處理 data/telemetry.json
    python scripts/migrate_legacy.py <path>     # 指定其他 telemetry.json

冪等：重複執行不會重複標記；已有 actual_result 的記錄不受影響。
"""
import json
import sys
from pathlib import Path


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path('data/telemetry.json')
    if not path.exists():
        print(f"❌ 找不到 {path}，無需遷移")
        return 1

    with path.open(encoding='utf-8') as f:
        data = json.load(f)

    records = data.get('records', [])
    marked = 0
    for r in records:
        # 無 entry_price 且尚未驗證 → 標記為 legacy（永遠無法驗證）
        if not r.get('entry_price') and not r.get('actual_result'):
            if not r.get('legacy'):
                r['legacy'] = True
                marked += 1

    if marked == 0:
        print("✅ 無需標記（所有記錄均已有 entry_price、已驗證、或已是 legacy）")
        return 0

    with path.open('w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    remaining_pending = sum(1 for r in records
                            if not r.get('actual_result') and not r.get('legacy'))
    print(f"✅ 已標記 {marked} 筆 legacy 記錄於 {path}")
    print(f"   遷移後真實待驗證筆數：{remaining_pending}（原含 legacy 之虛高數字已排除）")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
