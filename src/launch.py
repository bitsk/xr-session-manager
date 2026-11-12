#!/usr/bin/env python3
"""Start Frame's deferred Gamescope contract, or request native VR shutdown."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from common import current, environment, PREFIX, selected_gamescope, UID, STEAMVR, SYSTEM_ICD, SCENE

assert os.getuid() == UID and os.uname().machine == 'aarch64'
data = current()
env = environment(data)
state = Path(data['directory'])
action = sys.argv[1] if len(sys.argv) > 1 else 'start'
if action == 'shutdown':
    env.pop('ENABLE_GAMESCOPE_WSI', None)
    env['DISABLE_GAMESCOPE_WSI'] = '1'
    display = state / 'display.json'
    if display.exists():
        env.update(json.loads(display.read_text()))
    # Ask the native runtime to release its graphics objects before systemd
    # signals any members of the owning cgroup (SIGTERM alone leaked DMA).
    with (state / 'logs/native-shutdown.log').open('w') as log:
        result = subprocess.run([str(STEAMVR / 'bin/linuxarm64/vrstartup'), '-shutdown'],
                                env=env, timeout=12, stdout=log, stderr=subprocess.STDOUT)
    completed = False
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        running = [name for name in ('vrserver', 'vrcompositor')
                   if subprocess.run(['pgrep', '-u', str(UID), '-x', name],
                                     stdout=subprocess.DEVNULL).returncode == 0]
        if not running:
            completed = True
            break
        time.sleep(0.2)
    (state / 'shutdown.json').write_text(json.dumps({'native_shutdown_exit': result.returncode,
                'native_shutdown_completed': completed, 'remaining': running}) + '\n')
    raise SystemExit(result.returncode or (0 if completed else 1))
if action != 'start':
    raise SystemExit('Expected start or shutdown')
# PAM can move this process to a login scope, outside the service's journal
# filter. Keep the real exec'd Gamescope output in this trial without adding
# another process or changing the pinned main PID used during shutdown.
output = os.open(state / 'logs/gamescope-stdout.log', os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
try:
    os.dup2(output, 1)
    os.dup2(output, 2)
finally:
    os.close(output)
if not os.environ.get('XDG_SESSION_ID'):
    raise RuntimeError('A PAM/logind login session is required')
for name in ('DISPLAY', 'WAYLAND_DISPLAY', 'GAMESCOPE_WAYLAND_DISPLAY'):
    env.pop(name, None)
env.update({'LIBSEAT_BACKEND': 'logind', 'XDG_SESSION_TYPE': 'wayland',
            'XDG_CURRENT_DESKTOP': 'gamescope', 'QT_IM_MODULE': 'xim', 'GTK_IM_MODULE': 'xim'})
# Gamescope needs desktop WSI extensions; the compositor's private KMS ICD
# does not provide them. ready.py rebuilds the separate compositor environment.
env['VK_DRIVER_FILES'] = SYSTEM_ICD
env.pop('TU_A081_KMS_DEVICE', None)
env['MIX_VR_SHELL_MAIN_PID'] = str(os.getpid())
env['MIX_VR_SHELL_MAIN_START'] = Path('/proc/self/stat').read_text().rsplit(')', 1)[1].split()[19]
gamescope = selected_gamescope()
env['PATH'] = str(Path(gamescope['binary']).parent) + os.pathsep + env.get('PATH', os.defpath)
(state / 'gamescope.json').write_text(json.dumps(gamescope, indent=2) + '\n')
args = [gamescope['binary'], '--allow-deferred-backend', '--backend', 'openvr',
        '--vr-session-manager',
        '--virtual-connector-strategy', 'PerAppId', '--xwayland-count', '2',
        '-W', '1600', '-H', '1000', '-w', '1600', '-h', '1000', '-r', '72',
        '--vr-overlay-key', 'valve.steam.gamepadui.fallback',
        '--vr-app-overlay-key', 'valve.steam.desktopgame',
        '--vr-overlay-show-immediately', '--vr-overlay-physical-width', '1.8',
        '--', '/usr/bin/python3', str(PREFIX / 'ready.py')]
if SCENE['mode'] == 'steam':
    args.insert(5, '--steam')
recorded_environment = {
    'HOME', 'PATH', 'LD_LIBRARY_PATH', 'VRCOMPOSITOR_LD_LIBRARY_PATH', 'LD_PRELOAD',
    'VK_DRIVER_FILES', 'VK_ICD_FILENAMES', 'VK_INSTANCE_LAYERS', 'VK_LAYER_PATH',
    'VK_LOADER_LAYERS_ENABLE', 'VK_LOADER_LAYERS_DISABLE', 'VK_LOADER_DEBUG',
    'VK_LOADER_DRIVERS_SELECT', 'VK_LOADER_DRIVERS_DISABLE',
    'ENABLE_GAMESCOPE_WSI', 'DISABLE_GAMESCOPE_WSI', 'DISABLE_VULKAN_FDM_INJECTION_LAYER',
    'LIBSEAT_BACKEND', 'DISPLAY', 'WAYLAND_DISPLAY', 'GAMESCOPE_WAYLAND_DISPLAY',
    'XDG_SESSION_ID', 'XDG_RUNTIME_DIR', 'XDG_SESSION_TYPE', 'XDG_SESSION_CLASS',
    'XDG_SEAT', 'XDG_VTNR', 'XDG_CURRENT_DESKTOP', 'DBUS_SESSION_BUS_ADDRESS',
    'QIYU_VR_SESSION_PROFILE', 'VR_OVERRIDE', 'VR_PATHREG_OVERRIDE',
    'VR_CONFIG_PATH', 'VR_LOG_PATH', 'STEAMVR_TOOLSDIR',
    'MESA_LOADER_DRIVER_OVERRIDE', 'MESA_VK_WSI_PRESENT_MODE',
    'TU_A081_KMS_DEVICE', 'TU_DEBUG', 'TU_A081_QUERY_SLEEP_US', 'TU_A081_THREE_IMAGES',
    'A081_CONTROLLER_RENDER_MODEL_LEFT', 'A081_CONTROLLER_RENDER_MODEL_RIGHT',
}
(state / 'launch-environment.json').write_text(json.dumps({
    'format': 'qiyu-vr-launch-environment-v1', 'trial': data['trial'],
    'pid': os.getpid(), 'uid': os.getuid(), 'gid': os.getgid(),
    'environment': {key: env[key] for key in sorted(recorded_environment) if key in env},
    'unset': sorted(recorded_environment - env.keys()),
}, indent=2) + '\n')
print(json.dumps({'phase': 'gamescope_start', 'argv': args, 'trial': data['trial']}), flush=True)
os.execvpe(args[0], args, env)
