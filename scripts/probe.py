#!/usr/bin/env python3
"""Run inside a guest to probe a service from a specific test-network IP."""

import argparse
import json
import secrets
import socket
import subprocess


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--family", type=int, choices=[4, 6], required=True)
    parser.add_argument("--protocol", choices=["tcp", "udp"], required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--destination", required=True)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()

    route = subprocess.check_output(
        ["ip", f"-{args.family}", "route", "get", args.destination, "from", args.source],
        text=True,
    ).strip()
    words = route.split()
    dev = words[words.index("dev") + 1] if "dev" in words else None
    via = words[words.index("via") + 1] if "via" in words else None

    payload = secrets.token_bytes(32)
    family = socket.AF_INET if args.family == 4 else socket.AF_INET6
    protocol = socket.SOCK_STREAM if args.protocol == "tcp" else socket.SOCK_DGRAM
    received = b""
    error = None
    try:
        with socket.socket(family, protocol) as connection:
            connection.settimeout(2)
            connection.bind((args.source, 0))
            if args.protocol == "tcp":
                connection.connect((args.destination, args.port))
                connection.sendall(payload)
                received = connection.recv(len(payload))
            else:
                connection.sendto(payload, (args.destination, args.port))
                received, _ = connection.recvfrom(4096)
    except (OSError, TimeoutError) as exc:
        error = str(exc)

    print(json.dumps({
        "route": route,
        "dev": dev,
        "via": via,
        "reply": received == payload,
        "error": error,
    }))


if __name__ == "__main__":
    main()
