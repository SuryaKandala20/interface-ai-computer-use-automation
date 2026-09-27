from __future__ import annotations

import os
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

os.environ.setdefault("FVCU_OPERATOR_ID", "teller01")
os.environ.setdefault("FVCU_OPERATOR_PASSWORD", "fv-demo-Pass!")
os.environ.setdefault("HARBOR_OPERATOR_ID", "hp_teller")
os.environ.setdefault("HARBOR_OPERATOR_PASSWORD", "hp-demo-Pass!")


def _port_open(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


@pytest.fixture(scope="session")
def mockapp():
    """Mock app on the allowlisted port. Reuses it if it's already running."""
    if not _port_open(8765):
        import uvicorn

        from mockapp.app import app

        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=8765, log_level="warning"))
        threading.Thread(target=server.run, daemon=True).start()
        for _ in range(50):
            if _port_open(8765):
                break
            time.sleep(0.1)
    yield "http://127.0.0.1:8765"
    from mockapp.app import CHAOS

    CHAOS["mode"] = None
