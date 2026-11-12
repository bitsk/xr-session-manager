# SPDX-License-Identifier: GPL-3.0-or-later
"""Optional capture readiness from the existing root relay, never a pose source."""
import json
import math
import os
from pathlib import Path
import re
import select
import socket
import stat
import struct
import time

# Production xr-desktop/system/tracking/a081_tracking_wire.h, little-endian v2.
# The separately frozen qiyu-head-tracking v1 header is not this deployed ABI.
WIRE = struct.Struct('<4I8Q14f8I18f')
assert WIRE.size == 240
MAGIC, VERSION, KNOWN_FLAGS, CAMERA_SUBMITTED = 0x54523841, 2, 15, 8
MAX_AGE_NS = 100_000_000
TIMEOUT_SECONDS = 60


def _connect(path):
    """Connect only to this trial's immutable root-owned relay directory."""
    try:
        run = Path('/run').lstat()
        parent = path.parent.lstat()
        endpoint = path.lstat()
    except FileNotFoundError:
        return None
    for info in (run, parent):
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise RuntimeError('Untrusted first-packet relay directory')
    if not stat.S_ISSOCK(endpoint.st_mode) or endpoint.st_uid != 0 or endpoint.st_mode & 0o007:
        raise RuntimeError('Untrusted first-packet relay socket')
    stream = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET | socket.SOCK_NONBLOCK)
    try:
        stream.connect(str(path))
        peer = struct.unpack('3i', stream.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if peer[1] != 0:
            raise RuntimeError('First-packet relay peer must be root')
        current = path.lstat()
        current_parent = path.parent.lstat()
        if ((current.st_dev, current.st_ino) != (endpoint.st_dev, endpoint.st_ino) or
                (current_parent.st_dev, current_parent.st_ino) != (parent.st_dev, parent.st_ino)):
            raise RuntimeError('First-packet relay identity changed during connection')
        return stream, {'pid': peer[0], 'uid': peer[1], 'gid': peer[2]}
    except (FileNotFoundError, ConnectionRefusedError, BlockingIOError):
        stream.close()
        return None
    except BaseException:
        stream.close()
        raise


def _packet_evidence(payload, values, received_ns):
    quality = values[25]
    prediction_names = ('angular_s', 'angular_b', 'angular_bdt', 'angular_bdt2',
                        'translation_s', 'translation_b')
    return {'flags': values[3], 'session_id': values[4], 'sequence': values[5],
            'sample_monotonic_ns': values[8], 'send_monotonic_ns': values[9],
            'received_monotonic_ns': received_ns, 'age_ns': received_ns - values[9],
            'pose_boottime_ns': values[10], 'last_camera_boottime_ns': values[11],
            'pose_quality': quality if math.isfinite(quality) else str(quality),
            'pose_quality_wire_hex': payload[132:136].hex(),
            'tracking_state': values[26], 'warning_flags': values[27],
            'pose_index': values[28], 'clock_bracket_ns': values[29],
            'pose_read_status': values[30], 'reserved': list(values[31:34]),
            'prediction_coefficients': {
                name: [value if math.isfinite(value) else str(value)
                       for value in values[34 + index * 3:37 + index * 3]]
                for index, name in enumerate(prediction_names)},
            'prediction_wire_hex': payload[168:240].hex(),
            'wire_hex': payload.hex()}


def wait_for_first_tracking_packet(data, server, graphics_uid):
    """Wait for fresh CAMERA_SUBMITTED evidence, without gating pose validity."""
    if os.geteuid() != graphics_uid:
        raise RuntimeError('First-packet subscriber must use the selected graphics UID')
    path = Path(data['public_socket'])
    if (path.parent.parent != Path('/run') or path.name != 'tracking.sock' or
            not re.fullmatch('a081-qvr-pose-[A-Za-z0-9_-]+', path.parent.name)):
        raise RuntimeError('First-packet endpoint must match the session public relay')
    state = Path(data['directory'])
    output = state / 'tracking-first-packet.json'
    evidence = {'format': 'qiyu-tracking-first-packet-v1', 'trial': data['trial'],
                'wire_version': VERSION, 'wire_bytes': WIRE.size,
                'status': 'waiting', 'socket': str(path), 'subscriber_uid': os.geteuid(),
                'timeout_seconds': TIMEOUT_SECONDS, 'max_age_ns': MAX_AGE_NS,
                'pose_validity_gate_applied': False, 'received_packets': 0, 'rejected': {},
                'started_monotonic_ns': time.monotonic_ns()}
    output.write_text(json.dumps(evidence, indent=2) + '\n')
    deadline = time.monotonic() + TIMEOUT_SECONDS
    stream = None

    def lifecycle():
        if (state / 'native-stop-requested').exists():
            evidence['status'] = 'cancelled'
            raise RuntimeError('Native stop requested before first tracking packet')
        if Path(data['producer_done']).exists():
            raise RuntimeError('Real producer ended before first tracking packet')
        if server.poll() is not None:
            raise RuntimeError('Native server exited before first tracking packet')

    try:
        while True:
            lifecycle()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('No fresh camera-submitted tracking packet within 60 seconds')
            if stream is None:
                connection = _connect(path)
                if connection is None:
                    # Retry only endpoint publication; no fixed startup delay
                    # substitutes for the real packet condition below.
                    select.select([], [], [], min(0.05, remaining))
                    continue
                stream, evidence['peer'] = connection
            readable, _, _ = select.select([stream], [], [], min(0.05, remaining))
            if not readable:
                continue
            payload, _, message_flags, _ = stream.recvmsg(WIRE.size)
            if not payload:
                raise RuntimeError('Real tracking relay closed before first tracking packet')
            evidence['received_packets'] += 1
            reason = None
            if len(payload) != WIRE.size or message_flags & socket.MSG_TRUNC:
                reason = 'packet_size'
            else:
                values = WIRE.unpack(payload)
                now_ns = time.monotonic_ns()
                evidence['last_packet'] = _packet_evidence(payload, values, now_ns)
                if values[:3] != (MAGIC, VERSION, WIRE.size):
                    reason = 'packet_header'
                elif not values[4] or not values[5]:
                    reason = 'session_or_sequence'
                elif values[3] & ~KNOWN_FLAGS:
                    reason = 'unknown_flags'
                elif not 0 <= now_ns - values[9] <= MAX_AGE_NS:
                    reason = 'send_timestamp_not_fresh'
                elif not values[3] & CAMERA_SUBMITTED:
                    reason = 'camera_not_submitted'
            if reason is not None:
                evidence['rejected'][reason] = evidence['rejected'].get(reason, 0) + 1
                continue
            lifecycle()
            evidence.update(status='ready', packet=evidence.pop('last_packet'))
            return evidence
    except BaseException as error:
        if evidence['status'] != 'cancelled':
            evidence['status'] = 'failed'
        evidence['error'] = repr(error)
        raise
    finally:
        if stream is not None:
            stream.close()
        evidence['completed_monotonic_ns'] = time.monotonic_ns()
        output.write_text(json.dumps(evidence, indent=2) + '\n')
