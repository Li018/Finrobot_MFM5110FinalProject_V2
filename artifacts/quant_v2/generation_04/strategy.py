def generate_signals(data, fast, slow):
    close = data['Close']
    sma_fast = close.rolling(window=fast).mean()
    sma_slow = close.rolling(window=slow).mean()
    signals = (sma_fast > sma_slow).astype(int)
    signals = signals.fillna(0).astype(int)
    return signals.reindex(data.index).fillna(0).astype(int)


def run_strategy(data, config):
    signals = generate_signals(data, config['fast'], config['slow'])
    return run_signal_backtest(data, signals, config)
