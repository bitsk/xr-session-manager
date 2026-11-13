#!/usr/bin/python3
"""Bounded root-to-graphics relay of unchanged real 240-byte QVR packets."""
import argparse
import json
import os
from pathlib import Path
import pwd
import select
import signal
import socket
import stat
import struct
import time
from common import USER, UID, GID, ROOTFS

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--source', required=True, type=Path)
p.add_argument('--output', required=True, type=Path)
p.add_argument('--seconds', type=int, default=110)
a = p.parse_args()
assert os.geteuid() == 0 and os.uname().machine == 'aarch64'
assert Path('/etc/a081-rootfs-id').read_text().strip() == ROOTFS
assert b'A081' in Path('/proc/device-tree/model').read_bytes()
assert 0 <= a.seconds <= 450
account = pwd.getpwnam(USER)
assert (account.pw_uid, account.pw_gid) == (UID, GID)
assert a.source.is_absolute() and a.output.is_absolute()
assert len(str(a.source)) < 108 and len(str(a.output)) < 108
assert a.output.parent.parent == Path('/run') and a.output.parent.name.startswith('a081-qvr-pose-')
directory = a.output.parent
directory.mkdir(mode=0o750)
os.chown(directory, 0, account.pw_gid)
assert not directory.is_symlink() and not a.output.exists()
st = directory.stat()
assert st.st_uid == 0 and not st.st_mode & 0o022
running = True
def stop(_signum, _frame):
    global running
    running = False
signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)

listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET | socket.SOCK_NONBLOCK)
source = None
socket_identity = None
peers = []
received = delivered = rejected = disconnected = 0
deadline = time.monotonic() + a.seconds
try:
    listener.bind(str(a.output))
    st = a.output.lstat()
    socket_identity = (st.st_dev, st.st_ino)
    os.chown(a.output, 0, account.pw_gid)
    os.chmod(a.output, 0o660)
    listener.listen(4)
    print(json.dumps({'relay': 'ready', 'output': str(a.output), 'client_uid': account.pw_uid,
                      'unchanged_wire_bytes': 240, 'seconds': a.seconds}), flush=True)
    while running and (a.seconds == 0 or time.monotonic() < deadline):
        if source is None:
            if not a.source.exists():
                time.sleep(.02)
                continue
            st = a.source.lstat()
            assert stat.S_ISSOCK(st.st_mode) and st.st_uid == 0 and not st.st_mode & 0o077
            source = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET | socket.SOCK_NONBLOCK)
            source.connect(str(a.source))
            pid, uid, gid = struct.unpack('3i', source.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            assert uid == 0
        ready, _, _ = select.select([source, listener], [], [], .05)
        if listener in ready:
            for _ in range(4):
                try:
                    peer, _ = listener.accept()
                except BlockingIOError:
                    break
                peer.setblocking(False)
                pid, uid, gid = struct.unpack('3i', peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                # The root session controls consume the same IMU stream for
                # double tap. They must not open a second sensor producer.
                if uid not in (0, account.pw_uid) or len(peers) >= 4:
                    rejected += 1
                    peer.close()
                else:
                    peers.append(peer)
        if source not in ready:
            continue
        for _ in range(256):
            try:
                packet, _, flags, _ = source.recvmsg(240)
            except BlockingIOError:
                break
            if not packet:
                running = False
                break
            assert len(packet) == 240 and not flags & socket.MSG_TRUNC
            assert struct.unpack_from('<3I', packet) == (0x54523841, 2, 240)
            received += 1
            # A blocked reader is disconnected; never queue old poses or change
            # a timestamp/quality field to make a packet look fresh or valid.
            for peer in peers[:]:
                try:
                    if peer.send(packet, socket.MSG_DONTWAIT | socket.MSG_NOSIGNAL) != 240:
                        raise OSError('partial seqpacket')
                    delivered += 1
                except OSError:
                    disconnected += 1
                    peer.close()
                    peers.remove(peer)
finally:
    if source is not None:
        source.close()
    for peer in peers:
        peer.close()
    listener.close()
    if a.output.exists():
        st = a.output.lstat()
        if socket_identity == (st.st_dev, st.st_ino):
            a.output.unlink()
    directory.rmdir()
    print(json.dumps({'relay': 'exit', 'received': received, 'delivered': delivered,
                      'rejected': rejected, 'slow_or_closed_readers': disconnected}), flush=True)
