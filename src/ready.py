#!/usr/bin/env python3
"""Gamescope child: Xwayland readiness, native runtime, configured primary scene."""
import atexit
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import traceback
from common import current, environment, PREFIX, UID, SESSION_HOME, STEAMVR, SYSTEM_ICD, SCENE
from scene_return import SceneReturn

assert os.getuid() == UID
data = current()
state = Path(data['directory'])
ready_log = (state / 'logs/ready-stdout.log').open('a', buffering=1)
sys.stdout = ready_log
sys.stderr = ready_log


def stop_native_scene():
    result = state / 'scene-stop.json'
    if SCENE['mode'] != 'command' or result.exists():
        return
    scene = globals().get('children', {}).get('scene')
    if scene is None:
        result.write_text(json.dumps({'status': 'not_started', 'terminated_before_runtime': True,
            'completed_monotonic_ns': time.monotonic_ns()}) + '\n')
        return
    if scene.poll() is None:
        scene.terminate()
    try:
        scene.wait(timeout=50)
    except subprocess.TimeoutExpired:
        raise RuntimeError('Owned native scene did not finish its GPU/component cleanup')
    result.write_text(json.dumps({'exit': scene.returncode, 'terminated_before_runtime': True,
        'completed_monotonic_ns': time.monotonic_ns()}) + '\n')


def report_exception(kind, value, tb):
    (state / 'logs/ready-exception.log').write_text(''.join(traceback.format_exception(kind, value, tb)))
    sys.__excepthook__(kind, value, tb)
    observer = globals().get('scene_return')
    if observer is not None:
        observer.close()
    if (state / 'native-stop-requested').exists():
        stop_native_scene()
        # Keep Gamescope's primary child alive while the explicit root stop
        # lets native VR release KMS; systemd will finish this cgroup afterward.
        while True:
            time.sleep(1)
    try:
        result = subprocess.run(['python3', str(PREFIX / 'launch.py'), 'shutdown'], timeout=22)
        (state / 'failure-shutdown.json').write_text(json.dumps({'exit': result.returncode}) + '\n')
    except Exception as error:
        (state / 'failure-shutdown.json').write_text(json.dumps({'error': str(error)}) + '\n')
    # A lost primary child did not always make the actual Gamescope main
    # process exit. Signal only the launcher's pinned PID/start-time pair so
    # the service reaches its recorded cleanup and desktop fallback.
    try:
        pid = int(os.environ['MIX_VR_SHELL_MAIN_PID'])
        start = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19]
        if start == os.environ['MIX_VR_SHELL_MAIN_START']:
            os.kill(pid, signal.SIGTERM)
    except (OSError, KeyError, ValueError):
        pass


sys.excepthook = report_exception
env = environment(data)
if not env.get('DISPLAY'):
    raise RuntimeError('Gamescope did not provide DISPLAY')
subprocess.run(['xset', 'q'], env=env, check=True, timeout=5, stdout=subprocess.DEVNULL)
display = {key: env[key] for key in ('DISPLAY', 'XAUTHORITY', 'WAYLAND_DISPLAY',
           'GAMESCOPE_WAYLAND_DISPLAY') if key in env}
(state / 'display.json').write_text(json.dumps(display, indent=2) + '\n')
print(json.dumps({'phase': 'xwayland_ready', **display}), flush=True)

runtime = STEAMVR / 'bin/linuxarm64'
vr_env = dict(env)
# The direct compositor presents to KMS, not to Gamescope's nested X surface.
# Retain real DISPLAY for Dashboard CEF, but disable only its implicit WSI layer.
vr_env.pop('ENABLE_GAMESCOPE_WSI', None)
vr_env['DISABLE_GAMESCOPE_WSI'] = '1'
children = {}
logs = []


def spawn(name, args, child_env=vr_env, cwd=runtime):
    log = (state / 'logs' / (name + '-stdout.log')).open('w')
    logs.append(log)
    if name == 'pose-watch':
        log.close()
        logs.remove(log)
        (state / 'logs' / (name + '-stdout.log')).unlink()
        args = ['python3', str(PREFIX / 'bounded_run.py'), '--log',
                str(state / 'logs' / (name + '-stdout.log')), '--', *args]
        process = subprocess.Popen(args, cwd=cwd, env=child_env)
    else:
        process = subprocess.Popen(args, cwd=cwd, env=child_env, stdout=log, stderr=subprocess.STDOUT)
    children[name] = process
    if name in ('pose-watch', 'scene'):
        (state / (name + '-started.json')).write_text(json.dumps({
            'pid': process.pid, 'started_monotonic_ns': time.monotonic_ns()}) + '\n')
    print(json.dumps({'phase': 'child_started', 'name': name, 'pid': process.pid}), flush=True)
    return process


def start(name, args, child_env=vr_env, cwd=runtime):
    if name != 'scene':
        return spawn(name, args, child_env, cwd)
    with Path(data['scene_lock']).open('rb') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if (state / 'native-stop-requested').exists():
            raise RuntimeError('Native stop requested before scene startup')
        return spawn(name, args, child_env, cwd)


server = start('server', [str(runtime / 'vrserver'), '-keepalive'])
deadline = time.monotonic() + 15
server_log = state / 'logs/vrserver.txt'
while time.monotonic() < deadline:
    if server.poll() is not None:
        raise RuntimeError('Native server exited before IPC readiness')
    if server_log.exists() and f'CreatePipe(VR_ServerPipe_{server.pid}) bound' in server_log.read_text():
        break
    time.sleep(0.1)
else:
    raise RuntimeError('Native server IPC readiness timed out')
start('pose-watch', [str(PREFIX / 'pose-watch'), str(runtime / 'libopenvr_api.so'), data['producer_done'],
                     'continuous' if data['seconds'] == 0 else 'bounded'])
activation = start('activation', [str(runtime / 'vrcmd'), '--background', '--waitforquit'])
# The native VR Dashboard directly hosts vrwebhelper. The optional desktop
# vrmonitor has a separate Qt ABI dependency and is not needed for this shell.
if SCENE['mode'] == 'steam':
    dashboard_env = dict(vr_env)
    dashboard_env['VK_DRIVER_FILES'] = SYSTEM_ICD
    dashboard_env.pop('TU_A081_KMS_DEVICE', None)
    start('dashboard', [str(runtime / 'vrdashboard')], dashboard_env)
    client = SESSION_HOME / '.local/share/Steam'
    client_runtime = next((name for name in ('steamrtarm64', 'linuxarm64')
                           if (client / name / 'steam').is_file()), None)
    if client_runtime is None:
        raise RuntimeError('Native Steam client must already be installed by the desktop baseline')
    steam_env = dict(env)
    steam_env['VK_DRIVER_FILES'] = SYSTEM_ICD
    steam_env.pop('TU_A081_KMS_DEVICE', None)
    # Match the validated Frame launcher: adding runtime/libs mixes old nghttp2.
    steam_env['LD_LIBRARY_PATH'] = str(client / client_runtime)
    steam_env['PATH'] = str(client / 'steam-runtime-steamrt-arm64/bin') + ':' + env.get('PATH', '/usr/bin')
    steam = start('steam', [str(client / client_runtime / 'steam'), '-nobootstrapupdate',
                  '-skipinitialbootstrap', '-gamepadui', '-vrgamepadui'], steam_env, client)
else:
    scene_env = dict(env)
    scene_env['VK_DRIVER_FILES'] = SYSTEM_ICD
    scene_env.pop('TU_A081_KMS_DEVICE', None)
    scene_env.update(SCENE.get('environment', {}))
    executable = Path(SCENE['argv'][0])
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise RuntimeError('Missing executable native scene: ' + str(executable))
    if SCENE.get('wait_for_first_tracking_packet', False):
        from first_tracking_packet import wait_for_first_tracking_packet
        packet = wait_for_first_tracking_packet(data, server, UID)
        print(json.dumps({'phase': 'first_tracking_packet_ready',
            'session_id': packet['packet']['session_id'], 'sequence': packet['packet']['sequence'],
            'flags': packet['packet']['flags'], 'pose_quality': packet['packet']['pose_quality']}), flush=True)
    scene = start('scene', SCENE['argv'], scene_env,
                  Path(SCENE.get('working_directory', str(SESSION_HOME))))
(state / 'children.json').write_text(json.dumps({name: p.pid for name, p in children.items()}, indent=2) + '\n')
start('present-offset', ['python3', str(PREFIX / 'present-offset.py')])
print(json.dumps({'phase': 'native_ui_started', 'trial': data['trial'],
                  'tracking': 'qvr-pose', 'ui_accepted': False}), flush=True)
show_deadline = time.monotonic() + 45
next_overlay_poll = 0
show_requested = False
scene_return = None
while True:
    if (state / 'native-stop-requested').exists():
        stop_native_scene()
        if scene_return is not None:
            scene_return.close()
            scene_return = None
            print(json.dumps({'phase': 'scene_watch_closed'}), flush=True)
        time.sleep(0.2)
        continue
    for name in (('server', 'activation', 'dashboard', 'steam') if SCENE['mode'] == 'steam'
                 else ('server', 'activation', 'scene')):
        code = children[name].poll()
        if code is not None:
            (state / 'child-exit.json').write_text(json.dumps({'child': name, 'exit': code,
                'monotonic_ns': time.monotonic_ns(),
                'all_children': {key: child.poll() for key, child in children.items()}}) + '\n')
            raise RuntimeError(f'{name} exited with {code}; stop the owned session and restore desktop')
    if children['pose-watch'].poll() is not None and not Path(data['producer_done']).exists():
        raise RuntimeError('Pose observer exited before the real producer; preserve logs')
    now = time.monotonic()
    if SCENE['mode'] == 'steam' and not show_requested and now < show_deadline and now >= next_overlay_poll:
        next_overlay_poll = now + 2
        overlays = subprocess.run([str(runtime / 'vrcmd'), '--overlays'], env=vr_env,
                                  capture_output=True, text=True, timeout=8)
        (state / 'logs/overlays-before-show.log').write_text(overlays.stdout + overlays.stderr)
        if re.search(r"'valve\.steam\.gamepadui\.main'.*?\b[1-9][0-9]*x[1-9][0-9]*\b", overlays.stdout):
            shown = subprocess.run([str(runtime / 'vrcmd'), '--showdashboard', 'valve.steam.gamepadui.main'],
                                   env=vr_env, capture_output=True, text=True, timeout=8)
            (state / 'logs/show-dashboard.log').write_text(shown.stdout + shown.stderr)
            show_requested = True
            print(json.dumps({'phase': 'dashboard_show_requested', 'exit': shown.returncode,
                              'ui_accepted': False}), flush=True)
    if show_requested:
        if scene_return is None:
            scene_return = SceneReturn(runtime, vr_env, state / 'logs')
            atexit.register(scene_return.close)
            print(json.dumps({'phase': 'scene_watch_ready', 'interface': 'IVRApplications_007'}), flush=True)
        event = scene_return.poll(now)
        if event:
            print(json.dumps(event), flush=True)
    time.sleep(0.5)
