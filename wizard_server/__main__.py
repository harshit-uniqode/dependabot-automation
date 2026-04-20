"""Entry point: python3 -m wizard_server [--port PORT] [--host HOST]"""

import sys
from .server import start_server


def main():
    host = None
    port = None

    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--port" and i + 1 < len(args):
            port = int(args[i + 1])
            i += 2
        elif args[i] == "--host" and i + 1 < len(args):
            host = args[i + 1]
            i += 2
        elif args[i] in ("--help", "-h"):
            print("Usage: python3 -m wizard_server [--host HOST] [--port PORT]")
            print()
            print("Starts the Upgrade Wizard server.")
            print("Default: http://127.0.0.1:8787/")
            sys.exit(0)
        else:
            print(f"Unknown argument: {args[i]}")
            sys.exit(1)

    start_server(host=host, port=port)


if __name__ == "__main__":
    main()
