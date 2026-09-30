"""商品規格:點值、最小跳動、手數限制。

風險模組(圖 5)計算部位大小時需要每點價值;這裡用標準外匯合約的近似算法,
交叉盤(帳戶幣別不在報價中)以報價幣別近似,並在 :meth:`Instrument.pip_value` 註明。
"""
from __future__ import annotations

from pydantic import BaseModel


class Instrument(BaseModel):
    symbol: str
    base: str
    quote: str
    pip_size: float
    digits: int
    contract_size: float = 100_000.0
    lot_min: float = 0.01
    lot_max: float = 100.0
    lot_step: float = 0.01
    spread_pips: float = 1.0  # 紙上交易 / 回測用的典型點差(來回成本)

    def pips(self, distance: float) -> float:
        return abs(distance) / self.pip_size

    def round_price(self, price: float) -> float:
        return round(price, self.digits)

    def round_lots(self, lots: float) -> float:
        """向下取整到 lot_step,並夾在 [0, lot_max]。低於 lot_min 回傳 0。"""
        if lots <= 0:
            return 0.0
        steps = int(lots / self.lot_step + 1e-9)
        rounded = round(steps * self.lot_step, 4)
        if rounded < self.lot_min:
            return 0.0
        return min(rounded, self.lot_max)

    def pip_value(self, price: float, account_currency: str = "USD") -> float:
        """每 1 標準手、每 1 點的價值(以帳戶幣別計)。

        - 報價幣別 = 帳戶幣別(EURUSD 對 USD 帳戶):contract × pip
        - 基礎幣別 = 帳戶幣別(USDJPY 對 USD 帳戶):contract × pip / price
        - 交叉盤:以報價幣別近似(需要即時換匯才精確,這裡刻意保守處理)
        """
        value_in_quote = self.contract_size * self.pip_size
        if self.quote == account_currency:
            return value_in_quote
        if self.base == account_currency and price > 0:
            return value_in_quote / price
        return value_in_quote


_REGISTRY: dict[str, Instrument] = {}


def register(instrument: Instrument) -> None:
    _REGISTRY[instrument.symbol.upper()] = instrument


def get_instrument(symbol: str) -> Instrument:
    """依商品代碼取得規格;未登錄者依幣別自動推導(JPY 報價 → 0.01 點)。"""
    key = symbol.upper()
    if key in _REGISTRY:
        return _REGISTRY[key]
    if len(key) < 6:
        raise ValueError(f"無法推導商品規格:{symbol}")
    base, quote = key[:3], key[3:6]
    if quote == "JPY":
        inst = Instrument(symbol=key, base=base, quote=quote, pip_size=0.01, digits=3)
    else:
        inst = Instrument(symbol=key, base=base, quote=quote, pip_size=0.0001, digits=5)
    _REGISTRY[key] = inst
    return inst
