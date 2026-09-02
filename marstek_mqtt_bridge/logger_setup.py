"""Colored console logging, dependency-free (no colorlog needed).

Colors:
    DEBUG    cyan
    INFO     green
    WARNING  yellow
    ERROR    red
    CRITICAL magenta (bold)
"""

import logging
import sys

_RESET = "\033[0m"
_COLORS = {
    logging.DEBUG: "\033[36m",     # cyan
    logging.INFO: "\033[32m",      # green
    logging.WARNING: "\033[33m",   # yellow
    logging.ERROR: "\033[31m",     # red
    logging.CRITICAL: "\033[1;35m",  # bold magenta
}


class ColorFormatter(logging.Formatter):
    def __init__(self, use_color: bool = True):
        super().__init__(
            fmt="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        self.use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)
        if not self.use_color:
            return message
        color = _COLORS.get(record.levelno, "")
        return f"{color}{message}{_RESET}"


def setup_logging(level: str = "info") -> logging.Logger:
    level_map = {
        "debug": logging.DEBUG,
        "info": logging.INFO,
        "warning": logging.WARNING,
        "error": logging.ERROR,
    }
    log_level = level_map.get(str(level).lower(), logging.INFO)

    root = logging.getLogger("marstek")
    root.setLevel(log_level)
    root.handlers.clear()

    handler = logging.StreamHandler(sys.stdout)
    # Alpine/Docker logs are usually viewed in a terminal that supports ANSI;
    # HA's add-on log viewer also renders ANSI colors fine.
    handler.setFormatter(ColorFormatter(use_color=True))
    root.addHandler(handler)

    return root
