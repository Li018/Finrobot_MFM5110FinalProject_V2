def generate_signals(data, fast, slow):
    close = data['Close']
    fast_ma = close.rolling(fast).mean()
    slow_ma = close.rolling(slow).mean()
    return (fast_ma > slow_ma).astype(int)


def run_strategy(data, config):
    signals = generate_signals(data, config['fast'], config['slow'])
    return run_signal_backtest(data, signals, config)
