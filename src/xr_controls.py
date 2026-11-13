# SPDX-License-Identifier: GPL-3.0-or-later
"""Session-owned input preferences and optional IMU double-tap detection.

Consumes the existing head relay; never opens an IMU or camera device.
"""
import json
import math
import os
from pathlib import Path
import select
import socket
import stat
import struct
import threading
import time

RUNTIME = Path('/run/xr-session')
PREFERENCES = Path('/etc/xr/session-controls.json')
DEFAULTS = dict(input_mode='auto', hand_tracking_enabled=True,
                passthrough_enabled=False, double_tap_enabled=False)


def atomic_json(path, value, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + str(threading.get_ident()) + '.tmp')
    with temp.open('w') as out:
        os.fchmod(out.fileno(), mode)
        json.dump(value, out, separators=(',', ':'))
        out.write('\n')
    temp.replace(path)


class DoubleTap:
    """Two bounded acceleration impulses with decay, motion veto and cooldown.

    Thresholds are provisional until verified on a worn device. This option
    defaults off. Turning the head or sustained shaking must not form a tap.
    """
    def __init__(self):
        self.reset()

    def reset(self):
        self.gravity = None
        self.previous = 0
        self.low_since = 0
        self.pulse = 0
        self.first = 0
        self.cooldown = 0
        self.peak = 0.

    def sample(self, stamp, accel, gyro):
        if not all(math.isfinite(x) for x in (*accel, *gyro)):
            self.reset()
            return False
        if stamp == self.previous:
            return False
        if self.previous and not 0 < stamp - self.previous < 50_000_000:
            self.reset()
        dt = (stamp - self.previous) / 1e9 if self.previous else 0
        self.previous = stamp
        if self.gravity is None:
            self.gravity = list(accel)
            self.low_since = stamp
            return False
        alpha = dt / (.08 + dt)
        for i in range(3):
            self.gravity[i] += alpha * (accel[i] - self.gravity[i])
        high = math.sqrt(sum((accel[i] - self.gravity[i]) ** 2 for i in range(3)))
        rotation = math.sqrt(sum(x*x for x in gyro))
        if rotation > 4 or high > 70 or stamp < self.cooldown:
            self.first = self.pulse = self.low_since = 0
            return False
        if self.first and stamp - self.first > 450_000_000:
            self.first = 0
        if high < 2.5:
            if self.pulse:
                duration = stamp - self.pulse
                pulse = self.pulse
                self.pulse = 0
                if 2_000_000 <= duration <= 80_000_000 and self.peak >= 7:
                    if self.first and 100_000_000 <= pulse - self.first <= 450_000_000:
                        self.first = 0
                        self.cooldown = stamp + 1_200_000_000
                        self.low_since = stamp
                        return True
                    self.first = pulse
            if not self.low_since:
                self.low_since = stamp
        elif high >= 7:
            if not self.pulse and self.low_since and stamp - self.low_since >= 30_000_000:
                self.pulse = stamp
                self.peak = high
            self.peak = max(self.peak, high)
            self.low_since = 0
        if self.pulse and stamp - self.pulse > 80_000_000:
            self.first = self.pulse = 0
        return False


class Controls:
    def __init__(self, profile, session, hands):
        self.profile, self.session, self.hands = profile, session, hands
        self.uid = profile.get('uid', 1000)
        self.preferences = dict(DEFAULTS)
        if PREFERENCES.exists():
            info = PREFERENCES.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                raise RuntimeError('Unsafe XR preferences')
            saved = json.loads(PREFERENCES.read_text())
            self.preferences.update(self.validate(saved))
        self.available_hands = bool(hands.enabled)
        config = profile.get('scene', {}).get('environment', {}).get('XR_PASSTHROUGH_CONFIG')
        self.available_passthrough = bool(config and Path(config).is_file())
        if not self.available_hands:
            self.preferences['hand_tracking_enabled'] = False
        if not self.available_passthrough:
            self.preferences.update(passthrough_enabled=False, double_tap_enabled=False)
        if not self.preferences['hand_tracking_enabled'] and self.preferences['input_mode'] == 'hands':
            self.preferences['input_mode'] = 'controllers'
        self.revision = 1
        self.last_error = None
        self.stopped = threading.Event()
        self.tap = DoubleTap()
        self.head = None
        self.last_imu_ns = 0
        self.imu_samples = 0
        self.retry = 0
        self.events = []
        RUNTIME.mkdir(mode=0o755, exist_ok=True)
        info = RUNTIME.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise RuntimeError('Unsafe XR controls directory')
        self.path = RUNTIME / 'controls.sock'
        if self.path.exists():
            # Only an orphaned root socket may be reclaimed.
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            try:
                probe.connect(str(self.path))
            except ConnectionRefusedError:
                if self.path.lstat().st_uid != 0:
                    raise RuntimeError('Untrusted control endpoint')
                self.path.unlink()
            else:
                raise RuntimeError('XR controls already owned')
            finally:
                probe.close()
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET | socket.SOCK_CLOEXEC)
        self.server.bind(str(self.path))
        os.chown(self.path, 0, profile.get('gid', 1000))
        os.chmod(self.path, 0o660)
        self.server.listen(4)
        self.server.setblocking(False)
        self.publish()
        self.thread = threading.Thread(target=self.run, name='xr-controls', daemon=True)

    @staticmethod
    def validate(values):
        if not isinstance(values, dict) or set(values) - set(DEFAULTS):
            raise ValueError('Unknown XR control')
        for key, value in values.items():
            if key == 'input_mode':
                if value not in ('auto', 'controllers', 'hands'):
                    raise ValueError('Input mode must be auto, controllers or hands')
            elif type(value) is not bool:
                raise ValueError('XR switches require boolean values')
        return values

    def snapshot(self):
        return dict(schema=1, revision=self.revision, session_active=not self.stopped.is_set(),
                    preferences=self.preferences, capabilities=dict(hand_tracking=self.available_hands,
                    passthrough=self.available_passthrough, double_tap=self.available_passthrough),
                    hand_service_started=self.hands.started, last_error=self.last_error,
                    double_tap_ready=self.head is not None and
                    0 <= time.monotonic_ns()-self.last_imu_ns < 100_000_000,
                    double_tap_imu_samples=self.imu_samples,
                    double_tap_experimental=True, double_tap_events=self.events[-8:])

    def publish(self):
        atomic_json(RUNTIME / 'controls.json', self.snapshot())

    def start(self):
        self.publish()
        self.thread.start()

    def apply(self, changes, reason='control-center'):
        changes = self.validate(changes)
        values = {**self.preferences, **changes}
        if values['input_mode'] == 'hands' and changes.get('hand_tracking_enabled') is not False:
            values['hand_tracking_enabled'] = True
        if not values['hand_tracking_enabled'] and values['input_mode'] == 'hands':
            values['input_mode'] = 'controllers'
        if values['hand_tracking_enabled'] and not self.available_hands:
            raise RuntimeError('Hand tracking is not installed in this session profile')
        if (values['passthrough_enabled'] or values['double_tap_enabled']) and not self.available_passthrough:
            raise RuntimeError('Passthrough is unavailable in this session profile')
        self.preferences = values
        self.revision += 1
        self.last_error = None
        # Publish first: the runtime neutralizes input before DSP teardown.
        self.publish()
        try:
            if values['hand_tracking_enabled'] and not self.hands.started:
                self.hands.start()
            elif not values['hand_tracking_enabled'] and self.hands.started:
                result = self.hands.stop()
                # A previous DSP failure stays in the trial result; the
                # disable command succeeds when this stop actually completed.
                if result.get('stop_exit', 0) != 0:
                    raise RuntimeError('Hand service shutdown failed')
            atomic_json(PREFERENCES, values, 0o600)
            if reason != 'double-tap':
                self.tap.reset()
        except Exception as error:
            self.last_error = str(error)
            self.preferences['hand_tracking_enabled'] = False
            if self.preferences['input_mode'] == 'hands':
                self.preferences['input_mode'] = 'controllers'
            raise
        finally:
            self.publish()
        print(json.dumps(dict(event='xr_controls_changed', reason=reason, **self.snapshot())), flush=True)
        return self.snapshot()

    def connect_head(self):
        if self.head or time.monotonic() < self.retry:
            return
        self.retry = time.monotonic() + 1
        client=None
        try:
            client = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET | socket.SOCK_CLOEXEC)
            client.settimeout(.1)
            client.connect(self.session['public_socket'])
            peer = struct.unpack('3i', client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if peer[1] != 0:
                raise RuntimeError('Head relay must be root-owned')
            client.setblocking(False)
            self.head = client
        except (OSError, RuntimeError):
            if client is not None:client.close()

    def head_packet(self):
        packet = self.head.recv(241)
        if len(packet) != 240:
            raise OSError('Head stream disconnected')
        h = struct.unpack_from('<4I8Q6f', packet)
        now = time.monotonic_ns()
        if h[:3] != (0x54523841, 2, 240) or h[3] & 3 != 3 or not 0 <= now-h[8] < 100_000_000:
            self.tap.reset()
            return
        first = not self.last_imu_ns
        self.last_imu_ns = h[8]
        self.imu_samples += 1
        if first:
            self.publish()
        if self.preferences['double_tap_enabled'] and self.tap.sample(h[8], h[15:18], h[12:15]):
            self.events.append(dict(monotonic_ns=now, event='double-tap'))
            self.apply({'passthrough_enabled': not self.preferences['passthrough_enabled']}, 'double-tap')

    def run(self):
        while not self.stopped.is_set():
            if self.hands.started and not self.hands.result.get('pass',True) and not self.hands.transitioning:
                self.last_error=self.hands.result.get('error','Hand tracking service failed')
                self.preferences['hand_tracking_enabled']=False
                if self.preferences['input_mode']=='hands':self.preferences['input_mode']='controllers'
                self.revision+=1
                self.publish()
                self.hands.stop()
                self.publish()
            self.connect_head()
            readable, _, _ = select.select([self.server] + ([self.head] if self.head else []), [], [], .05)
            if self.head in readable:
                try:
                    for _ in range(64):
                        self.head_packet()
                except BlockingIOError:
                    pass
                except (RuntimeError, ValueError) as error:
                    self.last_error = str(error)
                    self.tap.reset()
                    self.publish()
                except OSError:
                    self.head.close()
                    self.head = None
                    self.last_imu_ns = 0
                    self.tap.reset()
                    self.publish()
            if self.server in readable:
                client, _ = self.server.accept()
                with client:
                    client.settimeout(1)
                    try:
                        peer = struct.unpack('3i', client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                        if peer[1] not in (0, self.uid):
                            raise PermissionError('Only the active XR account may change controls')
                        request = json.loads(client.recv(2049))
                        result = self.apply(request['set']) if 'set' in request else self.snapshot()
                        client.send(json.dumps(dict(ok=True, state=result)).encode())
                    except Exception as error:
                        try:
                            client.send(json.dumps(dict(ok=False, error=str(error))).encode())
                        except OSError:
                            pass

    def close(self):
        self.stopped.set()
        self.publish()
        if self.thread.is_alive():
            self.thread.join(timeout=25)
        self.server.close()
        if self.head:
            self.head.close()
        self.path.unlink(missing_ok=True)
