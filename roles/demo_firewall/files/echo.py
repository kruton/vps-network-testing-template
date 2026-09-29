#!/usr/bin/env python3
"""Dual-stack echo service with actual listeners for firewall testing."""

import argparse
import selectors
import socket


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tcp", type=int, action="append", default=[])
    parser.add_argument("--udp", type=int, action="append", default=[])
    args = parser.parse_args()
    selector = selectors.DefaultSelector()

    for protocol, ports in ((socket.SOCK_STREAM, args.tcp), (socket.SOCK_DGRAM, args.udp)):
        for port in ports:
            server = socket.socket(socket.AF_INET6, protocol)
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            server.bind(("::", port))
            if protocol == socket.SOCK_STREAM:
                server.listen()
            selector.register(server, selectors.EVENT_READ, protocol)

    while True:
        for key, _ in selector.select():
            server = key.fileobj
            if key.data == socket.SOCK_STREAM:
                connection, _ = server.accept()
                with connection:
                    connection.settimeout(3)
                    payload = connection.recv(4096)
                    if payload:
                        connection.sendall(payload)
            else:
                payload, peer = server.recvfrom(4096)
                server.sendto(payload, peer)


if __name__ == "__main__":
    main()
