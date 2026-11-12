#!/usr/bin/env python3
"""Continuous native VR UI. Root owns lifecycle; graphics use the configured user."""
import argparse
from contextlib import nullcontext
from datetime import datetime, timezone
import fcntl
import importlib
import hashlib
import json
import os
from pathlib import Path
import runpy
import subprocess
import time
import uuid
import re
import pwd
from controller_session import read_plan
from desktop_owner import stop_desktop, restore_desktop
from sensor_consumers import pause as pause_sensor_consumers, restore as restore_sensor_consumers
from common import (CURRENT, DESKTOP, PREFIX, UNIT, VIEWER, TRACKING_UNIT, current, selected_driver,
                    selected_gamescope, PROFILE, USER, UID, GID, SESSION_HOME, ROOTFS, STEAMVR,
                    UTILITIES, KMS_ICD, SCENE, RUNTIME_LIBRARY_PATH)

UNIT_PATH = Path('/run/systemd/system') / UNIT
HEADER = '# Managed by isolated mix-vr-shell\n'


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def active(unit):
    return bool(unit) and subprocess.run(['systemctl', 'is-active', '--quiet', unit]).returncode == 0


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
    skipped = []
    # Normal shutdown stopped the viewer before requesting native VR exit.
    # ExecStop is skipped when startup itself failed; preserve that distinction.
    if not data.get('shutdown_requested'):
        data['viewer_restore'] = active(VIEWER) or data.get('viewer_restore', False) or data['viewer_before']
        save(data)
        result = subprocess.run(['systemctl', 'stop', VIEWER]) if VIEWER else subprocess.CompletedProcess([], 0)
        data['startup_failure_viewer_stop_ns'] = time.monotonic_ns()
        if result.returncode:
            errors.append('viewer_stop_after_startup_failure')
    tracking_result = Path(data['tracking_result'])
    tracking_marker = Path(data['raw']) / 'supervisor-started.json'
    tracking_started = (tracking_marker.exists() or tracking_result.exists() or
        ('tracking_start_requested' not in data and (Path(data['raw']) / 'supervisor.log').exists()))
    tracking_requested = data.get('tracking_start_requested', tracking_started)
    if tracking_requested or tracking_started or active(TRACKING_UNIT):
        if subprocess.run(['systemctl', 'stop', TRACKING_UNIT]).returncode:
            errors.append('tracking_stop')
    else:
        skipped.append('tracking_not_started')
    # Stopping a requested Type=simple unit waits for its real supervisor to
    # finish. Read its marker/result afterward, including partial startup.
    tracking_started = tracking_started or tracking_marker.exists() or tracking_result.exists()
    if tracking_result.exists():
        data['tracking_result_data'] = json.loads(tracking_result.read_text())
        if not data['tracking_result_data'].get('pass'):
            runtime_errors.append('tracking_capture')
        errors.extend('tracking_' + str(error) for error in data['tracking_result_data'].get('cleanup_errors', []))
        data['tracking_status'] = 'completed' if data['tracking_result_data'].get('pass') else 'failed'
    elif tracking_started:
        errors.append('tracking_result_missing')
        data['tracking_status'] = 'result_missing'
    else:
        data['tracking_status'] = 'not_started'
        if 'tracking_not_started' not in skipped:
            skipped.append('tracking_not_started')
    scene_result = Path(data['directory']) / 'scene-stop.json'
    if SCENE['mode'] == 'command' and scene_result.exists():
        data['scene_stop'] = json.loads(scene_result.read_text())
        if data['scene_stop'].get('status') == 'not_started':
            data['native_scene_status'] = 'not_started'
            skipped.append('native_scene_not_started')
        elif data['scene_stop'].get('exit') != 0:
            runtime_errors.append('native_scene_exit')
    try:
        snapshot = runpy.run_path(str(UTILITIES / 'cleanup'))['snapshot']
        data['first_exit'] = str(snapshot(trial, 'first-exit'))
    except Exception as error:
        errors.append('first_exit: ' + str(error))
    probe_log = Path(data['directory']) / 'logs/pose-watch-stdout.log'
    retention = Path(str(probe_log) + '.retention.json')
    pose_watch_started = ((Path(data['directory']) / 'pose-watch-started.json').exists() or
                          probe_log.exists() or retention.exists())
    data['pose_watch_result'] = [line for line in probe_log.read_text().splitlines()
                                if line.startswith('A081_QVR_OPENVR_RESULT')] if probe_log.exists() else []
    if pose_watch_started and (not data['pose_watch_result'] or not data['pose_watch_result'][-1].endswith('pass=1')):
        validation_errors.append('pose_watch')
    data['pose_watch_retention'] = json.loads(retention.read_text()) if retention.exists() else None
    # Native observer exit8 is its explicit acceptance-gate failure. A
    # completed pass=0 report with drained logs is not a cleanup failure.
    observer_exit = (data['pose_watch_retention'] or {}).get('exit')
    observer_validation_only = (observer_exit == 8 and bool(data['pose_watch_result']) and
                                data['pose_watch_result'][-1].endswith('pass=0'))
    if pose_watch_started and observer_exit != 0 and not observer_validation_only:
        errors.append('pose_watch_log_drain')
    data['pose_watch_status'] = ('failed' if 'pose_watch' in validation_errors else 'passed') if pose_watch_started else 'not_started'
    if not pose_watch_started:
        skipped.append('pose_watch_not_started')
    result = subprocess.run(['python3', str(UTILITIES / 'iio-access'), 'recover'])
    if result.returncode:
        errors.append('iio_recover')
    errors.extend(restore_sensor_consumers(data, save))
    # This candidate always returns to the proven maintenance desktop. Keep
    # shutdown/reboot free of a new display startup.
    shutting_down = subprocess.run(['systemctl', 'is-system-running'], capture_output=True,
                                    text=True).stdout.strip() == 'stopping'
    if not shutting_down:
        try:
            data.update(restore_desktop())
        except Exception as error:
            errors.append('desktop_restore: ' + repr(error))
        if VIEWER and (data.get('viewer_restore', False) or data['viewer_before']) and subprocess.run(
                ['systemctl', 'start', VIEWER]).returncode:
            errors.append('viewer_restore')
    data.update(cleanup_complete=True, cleanup_errors=errors, cleanup_pass=not errors,
                cleanup_skipped=skipped,
                runtime_errors=runtime_errors, runtime_pass=(not runtime_errors if tracking_started else None),
                validation_errors=validation_errors,
                validation_pass=(not validation_errors and not runtime_errors if pose_watch_started else None))
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
    if VIEWER:
        run('systemctl', 'stop', VIEWER)
    data['viewer_stopped_before_vr_shutdown_ns'] = time.monotonic_ns()
    data['shutdown_requested'] = True
    state = Path(data['directory'])
    scene_result = state / 'scene-stop.json'
    # New sessions serialize the scene's Popen+marker with this stop request.
    # Older sessions retain their children.json evidence without a new lock.
    scene_context = Path(data['scene_lock']).open('rb') if data.get('scene_lock') else nullcontext()
    tracking_stopped = False
    with scene_context as lock:
        if lock is not None:
            fcntl.flock(lock, fcntl.LOCK_EX)
        children = json.loads((state / 'children.json').read_text()) if (state / 'children.json').exists() else {}
        data['scene_started'] = (state / 'scene-started.json').exists() or 'scene' in children
        if SCENE['mode'] == 'command' and data['scene_started']:
            # Keep graphics resources alive while the capture owner lowers the
            # gate, drains workers and releases DSP buffers. Destroying the
            # scene first overlapped GPU teardown with live camera dequeue.
            # The scene lock also excludes a late scene launch during this stop.
            data['tracking_stop_requested_ns'] = time.monotonic_ns()
            save(data)
            result = subprocess.run(['systemctl', 'stop', TRACKING_UNIT], timeout=105)
            data['tracking_stop_exit'] = result.returncode
            data['tracking_stop_completed_ns'] = time.monotonic_ns()
            save(data)
            if active(TRACKING_UNIT):
                raise RuntimeError('Tracking still active; preserve scene resources')
            # A failed capture still has to finish graphics cleanup. cleanup()
            # reads the producer result and retains its runtime failure.
            tracking_stopped = True
        data['native_stop_requested_ns'] = time.monotonic_ns()
        (state / 'native-stop-requested').write_text(str(data['native_stop_requested_ns']) + '\n')
        if SCENE['mode'] == 'command' and not data['scene_started']:
            data['scene_stop'] = {'status': 'not_started', 'terminated_before_runtime': True,
                                 'completed_monotonic_ns': time.monotonic_ns()}
            scene_result.write_text(json.dumps(data['scene_stop']) + '\n')
    save(data)
    if SCENE['mode'] == 'command' and data['scene_started']:
        deadline = time.monotonic() + 55
        while not scene_result.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        if not scene_result.exists():
            raise RuntimeError('Native scene did not finish before runtime stop')
        data['scene_stop'] = json.loads(scene_result.read_text())
        save(data)
    # End the real producer, close the relay, then observe SteamVR invalidation
    # before native graphics shutdown. No IIO orientation/position fallback.
    if not tracking_stopped:
        run('systemctl', 'stop', TRACKING_UNIT, timeout=105)
    probe_log = Path(data['directory']) / 'logs/pose-watch-stdout.log'
    retention = Path(str(probe_log) + '.retention.json')
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        if (probe_log.exists() and 'A081_QVR_OPENVR_RESULT' in probe_log.read_text() and retention.exists()
                and json.loads(retention.read_text()).get('exit') in (0, 8)):
            break
        time.sleep(0.1)
    run('runuser', '-u', USER, '--', 'python3', str(PREFIX / 'launch.py'), 'shutdown')


def start(seconds, gain, driver_directory=None, driver_sha256=None, controller_plan=None, controller_pose_socket=None, head_public_socket=None):
    if active(UNIT):
        raise RuntimeError('VR shell already running')
    if UNIT_PATH.exists() and not UNIT_PATH.read_text().startswith(HEADER):
        raise RuntimeError('Unrelated unit already exists')
    if (active('a081-mix-vr-shell.service') or active(TRACKING_UNIT) or
        active('a081-mix-vr-shell-qvr-live.service') or active('a081-mix-vr-shell-qvr-live-tracking.service')):
        raise RuntimeError('Stop the current IIO VR or QVR tracking session first')
    run('sha256sum', '--quiet', '-c', 'SHA256SUMS', cwd=PREFIX)
    if PROFILE.get('tracking_recovery_display_guard'):
        guard = json.loads((PREFIX / 'manifest.json').read_text()).get('tracking_display_guard', {})
        observer_sha = hashlib.sha256((PREFIX / 'pose-watch').read_bytes()).hexdigest()
        if guard.get('schema') != 1 or guard.get('pose_watch_sha256') != observer_sha:
            raise RuntimeError('Tracking recovery requires the matching supervised display guard')
    for process in ('vrserver', 'vrcompositor', 'gamescope'):
        if subprocess.run(['pgrep', '-x', process], stdout=subprocess.DEVNULL).returncode == 0:
            raise RuntimeError('Another VR session exists: ' + process)
    gamescope = selected_gamescope()['binary']
    reaper = Path(gamescope).parent / 'gamescopereaper'
    for path in (str(STEAMVR / 'bin/linuxarm64/vrserver'), str(STEAMVR / 'bin/linuxarm64/vrstartup'),
                 KMS_ICD, str(UTILITIES / 'iio-access'), str(UTILITIES / 'cleanup'),
                 gamescope, str(reaper), '/usr/bin/Xwayland', '/usr/bin/xset', '/usr/bin/chvt'):
        if not Path(path).is_file():
            raise RuntimeError('Missing dependency: ' + path)
    if not os.access(reaper, os.X_OK):
        raise RuntimeError('Selected Gamescope reaper is not executable: ' + str(reaper))
    required_camera_tools = ('/usr/bin/media-ctl', '/usr/bin/v4l2-ctl', '/usr/bin/stdbuf',
                             '/usr/bin/dmesg', '/usr/bin/bash')
    for tool in required_camera_tools:
        if not Path(tool).is_file() or not os.access(tool, os.X_OK):
            raise RuntimeError('Missing camera tool: ' + tool)
    try:
        gpio = importlib.import_module('gpiod')
    except ImportError as error:
        raise RuntimeError('Missing camera dependency: Python gpiod v2') from error
    if not callable(getattr(gpio, 'request_lines', None)):
        raise RuntimeError('Camera dependency requires gpiod.request_lines (Python API v2)')
    probe = subprocess.run(['runuser', '-u', USER, '--', gamescope, '--allow-deferred-backend',
        '--vr-session-manager', '--help'],
        env=dict(os.environ, LD_LIBRARY_PATH=RUNTIME_LIBRARY_PATH),
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if probe.returncode:
        raise RuntimeError('Selected Gamescope dependency probe failed: ' +
                           probe.stderr.decode(errors='replace').strip())
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
    directory = SESSION_HOME / '.local/state/mix-vr-shell-controller' / trial
    data = {'trial': trial, 'directory': str(directory), 'unit': UNIT,
            'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
            'seconds': seconds, 'gain_v4l2': gain, 'tracking': 'qvr-pose', 'kms_device': '/dev/dri/' + cards.pop(),
            'driver': driver,
            'desktop_before': active(DESKTOP), 'viewer_before': active(VIEWER),
            'scene_mode': SCENE['mode'],
            'profile': ({'path': PROFILE['_path'], 'sha256': PROFILE['_sha256'], 'id': PROFILE['id']} if PROFILE else None),
            'cleanup_complete': False, 'tracking_start_requested': False}
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
        os.chown(parent, UID, GID)
    for name in ('config', 'logs'):
        (directory / name).mkdir(mode=0o700)
    settings = json.loads(Path('/opt/mix-steamvr/settings.json').read_text())
    settings['steamvr'].update(startMonitorFromAppLaunch=False, startCompositorFromAppLaunch=True,
                               startDashboardFromAppLaunch=SCENE['mode'] == 'steam',
                               startOverlayAppsFromDashboard=SCENE['mode'] == 'steam')
    if SCENE['mode'] == 'command':
        settings['steamvr']['enableHomeApp'] = False
    settings['dashboard'] = {key: SCENE['mode'] == 'steam' for key in
        ('enableDashboard', 'allowSteamOverlays', 'allowVRGamepadUI', 'allowVRGamepadUIViaGamescope', 'expectSteam')}
    if PROFILE.get('tracking_recovery_display_guard'):
        # Keep truthful RotationOnly tracking flags; the supervised observer
        # explicitly protects with black when orientation is unavailable.
        settings['steamvr']['forceFadeOnBadTracking'] = False
        settings['steamvr']['doNotFadeToGrid'] = False
    settings.setdefault('power', {}).update(turnOffScreensTimeout=1800.0, pauseCompositorOnStandby=False)
    (directory / 'config/steamvr.vrsettings').write_text(json.dumps(settings, indent=2) + '\n')
    paths = {'version': 1, 'jsonid': 'vrpathreg', 'runtime': [str(STEAMVR)],
             'config': [str(directory / 'config')], 'log': [str(directory / 'logs')],
             'external_drivers': [driver['directory']]}
    (directory / 'openvrpaths.vrpath').write_text(json.dumps(paths, indent=2) + '\n')
    for path in directory.rglob('*'):
        os.chown(path, UID, GID)
    CURRENT.parent.mkdir(mode=0o755, exist_ok=True)
    scene_lock = CURRENT.parent / (trial + '-scene.lock')
    scene_lock.touch(mode=0o644, exist_ok=False)
    scene_lock.chmod(0o644)
    data['scene_lock'] = str(scene_lock)
    save(data)
    owned_unit_started = False
    try:
        data.update(stop_desktop())
        save(data)
        pause_sensor_consumers(data, save, PROFILE.get('sensor_consumers', []))
        runpy.run_path(str(UTILITIES / 'cleanup'))['snapshot'](trial, 'desktop-first-exit')
        deadline = time.monotonic() + 5
        while True:
            # systemd can finish a desktop stop before the exiting Steam PID
            # is reaped. Wait for live processes; zombies cannot own graphics.
            selection = subprocess.run(['ps', '-u', str(UID), '-o', 'stat=,comm='],
                                       text=True, capture_output=True)
            if selection.returncode != 0 and not (selection.returncode == 1 and
                                                  selection.stdout == '' and selection.stderr == ''):
                selection.check_returncode()
            rows = selection.stdout
            live_steam = any(row.split()[1:] == ['steam'] and not row.split()[0].startswith('Z')
                             for row in rows.splitlines() if row.split())
            if not live_steam:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError('Steam remains outside the stopped desktop; leave it untouched')
            time.sleep(0.1)
        run('python3', str(UTILITIES / 'iio-access'), 'prepare')
        UNIT_PATH.write_text(HEADER + f'''[Unit]
Description=Native SteamVR and Steam VR UI on MIX (isolated candidate)
After=systemd-user-sessions.service systemd-logind.service a081-sensors-files.service
Requires=systemd-logind.service a081-sensors-files.service
Conflicts=getty@tty7.service

[Service]
Type=simple
User={USER}
Group={GID}
PAMName=login
WorkingDirectory={SESSION_HOME}
Environment=HOME={SESSION_HOME}
Environment=QIYU_VR_SESSION_PROFILE={PROFILE.get('_path', '')}
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
TimeoutStopSec={210 if SCENE['mode'] == 'command' else 150}
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
Environment=QIYU_VR_SESSION_PROFILE={PROFILE.get('_path', '')}
ExecStart=/usr/bin/python3 {PREFIX}/tracking-supervisor.py
KillMode=mixed
TimeoutStopSec=95
RuntimeMaxSec={seconds + 130 if seconds else 'infinity'}
Restart=no
StandardOutput=append:{raw}/supervisor.log
StandardError=append:{raw}/supervisor.log
''')
        run('systemctl', 'daemon-reload')
        data['tracking_start_requested'] = True
        save(data)
        run('systemctl', 'start', TRACKING_UNIT)
        if not active(TRACKING_UNIT) or done.exists():
            raise RuntimeError('Real producer did not stay running')
        if VIEWER:
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
    assert os.uname().release in ('7.3.0-qiyu-mix-steamos', '7.3.0-qiyu-mix-steamos-qcft-pair', '7.3.0-qiyu-mix-steamos-venus', '7.3.0-qiyu-mix-steamos-preempt', '7.3.0-qiyu-mix-steamos-perf', '7.3.0-qiyu-mix-xr2-cameras+', '7.3.0-qiyu-mix-xr2-optics+', '7.3.0-qiyu-mix-usb-pd5v', '7.3.0-qiyu-mix-scheduling')
    assert Path('/etc/a081-rootfs-id').read_text().strip() == ROOTFS
    assert b'A081' in Path('/proc/device-tree/model').read_bytes()
    account = pwd.getpwnam(USER)
    if (account.pw_uid, account.pw_gid, account.pw_dir) != (UID, GID, str(SESSION_HOME)):
        raise RuntimeError('Configured graphics account does not match the installed user')
    if args.action == 'start':
        CURRENT.parent.mkdir(mode=0o755, exist_ok=True)
        descriptor = os.open(CURRENT.parent / 'start.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            start(args.seconds, args.gain,
                  args.driver_directory or PROFILE.get('driver_directory'),
                  args.driver_sha256 or PROFILE.get('driver_sha256'),
                  args.controller_plan or PROFILE.get('controller_plan'),
                  args.controller_pose_socket, args.head_public_socket)
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
