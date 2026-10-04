"""Mock of both external services, so the network clients can be tested offline.

The ComfyUI client is the one component that cannot be verified without a GPU and
a second machine. This stands in for it: /system_stats, /prompt, /history and
/view, plus an OpenAI-compatible /v1/chat/completions.

Run standalone to smoke the whole pipeline without a key:
    python tests/mock_services.py 8765
"""

import json
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPORT = "# Dossier: mock topic\n\n## One-paragraph orientation\nSomething about energy.\n"
STORY = "# Story: mock story\n\n## The angle\nOne sentence.\n"
SCRIPT = json.dumps(
    {
        "schema_version": "1.0",
        "title": "Mock",
        "style_prompt": "minimal 2D stick-figure explainer illustration on flat off-white paper",
        "negative_prompt": "lettering, watermark",
        "width": 1920,
        "height": 1080,
        "segments": [
            {
                "segment_id": "s01",
                "narration": "A mock segment of narration.",
                "scene_intent": "one figure",
                "duration_hint_s": 2.5,
                "transition": "cut",
                "on_screen_text": None,
            }
        ],
    }
)


def _png_bytes(width: int = 768, height: int = 432) -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "f.png"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
             f"testsrc=s={width}x{height}:d=1", "-frames:v", "1", str(path)],
            check=True,
        )
        return path.read_bytes()


class Handler(BaseHTTPRequestHandler):
    png = b""

    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/system_stats":
            self._send(200, b'{"system":{"comfyui_version":"mock"}}')
        elif path.startswith("/history/"):
            prompt_id = path.rsplit("/", 1)[-1]
            self._send(200, json.dumps({prompt_id: {
                "status": {"status_str": "success"},
                "outputs": {"9": {"images": [{"filename": "mock.png", "subfolder": "", "type": "output"}]}},
            }}).encode())
        elif path == "/view":
            self._send(200, self.png, "image/png")
        else:
            self._send(404, b'{"error":"not found"}')

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) if length else b"{}")
        if self.path.endswith("/chat/completions"):
            system = body.get("messages", [{}])[0].get("content", "")
            if "Researcher" in system:
                content = REPORT
            elif "Editor" in system:
                content = STORY
            else:
                content = SCRIPT
            self._send(200, json.dumps({
                "choices": [{"message": {"content": content}}],
                "usage": {"total_tokens": 3, "prompt_tokens": 2, "completion_tokens": 1},
            }).encode())
        elif self.path.endswith("/prompt"):
            self._send(200, json.dumps({"prompt_id": "mock-prompt-id"}).encode())
        else:
            self._send(404, b'{"error":"not found"}')


class MockServices:
    """Context manager: python with MockServices(port) as mocks: mocks.base_url"""

    def __init__(self, port: int = 0, png: bytes | None = None):
        Handler.png = png if png is not None else _png_bytes()
        self.server = HTTPServer(("127.0.0.1", port), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    with MockServices(port) as mocks:
        print(f"mock on {mocks.base_url}", flush=True)
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass