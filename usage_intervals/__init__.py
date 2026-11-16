"""Offline device usage interval processor.

Public entry points:
  * load_package / load_package_files -- structural input checks
  * Processor                          -- build, per-device/window reads,
                                          interval interpretation
  * reference.build_reference          -- pure-Python semantic oracle
  * reference.verify_discrete          -- discrete small-domain cross-check
"""

from .engine import Processor
from .model import (
    StructuralError,
    load_package,
    load_package_files,
)
from .reference import (
    CONFIRMED,
    CONFLICT,
    IDLE,
    UNKNOWN,
    build_reference,
    verify_discrete,
)

__all__ = [
    "Processor",
    "StructuralError",
    "load_package",
    "load_package_files",
    "build_reference",
    "verify_discrete",
    "CONFLICT",
    "CONFIRMED",
    "IDLE",
    "UNKNOWN",
]
