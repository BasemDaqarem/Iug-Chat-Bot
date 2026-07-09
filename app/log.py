"""
Central logging for the app package.

get_logger(name) returns a namespaced logger ("iug.<name>") whose root gets a
console handler on first use — so cli.py / server.py need zero setup, while
level/format stay controlled in one place (LOG_LEVEL in .env).

cli.py intentionally keeps print(): its output IS the console UI, not logs.
"""

import logging
import sys

from app import config

_FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"
_DATEFMT = "%H:%M:%S"


def _root() -> logging.Logger:
    root = logging.getLogger("iug")
    if not root.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(_FORMAT, _DATEFMT))
        root.addHandler(handler)
        root.setLevel(getattr(logging, config.LOG_LEVEL.upper(), logging.INFO))
        root.propagate = False
    return root


def get_logger(name: str) -> logging.Logger:
    _root()
    return logging.getLogger(f"iug.{name}")
