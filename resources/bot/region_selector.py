"""Screen region selector — click-and-drag to define capture areas.

Usage:
    from region_selector import select_region, select_two_regions

    # Single region (backward-compatible)
    rect = select_region()
    if rect:
        left, top, right, bottom = rect

    # Two regions: chat list + conversation panel
    chat_rect, conv_rect = select_two_regions()
    if chat_rect and conv_rect:
        ...
"""

import ctypes
import sys
import tkinter as tk
from typing import Optional


def _set_dpi_aware():
    """Make the process DPI-aware so Tkinter coordinates match physical pixels."""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _select_single(hint_lines: list[str]) -> Optional[tuple[int, int, int, int]]:
    """Show a semi-transparent overlay with a hint and let the user draw a rectangle.

    Returns (left, top, right, bottom) in screen pixels, or None if cancelled.
    """
    _set_dpi_aware()

    root = tk.Tk()
    root.title("Select Region")

    sw = root.winfo_screenwidth()
    sh = root.winfo_screenheight()
    root.geometry(f"{sw}x{sh}+0+0")

    root.attributes("-fullscreen", True)
    root.attributes("-topmost", True)
    root.attributes("-alpha", 0.25)
    root.configure(bg="gray30")

    canvas = tk.Canvas(
        root, cursor="cross", highlightthickness=0,
        bg="gray30", bd=0,
    )
    canvas.pack(fill=tk.BOTH, expand=True)

    result: list = [None]
    start = [0, 0]
    rect_id = [None]

    def _on_press(event):
        start[0], start[1] = event.x, event.y

    def _on_drag(event):
        if rect_id[0] is not None:
            canvas.delete(rect_id[0])
        rect_id[0] = canvas.create_rectangle(
            start[0], start[1], event.x, event.y,
            outline="#ff3333", width=3, dash=(8, 3),
        )

    def _on_release(event):
        x1, y1 = start[0], start[1]
        x2, y2 = event.x, event.y
        if abs(x2 - x1) >= 20 and abs(y2 - y1) >= 20:
            result[0] = (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))
        root.destroy()

    canvas.bind("<ButtonPress-1>", _on_press)
    canvas.bind("<B1-Motion>", _on_drag)
    canvas.bind("<ButtonRelease-1>", _on_release)
    root.bind("<Escape>", lambda _e: root.destroy())

    root.update()
    root.focus_force()

    print()
    for line in hint_lines:
        print(f"  {line}")

    root.mainloop()

    return result[0]


def select_region() -> Optional[tuple[int, int, int, int]]:
    """Single-region selection (backward-compatible)."""
    return _select_single([
        "╔══════════════════════════════════════╗",
        "║  框选微信窗口区域                   ║",
        "║  按住鼠标左键拖拽绘制矩形           ║",
        "║  Esc = 取消                         ║",
        "╚══════════════════════════════════════╝",
    ])


def select_two_regions() -> tuple[
    Optional[tuple[int, int, int, int]],
    Optional[tuple[int, int, int, int]],
]:
    """Let the user select the chat list panel and conversation panel separately.

    First selection:  left-side chat list (contacts + message previews)
    Second selection: right-side conversation panel (actual messages)

    Returns (chat_list_rect, conversation_rect).  Either may be None if
    the user pressed Escape.
    """
    chat_rect = _select_single([
        "╔══════════════════════════════════════╗",
        "║  【第 1 步】框选 左侧聊天列表区域   ║",
        "║  (联系人 + 消息预览)                 ║",
        "║  按住鼠标左键拖拽绘制矩形           ║",
        "║  Esc = 取消                         ║",
        "╚══════════════════════════════════════╝",
    ])
    if chat_rect is None:
        return None, None

    conv_rect = _select_single([
        "╔══════════════════════════════════════╗",
        "║  【第 2 步】框选 右侧聊天内容区域   ║",
        "║  (对话内容 + 输入框)                 ║",
        "║  按住鼠标左键拖拽绘制矩形           ║",
        "║  Esc = 取消                         ║",
        "╚══════════════════════════════════════╝",
    ])
    if conv_rect is None:
        return chat_rect, None

    return chat_rect, conv_rect
