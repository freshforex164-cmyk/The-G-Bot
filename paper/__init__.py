"""Signal outcome tracking and storage."""

from paper.store import CsvSignalStore, InMemorySignalStore, SignalStore
from paper.tracker import PaperTracker

__all__ = ["CsvSignalStore", "InMemorySignalStore", "PaperStore", "PaperTracker"]

# Expose the interface under a concise package-level name.
PaperStore = SignalStore
