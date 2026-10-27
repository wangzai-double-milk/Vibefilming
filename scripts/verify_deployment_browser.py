#!/usr/bin/env python3
"""Disposable Studio/Canvas reverse proxy for browser integration checks; no model calls."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import argparse
import shutil
import sys
import tempfile
import urllib.request
import urllib.error

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from film import workspace as ws, studio, workflow_canvas, server_config

destinations = {}
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--reference", type=Path, help="Optional local video for the preview check")
args = parser.parse_args()
if args.reference is not None and not args.reference.is_file():
    parser.error("--reference must point to an existing local file")


class Proxy(BaseHTTPRequestHandler):
    def do_GET(self):
        prefix = self.path.split("/")[1]
        if prefix not in destinations:
            self.send_error(404)
            return
        suffix = self.path[len(prefix) + 1:] or "/"
        target = destinations[prefix] + suffix
        try:
            with urllib.request.urlopen(target, timeout=10) as response:
                body = response.read()
                self.send_response(response.status)
                self.send_header("Content-Type", response.headers.get("Content-Type", "text/plain"))
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        except (urllib.error.URLError, OSError):
            self.send_error(502)

    def log_message(self, *_):
        pass


with tempfile.TemporaryDirectory(prefix="vf_proxy_check_") as temporary:
    ws.PROJECTS_ROOT = Path(temporary) / "projects"
    ws.PROJECTS_ROOT.mkdir()
    m = ws.project_create("Game deployment browser check")
    ws.set_active_project(m["project_id"])
    p = Path(m["project_dir"])
    src = args.reference
    if src is not None:
        dest = p / "materials/reference.mp4"
        shutil.copyfile(src, dest)
        ws.update_manifest(m["project_id"], lambda data: data.update(
            materials={"reference_video": [str(dest)]}))
    proxy = ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
    base = f"http://127.0.0.1:{proxy.server_port}"
    server_config.configure_server_overrides(
        studio={"host": "127.0.0.1", "port": 0, "public_url": base + "/film/"},
        canvas={"host": "127.0.0.1", "port": 0, "public_url": base + "/canvas/"},
    )
    started = studio.start_studio_server(None)
    live = workflow_canvas.ensure_live_server(p)
    destinations.update(film=f"http://127.0.0.1:{started['port']}",
                        canvas=f"http://127.0.0.1:{live['port']}")
    print("BROWSER_CHECK_URL=" + base + "/film/", flush=True)
    try:
        proxy.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        proxy.server_close()
        for state in (studio._STATE, workflow_canvas._LIVE_STATE):
            if state.get("server"):
                state["server"].shutdown()
                state["server"].server_close()
