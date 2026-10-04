from __future__ import annotations

import base64
import io
import sys
import tarfile
from pathlib import Path

BASE = Path(__file__).parent
RUNTIME = Path("/tmp/minna-no-trump-runtime")
RUNTIME.mkdir(parents=True, exist_ok=True)

if not (RUNTIME / "game_registry.py").exists():
    bundle_text = "".join(
        (BASE / f"runtime_bundle.b64.{i}").read_text().strip()
        for i in range(1, 5)
    )
    bundle = base64.b64decode(bundle_text)
    with tarfile.open(fileobj=io.BytesIO(bundle), mode="r:xz") as tf:
        tf.extractall(RUNTIME)

sys.path.insert(0, str(RUNTIME))

# The branch app imports the historical module name "game_engine".
# Keep that import compatible with the new split registry without touching main.
import game_registry
sys.modules["game_engine"] = game_registry

import app as server

# app.py resolves the front-end file through BASE at request time.
server.BASE = RUNTIME
app = server.app
