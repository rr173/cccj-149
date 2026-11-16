"""High-level offline usage-interval processing facade."""

from __future__ import annotations

from .db import UsageIntervalStore
from .model import (
    StructuralError,
    load_package,
    load_package_files,
)

__all__ = [
    "Processor",
    "StructuralError",
    "load_package",
    "load_package_files",
]


class Processor:
    """Thin facade combining input checks, build and the read/interpret APIs."""

    def __init__(self, dsn=None, conn=None):
        self.store = UsageIntervalStore(dsn=dsn, conn=conn)

    def install(self):
        self.store.install_schema()

    def close(self):
        self.store.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- input check / build ------------------------------------------------
    def validate(self, package_id, window, batches):
        """Structural input check; raises StructuralError, returns the model."""
        return load_package(package_id, window, batches)

    def validate_files(self, package_id, window, paths):
        return load_package_files(package_id, window, paths)

    def process(self, pkg):
        """Publish one fixed bundle; returns package status metadata."""
        self.store.build(pkg)
        return self.store.package_status(pkg.package_id)

    # -- reads --------------------------------------------------------------
    def devices(self, package_id):
        return self.store.list_devices(package_id)

    def segments(self, package_id, device, window_lo=None, window_hi=None):
        return self.store.read_segments(package_id, device, window_lo, window_hi)

    def evidence(self, package_id, device):
        return self.store.read_evidence(package_id, device)

    def interpret(self, package_id, device, window_lo=None, window_hi=None):
        return self.store.interpret(package_id, device, window_lo, window_hi)

    def status(self, package_id):
        return self.store.package_status(package_id)
