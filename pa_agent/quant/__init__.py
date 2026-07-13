"""Paper-only automated quant validation built around VerdictQuant signals."""

from pa_agent.quant.models import PaperConfig, SignalIntent
from pa_agent.quant.service import QuantPaperService

__all__ = ["PaperConfig", "QuantPaperService", "SignalIntent"]
