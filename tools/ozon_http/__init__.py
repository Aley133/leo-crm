from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .adapter import OzonSessionHttpAdapter
    from .resolver import OzonSessionResolver


def __getattr__(name: str):
    # Keep the Fast Dumping executable standalone: importing parser/search
    # modules must not import backend/SQLAlchemy through the monitoring adapter.
    if name == "OzonSessionHttpAdapter":
        from .adapter import OzonSessionHttpAdapter

        return OzonSessionHttpAdapter
    if name == "OzonSessionResolver":
        from .resolver import OzonSessionResolver

        return OzonSessionResolver
    raise AttributeError(name)

__all__ = ["OzonSessionHttpAdapter", "OzonSessionResolver"]
