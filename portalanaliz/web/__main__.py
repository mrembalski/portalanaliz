"""Run the archive UI: python -m portalanaliz.web [--port 8000]"""

import argparse

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-reload", action="store_true", help="disable auto-reload")
    args = parser.parse_args()
    uvicorn.run("portalanaliz.web.app:app", host=args.host, port=args.port,
                reload=not args.no_reload)


if __name__ == "__main__":
    main()
