"""
Best-effort text extraction for attachment binaries (mostly PDFs).

Deliberately best-effort: many Legistar attachments (staff reports) are
scanned images with no text layer, and OCR is out of scope for the hackathon
MVP (see README risk #1). This module extracts what it can and returns None
otherwise -- callers must handle None gracefully, not crash.
"""

from __future__ import annotations

import io

import httpx
from pypdf import PdfReader


async def fetch_and_extract_pdf_text(url: str, *, client: httpx.AsyncClient | None = None, max_pages: int = 40) -> str | None:
    """Fetch `url` and extract text if it's a PDF with a real text layer.

    Returns None on any failure (network error, not a PDF, no extractable
    text) rather than raising -- attachment extraction is a nice-to-have,
    not something that should abort ingestion of the parent matter.
    """
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=30)
    try:
        resp = await client.get(url)
        if resp.status_code != 200:
            return None
        content_type = resp.headers.get("content-type", "")
        if "pdf" not in content_type.lower() and not url.lower().endswith(".pdf"):
            return None

        reader = PdfReader(io.BytesIO(resp.content))
        chunks: list[str] = []
        for page in reader.pages[:max_pages]:
            text = page.extract_text() or ""
            if text.strip():
                chunks.append(text)

        full_text = "\n".join(chunks).strip()
        return full_text or None
    except Exception:
        # scanned PDFs, encrypted PDFs, network hiccups, malformed files --
        # all fall through to "couldn't extract, move on"
        return None
    finally:
        if owns_client:
            await client.aclose()
