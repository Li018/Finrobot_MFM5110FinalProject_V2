import importlib.util

from .finnhub_utils import FinnHubUtils
from .yfinance_utils import YFinanceUtils
from .fmp_utils import FMPUtils
from .sec_utils import SECUtils
from .reddit_utils import RedditUtils


# A 股适配层：模块内部延迟导入 akshare / tushare，未安装依赖时只在调用时报错，
# 因此这里可以无条件导出，避免上层 agent 在导入阶段找不到类。
from .akshare_utils import AkShareUtils, is_cn_symbol
from .tushare_utils import TushareUtils
from .cn_provider import CNStockUtils, describe_provider

__all__ = [
    "FinnHubUtils",
    "YFinanceUtils",
    "FMPUtils",
    "SECUtils",
    "AkShareUtils",
    "TushareUtils",
    "CNStockUtils",
    "describe_provider",
    "is_cn_symbol",
]

if importlib.util.find_spec("finnlp") is not None:
    from .finnlp_utils import FinNLPUtils
    __all__.append("FinNLPUtils")
