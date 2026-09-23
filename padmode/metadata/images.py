"""Telling an image from something that only has an image's name.

Shared by the generators. Heroic downloads art and Steam copies it, and both
can end up holding bytes that are not a picture: a CDN answers a failed
request with an HTML error page and a 200 status, and a file copied while
Steam is still writing it is whatever Steam had written so far. Saved as
``.jpg``, either is a broken cover in the frontend **forever**, because the
next run sees a file of non-zero size and leaves it alone.
"""

from __future__ import annotations

#: Bytes that start a real image, and where they sit.
SIGNATURES = (
    (0, b"\xff\xd8\xff"),  # JPEG
    (0, b"\x89PNG\r\n\x1a\n"),  # PNG
    (0, b"GIF8"),  # GIF
    (8, b"WEBP"),  # WebP, after a RIFF header
)


def looks_like_image(data: bytes) -> bool:
    """True if these bytes start like an image we can trust."""
    return any(
        data[offset:offset + len(sig)] == sig for offset, sig in SIGNATURES
    )
