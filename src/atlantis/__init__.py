"""Atlantis package."""

from importlib.metadata import PackageNotFoundError, version

from loguru import logger

try:
    __version__ = version("atlantis")
except PackageNotFoundError:  # running from a source tree without an install
    __version__ = "0.0.0"

logger.disable("atlantis")
