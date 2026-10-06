"""The focus guard: agents drive Chrome fully, but can never raise its window."""
import json
import os
import shutil
import socket
import struct
import subprocess
import tempfile
import textwrap
import time
import unittest
import urllib.request

import cdp_focus_guard as guard

CHROME = "/usr/bin/google-chrome-stable"
NODE = "/usr/bin/node"
PLAYWRIGHT = os.environ.get("OCDECK_PLAYWRIGHT_CORE", "")  # path of an installed playwright-core


class FilterTests(unittest.TestCase):
    def test_bring_to_front_is_answered_locally_and_never_forwarded(self):
        for method in ("Page.bringToFront", "Target.activateTarget"):
            forward, reply = guard.filter_client_message(json.dumps({"id": 7, "method": method}))
            self.assertIsNone(forward)
            self.assertEqual(json.loads(reply), {"id": 7, "result": {}})

    def test_flattened_session_replies_keep_their_session(self):
        _, reply = guard.filter_client_message(
            json.dumps({"id": 3, "sessionId": "S1", "method": "Page.bringToFront", "params": {}}))
        self.assertEqual(json.loads(reply), {"id": 3, "sessionId": "S1", "result": {}})

    def test_new_tabs_open_in_the_background(self):
        forward, reply = guard.filter_client_message(
            json.dumps({"id": 1, "method": "Target.createTarget", "params": {"url": "about:blank"}}))
        self.assertIsNone(reply)
        self.assertTrue(json.loads(forward)["params"]["background"])

    def test_everything_else_passes_through_unchanged(self):
        for text in ('{"id":2,"method":"Input.dispatchMouseEvent","params":{}}', "not json", "[1,2]"):
            self.assertEqual(guard.filter_client_message(text), (text, None))


class FramingTests(unittest.TestCase):
    def pair(self, data):
        # Write from a thread: a multi-MB frame exceeds the socket buffer.
        import threading
        a, b = socket.socketpair()

        def write():
            a.sendall(data)
            a.close()

        threading.Thread(target=write, daemon=True).start()
        return b

    def test_rfc6455_masked_hello(self):
        sock = self.pair(bytes([0x81, 0x85, 0x37, 0xFA, 0x21, 0x3D, 0x7F, 0x9F, 0x4D, 0x51, 0x58]))
        self.assertEqual(guard.read_frame(sock), (True, 0x1, b"Hello"))

    def test_rfc6455_accept_key(self):
        self.assertEqual(guard.accept_key("dGhlIHNhbXBsZSBub25jZQ=="), "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")

    def test_long_lengths_round_trip(self):
        for size in (125, 126, 65535, 65536, 3_000_000):  # snapshots can be several MB
            payload = os.urandom(size)
            fin, opcode, data = guard.read_frame(self.pair(guard.encode_frame(0x2, payload)))
            self.assertEqual((fin, opcode, data), (True, 0x2, payload))

    def test_fragmented_message_and_ping_are_handled(self):
        pings = []
        frames = (struct.pack("!BB", 0x01, 3) + b"abc"          # text, not final
                  + struct.pack("!BB", 0x89, 1) + b"p"          # ping in between
                  + struct.pack("!BB", 0x80, 3) + b"def")       # final continuation
        self.assertEqual(guard.read_message(self.pair(frames), on_ping=pings.append), (0x1, b"abcdef"))
        self.assertEqual(pings, [b"p"])


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@unittest.skipUnless(os.path.exists(CHROME) and os.path.exists(NODE) and os.path.isdir(PLAYWRIGHT),
                     "needs Chrome, node and the protected Playwright")
class EndToEndTests(unittest.TestCase):
    """A throwaway headless Chrome behind the guard, driven by real Playwright."""

    def test_playwright_works_through_the_guard_without_activation(self):
        chrome_port, public_port = free_port(), free_port()
        profile = tempfile.mkdtemp(prefix="focus-guard-test-")
        chrome = subprocess.Popen(
            [CHROME, "--headless=new", "--disable-gpu", f"--user-data-dir={profile}",
             f"--remote-debugging-port={chrome_port}", "--remote-debugging-address=127.0.0.1",
             "--no-first-run", "--no-default-browser-check", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        server = None
        try:
            deadline = time.monotonic() + 20
            while True:
                try:
                    urllib.request.urlopen(f"http://127.0.0.1:{chrome_port}/json/version", timeout=1)
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        raise
                    time.sleep(0.2)
            server = guard.start_focus_guard(public_port, chrome_port)
            version = json.load(urllib.request.urlopen(f"http://127.0.0.1:{public_port}/json/version"))
            self.assertIn(f":{public_port}/", version["webSocketDebuggerUrl"])  # rewritten to the guard
            script = textwrap.dedent(f"""
                const {{ chromium }} = require({json.dumps(PLAYWRIGHT)});
                (async () => {{
                  const browser = await chromium.connectOverCDP("http://127.0.0.1:{public_port}");
                  const context = browser.contexts()[0];
                  const first = await context.newPage();
                  await first.setContent('<input id="a"><button id="b" onclick="this.textContent=1">x</button>');
                  const second = await context.newPage();
                  await second.setContent('<p>second</p>');
                  await second.bringToFront();              // answered by the guard
                  await first.fill('#a', 'typed in a background tab');
                  await first.click('#b');
                  const value = await first.inputValue('#a');
                  const clicked = await first.textContent('#b');
                  console.log(JSON.stringify({{ value, clicked, pages: context.pages().length }}));
                  await browser.close();
                }})().catch((error) => {{ console.error(error); process.exit(1); }});
            """)
            result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            outcome = json.loads(result.stdout.strip().splitlines()[-1])
            self.assertEqual(outcome["value"], "typed in a background tab")
            self.assertEqual(outcome["clicked"], "1")
            self.assertGreaterEqual(outcome["pages"], 2)
        finally:
            if server is not None:
                server.shutdown()
                server.server_close()
            chrome.terminate()
            try:
                chrome.wait(timeout=10)
            except subprocess.TimeoutExpired:
                chrome.kill()
            shutil.rmtree(profile, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
