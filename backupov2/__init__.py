"""backupov2 - Assistente de Backup de Discos.

A Windows helper that copies a batch of optical discs into an ordered set of
folders, one disc per folder, with skip/redirect/resume support.

Layering rule: nothing in this package outside ``ui`` may import ``tkinter``.
That is what keeps the state machine testable without a GUI or hardware.
"""

from __future__ import annotations

__version__ = "2.0.0"
__all__ = ["__version__"]
