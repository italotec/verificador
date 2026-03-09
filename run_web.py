"""
Flask web interface for the Verificador agent.

Usage:
    python run_web.py
    python run_web.py --port 8080
    python run_web.py --host 0.0.0.0 --port 8080
"""
import argparse
from web_app import create_app

app = create_app()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Verificador Web Interface")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5001)
    parser.add_argument("--debug", action="store_true", default=True)
    args = parser.parse_args()

    print(f"  Verificador Web — http://{args.host}:{args.port}")
    print(f"  Login padrão: admin / admin")
    # use_reloader=False is critical: the watchdog reloader kills background
    # threads (Playwright jobs) whenever it detects a file change in imported
    # libraries (asyncio, playwright internals), corrupting running verifications.
    app.run(host=args.host, port=args.port, debug=args.debug, use_reloader=False)
