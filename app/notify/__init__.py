"""Transactional email: branded templates for every lifecycle transition + SES sender."""

from .templates import TEMPLATES, RenderedEmail, render

__all__ = ["TEMPLATES", "RenderedEmail", "render"]
