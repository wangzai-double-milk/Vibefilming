"""临时：起一个隔离工作区的 Studio，用于浏览器手验新建两步式（验完即删）。"""
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from film import workspace as ws  # noqa: E402

root = Path(tempfile.mkdtemp(prefix="vf_ui_")) / "projects"
root.mkdir(parents=True)
ws.PROJECTS_ROOT = root

from film import studio  # noqa: E402

print(studio.start_studio_server(None, port=8901), flush=True)
print("SANDBOX_ROOT", root, flush=True)
time.sleep(1800)
