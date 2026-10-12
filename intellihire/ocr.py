"""Optional OCR for newspaper-ad images. Off by default (OCR_ENABLED=1 to turn on).

MVP engine: Tesseract (light, runs anywhere). Swap in PaddleOCR later behind the same
function signature once you have a labelled sample to compare engines on (CER/WER).
"""
from __future__ import annotations

import io
import os


def ocr_enabled() -> bool:
    return os.environ.get("OCR_ENABLED", "0") == "1"


def run_ocr(image_bytes: bytes) -> tuple[str, str]:
    """Return (text, engine_name). Raises if tesseract is unavailable."""
    import pytesseract
    from PIL import Image

    img = Image.open(io.BytesIO(image_bytes)).convert("L")
    langs = os.environ.get("OCR_LANGS", "eng+urd")
    text = pytesseract.image_to_string(img, lang=langs)
    return text.strip(), f"tesseract[{langs}]"
