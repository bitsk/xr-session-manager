# SPDX-License-Identifier: GPL-3.0-or-later
"""Unprivileged client of the session-owned, credential-checked controls API."""
import json
import socket
import sys


def request(values=None):
    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as client:
        client.settimeout(25)
        client.connect('/run/xr-session/controls.sock')
        client.send(json.dumps({'set': values} if values is not None else {}).encode())
        result = json.loads(client.recv(16384))
        if not result.get('ok'):
            raise RuntimeError(result.get('error', 'XR controls request failed'))
        return result['state']


if __name__ == '__main__':
    if sys.argv[1:] == ['get']:
        print(json.dumps(request()))
    elif len(sys.argv) == 4 and sys.argv[1] == 'set':
        key, value = sys.argv[2:]
        print(json.dumps(request({key: value if key == 'input_mode' else json.loads(value)})))
    else:
        raise SystemExit('xr-session controls get | set FIELD VALUE')
