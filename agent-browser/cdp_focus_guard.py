"""Keep agents from stealing the owner's keyboard focus.

Agents drive the dedicated Chrome over the DevTools protocol (CDP). Playwright
switches tabs with ``Page.bringToFront``, and Chrome, running as an X11 window
so it can stay on the laptop display, then raises itself and takes the owner's
keyboard focus. This proxy sits on the port agents already use, forwards
everything else unchanged to Chrome's private port, and:

- answers ``Page.bringToFront`` / ``Target.activateTarget`` (and HTTP
  ``/json/activate``) itself, without telling Chrome;
- opens new tabs from ``Target.createTarget`` in the background.

Clicks, typing and page reads are unaffected: CDP input does not need window
focus. Loopback only. Set ``"focus_guard": false`` to turn it off.
"""
from __future__ import annotations

import base64
import hashlib
import http.client
import json
import socket
import socketserver
import struct
import threading

import websocket

SUPPRESSED = frozenset({"Page.bringToFront", "Target.activateTarget"})
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_HEADER_BYTES = 65536


def filter_client_message(text):
    """Return (text to forward or None, local reply or None) for one CDP message."""
    try:
        message = json.loads(text)
    except ValueError:
        return text, None
    if not isinstance(message, dict):
        return text, None
    method = message.get("method")
    if method in SUPPRESSED and "id" in message:
        reply = {"id": message["id"], "result": {}}
        if "sessionId" in message:
            reply["sessionId"] = message["sessionId"]
        return None, json.dumps(reply)
    if method == "Target.createTarget" and isinstance(message.get("params"), dict):
        message["params"]["background"] = True
        return json.dumps(message), None
    return text, None


# --- minimal RFC 6455 server side (the client side uses websocket-client) ----

def read_exact(sock, count):
    data = bytearray()
    while len(data) < count:
        chunk = sock.recv(count - len(data))
        if not chunk:
            raise ConnectionError("websocket peer closed")
        data += chunk
    return bytes(data)


def read_frame(sock):
    """One frame: (fin, opcode, payload), unmasked."""
    first, second = read_exact(sock, 2)
    fin, opcode = bool(first & 0x80), first & 0x0F
    masked, length = bool(second & 0x80), second & 0x7F
    if length == 126:
        (length,) = struct.unpack("!H", read_exact(sock, 2))
    elif length == 127:
        (length,) = struct.unpack("!Q", read_exact(sock, 8))
    mask = read_exact(sock, 4) if masked else b""
    payload = read_exact(sock, length)
    if masked:
        payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    return fin, opcode, payload


def read_message(sock, on_ping=None):
    """A whole data message (text/binary), joining continuation frames."""
    parts, kind = [], None
    while True:
        fin, opcode, payload = read_frame(sock)
        if opcode == 0x8:
            return 0x8, payload
        if opcode == 0x9:
            if on_ping:
                on_ping(payload)
            continue
        if opcode == 0xA:
            continue
        if opcode in (0x1, 0x2):
            kind, parts = opcode, [payload]
        elif opcode == 0x0 and kind is not None:
            parts.append(payload)
        if fin and kind is not None:
            return kind, b"".join(parts)


def encode_frame(opcode, payload):
    length = len(payload)
    if length < 126:
        header = struct.pack("!BB", 0x80 | opcode, length)
    elif length < 65536:
        header = struct.pack("!BBH", 0x80 | opcode, 126, length)
    else:
        header = struct.pack("!BBQ", 0x80 | opcode, 127, length)
    return header + payload


def accept_key(key):
    return base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()


class _Handler(socketserver.BaseRequestHandler):
    upstream_port = 0
    public_port = 0

    def handle(self):
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.request.recv(4096)
            if not chunk:
                return
            head += chunk
            if len(head) > MAX_HEADER_BYTES:
                return
        head, _, rest = head.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        method, path, _version = (lines[0].split(" ") + ["", "", ""])[:3]
        headers = {}
        for line in lines[1:]:
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
        if headers.get("upgrade", "").lower() == "websocket":
            self.relay_websocket(path, headers)
        else:
            self.relay_http(method, path, headers, rest)

    def relay_http(self, method, path, headers, body):
        if path.startswith("/json/activate/"):
            self.respond(200, b"Target activated", "text/plain")
            return
        length = int(headers.get("content-length", "0") or 0)
        while len(body) < length:
            chunk = self.request.recv(length - len(body))
            if not chunk:
                break
            body += chunk
        connection = http.client.HTTPConnection("127.0.0.1", self.upstream_port, timeout=15)
        try:
            # Chrome rejects Host headers that are not an IP or localhost.
            connection.request(method, path, body=body or None,
                               headers={"Host": f"127.0.0.1:{self.upstream_port}"})
            response = connection.getresponse()
            data = response.read()
            kind = response.getheader("Content-Type", "application/json")
            status = response.status
        finally:
            connection.close()
        # Advertise this proxy, not Chrome's private port, to the agents.
        for host in ("127.0.0.1", "localhost"):
            data = data.replace(f"{host}:{self.upstream_port}".encode(), f"{host}:{self.public_port}".encode())
        self.respond(status, data, kind)

    def respond(self, status, data, kind):
        reason = {200: "OK", 404: "Not Found"}.get(status, "OK")
        self.request.sendall(
            f"HTTP/1.1 {status} {reason}\r\nContent-Type: {kind}\r\nContent-Length: {len(data)}\r\n"
            "Connection: close\r\n\r\n".encode() + data)

    def relay_websocket(self, path, headers):
        key = headers.get("sec-websocket-key", "")
        try:
            upstream = websocket.create_connection(
                f"ws://127.0.0.1:{self.upstream_port}{path}", suppress_origin=True, timeout=None)
        except (OSError, websocket.WebSocketException):
            self.respond(502, b"Chrome is unavailable", "text/plain")
            return
        self.request.sendall(
            "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept_key(key)}\r\n\r\n".encode())
        send_lock = threading.Lock()  # replies and relayed messages share the socket

        def send_down(opcode, payload):
            with send_lock:
                self.request.sendall(encode_frame(opcode, payload))

        def from_chrome():
            try:
                while True:
                    opcode, data = upstream.recv_data()
                    if opcode == websocket.ABNF.OPCODE_CLOSE:
                        break
                    send_down(opcode if opcode in (0x1, 0x2) else 0x1, data)
            except (OSError, websocket.WebSocketException):
                pass
            finally:
                try:
                    send_down(0x8, b"")
                except OSError:
                    pass
                try:
                    self.request.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

        reader = threading.Thread(target=from_chrome, daemon=True)
        reader.start()
        try:
            while True:
                opcode, payload = read_message(self.request, on_ping=lambda data: send_down(0xA, data))
                if opcode == 0x8:
                    break
                if opcode == 0x2:
                    upstream.send_binary(payload)
                    continue
                forward, reply = filter_client_message(payload.decode("utf-8", errors="replace"))
                if reply is not None:
                    send_down(0x1, reply.encode())
                if forward is not None:
                    upstream.send(forward)
        except (OSError, ConnectionError, websocket.WebSocketException):
            pass
        finally:
            upstream.close()


class FocusGuard(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


def start_focus_guard(public_port, upstream_port):
    """Serve on 127.0.0.1:public_port in a daemon thread; returns the server."""
    handler = type("Handler", (_Handler,), {"upstream_port": upstream_port, "public_port": public_port})
    server = FocusGuard(("127.0.0.1", public_port), handler)
    threading.Thread(target=server.serve_forever, name="cdp-focus-guard", daemon=True).start()
    return server
