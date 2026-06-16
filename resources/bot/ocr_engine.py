"""OCR engine abstraction layer for the WeChat bot.

Supports multiple backends:
  - rapidocr   : RapidOCR with ONNX Runtime (PP-OCR Chinese models, ~97% accuracy)
  - windows    : Windows built-in OCR via winrt (fallback)

RapidOCR is the default — it uses the same PP-OCR models as PaddleOCR
but runs via ONNX Runtime with no PaddlePaddle dependency (~50MB models).
"""

import logging
import re
import io
from typing import Optional
from PIL import Image, ImageFilter
import numpy as np

logger = logging.getLogger("wechat-bot-ocr")

# ---------------------------------------------------------------------------
# RapidOCR backend
# ---------------------------------------------------------------------------

_rapidocr: Optional[object] = None  # RapidOCR singleton
_rapidocr_available: bool | None = None  # None = not checked yet


def _get_rapidocr():
    """Return a cached RapidOCR instance, or None if unavailable."""
    global _rapidocr, _rapidocr_available
    if _rapidocr_available is False:
        return None
    if _rapidocr is not None:
        return _rapidocr
    try:
        from rapidocr_onnxruntime import RapidOCR  # type: ignore
        _rapidocr = RapidOCR(
            box_thresh=0.5,
            unclip_ratio=1.6,
            text_score=0.4,  # lower threshold — OCR corruption still yields CJK chars
        )
        _rapidocr_available = True
        logger.info("RapidOCR engine initialized (ONNX Runtime, PP-OCR Chinese model)")
        return _rapidocr
    except ImportError:
        _rapidocr_available = False
        logger.warning("rapidocr_onnxruntime not installed, falling back to Windows OCR")
        return None
    except Exception as exc:
        _rapidocr_available = False
        logger.warning(f"RapidOCR initialization failed: {exc}, falling back to Windows OCR")
        return None


# ---------------------------------------------------------------------------
# Public API — matches the interface bot_image.py expects
# ---------------------------------------------------------------------------

# Re-exported for bot_image.py compatibility
OCR_MIN_DIMENSION = 700
OCR_SCALE_FACTOR = 4


def ensure_readable(img: Image.Image) -> tuple[Image.Image, float]:
    """Preprocess image for reliable CJK OCR.

    Same pipeline as before: upscale → grayscale → contrast stretch → sharpen.
    RapidOCR handles colour images natively, but the preprocessing helps with
    WeChat's small font sizes (~14-16px) and low-contrast bubble backgrounds.
    """
    scale = 1.0
    if img.width < OCR_MIN_DIMENSION or img.height < OCR_MIN_DIMENSION:
        scale = OCR_SCALE_FACTOR
        img = img.resize(
            (int(img.width * scale), int(img.height * scale)),
            Image.LANCZOS,
        )

    # Convert to RGB (RapidOCR works with colour; grayscale for legacy pipeline)
    if img.mode not in ('RGB', 'L'):
        img = img.convert('RGB')

    # Contrast stretch on the luminance channel to crush backgrounds
    arr = np.array(img.convert('L'), dtype=np.int16)
    lo, hi = np.percentile(arr, (2, 98))
    if hi - lo > 20:
        arr = np.clip((arr - lo) * 255.0 / (hi - lo), 0, 255).astype(np.uint8)
        img = Image.fromarray(arr, mode='L').convert('RGB')
    else:
        img = Image.fromarray(arr.astype(np.uint8), mode='L').convert('RGB')

    # Mild sharpen to crispen character edges after upscale
    img = img.filter(ImageFilter.UnsharpMask(radius=1.5, percent=80, threshold=2))

    return img, scale


def _collapse_cjk_spaces(text: str) -> str:
    """Remove spaces inserted between adjacent CJK characters."""
    return re.sub(
        r'(?<=[一-鿿㐀-䶿豈-﫿'
        r'　-〿＀-￯]) '
        r'(?=[一-鿿㐀-䶿豈-﫿'
        r'　-〿＀-￯])',
        '', text
    )


def ocr_image(img: Image.Image) -> str:
    """Run OCR on a PIL image and return plain text.

    Tries RapidOCR first; falls back to Windows OCR if unavailable.
    """
    engine = _get_rapidocr()
    if engine is not None:
        text = _ocr_rapidocr(img, engine)
        if text:
            return text
        # RapidOCR returned nothing — fall through to Windows OCR
        logger.debug("RapidOCR returned empty, trying Windows OCR fallback")

    return _ocr_windows(img)


def ocr_image_detailed(img: Image.Image) -> list[dict]:
    """Run OCR on a PIL image and return per-line {text, y, height, x}.

    Tries RapidOCR first; falls back to Windows OCR if unavailable.
    """
    engine = _get_rapidocr()
    if engine is not None:
        lines = _ocr_rapidocr_detailed(img, engine)
        if lines:
            return lines
        logger.debug("RapidOCR detailed returned empty, trying Windows OCR fallback")

    return _ocr_windows_detailed(img)


# ---------------------------------------------------------------------------
# RapidOCR internal
# ---------------------------------------------------------------------------

def _ocr_rapidocr(img: Image.Image, engine=None) -> str:
    """RapidOCR → plain text."""
    if engine is None:
        engine = _get_rapidocr()
    if engine is None:
        return ""

    img, scale = ensure_readable(img)
    arr = np.array(img)

    try:
        result, _elapse = engine(arr)
    except Exception:
        logger.debug("RapidOCR call failed", exc_info=True)
        return ""

    if not result:
        return ""

    lines = []
    for dt_box, rec_text, score in result:
        rec_text = rec_text.strip()
        if not rec_text:
            continue
        rec_text = _collapse_cjk_spaces(rec_text)
        lines.append(rec_text)

    return "\n".join(lines)


def _ocr_rapidocr_detailed(img: Image.Image, engine=None) -> list[dict]:
    """RapidOCR → list of {text, y, height, x} in original image space."""
    if engine is None:
        engine = _get_rapidocr()
    if engine is None:
        return []

    img, scale = ensure_readable(img)
    arr = np.array(img)

    try:
        result, _elapse = engine(arr)
    except Exception:
        logger.debug("RapidOCR detailed call failed", exc_info=True)
        return []

    if not result:
        return []

    lines = []
    for dt_box, rec_text, score in result:
        rec_text = rec_text.strip()
        if not rec_text:
            continue
        rec_text = _collapse_cjk_spaces(rec_text)

        # dt_box is [[x1,y1], [x2,y2], [x3,y3], [x4,y4]] clockwise from top-left
        min_x = min(p[0] for p in dt_box) / scale
        min_y = min(p[1] for p in dt_box) / scale
        max_y = max(p[1] for p in dt_box) / scale

        lines.append({
            "text": rec_text,
            "y": min_y,
            "height": max_y - min_y,
            "x": min_x,
            "score": score,
        })

    lines.sort(key=lambda L: L["y"])
    return lines


# ---------------------------------------------------------------------------
# Windows OCR fallback (kept for when RapidOCR can't be installed)
# ---------------------------------------------------------------------------

def _ocr_windows(img: Image.Image) -> str:
    """Windows built-in OCR → plain text."""
    lines = _ocr_windows_detailed(img)
    return "\n".join(L["text"] for L in lines)


def _ocr_windows_detailed(img: Image.Image) -> list[dict]:
    """Windows built-in OCR → list of {text, y, height, x}."""
    try:
        from winrt.windows.media.ocr import OcrEngine
        from winrt.windows.graphics.imaging import (
            BitmapDecoder,
            SoftwareBitmap,
            BitmapPixelFormat,
        )
        from winrt.windows.storage.streams import (
            InMemoryRandomAccessStream,
            DataWriter,
        )
    except ImportError:
        logger.warning("winrt not available — Windows OCR unavailable")
        return []

    try:
        engine = OcrEngine.try_create_from_user_profile_languages()
        if not engine:
            engine = OcrEngine.try_create_from_language("zh-Hans".split(","))
    except Exception:
        logger.debug("Windows OCR engine not available", exc_info=True)
        return []

    img, scale = ensure_readable(img)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    data = buf.getvalue()

    stream = InMemoryRandomAccessStream()
    writer = DataWriter(stream.get_output_stream_at(0))
    writer.write_bytes(bytes(data))
    writer.store_async().get()
    writer.detach_stream()

    try:
        decoder = BitmapDecoder.create_async(stream).get()
        frame = decoder.get_software_bitmap_async().get()
        if frame.bitmap_pixel_format != BitmapPixelFormat.BGRA8:
            frame = SoftwareBitmap.convert(frame, BitmapPixelFormat.BGRA8)
        result = engine.recognize_async(frame).get()
    except Exception:
        logger.debug("Windows OCR call failed", exc_info=True)
        return []

    if not result:
        return []

    lines = []
    for ocr_line in result.lines:
        if not ocr_line.words:
            continue
        min_y = min(w.bounding_rect.y for w in ocr_line.words)
        max_y = max(w.bounding_rect.y + w.bounding_rect.height for w in ocr_line.words)
        min_x = min(w.bounding_rect.x for w in ocr_line.words)

        text = _collapse_cjk_spaces(ocr_line.text)
        if text.strip():
            lines.append({
                "text": text.strip(),
                "y": min_y / scale,
                "height": (max_y - min_y) / scale,
                "x": min_x / scale,
            })

    lines.sort(key=lambda L: L["y"])
    return lines


# ---------------------------------------------------------------------------
# Utility: check which backend is active
# ---------------------------------------------------------------------------

def active_backend() -> str:
    """Return the name of the currently active OCR backend."""
    engine = _get_rapidocr()
    if engine is not None:
        return "rapidocr"
    return "windows"
