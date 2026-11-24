"""A 股交易成本模型。

与 nooshare-strategy 基线 CostModel 口径逐位对齐：
    buy(px)  = px * (1 + slippage_bp*BP) * (1 + commission_bp*BP)
    sell(px) = px * (1 - slippage_bp*BP) * (1 - (commission_bp + stamp_bp)*BP)
其中滑点包含在成交价里，佣金/印花税作为显式费用列示。
默认 2.5bp 佣金双边、5bp 印花税卖出、5bp 滑点双边（与基线默认一致）。
"""
from __future__ import annotations

from dataclasses import dataclass

BP = 1e-4


@dataclass(frozen=True)
class CostModel:
    commission_bp: float = 2.5
    stamp_bp: float = 5.0
    slippage_bp: float = 5.0
    transfer_bp: float = 0.0     # 过户费（沪深双边），默认 0，可按口径开启

    def buy_price(self, px: float) -> float:
        """含滑点与佣金的等效买入价（基线 buy 公式）。"""
        return px * (1 + self.slippage_bp * BP) * (1 + self.commission_bp * BP)

    def sell_price(self, px: float) -> float:
        """扣除滑点、佣金、印花税后的等效卖出价（基线 sell 公式）。"""
        return px * (1 - self.slippage_bp * BP) * (1 - (self.commission_bp + self.stamp_bp) * BP)

    def breakdown(self, px: float, shares: int, side: str) -> dict[str, float]:
        """把成交价拆为滑点成交价 + 显式费用，用于 Fill 逐笔记录。

        保持 net_cash 与 buy_price/sell_price 等效：
          买入付现 = raw*(1+slip)*(1+comm)*shares
          卖出得现 = raw*(1-slip)*(1-comm-stamp)*shares
        """
        slip_px = px * (1 + self.slippage_bp * BP) if side == "BUY" else px * (1 - self.slippage_bp * BP)
        slip_px = round(slip_px, 6)
        gross = slip_px * shares
        commission = round(gross * self.commission_bp * BP, 6)
        stamp = round(gross * self.stamp_bp * BP, 6) if side == "SELL" else 0.0
        transfer = round(gross * self.transfer_bp * BP, 6)
        slippage_amount = round(abs(slip_px - px) * shares, 6)
        return {
            "price": slip_px,
            "commission": commission,
            "stamp_duty": stamp,
            "transfer_fee": transfer,
            "slippage": slippage_amount,
        }
