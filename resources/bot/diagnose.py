"""WeChat bot diagnostic tool — captures window, analyzes layout, saves debug info.

Usage:  python diagnose.py

Saves:
  diagnose_full.png           — full WeChat window screenshot
  diagnose_chatlist.png       — chat list panel
  diagnose_conversation.png   — conversation panel
  diagnose_annotated.png      — full window with divider lines drawn
"""

import sys
import io
import os
import re
from datetime import datetime

import numpy as np
import win32gui
import win32con
from PIL import ImageGrab, Image, ImageDraw, ImageFont

from winrt.windows.media.ocr import OcrEngine
from winrt.windows.graphics.imaging import (
    BitmapDecoder, SoftwareBitmap, BitmapPixelFormat,
)
from winrt.windows.storage.streams import InMemoryRandomAccessStream, DataWriter

WECHAT_TITLE = "微信"
WECHAT_CLASS = "Qt51514QWindowIcon"
OUT_DIR = os.path.dirname(os.path.abspath(__file__))


def find_wechat():
    matches = []
    def cb(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            t = win32gui.GetWindowText(hwnd)
            c = win32gui.GetClassName(hwnd)
            if t == WECHAT_TITLE or c == WECHAT_CLASS:
                matches.append((hwnd, t, c))
        return True
    win32gui.EnumWindows(cb, None)
    return matches


def ocr_image(img: Image.Image):
    """Basic OCR — returns text."""
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    data = buf.getvalue()
    stream = InMemoryRandomAccessStream()
    writer = DataWriter(stream.get_output_stream_at(0))
    writer.write_bytes(bytes(data))
    writer.store_async().get()
    writer.detach_stream()
    decoder = BitmapDecoder.create_async(stream).get()
    frame = decoder.get_software_bitmap_async().get()
    if frame.bitmap_pixel_format != BitmapPixelFormat.BGRA8:
        frame = SoftwareBitmap.convert(frame, BitmapPixelFormat.BGRA8)
    engine = OcrEngine.try_create_from_user_profile_languages()
    result = engine.recognize_async(frame).get()
    if not result:
        return ""
    lines = []
    for line in result.lines:
        text = line.text
        text = re.sub(
            r'(?<=[一-鿿㐀-䶿豈-﫿　-〿＀-￯]) '
            r'(?=[一-鿿㐀-䶿豈-﫿　-〿＀-￯])',
            '', text
        )
        if text.strip():
            lines.append(text.strip())
    return "\n".join(lines)


def ocr_with_positions(img: Image.Image):
    """OCR with word-level bounding boxes."""
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    data = buf.getvalue()
    stream = InMemoryRandomAccessStream()
    writer = DataWriter(stream.get_output_stream_at(0))
    writer.write_bytes(bytes(data))
    writer.store_async().get()
    writer.detach_stream()
    decoder = BitmapDecoder.create_async(stream).get()
    frame = decoder.get_software_bitmap_async().get()
    if frame.bitmap_pixel_format != BitmapPixelFormat.BGRA8:
        frame = SoftwareBitmap.convert(frame, BitmapPixelFormat.BGRA8)
    engine = OcrEngine.try_create_from_user_profile_languages()
    result = engine.recognize_async(frame).get()
    if not result:
        return []
    lines = []
    for ocr_line in result.lines:
        if not ocr_line.words:
            continue
        min_y = min(w.bounding_rect.y for w in ocr_line.words)
        max_y = max(w.bounding_rect.y + w.bounding_rect.height for w in ocr_line.words)
        min_x = min(w.bounding_rect.x for w in ocr_line.words)
        max_x = max(w.bounding_rect.x + w.bounding_rect.width for w in ocr_line.words)
        text = ocr_line.text
        text = re.sub(
            r'(?<=[一-鿿㐀-䶿豈-﫿　-〿＀-￯]) '
            r'(?=[一-鿿㐀-䶿豈-﫿　-〿＀-￯])',
            '', text
        )
        if text.strip():
            lines.append({
                "text": text.strip(),
                "y": min_y, "height": max_y - min_y,
                "x": min_x, "width": max_x - min_x,
            })
    lines.sort(key=lambda L: L["y"])
    return lines


def analyze_column(img, x_pct):
    """Analyze a vertical column in the image."""
    w, h = img.size
    x = int(w * x_pct / 100)
    samples = []
    for y in range(int(h * 0.08), int(h * 0.92), 3):
        try:
            p = img.getpixel((x, y))
            if isinstance(p, tuple):
                samples.append(sum(p) / len(p))
            else:
                samples.append(p)
        except IndexError:
            break
    if len(samples) < 20:
        return None
    mean = sum(samples) / len(samples)
    variance = sum((s - mean) ** 2 for s in samples) / len(samples)
    dark = sum(1 for s in samples if s < 150)
    light = sum(1 for s in samples if s > 220)
    return {
        "mean": mean, "variance": variance,
        "dark": dark, "light": light,
        "total": len(samples),
    }


def detect_red_dots(img, divider_x):
    """Test red dot detection on the full image."""
    w, h = img.size
    rd_left = divider_x - 0.12
    rd_right = divider_x
    region = (
        int(w * rd_left), int(h * 0.06),
        int(w * rd_right), int(h * 0.95),
    )
    # valid region?
    if region[0] >= region[2] or region[1] >= region[3]:
        return [], None

    crop = img.crop(region)
    arr = np.array(crop.convert("RGB"))
    if arr.shape[0] == 0 or arr.shape[1] == 0:
        return [], None

    r = arr[:, :, 0].astype(np.int16)
    g = arr[:, :, 1].astype(np.int16)
    b = arr[:, :, 2].astype(np.int16)
    red_mask = (
        ((r > 200) & (g < 100) & (b < 100))
        | ((r > 170) & (g < 70) & (b < 70))
    )
    row_counts = np.sum(red_mask, axis=1)
    hits = np.where(row_counts >= 8)[0]
    if len(hits) == 0:
        return [], crop

    clusters = []
    cs = hits[0]
    prev = hits[0]
    for idx in hits[1:]:
        if idx - prev > 3:
            clusters.append((cs, prev))
            cs = idx
        prev = idx
    clusters.append((cs, prev))

    results = []
    for y1, y2 in clusters:
        if 6 <= y2 - y1 <= 40:
            results.append({
                "y_rel": int((y1 + y2) / 2),
                "size": y2 - y1,
            })
    return results, crop


def main():
    print("=" * 60)
    print("  赵有才微信机器人 — 诊断工具")
    print("=" * 60)

    # 1. Find WeChat window
    matches = find_wechat()
    if not matches:
        print("❌ 未找到微信窗口！请确保微信已启动并可见。")
        sys.exit(1)

    hwnd, title, cls = matches[0]
    rect = win32gui.GetWindowRect(hwnd)
    w = rect[2] - rect[0]
    h = rect[3] - rect[1]
    print(f"\n✅ 找到微信窗口: HWND={hwnd}")
    print(f"   标题: '{title}'  类名: '{cls}'")
    print(f"   位置: left={rect[0]} top={rect[1]} right={rect[2]} bottom={rect[3]}")
    print(f"   尺寸: {w} x {h}")

    # 2. Capture full window
    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    import time
    time.sleep(0.3)
    img = ImageGrab.grab(bbox=rect)
    img.save(os.path.join(OUT_DIR, "diagnose_full.png"))
    print(f"\n✅ 已保存完整截图: diagnose_full.png ({img.size[0]}x{img.size[1]})")

    # 3. Column analysis
    print(f"\n{'='*60}")
    print("  列分析 (寻找分隔线)")
    print(f"{'='*60}")
    print(f"  {'位置':<8} {'平均亮度':<10} {'方差':<10} {'暗像素':<10} {'亮像素'}")
    print(f"  {'-'*50}")

    for pct in range(32, 66, 2):
        info = analyze_column(img, pct)
        if info:
            marker = ""
            # A good divider: high brightness, low variance, few dark pixels,
            # lots of light pixels
            if info["mean"] > 220 and info["variance"] < 100 and info["dark"] < info["total"] * 0.05:
                marker = " ← 可能是分隔线"
            print(f"  {pct:>3}%     {info['mean']:>6.1f}      {info['variance']:>6.1f}     "
                  f"{info['dark']:>3}/{info['total']:<4}    {info['light']:>3}/{info['total']}{marker}")

    # 4. Try OCR on chat list (left of various divider guesses)
    for div_guess in [0.42, 0.45, 0.48, 0.50, 0.52, 0.55]:
        chat_left = 0.02
        chat_right = div_guess
        chat_top = 0.06
        chat_bottom = 0.95
        region = (
            rect[0] + int(w * chat_left),
            rect[1] + int(h * chat_top),
            rect[0] + int(w * chat_right),
            rect[1] + int(h * chat_bottom),
        )
        crop = ImageGrab.grab(bbox=region)
        text = ocr_image(crop)
        crop.save(os.path.join(OUT_DIR, f"diagnose_chatlist_{int(div_guess*100)}.png"))
        has_content = len(text.strip()) > 10 if text else False
        print(f"\n  分隔线={div_guess:.0%}: 聊天列表OCR ({crop.size[0]}x{crop.size[1]}) "
              f"{'✅ 有内容' if has_content else '⚠ 无内容/太少'}")
        if text.strip():
            for line in text.strip().split("\n")[:10]:
                print(f"    │ {line[:80]}")

    # 5. Test red dot detection at various dividers
    print(f"\n{'='*60}")
    print("  红点检测测试")
    print(f"{'='*60}")
    for div in [0.42, 0.45, 0.48, 0.50, 0.52, 0.55]:
        dots, crop = detect_red_dots(img, div)
        if crop:
            crop.save(os.path.join(OUT_DIR, f"diagnose_redzone_{int(div*100)}.png"))
        print(f"  分隔线={div:.0%}: 检测到 {len(dots)} 个红点")

    # 6. Annotated image
    annotated = img.copy()
    draw = ImageDraw.Draw(annotated)
    for div_pct in [0.40, 0.45, 0.50, 0.55, 0.60]:
        x = int(w * div_pct)
        color = "red" if div_pct == 0.50 else "yellow"
        draw.line([(x, 0), (x, h)], fill=color, width=2)
        draw.text((x + 5, 10), f"{div_pct:.0%}", fill=color)
    annotated.save(os.path.join(OUT_DIR, "diagnose_annotated.png"))
    print(f"\n✅ 已保存标注截图: diagnose_annotated.png")
    print(f"   黄线 = 40%/45%/55%/60%  红线 = 50%")

    print(f"\n{'='*60}")
    print(f"  请在 VSCode 中打开以下图片查看:")
    print(f"  - diagnose_full.png       (完整窗口)")
    print(f"  - diagnose_annotated.png  (标注分隔线位置)")
    print(f"  - diagnose_chatlist_*.png (不同分隔线的聊天列表)")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
