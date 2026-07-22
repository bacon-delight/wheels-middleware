"""Billing setup: turn approved terms into a machine-readable billing config (no manual keying)."""

from .config_builder import build_billing_config

__all__ = ["build_billing_config"]
