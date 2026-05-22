"""Abstract typing backend."""
from __future__ import annotations

from abc import ABC, abstractmethod


class TypingBackend(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def type_text(self, text: str) -> None:
        """Type text into the currently focused application."""

    @abstractmethod
    def check(self) -> tuple[bool, str]:
        """Return (ok, message). If not ok, message explains how to fix it."""
