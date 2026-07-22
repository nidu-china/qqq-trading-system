"""Centralized logging configuration with file and console output."""
from __future__ import annotations

import logging
import sys
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path


def setup_logging(log_dir: Path, level: str = "INFO", name: str = "qqq_trader") -> logging.Logger:
    """Configure root logger with console and daily rotating file handler.

    Log files are stored as ``<log_dir>/qqq_trader_YYYY-MM-DD.log`` and rotated
    daily at midnight, keeping the last 30 days.
    """
    log_dir.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(formatter)
    logger.addHandler(console)

    today = datetime.now().strftime("%Y-%m-%d")
    log_file = log_dir / f"qqq_trader_{today}.log"
    file_handler = TimedRotatingFileHandler(
        log_file,
        when="midnight",
        interval=1,
        backupCount=30,
        encoding="utf-8",
    )
    file_handler.setLevel(getattr(logging, level.upper(), logging.INFO))
    file_handler.setFormatter(formatter)
    file_handler.namer = lambda name: name.replace(".log.", "_") + ".log" if ".log." in name else name
    logger.addHandler(file_handler)

    logging.getLogger("uvicorn").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    return logger
