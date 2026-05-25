"""Common types and extractor interface for handler symbol indexing."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(slots=True)
class FileSymbols:
    """Language-agnostic symbol extraction result for one source file."""

    path: str
    language: str
    imports: list[dict]
    exports: list[dict]
    symbols: list[dict]

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "language": self.language,
            "imports": self.imports,
            "exports": self.exports,
            "symbols": self.symbols,
        }


class HandlerSymbolExtractor(Protocol):
    """Adapter interface for language-specific symbol extraction."""

    language: str
    extensions: set[str]

    def extract_file(self, repo_root: Path, relative_path: str, text: str) -> FileSymbols:
        """Extract symbols/imports/exports from one source file."""

