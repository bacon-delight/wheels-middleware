"""Async pipeline Lambda handlers (parse -> extract -> notify).

Each module exposes `handler(event, context)`. The container image runs the same code with a
different CMD per function (see the infra lambda module's image_config.command).
"""
