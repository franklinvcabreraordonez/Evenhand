"""Evenhand: a neutral evidence and adjudication layer for AI-agent transactions (prototype)."""
from .integration import Dispute, Gateway, quick_mandate, verify_bundle

__version__ = "0.2.0"
__all__ = ["Gateway", "Dispute", "quick_mandate", "verify_bundle", "__version__"]