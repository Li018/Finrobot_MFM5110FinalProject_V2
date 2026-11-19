"""Lazy functional exports.

Importing a submodule must not pull in the optional document/RAG stack
(``unstructured``, ``magic``, embedding models). Names are resolved on first
access, so ``from finrobot.functional import QuantTools`` still works while
``import finrobot.functional.factor_dsl`` stays light.
"""
from importlib import import_module

_EXPORTS = {
    "ReportAnalysisUtils": ".analyzer",
    "MplFinanceUtils": ".charting",
    "ReportChartUtils": ".charting",
    "CodingUtils": ".coding",
    "IPythonUtils": ".coding",
    "BackTraderUtils": ".quantitative",
    "QuantTools": ".quant_tools",
    "QuantDialogueTools": ".quant_dialogue",
    "ReportLabUtils": ".reportlab",
    "TextUtils": ".text",
    "get_rag_function": ".rag",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = import_module(_EXPORTS[name], __name__)
    value = getattr(module, name)
    globals()[name] = value
    return value
