"""Phase 2 — Mireye data service.

Mountable router: ``app.include_router(phase2.router)``.
"""

from .router import router

__all__ = ["router"]
