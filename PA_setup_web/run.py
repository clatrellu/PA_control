"""Entry point — run with: uv run python run.py [--mock]"""
import argparse
import os
import threading
import time
import webbrowser


def main() -> None:
    parser = argparse.ArgumentParser(description="PA Setup Web Control")
    parser.add_argument("--mock", action="store_true",
                        help="Use simulated hardware")
    parser.add_argument("--host", default="127.0.0.1",
                        help="Bind address (use 0.0.0.0 for network access)")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    if args.mock:
        os.environ["PA_MOCK"] = "1"

    url = f"http://{args.host}:{args.port}"
    print(f"PA Setup Web → {url}")
    if args.mock:
        print("  Mock mode active — no hardware required.")

    def _open_browser():
        time.sleep(1.2)
        webbrowser.open(url)

    threading.Thread(target=_open_browser, daemon=True).start()

    import uvicorn
    uvicorn.run("server:app", host=args.host, port=args.port, reload=False)


if __name__ == "__main__":
    main()
