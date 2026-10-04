# dots/__main__.py — `python -m dots [--host 127.0.0.1] [--port 8765]`
from __future__ import annotations

import argparse


def main() -> None:
    import uvicorn
    parser = argparse.ArgumentParser(
        description="Dots — self-hosted specialist-agent workspace")
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind address (default: loopback only; "
                             "LAN exposure adds a PIN in batch 6f)")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    print(f"[Dots] serving on http://{args.host}:{args.port} "
          f"(UI arrives in batch 6f; API at /api/…)")
    uvicorn.run("dots.server:app", host=args.host, port=args.port,
                log_level="info")


if __name__ == "__main__":
    main()
