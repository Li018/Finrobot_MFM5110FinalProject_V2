import os
import json
import importlib
import yfinance as yf
import backtrader as bt
from backtrader.strategies import SMA_CrossOver
from typing import Annotated, List, Tuple
from matplotlib import pyplot as plt
from pprint import pformat
from IPython import get_ipython
from pathlib import Path
import numpy as np
import pandas as pd


class DeployedCapitalAnalyzer(bt.Analyzer):
    def start(self):
        self.deployed_capital = []
        self.initial_cash = self.strategy.broker.get_cash()  # Initial cash in account

    def notify_order(self, order):
        if order.status in [order.Completed]:
            if order.isbuy():
                self.deployed_capital.append(order.executed.price * order.executed.size)
            elif order.issell():
                self.deployed_capital.append(order.executed.price * order.executed.size)

    def stop(self):
        total_deployed = sum(self.deployed_capital)
        final_cash = self.strategy.broker.get_value()
        net_profit = final_cash - self.initial_cash
        if total_deployed > 0:
            self.retn = net_profit / total_deployed
        else:
            self.retn = 0

    def get_analysis(self):
        return {"return_on_deployed_capital": self.retn}


class BackTraderUtils:

    def back_test(
        ticker_symbol: Annotated[
            str, "Ticker symbol of the stock (e.g., 'AAPL' for Apple)"
        ],
        start_date: Annotated[
            str, "Start date of the historical data in 'YYYY-MM-DD' format"
        ],
        end_date: Annotated[
            str, "End date of the historical data in 'YYYY-MM-DD' format"
        ],
        strategy: Annotated[
            str,
            "BackTrader Strategy class to be backtested. Can be pre-defined or custom. Pre-defined options: 'SMA_CrossOver'. If custom, provide module path and class name as a string like 'my_module:TestStrategy'.",
        ],
        strategy_params: Annotated[
            str,
            "Additional parameters to be passed to the strategy class formatted as json string. E.g. {'fast': 10, 'slow': 30} for SMACross.",
        ] = "",
        sizer: Annotated[
            int | str | None,
            "Sizer used for backtesting. Can be a fixed number or a custom Sizer class. If input is integer, a corresponding fixed sizer will be applied. If custom, provide module path and class name as a string like 'my_module:TestSizer'.",
        ] = None,
        sizer_params: Annotated[
            str,
            "Additional parameters to be passed to the sizer class formatted as json string.",
        ] = "",
        indicator: Annotated[
            str | None,
            "Custom indicator class added to strategy. Provide module path and class name as a string like 'my_module:TestIndicator'.",
        ] = None,
        indicator_params: Annotated[
            str,
            "Additional parameters to be passed to the indicator class formatted as json string.",
        ] = "",
        cash: Annotated[
            float, "Initial cash amount for the backtest. Default to 10000.0"
        ] = 10000.0,
        save_fig: Annotated[
            str | None, "Path to save the plot of backtest results. Default to None."
        ] = None,
        data_path: Annotated[str | None, "V2 local cleaned OHLCV CSV; uses the fixed SMA rule"] = None,
        config_path: Annotated[str | None, "V2 experiment JSON configuration"] = None,
    ) -> str:
        """
        Use the Backtrader library to backtest a trading strategy on historical stock data.
        """
        if data_path:
            cfg = json.loads(Path(config_path).read_text(encoding="utf-8")) if config_path else {}
            cfg.update(json.loads(strategy_params) if strategy_params else {})
            frame = pd.read_csv(data_path, index_col="Date", parse_dates=True)
            signals = reference_signals(frame, cfg.get("fast", 20), cfg.get("slow", 60))
            return json.dumps(run_signal_backtest(frame, signals, cfg), ensure_ascii=False, allow_nan=False)
        cerebro = bt.Cerebro()

        if strategy == "SMA_CrossOver":
            strategy_class = SMA_CrossOver
        else:
            assert (
                ":" in strategy
            ), "Custom strategy should be module path and class name separated by a colon."
            module_path, class_name = strategy.split(":")
            module = importlib.import_module(module_path)
            strategy_class = getattr(module, class_name)

        strategy_params = json.loads(strategy_params) if strategy_params else {}
        cerebro.addstrategy(strategy_class, **strategy_params)

        # Create a data feed
        data = bt.feeds.PandasData(
            dataname=yf.download(ticker_symbol, start_date, end_date, auto_adjust=True)
        )
        cerebro.adddata(data)  # Add the data feed
        # Set our desired cash start
        cerebro.broker.setcash(cash)

        # Set the size of the trades
        if sizer is not None:
            if isinstance(sizer, int):
                cerebro.addsizer(bt.sizers.FixedSize, stake=sizer)
            else:
                assert (
                    ":" in sizer
                ), "Custom sizer should be module path and class name separated by a colon."
                module_path, class_name = sizer.split(":")
                module = importlib.import_module(module_path)
                sizer_class = getattr(module, class_name)
                sizer_params = json.loads(sizer_params) if sizer_params else {}
                cerebro.addsizer(sizer_class, **sizer_params)

        # Set additional indicator
        if indicator is not None:
            assert (
                ":" in indicator
            ), "Custom indicator should be module path and class name separated by a colon."
            module_path, class_name = indicator.split(":")
            module = importlib.import_module(module_path)
            indicator_class = getattr(module, class_name)
            indicator_params = json.loads(indicator_params) if indicator_params else {}
            cerebro.addindicator(indicator_class, **indicator_params)

        # Attach analyzers
        cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name="sharpe_ratio")
        cerebro.addanalyzer(bt.analyzers.DrawDown, _name="draw_down")
        cerebro.addanalyzer(bt.analyzers.Returns, _name="returns")
        cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name="trade_analyzer")
        # cerebro.addanalyzer(DeployedCapitalAnalyzer, _name="deployed_capital")

        stats_dict = {"Starting Portfolio Value:": cerebro.broker.getvalue()}

        results = cerebro.run()  # run it all
        first_strategy = results[0]

        # Access analysis results
        stats_dict["Final Portfolio Value"] = cerebro.broker.getvalue()
        # stats_dict["Deployed Capital"] = pformat(
        #     first_strategy.analyzers.deployed_capital.get_analysis(), indent=4
        # )
        stats_dict["Sharpe Ratio"] = (
            first_strategy.analyzers.sharpe_ratio.get_analysis()
        )
        stats_dict["Drawdown"] = first_strategy.analyzers.draw_down.get_analysis()
        stats_dict["Returns"] = first_strategy.analyzers.returns.get_analysis()
        stats_dict["Trade Analysis"] = (
            first_strategy.analyzers.trade_analyzer.get_analysis()
        )

        if save_fig:
            directory = os.path.dirname(save_fig)
            if directory:
                os.makedirs(directory, exist_ok=True)
            plt.figure(figsize=(12, 8))
            cerebro.plot()
            plt.savefig(save_fig)
            plt.close()

        return "Back Test Finished. Results: \n" + pformat(stats_dict, indent=2)


def reference_signals(data, fast=20, slow=60):
    if not 1 <= fast < slow:
        raise ValueError("Require 1 <= fast < slow")
    return (data["Close"].rolling(fast).mean() > data["Close"].rolling(slow).mean()).astype(int)


class _SignalFeed(bt.feeds.PandasData):
    lines = ("target",)
    params = (("target", "Target"),)


class _Commission(bt.CommInfoBase):
    params = (("stocklike", True), ("commtype", bt.CommInfoBase.COMM_PERC),
              ("percabs", True), ("minimum", 5.0))

    def _getcommission(self, size, price, pseudoexec):
        return max(abs(size) * price * self.p.commission, self.p.minimum) if size else 0.0


class _SignalStrategy(bt.Strategy):
    params = (("config", None),)

    def __init__(self):
        self.cfg = self.p.config
        self.equity, self.trades, self.risk_events = [], [], []
        self.pending = None

    def next_open(self):
        if len(self.data) < 2 or self.pending:
            return
        if self.cfg.get("evaluation_start") and not self.equity:
            # Warm-up bars do not trade. The first evaluation close supplies the first signal.
            return
        target = int(self.data.target[-1])  # Previous close only; current open is observable.
        signal_date = self.data.datetime.date(-1).isoformat()
        if target == 0 and self.position:
            self.pending = self.close()
        elif target == 1 and not self.position:
            price = float(self.data.open[0]) * (1 + self.cfg["slippage"])
            budget = min(self.broker.getvalue() * self.cfg["max_position"], self.broker.getcash())
            lot = self.cfg["lot_size"]
            size = int(max(0, budget - self.cfg["min_commission"]) /
                       (price * (1 + self.cfg["commission"])) / lot) * lot
            if size:
                self.pending = self.buy(size=size)
                self.pending.addinfo(entry_budget=budget)
            else:
                self.risk_events.append({"date": self.data.datetime.date(0).isoformat(),
                    "reason": "entry_budget_below_one_lot", "budget": budget})
        if self.pending:
            self.pending.addinfo(signal_date=signal_date)

    def notify_order(self, order):
        if order.status in (order.Submitted, order.Accepted):
            return
        if order.status == order.Completed:
            row = {"date": bt.num2date(order.executed.dt).date().isoformat(),
                   "signal_date": order.info.get("signal_date"),
                   "side": "buy" if order.isbuy() else "sell", "size": int(order.executed.size),
                   "price": float(order.executed.price), "commission": float(order.executed.comm),
                   "cash_after": float(self.broker.getcash()),
                   "entry_budget": order.info.get("entry_budget")}
            self.trades.append(row)
        else:
            self.risk_events.append({"date": self.data.datetime.date(0).isoformat(),
                                     "reason": order.getstatusname()})
        self.pending = None

    def next(self):
        if self.cfg.get("evaluation_start") and self.data.datetime.date(0).isoformat() < self.cfg["evaluation_start"]:
            return
        self.equity.append({"Date": self.data.datetime.date(0).isoformat(),
            "equity": float(self.broker.getvalue()), "cash": float(self.broker.getcash()),
            "shares": int(self.position.size), "close": float(self.data.close[0])})


def calculate_metrics(equity, initial_cash, annual_periods=252, risk_free_rate=0.0):
    values = pd.Series(equity, dtype=float)
    returns = values.pct_change().dropna()
    total = float(values.iloc[-1] / initial_cash - 1)
    periods = len(returns)
    annual = float((values.iloc[-1] / initial_cash) ** (annual_periods / periods) - 1) if periods else 0.0
    sigma = float(returns.std(ddof=1)) if len(returns) > 1 else 0.0
    rf_daily = (1 + risk_free_rate) ** (1 / annual_periods) - 1
    sharpe = float((returns.mean() - rf_daily) / sigma * np.sqrt(annual_periods)) if sigma > 1e-12 else None
    with_initial = pd.Series([initial_cash] + values.tolist())
    drawdown = float((1 - with_initial / with_initial.cummax()).max())
    return {"initial_cash": float(initial_cash), "final_equity": float(values.iloc[-1]),
            "total_return": total, "annual_return": annual, "sharpe": sharpe,
            "max_drawdown": drawdown, "observations": len(values), "return_periods": periods}


def run_signal_backtest(data, signals, config):
    """Daily adjusted-price experiment; yesterday's target, today's open fill."""
    cfg = {"initial_cash": 100000.0, "max_position": 0.8, "lot_size": 100,
           "commission": 0.0003, "min_commission": 5.0, "slippage": 0.0005,
           "annual_periods": 252, "risk_free_rate": 0.0, **config}
    if not 0 < cfg["max_position"] <= 1 or cfg["lot_size"] < 1 or cfg["initial_cash"] <= 0:
        raise ValueError("Invalid position, lot size or initial cash")
    if any(cfg[k] < 0 for k in ("commission", "min_commission", "slippage")):
        raise ValueError("Costs must be nonnegative")
    if len(data) < 2 or not data.index.is_monotonic_increasing or data.index.has_duplicates:
        raise ValueError("Need at least two sorted, unique observations")
    if cfg.get("evaluation_start") and len(data.loc[cfg["evaluation_start"]:]) < 2:
        raise ValueError("Need at least two evaluation observations after warm-up")
    if not isinstance(signals, pd.Series) or not signals.index.equals(data.index):
        raise ValueError("Signals must be a Series aligned with the input dates")
    if signals.isna().any() or not signals.isin([0, 1]).all():
        raise ValueError("Only binary long/flat targets are allowed")
    frame = data.copy()
    frame["Target"] = signals.astype(int)
    cerebro = bt.Cerebro(cheat_on_open=True, stdstats=False)
    cerebro.adddata(_SignalFeed(dataname=frame))
    cerebro.addstrategy(_SignalStrategy, config=cfg)
    cerebro.broker.setcash(cfg["initial_cash"])
    cerebro.broker.addcommissioninfo(_Commission(commission=cfg["commission"], minimum=cfg["min_commission"]))
    # Fixed transaction-price penalty; no dependence on future daily high/low.
    cerebro.broker.set_slippage_perc(cfg["slippage"], slip_open=True, slip_match=True, slip_out=True)
    strategy = cerebro.run()[0]
    metrics = calculate_metrics([row["equity"] for row in strategy.equity], cfg["initial_cash"],
                                cfg["annual_periods"], cfg["risk_free_rate"])
    metrics.update(executed_orders=len(strategy.trades),
                   total_commission=float(sum(t["commission"] for t in strategy.trades)),
                   min_cash=float(min(row["cash"] for row in strategy.equity)))
    return {"metrics": metrics, "equity": strategy.equity, "trades": strategy.trades,
            "risk_events": strategy.risk_events}


def audit_backtest(result, config, data):
    """Reconstruct cash, shares and metrics from the recorded executions."""
    cash, shares = config["initial_cash"], 0
    by_date = {}
    for trade in result["trades"]:
        by_date.setdefault(trade["date"], []).append(trade)
    errors = []
    for row in result["equity"]:
        for trade in by_date.get(row["Date"], []):
            cash -= trade["size"] * trade["price"] + trade["commission"]
            shares += trade["size"]
            if trade["signal_date"] >= trade["date"]:
                errors.append("noncausal_fill")
            if abs(trade["size"]) % config["lot_size"]:
                errors.append("non_lot_order")
            if trade["side"] == "buy" and trade["size"] * trade["price"] + trade["commission"] > trade["entry_budget"] + 1e-6:
                errors.append("entry_budget_exceeded")
        expected = cash + shares * float(data.loc[row["Date"], "Close"])
        if abs(expected - row["equity"]) > 1e-6 or abs(cash - row["cash"]) > 1e-6 or shares != row["shares"]:
            errors.append("ledger_mismatch")
        if cash < -1e-6 or shares < 0:
            errors.append("cash_or_short_violation")
    recomputed = calculate_metrics([row["equity"] for row in result["equity"]], config["initial_cash"], config["annual_periods"], config["risk_free_rate"])
    for key, value in recomputed.items():
        if value != result["metrics"][key]:
            errors.append("metric_mismatch:" + key)
    return {"ok": not errors, "errors": sorted(set(errors)), "ledger_rows": len(result["equity"])}


def save_backtest(result, folder):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False,
                                                  allow_nan=False) + "\n", encoding="utf-8")
    pd.DataFrame(result["equity"]).to_csv(folder / "equity.csv", index=False)
    pd.DataFrame(result["trades"]).to_csv(folder / "trades.csv", index=False)
    return result["metrics"]


if __name__ == "__main__":
    # Example usage:
    start_date = "2011-01-01"
    end_date = "2012-12-31"
    ticker = "MSFT"
    # BackTraderUtils.back_test(
    #     ticker, start_date, end_date, "SMA_CrossOver", {"fast": 10, "slow": 30}
    # )
    BackTraderUtils.back_test(
        ticker,
        start_date,
        end_date,
        "test_module:TestStrategy",
        {"exitbars": 5},
    )
