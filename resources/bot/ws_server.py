"""WebSocket bridge between bot_image.py and Electron UI.

Zero external dependencies beyond Python stdlib — uses raw TCP with a
minimal HTTP upgrade handshake and simple text-frame protocol.  Runs in a
background daemon thread so the main bot loop is unchanged.
"""

import asyncio
import json
import logging
import struct
import threading
import time
import hashlib
import base64

logger = logging.getLogger("wechat-bot-ws")

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class BotWebSocketServer:
    """Thin async WebSocket server bridging bot events to Electron.

    The bot calls ``broadcast_sync(event)`` from its main thread and the
    server pushes JSON to every connected client.  Commands from clients
    are queued for the bot to pick up in its poll loop.

    Usage inside bot_image.py::

        ws = BotWebSocketServer(port=9877)
        ws.start()
        # … inside ZhaoyoucaiImageBot.__init__ …
        self.ws = ws
        # … when events happen …
        self.ws.broadcast_sync({"type": "scan", ...})
    """

    def __init__(self, port: int = 9877):
        self.port = port
        self._clients: dict = {}  # id → (reader, writer)
        self._server = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._command_queue: list[dict] = []
        self._lock = threading.Lock()
        self._running = False
        self._started_at: float | None = None
        self._heartbeat_task = None

    # ------------------------------------------------------------------
    # Public API (called from bot main thread)
    # ------------------------------------------------------------------

    def broadcast_sync(self, event: dict) -> None:
        """Push an event dict to all connected clients.  Thread-safe."""
        if not self._running or not self._loop:
            return
        event.setdefault("ts", time.time())
        asyncio.run_coroutine_threadsafe(self._broadcast(event), self._loop)

    def pop_commands(self) -> list[dict]:
        """Drain and return queued commands.  Call from bot poll loop."""
        with self._lock:
            cmds = self._command_queue[:]
            self._command_queue.clear()
        return cmds

    def get_stats(self) -> dict:
        return {
            "running": self._running,
            "port": self.port,
            "clients": len(self._clients),
            "uptime": time.time() - self._started_at if self._started_at else 0,
        }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Launch the server in a daemon thread."""
        if self._running:
            return
        self._running = True
        self._started_at = time.time()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        logger.info("WebSocket server starting on ws://127.0.0.1:%d", self.port)

    def stop(self) -> None:
        """Shut down the server and disconnect all clients."""
        self._running = False
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=2.0)
        logger.info("WebSocket server stopped")

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
        except Exception:
            logger.exception("WebSocket server crashed")
        finally:
            self._running = False

    async def _serve(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_client, "127.0.0.1", self.port
        )
        # Heartbeat every 10s so clients can detect stale connections
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        try:
            async with self._server:
                await self._server.serve_forever()
        except asyncio.CancelledError:
            pass

    async def _heartbeat_loop(self) -> None:
        while self._running:
            await asyncio.sleep(10)
            await self._broadcast({
                "type": "heartbeat",
                "clients": len(self._clients),
                "ts": time.time(),
            })

    async def _broadcast(self, event: dict) -> None:
        if not self._clients:
            return
        payload = json.dumps(event, ensure_ascii=False)
        dead = []
        for cid, (reader, writer) in list(self._clients.items()):
            try:
                await self._ws_send(writer, payload)
            except Exception:
                dead.append(cid)
        for cid in dead:
            self._clients.pop(cid, None)
            logger.debug("Client %s removed", cid[:8])

    # ------------------------------------------------------------------
    # Per-client handling
    # ------------------------------------------------------------------

    async def _handle_client(self, reader: asyncio.StreamReader,
                             writer: asyncio.StreamWriter) -> None:
        """Accept one TCP connection, perform WebSocket handshake, then
        read text frames until the socket closes."""
        cid = None
        try:
            # ── HTTP Upgrade handshake ──
            request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
            key = None
            for line in request.decode("utf-8", errors="replace").split("\r\n"):
                if line.lower().startswith("sec-websocket-key:"):
                    key = line.split(":", 1)[1].strip()
                    break
            if not key:
                writer.close()
                return

            accept = base64.b64encode(
                hashlib.sha1((key + GUID).encode()).digest()
            ).decode()
            writer.write(
                f"HTTP/1.1 101 Switching Protocols\r\n"
                f"Upgrade: websocket\r\n"
                f"Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n"
                f"\r\n".encode()
            )
            await writer.drain()

            cid = f"{id(writer):x}"
            self._clients[cid] = (reader, writer)
            logger.debug("Client %s connected (%d total)", cid[:8], len(self._clients))

            # Push current status
            await self._ws_send(writer, json.dumps({
                "type": "status",
                "state": "running",
                "uptime": time.time() - self._started_at
                if self._started_at else 0,
                "ts": time.time(),
            }))

            # ── Frame read loop ──
            while self._running:
                frame = await self._read_frame(reader)
                if frame is None:
                    break  # connection closed
                if not frame:
                    continue  # empty frame
                try:
                    cmd = json.loads(frame)
                    with self._lock:
                        self._command_queue.append(cmd)
                except json.JSONDecodeError:
                    logger.debug("Invalid JSON from client: %s", frame[:100])

        except (asyncio.TimeoutError, ConnectionError, OSError):
            pass
        finally:
            if cid:
                self._clients.pop(cid, None)
            try:
                writer.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Minimal WebSocket frame helpers (RFC 6455, text frames only)
    # ------------------------------------------------------------------

    @staticmethod
    async def _ws_send(writer: asyncio.StreamWriter, text: str) -> None:
        data = text.encode("utf-8")
        length = len(data)
        header = bytearray()
        header.append(0x81)  # FIN + text opcode
        if length < 126:
            header.append(length)
        elif length < 65536:
            header.append(126)
            header.extend(struct.pack(">H", length))
        else:
            header.append(127)
            header.extend(struct.pack(">Q", length))
        writer.write(bytes(header) + data)
        await writer.drain()

    @staticmethod
    async def _read_frame(reader: asyncio.StreamReader) -> str | None:
        """Read one WebSocket text frame.  Returns str, None (closed), or '' (non-text)."""
        try:
            b0 = await reader.readexactly(1)
        except asyncio.IncompleteReadError:
            return None
        opcode = b0[0] & 0x0F
        if opcode == 0x8:  # close
            return None
        if opcode == 0x9:  # ping → pong
            b1 = await reader.readexactly(1)
            length = b1[0] & 0x7F
            if length == 126:
                length = struct.unpack(">H", await reader.readexactly(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", await reader.readexactly(8))[0]
            mask = await reader.readexactly(4)
            # consume payload
            _ = await reader.readexactly(length)
            return ""  # ignore pings in application layer
        if opcode != 0x1:  # only handle text frames
            return ""

        b1 = await reader.readexactly(1)
        masked = bool(b1[0] & 0x80)
        length = b1[0] & 0x7F
        if length == 126:
            length = struct.unpack(">H", await reader.readexactly(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", await reader.readexactly(8))[0]

        if masked:
            mask = await reader.readexactly(4)
        else:
            mask = b"\x00\x00\x00\x00"

        payload = bytearray(await reader.readexactly(length))
        for i in range(len(payload)):
            payload[i] ^= mask[i % 4]
        return payload.decode("utf-8", errors="replace")
