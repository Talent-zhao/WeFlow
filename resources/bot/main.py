#!/usr/bin/env python3
"""赵有才微信机器人 — WeChat bot powered by Claude with zhaoyoucai persona.

Usage:
    python main.py              # Start the bot (image/OCR backend, works with any WeChat)
    python main.py --test       # Test Claude API without WeChat
    python main.py --once NAME  # Single reply to the current chat, then exit
    python main.py --wcf        # WeChatFerry backend (WeChat 3.9.x only)
    python main.py --legacy     # wxauto4 backend (legacy)

Prerequisites:
    Default (image/OCR) backend:
        1. WeChat running with window visible on screen
        2. ANTHROPIC_API_KEY environment variable set
        3. Windows 10/11 (uses built-in OCR)
        4. pip install pyautogui pyperclip pywin32 pillow winrt anthropic

    WeChatFerry (wcf) backend:
        1. WeChat PC 3.9.5.81 installed and logged in
        2. pip install wcferry
"""

import argparse
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("wechat-bot")


def main():
    parser = argparse.ArgumentParser(
        description="赵有才微信机器人",
        epilog="默认使用截图+OCR后端，兼容所有微信版本。"
    )
    parser.add_argument(
        "--test", action="store_true",
        help="测试 Claude API 回复（无需微信）"
    )
    parser.add_argument(
        "--wcf", action="store_true",
        help="使用 WeChatFerry 后端（仅限微信 3.9.x）"
    )
    parser.add_argument(
        "--legacy", action="store_true",
        help="使用 wxauto4 后端（已废弃）"
    )
    parser.add_argument(
        "--image", action="store_true",
        help="截图+OCR 后端（已是默认，无需指定）"
    )
    parser.add_argument(
        "--once", type=str, metavar="CONTACT",
        help="单次模式：读取当前聊天并回复一次后退出"
    )
    parser.add_argument(
        "--quiet", "-q", action="store_true",
        help="静默模式：减少终端输出（仅显示关键事件）"
    )
    parser.add_argument(
        "--select", "-s", action="store_true",
        help="手动框选微信窗口区域（覆盖自动检测）"
    )
    parser.add_argument(
        "--ws-port", type=int, default=None,
        metavar="PORT",
        help="WebSocket 端口（用于 Electron 集成，默认 9877）"
    )
    args = parser.parse_args()

    verbose = not args.quiet

    # --- Test mode: no WeChat needed ---
    if args.test:
        from bot import ZhaoyoucaiBot as Bot

        bot = Bot()
        test_messages = [
            ("王小明", "在吗，忙啥呢"),
            ("李华", "考研成绩出了你查了吗"),
            ("陶页川", "彬哥打球不"),
        ]
        for contact, msg in test_messages:
            print(f"\n{'='*50}")
            print(f"[{contact}] {msg}")
            reply = bot.handle_message(contact, msg)
            print(f"[赵有才 → {contact}] {reply}")
        return

    # --- WCF mode (WeChat 3.9.x only) ---
    if args.wcf:
        from bot_wcf import ZhaoyoucaiBot as Bot

        logger.info("启动赵有才微信机器人 (WeChatFerry 后端)...")
        logger.info("请确保微信 3.9.5.81 已登录。")

        bot = Bot()
        bot.run_forever()
        return

    # --- Legacy mode: wxauto4 ---
    if args.legacy:
        from bot import ZhaoyoucaiBot as Bot
        bot = Bot()
        bot.run_forever()
        return

    # --- Default: Image/OCR mode (works with any WeChat version) ---
    from bot_image import ZhaoyoucaiImageBot as Bot

    chat_list_rect = None
    conversation_rect = None
    if args.select:
        from region_selector import select_two_regions
        print("\n  ℹ  请在弹出的透明遮罩上依次框选两个区域")
        chat_list_rect, conversation_rect = select_two_regions()
        if chat_list_rect is None:
            print("  ✗ 已取消选择，退出。")
            return
        cl_l, cl_t, cl_r, cl_b = chat_list_rect
        print(f"  ✓ 聊天列表: ({cl_l}, {cl_t}) - ({cl_r}, {cl_b})  "
              f"[{cl_r - cl_l}×{cl_b - cl_t}]")
        if conversation_rect is None:
            print("  ✗ 第2步已取消，退出。")
            return
        cv_l, cv_t, cv_r, cv_b = conversation_rect
        print(f"  ✓ 聊天内容: ({cv_l}, {cv_t}) - ({cv_r}, {cv_b})  "
              f"[{cv_r - cv_l}×{cv_b - cv_t}]")

    # -- WebSocket bridge (Electron integration)
    ws = None
    if args.ws_port:
        from ws_server import BotWebSocketServer
        ws_port = args.ws_port or 9877
        ws = BotWebSocketServer(port=ws_port)
        ws.start()
        logger.info("WebSocket bridge active on ws://127.0.0.1:%d", ws_port)

    if args.once:
        bot = Bot(verbose=verbose,
                  chat_list_rect=chat_list_rect,
                  conversation_rect=conversation_rect,
                  ws_server=ws)
        bot.process_current_chat(args.once)
        if ws:
            ws.stop()
        return

    try:
        bot = Bot(verbose=verbose,
                  chat_list_rect=chat_list_rect,
                  conversation_rect=conversation_rect,
                  ws_server=ws)
        bot.run_forever()
    finally:
        if ws:
            ws.stop()


if __name__ == "__main__":
    main()
