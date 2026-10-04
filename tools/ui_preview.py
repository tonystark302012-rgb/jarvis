"""Dashboard preview bootstrap — server on :8712, PIN `PREVIEW` (reseeded).

Writes /home/user/ui_preview.json with {base, token, pin} so tests can skip
the interactive login. Run (cwd-independent):
  /path/to/venv/bin/python tools/ui_preview.py
"""
import asyncio
import json
import os
import sys
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
os.chdir(_REPO)
sys.path.insert(0, str(_REPO))

import dashboard.server as dsrv  # noqa: E402

dsrv.PORT = 8712
from dashboard.server import DashboardServer  # noqa: E402


async def main() -> None:
    srv = DashboardServer()
    token = srv.ui_session()
    proto = "https" if (srv._ssl_enabled() or (_REPO / "config" / "certs").exists()) else "http"
    info = {"base": f"{proto}://127.0.0.1:8712",
            "token": token, "pin": "PREVIEW"}
    with open("/home/user/ui_preview.json", "w", encoding="utf-8") as fh:
        json.dump(info, fh)
    print("PREVIEW READY", json.dumps(info), flush=True)

    async def reseed() -> None:
        # login consumes the key (one-time) — re-add so every fresh browser
        # session can type PREVIEW again.
        while True:
            srv._pending_keys["PREVIEW"] = time.time() + 86400
            await asyncio.sleep(4)

    asyncio.ensure_future(reseed())
    await srv.serve()


if __name__ == "__main__":
    asyncio.run(main())
