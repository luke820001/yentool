"""
Tiny static server for the YenTool mobile PWA.

Run from the project root or this folder:
    python mobile/serve.py

It serves the mobile/ directory on port 8000 and prints a LAN URL you can
open from your iPhone (same Wi-Fi). All strings ASCII.
"""
import http.server
import os
import socket

PORT = int(os.environ.get("PORT", "8000"))
HERE = os.path.dirname(os.path.abspath(__file__))


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=HERE, **k)

    def end_headers(self):
        # Never let the browser cache the data feed.
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


if __name__ == "__main__":
    ip = lan_ip()
    print("Serving mobile PWA:")
    print("  local:  http://localhost:{}/".format(PORT))
    print("  iPhone: http://{}:{}/".format(ip, PORT))
    print("Open the iPhone URL in Safari, then Share -> Add to Home Screen.")
    print("Ctrl+C to stop.")
    # Threading server so one held-open browser connection (keep-alive) cannot
    # block a concurrent request from the phone (refresh, data fetch).
    httpd = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
        httpd.shutdown()
