#!/usr/bin/env python3
"""Apply the validated MIX present offset to each compositor in this session."""
import base64
import hashlib
import json
import os
from pathlib import Path
import socket
import struct
import time

from common import current


def frame(text):
    payload = text.encode()
    mask = os.urandom(4)
    size = len(payload)
    header = (struct.pack('!BB', 0x81, 0x80 | size) if size < 126 else
              struct.pack('!BBH', 0x81, 0x80 | 126, size))
    return header + mask + bytes(value ^ mask[i % 4] for i, value in enumerate(payload))


def send_offset(identity):
    key = base64.b64encode(os.urandom(16)).decode()
    with socket.create_connection(('127.0.0.1', 27062), timeout=1) as sock:
        sock.settimeout(1)
        sock.sendall(('GET / HTTP/1.1\r\nHost: 127.0.0.1:27062\r\n'
                      'Upgrade: websocket\r\nConnection: Upgrade\r\n'
                      f'Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n'
                      'Origin: http://127.0.0.1:27062\r\n\r\n').encode())
        response = b''
        while b'\r\n\r\n' not in response:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError('WebSocket handshake closed')
            response += chunk
            if len(response) > 16384:
                raise ValueError('WebSocket handshake too large')
        lines = response.split(b'\r\n\r\n', 1)[0].decode().split('\r\n')
        accept = base64.b64encode(hashlib.sha1(
            (key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
        # Header names are case insensitive; the accept value is not.
        accept_value = next((line.split(':', 1)[1].strip() for line in lines[1:]
                             if line.lower().startswith('sec-websocket-accept:')), '')
        if lines[0].split()[1] != '101' or accept_value != accept:
            raise ConnectionError('WebSocket upgrade rejected')
        name = f'a081_present_offset/{os.getpid()}'
        sock.sendall(frame('mailbox_open ' + name))
        sock.sendall(frame('mailbox_send vrcompositor_mailbox ' + json.dumps({
            'type': 'set_present_offset_ms', 'value': -11, 'returnAddress': name})))
        # Give the local mailbox server time to dispatch before closing.
        time.sleep(0.1)
        sock.sendall(frame('websocket_close'))
    print(json.dumps({'phase': 'present_offset_sent', 'compositor': identity,
                      'offset_ms': -11, 'monotonic_ns': time.monotonic_ns()}), flush=True)


def main():
    data = current()
    state = Path(data['directory'])
    cgroup = Path('/proc/self/cgroup').read_text()
    applied = None
    next_send = 0
    while not (state / 'native-stop-requested').exists():
        identities = []
        for proc in Path('/proc').glob('[0-9]*'):
            try:
                if (proc.stat().st_uid == os.getuid() and
                        (proc / 'comm').read_text().strip() == 'vrcompositor' and
                        (proc / 'cgroup').read_text() == cgroup):
                    start = (proc / 'stat').read_text().rsplit(')', 1)[1].split()[19]
                    identities.append((int(proc.name), start))
            except (OSError, IndexError):
                continue
        identity = identities[0] if len(identities) == 1 else None
        now = time.monotonic()
        if identity is not None and (identity != applied or now >= next_send):
            try:
                send_offset(identity)
                applied = identity
                # Reassert after startup mailbox races, and after runtime resets.
                next_send = now + 10
            except (OSError, ValueError, IndexError) as error:
                print(json.dumps({'phase': 'present_offset_retry', 'error': str(error)}), flush=True)
                time.sleep(2)
        time.sleep(1)


if __name__ == '__main__':
    main()
