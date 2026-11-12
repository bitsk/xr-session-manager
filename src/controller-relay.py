#!/usr/bin/env python3
"""Root relay of unchanged real SDK controller packets to the graphics user.

Input is a pipe carrying the 96-byte a081_controller_pose_packet (v1 or v2), never JSON
or a replay file. No timestamps, coordinate profiles or validity flags are
created here. Closing the source closes subscribers and invalidates poses.
"""
import argparse
import json
import os
from pathlib import Path
import pwd
import re
import selectors
import socket
import stat
import struct
import sys
import time
from common import USER, UID, GID, ROOTFS

WIRE = struct.Struct('<4I4Q2IfI7fI')
assert WIRE.size == 96

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--input-fd', type=int, default=0)
    parser.add_argument('--seconds', type=int, default=120)
    parser.add_argument('--record', type=Path, help='Optional new root-private raw packet log')
    args = parser.parse_args()
    directory = args.directory
    if os.geteuid() != 0 or not 0 <= args.seconds <= 3600:
        parser.error('Run as root; duration 0 (continuous) through 3600')
    if (Path('/etc/a081-rootfs-id').read_text().strip() != ROOTFS or
        Path('/proc/device-tree/model').read_bytes().rstrip(b'\0') != b'iQIYI Qiyu MIX (A081)'):
        parser.error('Expected the selected A081 rootfs')
    if directory.parent != Path('/run') or not re.fullmatch(r'a081-controller-pose-[A-Za-z0-9_-]+', directory.name):
        parser.error('Select a new /run/a081-controller-pose-TOKEN directory')
    account = pwd.getpwnam(USER)
    if (account.pw_uid, account.pw_gid) != (UID, GID) or not stat.S_ISFIFO(os.fstat(args.input_fd).st_mode):
        parser.error('Expected the selected graphics account and a live producer pipe; files are not input')
    directory.mkdir(mode=0o755)
    path = directory / 'tracking.sock'
    server = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET | socket.SOCK_CLOEXEC)
    selector = selectors.DefaultSelector()
    clients = set()
    record = None
    packets = dropped_clients = rejected_clients = 0
    pending = bytearray()
    started = time.monotonic()
    cause = 'duration'
    try:
        server.bind(str(path)); os.chown(path, 0, account.pw_gid); os.chmod(path, 0o660)
        server.listen(4); server.setblocking(False)
        selector.register(server, selectors.EVENT_READ, 'accept')
        os.set_blocking(args.input_fd, False)
        selector.register(args.input_fd, selectors.EVENT_READ, 'source')
        if args.record:
            record = open(args.record, 'xb', buffering=0); os.fchmod(record.fileno(), 0o600)
        print(json.dumps({'kind':'controller_relay_ready','socket':str(path),'source':'live_pipe',
                          'rewrites_packets':False,'producer_uid':os.getuid()}), flush=True)
        while not args.seconds or time.monotonic() - started < args.seconds:
            for key, _ in selector.select(.1):
                if key.data == 'accept':
                    client, _ = server.accept(); client.setblocking(False)
                    _, uid, _ = struct.unpack('3i', client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                    if uid != account.pw_uid or len(clients) >= 4:
                        rejected_clients += 1; client.close(); continue
                    clients.add(client); selector.register(client, selectors.EVENT_READ, 'client')
                elif key.data == 'client':
                    # Receivers may not issue producer commands. Any data or EOF
                    # closes only this receiver, without affecting the source.
                    client = key.fileobj
                    if client not in clients: continue
                    selector.unregister(client); clients.remove(client); client.close()
                else:
                    chunk = os.read(args.input_fd, 96 * 128)
                    if not chunk:
                        cause = 'source_eof'
                        if pending: raise ValueError('partial final packet')
                        return
                    pending.extend(chunk)
                    while len(pending) >= WIRE.size:
                        packet = bytes(pending[:WIRE.size]); del pending[:WIRE.size]
                        values = WIRE.unpack(packet)
                        v2 = values[1] == 2
                        if (values[0] != 0x43503841 or values[1] not in (1, 2) or values[2] != 96 or
                            values[3] & ~(127 if v2 else 63) or
                            not values[4] or not values[5] or values[8] > 1 or values[9] > 1 or
                            values[11] & (0xe0f0000f if v2 else ~0) or values[-1] & (0xffff0000 if v2 else ~0)):
                            raise ValueError('producer packet contract mismatch')
                        if record: record.write(packet)
                        packets += 1
                        for client in tuple(clients):
                            try:
                                if client.send(packet) != WIRE.size: raise OSError('short seqpacket')
                            except OSError:
                                selector.unregister(client); clients.remove(client); client.close(); dropped_clients += 1
    except BaseException as error:
        cause = type(error).__name__ + ': ' + str(error)
        raise
    finally:
        for client in clients: client.close()
        selector.close(); server.close()
        if record: record.close()
        path.unlink(missing_ok=True); directory.rmdir()
        print(json.dumps({'kind':'controller_relay_end','cause':cause,'packets':packets,
                          'rejected_clients':rejected_clients,'dropped_clients':dropped_clients,
                          'elapsed_seconds':time.monotonic()-started}), flush=True)

if __name__ == '__main__':
    main()
