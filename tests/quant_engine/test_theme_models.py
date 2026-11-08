import pytest
import os
import yaml
from pydantic import ValidationError
from src.quant_engine.theme_models import ThemeStock, Theme, ThemeConfig, load_theme_config

# 測試 ThemeStock 模型
def test_theme_stock_valid():
    stock = ThemeStock(symbol="2330.TW", role="individual", odd_lot_allowed=True)
    assert stock.symbol == "2330.TW"
    assert stock.role == "individual"
    assert stock.odd_lot_allowed is True

def test_theme_stock_invalid_role():
    with pytest.raises(ValidationError):
        ThemeStock(symbol="2330.TW", role="invalid_role", odd_lot_allowed=True)

def test_theme_stock_missing_symbol():
    with pytest.raises(ValidationError):
        ThemeStock(role="individual", odd_lot_allowed=True)

# 測試 Theme 模型
def test_theme_valid():
    theme_data = {
        "theme_id": "cyber_security",
        "seasonal_months": [1, 2, 3],
        "stocks": [
            {"symbol": "2480.TW", "role": "individual", "odd_lot_allowed": True}
        ],
    }
    theme = Theme(**theme_data)
    assert theme.theme_id == "cyber_security"
    assert theme.seasonal_months == [1, 2, 3]
    assert len(theme.stocks) == 1
    assert theme.stocks[0].symbol == "2480.TW"

def test_theme_invalid_seasonal_month():
    with pytest.raises(ValidationError, match="月份必須在 1 到 12 之間"):
        Theme(theme_id="test", seasonal_months=[0], stocks=[])
    with pytest.raises(ValidationError, match="月份必須在 1 到 12 之間"):
        Theme(theme_id="test", seasonal_months=[13], stocks=[])

# 測試 ThemeConfig 模型
def test_theme_config_valid():
    config_data = {
        "themes": [
            {
                "theme_id": "cyber_security",
                "seasonal_months": [1, 2, 3],
                "stocks": [
                    {"symbol": "2480.TW", "role": "individual", "odd_lot_allowed": True}
                ],
            }
        ]
    }
    config = ThemeConfig(**config_data)
    assert len(config.themes) == 1
    assert config.themes[0].theme_id == "cyber_security"

# 測試 load_theme_config 函數
def test_load_theme_config_success(tmp_path):
    # 創建一個臨時的 YAML 文件
    yaml_content = """
themes:
  - theme_id: test_theme_1
    seasonal_months: [4, 5]
    stocks:
      - symbol: S1.TW
        role: individual
        odd_lot_allowed: true
  - theme_id: test_theme_2
    seasonal_months: [6]
    stocks:
      - symbol: E1.TW
        role: etf
        odd_lot_allowed: false
    """
    file_path = tmp_path / "test_config_valid.yaml"
    file_path.write_text(yaml_content, encoding="utf-8")

    config = load_theme_config(str(file_path))
    assert isinstance(config, ThemeConfig)
    assert len(config.themes) == 2
    assert config.themes[0].theme_id == "test_theme_1"
    assert config.themes[1].stocks[0].symbol == "E1.TW"

def test_load_theme_config_file_not_found():
    with pytest.raises(FileNotFoundError):
        load_theme_config("/non/existent/path/config.yaml")

def test_load_theme_config_invalid_format(tmp_path):
    # 創建一個無效的 YAML 文件 (缺少 symbol 字段)
    yaml_content = """
themes:
  - theme_id: test_theme
    seasonal_months: [1]
    stocks:
      - role: individual
        odd_lot_allowed: true
    """
    file_path = tmp_path / "test_config_invalid.yaml"
    file_path.write_text(yaml_content, encoding="utf-8")

    with pytest.raises(ValidationError): # 期望捕獲 ValidationError
        load_theme_config(str(file_path))

def test_load_theme_config_empty_file(tmp_path):
    file_path = tmp_path / "empty_config.yaml"
    file_path.write_text("", encoding="utf-8")

    with pytest.raises(ValidationError): # 期望捕獲 ValidationError
        load_theme_config(str(file_path))

def test_load_theme_config_malformed_yaml(tmp_path):
    file_path = tmp_path / "malformed_config.yaml"
    file_path.write_text("themes: -") # 無效 YAML

    with pytest.raises(Exception, match="載入題材配置文件時發生未知錯誤"): # yaml.safe_load 會拋出 YAMLError，被捕獲為 Exception
        load_theme_config(str(file_path))
