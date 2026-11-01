"""Execution backends. The daemon only talks to the Backend protocol; Docker,
Proxmox and CAPE implementations slot in behind it."""
from .base import Backend, ExecResult, GuestHandle, Screenshot
from .fake import FakeBackend

__all__ = ["Backend", "ExecResult", "GuestHandle", "Screenshot", "FakeBackend"]
