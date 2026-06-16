"""Quick OCR comparison test — RapidOCR vs Windows OCR.

Usage:
    python test_ocr.py                                     # test debug screenshot
    python test_ocr.py path/to/screenshot.png              # test specific image
    python test_ocr.py --compare                           # side-by-side comparison
"""

import sys
import time
from PIL import Image


def test_rapidocr(img: Image.Image) -> tuple[str, float]:
    from ocr_engine import _ocr_rapidocr, ensure_readable

    t0 = time.time()
    text = _ocr_rapidocr(img)
    elapsed = time.time() - t0
    return text, elapsed


def test_windows_ocr(img: Image.Image) -> tuple[str, float]:
    from ocr_engine import _ocr_windows

    t0 = time.time()
    text = _ocr_windows(img)
    elapsed = time.time() - t0
    return text, elapsed


def main():
    compare = "--compare" in sys.argv

    # Find image path
    img_path = None
    for arg in sys.argv[1:]:
        if not arg.startswith("--"):
            img_path = arg
            break
    if img_path is None:
        img_path = "C:/Users/Trouvailler/wechat-bot/debug_screenshot.png"

    try:
        img = Image.open(img_path)
    except FileNotFoundError:
        print(f"Image not found: {img_path}")
        print("Run the bot first to generate debug_screenshot.png, or pass a path.")
        sys.exit(1)

    print(f"Testing: {img_path} ({img.width}x{img.height})")
    print()

    if compare:
        print("=" * 50)
        print("  Windows OCR (current):")
        print("=" * 50)
        text, elapsed = test_windows_ocr(img)
        print(text if text else "(no text detected)")
        print(f"  -> {elapsed:.3f}s")
        print()

    print("=" * 50)
    print("  RapidOCR (PP-OCR Chinese model):")
    print("=" * 50)
    text, elapsed = test_rapidocr(img)
    print(text if text else "(no text detected)")
    print(f"  -> {elapsed:.3f}s")
    print()

    from ocr_engine import active_backend
    print(f"Active backend: {active_backend()}")


if __name__ == "__main__":
    main()
