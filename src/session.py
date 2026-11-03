#!/usr/bin/env python3
"""Continuous native VR UI candidate. Root owns lifecycle; graphics use UID 1000."""
import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import runpy
import subprocess
import time
import uuid
import re
from controller_session import read_plan
from desktop_owner import stop_desktop, restore_desktop
from common import CURRENT, DESKTOP, PREFIX, UNIT, VIEWER, TRACKING_UNIT, current, selected_driver

UNIT_PATH = Path('/run/systemd/system') / UNIT
HEADER = '# Managed by isolated mix-vr-shell\n'


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def active(unit):
    return subprocess.run(['systemctl', 'is-active', '--quiet', unit]).returncode == 0


def save(data):
    CURRENT.parent.mkdir(mode=0o755, exist_ok=True)
    temporary = CURRENT.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, indent=2) + '\n')
    temporary.chmod(0o644)
    temporary.replace(CURRENT)


def cleanup(trial):
    data = current()
    if data['trial'] != trial or data.get('cleanup_complete'):
        return
    errors = []
    runtime_errors = []
    validation_errors = []
    # Normal shutdown stopped the viewer before requesting native VR exit.
    # ExecStop is skipped when startup itself failed; preserve that distinction.
    if not data.get('shutdown_requested'):
        data['viewer_restore'] = active(VIEWER) or data.get('viewer_restore', False) or data['viewer_before']
        save(data)
        result = subprocess.run(['systemctl', 'stop', VIEWER])
        data['startup_failure_viewer_stop_ns'] = time.monotonic_ns()
        if result.returncode:
            errors.append('viewer_stop_after_startup_failure')
    if subprocess.run(['systemctl', 'stop', TRACKING_UNIT]).returncode:
        errors.append('tracking_stop')
    tracking_result = Path(data['tracking_result'])
    if tracking_result.exists():
        data['tracking_result_data'] = json.loads(tracking_result.read_text())
        if not data['tracking_result_data'].get('pass'):
            runtime_errors.append('tracking_capture')
        errors.extend('tracking_' + str(error) for error in data['tracking_result_data'].get('cleanup_errors', []))
    else:
        errors.append('tracking_result_missing')
    try:
        snapshot = runpy.run_path('/opt/mix-steamvr/bin/cleanup')['snapshot']
        data['first_exit'] = str(snapshot(trial, 'first-exit'))
    except Exception as error:
        errors.append('first_exit: ' + str(error))
    probe_log = Path(data['directory']) / 'logs/pose-watch-stdout.log'
    data['pose_watch_result'] = [line for line in probe_log.read_text().splitlines()
                                if line.startswith('A081_QVR_OPENVR_RESULT')] if probe_log.exists() else []
    if not data['pose_watch_result'] or not data['pose_watch_result'][-1].endswith('pass=1'):
        validation_errors.append('pose_watch')
    retention = Path(str(probe_log) + '.retention.json')
    data['pose_watch_retention'] = json.loads(retention.read_text()) if retention.exists() else None
    # Native observer exit8 is its explicit acceptance-gate failure. A
    # completed pass=0 report with drained logs is not a cleanup failure.
    observer_exit = (data['pose_watch_retention'] or {}).get('exit')
    observer_validation_only = (observer_exit == 8 and bool(data['pose_watch_result']) and
                                data['pose_watch_result'][-1].endswith('pass=0'))
    if observer_exit != 0 and not observer_validation_only:
        errors.append('pose_watch_log_drain')
    result = subprocess.run(['python3', '/opt/mix-steamvr/bin/iio-access', 'recover'])
    if result.returncode:
        errors.append('iio_recover')
    # This candidate always returns to the proven maintenance desktop. Keep
    # shutdown/reboot free of a new display startup.
    shutting_down = subprocess.run(['systemctl', 'is-system-running'], capture_output=True,
                                    text=True).stdout.strip() == 'stopping'
    if not shutting_down:
        try:
            data.update(restore_desktop())
        except Exception as error:
            errors.append('desktop_restore: ' + repr(error))
        if (data.get('viewer_restore', False) or data['viewer_before']) and subprocess.run(
                ['systemctl', 'start', VIEWER]).returncode:
            errors.append('viewer_restore')
    data.update(cleanup_complete=True, cleanup_errors=errors, cleanup_pass=not errors,
                runtime_errors=runtime_errors, runtime_pass=not runtime_errors,
                validation_errors=validation_errors, validation_pass=not validation_errors and not runtime_errors)
    save(data)
    print(json.dumps(data), flush=True)
    if errors:
        raise RuntimeError('Cleanup needs inspection; preserve first-exit evidence')
    if runtime_errors:
        raise RuntimeError('Tracking runtime failed; cleanup result recorded separately')


def shutdown():
    data = current()
    # Boot can start the viewer after viewer_before was sampled. Preserve its
    # actual running state before stopping capture for the first-exit snapshot.
    data['viewer_restore'] = active(VIEWER) or data.get('viewer_restore', False) or data['viewer_before']
    save(data)
    run('systemctl', 'stop', VIEWER)
    data['viewer_stopped_before_vr_shutdown_ns'] = time.monotonic_ns()
    data['shutdown_requested'] = True
    (Path(data['directory']) / 'native-stop-requested').write_text(str(time.monotonic_ns()) + '\n')
    save(data)
    # End the real producer, close the relay, then observe SteamVR invalidation
    # before native graphics shutdown. No IIO orientation/position fallback.
    run('systemctl', 'stop', TRACKING_UNIT, timeout=105)
    probe_log = Path(data['directory']) / 'logs/pose-watch-stdout.log'
    retention = Path(str(probe_log) + '.retention.json')
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        if (probe_log.exists() and 'A081_QVR_OPENVR_RESULT' in probe_log.read_text() and retention.exists()
                and json.loads(retention.read_text()).get('exit') in (0, 8)):
            break
        time.sleep(0.1)
    run('runuser', '-u', 'steamos', '--', 'python3', str(PREFIX / 'launch.py'), 'shutdown')


def start(seconds, gain, driver_directory=None, driver_sha256=None, controller_plan=None, controller_pose_socket=None, head_public_socket=None):
    if active(UNIT):
        raise RuntimeError('VR shell already running')
    if UNIT_PATH.exists() and not UNIT_PATH.read_text().startswith(HEADER):
        raise RuntimeError('Unrelated unit already exists')
    if (active('a081-mix-vr-shell.service') or active(TRACKING_UNIT) or
        active('a081-mix-vr-shell-qvr-live.service') or active('a081-mix-vr-shell-qvr-live-tracking.service')):
        raise RuntimeError('Stop the current IIO VR or QVR tracking session first')
    run('sha256sum', '--quiet', '-c', 'SHA256SUMS', cwd=PREFIX)
    for process in ('vrserver', 'vrcompositor', 'gamescope'):
        if subprocess.run(['pgrep', '-x', process], stdout=subprocess.DEVNULL).returncode == 0:
            raise RuntimeError('Another VR session exists: ' + process)
    for path in ('/opt/steamvr/bin/linuxarm64/vrserver', '/opt/steamvr/bin/linuxarm64/vrstartup',
                 '/opt/steamvr/bin/linuxarm64/vrdashboard',
                 '/opt/mix-steamvr/drivers/monado/bin/linuxarm64/driver_monado.so',
                 '/opt/mix-steamvr-turnip/share/vulkan/icd.d/freedreno_icd.aarch64.json',
                 '/opt/mix-steamvr/bin/iio-access', '/opt/mix-steamvr/bin/cleanup',
                 '/usr/bin/gamescope', '/usr/bin/Xwayland', '/usr/bin/xset'):
        if not Path(path).is_file():
            raise RuntimeError('Missing dependency: ' + path)
    run('runuser', '-u', 'steamos', '--', '/usr/bin/gamescope', '--allow-deferred-backend',
        '--vr-session-manager', '--help', stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    cards = {p.name.split('-')[0] for p in Path('/sys/class/drm').glob('card[0-9]*-*')
             if (p / 'status').exists() and (p / 'status').read_text().strip() == 'connected'}
    if len(cards) != 1:
        raise RuntimeError('Expected exactly one connected panel KMS card')
    trial = 'vr-shell-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:6]
    if bool(driver_directory) != bool(driver_sha256):
        raise ValueError('External driver requires its exact SHA256')
    plan, plan_sha = read_plan(controller_plan, seconds) if controller_plan else (None, None)
    if controller_pose_socket and not re.fullmatch(r'/run/a081-controller-pose-[A-Za-z0-9_-]+/tracking.sock', controller_pose_socket):
        raise ValueError('Unexpected controller pose socket')
    if controller_pose_socket and not controller_plan:
        raise ValueError('Controller socket requires the owned SDK lifecycle plan')
    if head_public_socket and not re.fullmatch(r'/run/a081-qvr-pose-[A-Za-z0-9_-]+/tracking.sock', head_public_socket):
        raise ValueError('Unexpected HMD public socket')
    driver = selected_driver(driver_directory, driver_sha256)
    directory = Path('/home/steamos/.local/state/mix-vr-shell-controller') / trial
    data = {'trial': trial, 'directory': str(directory), 'unit': UNIT,
            'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
            'seconds': seconds, 'gain_v4l2': gain, 'tracking': 'qvr-pose', 'kms_device': '/dev/dri/' + cards.pop(),
            'driver': driver,
            'desktop_before': active(DESKTOP), 'viewer_before': active(VIEWER),
            'cleanup_complete': False}
    raw = PREFIX / 'tracking/runs' / trial
    raw.mkdir(mode=0o700, parents=True)
    done = CURRENT.parent / (trial + '-producer-done')
    data.update(raw=str(raw), raw_socket=str(raw / 'tracking.sock'),
                public_socket='/run/a081-qvr-pose-' + uuid.uuid4().hex[:8] + '/tracking.sock',
                producer_done=str(done), tracking_result=str(raw / 'tracking-result.json'))
    if head_public_socket:
        data['public_socket'] = head_public_socket
    if controller_plan:
        data.update(controller_plan=str(controller_plan), controller_plan_sha256=plan_sha,
                    controller_pose_socket=controller_pose_socket or '/run/a081-controller-pose-' + uuid.uuid4().hex[:8] + '/tracking.sock')
    for name in ('public_socket', 'controller_pose_socket'):
        if name in data and Path(data[name]).parent.exists():
            raise ValueError('Choose a new private socket directory: ' + data[name])
    # Build only this new private trial; the existing SteamVR registry and
    # tested IIO settings remain untouched.
    directory.mkdir(parents=True, mode=0o700)
    for parent in (directory.parent, directory):
        os.chown(parent, 1000, 1000)
    for name in ('config', 'logs'):
        (directory / name).mkdir(mode=0o700)
    settings = json.loads(Path('/opt/mix-steamvr/settings.json').read_text())
    settings['steamvr'].update(startMonitorFromAppLaunch=False, startCompositorFromAppLaunch=False,
                               startDashboardFromAppLaunch=True, startOverlayAppsFromDashboard=True)
    settings['dashboard'] = {'enableDashboard': True, 'allowSteamOverlays': True, 'allowVRGamepadUI': True,
                             'allowVRGamepadUIViaGamescope': True, 'expectSteam': True}
    settings.setdefault('power', {}).update(turnOffScreensTimeout=1800.0, pauseCompositorOnStandby=False)
    (directory / 'config/steamvr.vrsettings').write_text(json.dumps(settings, indent=2) + '\n')
    paths = {'version': 1, 'jsonid': 'vrpathreg', 'runtime': ['/opt/steamvr'],
             'config': [str(directory / 'config')], 'log': [str(directory / 'logs')],
             'external_drivers': [driver['directory']]}
    (directory / 'openvrpaths.vrpath').write_text(json.dumps(paths, indent=2) + '\n')
    for path in directory.rglob('*'):
        os.chown(path, 1000, 1000)
    save(data)
    owned_unit_started = False
    try:
        data.update(stop_desktop())
        save(data)
        runpy.run_path('/opt/mix-steamvr/bin/cleanup')['snapshot'](trial, 'desktop-first-exit')
        deadline = time.monotonic() + 5
        while True:
            # systemd can finish a desktop stop before the exiting Steam PID
            # is reaped. Wait for live processes; zombies cannot own graphics.
            rows = subprocess.check_output(['ps', '-u', '1000', '-o', 'stat=,comm='], text=True)
            live_steam = any(row.split()[1:] == ['steam'] and not row.split()[0].startswith('Z')
                             for row in rows.splitlines() if row.split())
            if not live_steam:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError('Steam remains outside the stopped desktop; leave it untouched')
            time.sleep(0.1)
        run('python3', '/opt/mix-steamvr/bin/iio-access', 'prepare')
        UNIT_PATH.write_text(HEADER + f'''[Unit]
Description=Native SteamVR and Steam VR UI on MIX (isolated candidate)
After=systemd-user-sessions.service systemd-logind.service a081-sensors-files.service
Requires=systemd-logind.service a081-sensors-files.service
Conflicts=getty@tty7.service

[Service]
Type=simple
User=steamos
Group=steamos
PAMName=login
WorkingDirectory=/home/steamos
Environment=HOME=/home/steamos
Environment=XDG_SESSION_TYPE=wayland
Environment=XDG_SESSION_CLASS=user
Environment=XDG_SEAT=seat0
Environment=XDG_VTNR=7
ExecStartPre=+/usr/bin/chvt 7
ExecStart=/usr/bin/python3 {PREFIX}/launch.py start
ExecStop=+/usr/bin/python3 {PREFIX}/session.py shutdown
ExecStopPost=+/usr/bin/python3 {PREFIX}/session.py cleanup --trial {trial}
StandardInput=tty
TTYPath=/dev/tty7
# Stop commands must not hang up the still-running VR session before native
# vrstartup -shutdown. These settings apply around every Exec invocation.
TTYReset=no
TTYVHangup=no
TTYVTDisallocate=no
StandardOutput=journal
StandardError=journal
AmbientCapabilities=CAP_SYS_NICE
LimitRTPRIO=95
KillMode=mixed
TimeoutStopSec=150
RuntimeMaxSec={seconds + 145 if seconds else 'infinity'}
Restart=no
InaccessiblePaths=/opt/steamvr/drivers/cv/bin/linuxarm64/libArcturusPerception.so
''')
        tracking_unit_path = Path('/run/systemd/system') / TRACKING_UNIT
        if tracking_unit_path.exists() and not tracking_unit_path.read_text().startswith(HEADER):
            raise RuntimeError('Unrelated tracking unit already exists')
        tracking_unit_path.write_text(HEADER + f'''[Unit]
Description=Real MIX QVR camera IMU producer and restricted relay

[Service]
Type=simple
WorkingDirectory={PREFIX}
ExecStart=/usr/bin/python3 {PREFIX}/tracking-supervisor.py
KillMode=mixed
TimeoutStopSec=95
RuntimeMaxSec={seconds + 130 if seconds else 'infinity'}
Restart=no
StandardOutput=append:{raw}/supervisor.log
StandardError=append:{raw}/supervisor.log
''')
        run('systemctl', 'daemon-reload')
        run('systemctl', 'start', TRACKING_UNIT)
        if not active(TRACKING_UNIT) or done.exists():
            raise RuntimeError('Real producer did not stay running')
        run('systemctl', 'start', VIEWER)
        owned_unit_started = True
        run('systemctl', 'start', UNIT)
        if done.exists():
            raise RuntimeError('Real producer ended during UI startup')
    except BaseException:
        if owned_unit_started:
            subprocess.run(['systemctl', 'stop', UNIT])
        if not current().get('cleanup_complete'):
            cleanup(trial)
        raise
    print(json.dumps(data, indent=2), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('start', 'stop', 'status', 'shutdown', 'cleanup'))
    parser.add_argument('--seconds', type=int, default=60, help='0 continues until explicit stop; 60..300 is bounded')
    parser.add_argument('--trial')
    parser.add_argument('--gain', type=int, choices=(16, 64), default=16)
    parser.add_argument('--driver-directory', type=Path)
    parser.add_argument('--driver-sha256')
    parser.add_argument('--controller-plan', type=Path)
    parser.add_argument('--controller-pose-socket')
    parser.add_argument('--head-public-socket')
    args = parser.parse_args()
    if args.seconds != 0 and not 60 <= args.seconds <= 300:
        parser.error('--seconds must be 0 or 60..300')
    assert os.geteuid() == 0 and os.uname().machine == 'aarch64'
    assert os.uname().release in ('7.3.0-xr-mix-steamos', '7.3.0-xr-mix-steamos-qcft-pair', '7.3.0-xr-mix-steamos-venus', '7.3.0-xr-mix-steamos-preempt')
    assert Path('/etc/a081-rootfs-id').read_text().strip() == 'a081-steamos-arm64'
    assert b'A081' in Path('/proc/device-tree/model').read_bytes()
    if args.action == 'start':
        CURRENT.parent.mkdir(mode=0o755, exist_ok=True)
        descriptor = os.open(CURRENT.parent / 'start.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            start(args.seconds, args.gain, args.driver_directory, args.driver_sha256, args.controller_plan, args.controller_pose_socket, args.head_public_socket)
    elif args.action == 'cleanup':
        cleanup(args.trial)
    elif args.action == 'shutdown':
        shutdown()
    elif args.action == 'stop':
        if UNIT_PATH.exists():
            if not UNIT_PATH.read_text().startswith(HEADER):
                raise RuntimeError('Refusing unrelated unit')
            run('systemctl', 'stop', UNIT)
    else:
        print(json.dumps({'active': active(UNIT), **(current() if CURRENT.exists() else {})}, indent=2))


if __name__ == '__main__':
    main()
