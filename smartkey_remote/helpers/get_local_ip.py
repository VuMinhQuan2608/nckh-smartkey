#!/usr/bin/env python3
"""In IP LAN ra stdout — dùng test nhanh trên Pi."""
import socket


def main():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.3)
        s.connect(("8.8.8.8", 80))
        print(s.getsockname()[0])
        s.close()
    except Exception as e:
        print(f"error: {e}")


if __name__ == "__main__":
    main()
