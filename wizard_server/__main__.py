"""Entry point: python3 -m wizard_server [--port PORT] [--host HOST]"""

import argparse

from .server import start_server


def main():
    parser = argparse.ArgumentParser(
        prog="python3 -m wizard_server",
        description="Start the dependabot wizard server. Default: http://127.0.0.1:8787/",
    )
    parser.add_argument("--host", help="Bind host (overrides wizard-config.json)")
    parser.add_argument("--port", type=int, help="Bind port (overrides wizard-config.json)")
    args = parser.parse_args()

    start_server(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
