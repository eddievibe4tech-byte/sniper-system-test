from pydantic import BaseModel, Field, ValidationError, model_validator
from typing import List, Literal, Optional

class ThemeStock(BaseModel):
    symbol: str = Field(..., description="股票代號，例如 '2330.TW'")
    role: Literal["pick_and_shovel", "etf", "individual"] = Field(..., description="標的在題材中的角色")
    odd_lot_allowed: bool = Field(True, description="是否允許零股交易")

class Theme(BaseModel):
    theme_id: str = Field(..., description="題材唯一識別碼，例如 'cyber_security'")
    seasonal_months: List[int] = Field(..., description="適合操作此題材的月份列表 (1-12)")

    @model_validator(mode='after')
    def validate_seasonal_months(self) -> 'Theme':
        for month in self.seasonal_months:
            if not (1 <= month <= 12):
                raise ValueError("月份必須在 1 到 12 之間")
        return self
    stocks: List[ThemeStock] = Field(..., description="此題材包含的股票列表")

class ThemeConfig(BaseModel):
    themes: List[Theme] = Field(..., description="所有戰備題材的列表")

def load_theme_config(file_path: str) -> ThemeConfig:
    import yaml
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            config_data = yaml.safe_load(f)
        return ThemeConfig.model_validate(config_data)
    except FileNotFoundError:
        raise FileNotFoundError(f"題材配置文件未找到：{file_path}")
    except ValidationError as e:
        raise e
    except Exception as e:
        raise Exception(f"載入題材配置文件時發生未知錯誤：{e}")

if __name__ == "__main__":
    # 範例使用 (用於測試 ValidationError)
    example_yaml_content_valid = """
themes:
  - theme_id: cyber_security
    seasonal_months: [1, 2, 3]
    stocks:
      - symbol: 2480.TW
        role: individual
        odd_lot_allowed: true
      - symbol: 00770.TW # 範例，實際可能沒有此 ETF
        role: etf
        odd_lot_allowed: false
  - theme_id: power_shortage
    seasonal_months: [6, 7, 8]
    stocks:
      - symbol: 1504.TW # 東元
        role: pick_and_shovel
        odd_lot_allowed: true
    """

    example_yaml_content_invalid = """
themes:
  - theme_id: cyber_security
    seasonal_months: [1, 2, 3]
    stocks:
      - symbol: 2480.TW
        role: individual
        # odd_lot_allowed 欄位缺失
    """
    
    # 模擬寫入一個有效的 YAML 檔案並載入
    with open("/tmp/valid_theme_watchlist.yaml", "w", encoding="utf-8") as f:
        f.write(example_yaml_content_valid)
    
    try:
        valid_config = load_theme_config("/tmp/valid_theme_watchlist.yaml")
        print("Valid config loaded successfully.")
        # print(valid_config.model_dump_json(indent=2))
    except Exception as e:
        print(f"Error loading valid config: {e}")

    # 模擬寫入一個無效的 YAML 檔案並嘗試載入
    with open("/tmp/invalid_theme_watchlist.yaml", "w", encoding="utf-8") as f:
        f.write(example_yaml_content_invalid)

    try:
        invalid_config = load_theme_config("/tmp/invalid_theme_watchlist.yaml")
    except ValidationError as e:
        print(f"Invalid config caught expected ValidationError: {e.json()}")
    except Exception as e:
        print(f"Unexpected error for invalid config: {e}")
