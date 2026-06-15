import pandas as pd
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class SignalResult:
    stock_id: str
    triggered_golden: bool
    triggered_breakout: bool
    signal_rows: pd.DataFrame = field(default_factory=pd.DataFrame)


class BaseSignal(ABC):

    @abstractmethod
    def evaluate(self, stock_id: str) -> SignalResult:
        pass
