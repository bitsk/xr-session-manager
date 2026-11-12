"""Private native MIX VR shell environment; never changes the global registry."""
import json
import hashlib
import os
from pathlib import Path
import re
from profile import load as load_profile
from trusted_paths import trusted_opt_path

PROFILE = load_profile(os.environ['QIYU_VR_SESSION_PROFILE']) if os.environ.get('QIYU_VR_SESSION_PROFILE') else {}

PREFIX = Path(PROFILE.get('prefix', '/opt/mix-vr-shell-controller'))
CURRENT = Path(PROFILE.get('runtime_directory', '/run/mix-vr-shell-controller')) / 'session.json'
UNIT = PROFILE.get('unit', 'a081-mix-vr-shell-controller.service')
TRACKING_UNIT = PROFILE.get('tracking_unit', 'a081-mix-vr-shell-controller-tracking.service')
DESKTOP = PROFILE.get('desktop_unit', 'a081-steamos-desktop.service')
VIEWER = PROFILE.get('viewer_unit', 'a081-remote-view.service')
USER = PROFILE.get('user', 'steamos')
UID = PROFILE.get('uid', 1000)
GID = PROFILE.get('gid', 1000)
SESSION_HOME = Path(PROFILE.get('home', '/home/steamos'))
ROOTFS = PROFILE.get('rootfs_id', 'a081-steamos-arm64')
STEAMVR = Path(PROFILE.get('steamvr', '/opt/steamvr'))
RUNTIME_LIBRARY_PATH = ':'.join(dict.fromkeys(
    [*PROFILE.get('runtime_library_paths', []), str(STEAMVR / 'bin/linuxarm64')]))
UTILITIES = Path(PROFILE.get('utilities', '/opt/mix-steamvr/bin'))
SYSTEM_ICD = PROFILE.get('system_icd', '/usr/share/vulkan/icd.d/freedreno_icd.aarch64.json')
KMS_ICD = PROFILE.get('kms_icd', '/opt/mix-steamvr-turnip/share/vulkan/icd.d/freedreno_icd.aarch64.json')
SCENE = PROFILE.get('scene', {'mode': 'steam'})
DRIVER_CHOICE = Path('/etc/mix-vr-shell-driver.json')
BASELINE_DRIVER = Path('/opt/mix-steamvr/drivers/monado')


def check_driver(directory, expected=None):
    directory = Path(directory)
    if directory.name != 'monado':
        raise RuntimeError('The native driver must be a private /opt/.../monado directory')
    binary = trusted_opt_path(directory / 'bin/linuxarm64/driver_monado.so')
    header = binary.read_bytes()[:20]
    if header[:6] != b'\x7fELF\x02\x01' or header[18:20] != b'\xb7\x00':
        raise RuntimeError('Expected a native AArch64 driver ELF')
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    if expected is not None and digest != expected:
        raise RuntimeError('Selected driver checksum changed')
    return {'directory': str(directory), 'sha256': digest}


def read_driver_choice():
    if not os.path.lexists(DRIVER_CHOICE):
        return None
    info = DRIVER_CHOICE.lstat()
    if DRIVER_CHOICE.is_symlink() or info.st_uid != 0 or info.st_mode & 0o022:
        raise RuntimeError('Driver choice must be a root-owned immutable file')
    choice = json.loads(DRIVER_CHOICE.read_text())
    if set(choice) != {'format', 'directory', 'sha256'} or choice['format'] != 'mix-vr-shell-driver-v1':
        raise RuntimeError('Unexpected driver choice format')
    if not re.fullmatch('[0-9a-f]{64}', choice['sha256']):
        raise RuntimeError('Invalid selected driver hash')
    return choice


def selected_driver(directory=None, expected=None):
    if directory is not None:
        return check_driver(directory, expected)
    manifest = json.loads((PREFIX / 'manifest.json').read_text())
    return check_driver(PREFIX / 'monado', manifest['files']['monado/bin/linuxarm64/driver_monado.so'])



def selected_gamescope():
    """Select a checksum-pinned private build, or the system package."""
    choice_path = Path('/etc/mix-vr-shell-gamescope.json')
    binary = Path('/usr/bin/gamescope')
    expected = None
    if os.path.lexists(choice_path):
        info = choice_path.lstat()
        if choice_path.is_symlink() or info.st_uid != 0 or info.st_mode & 0o022:
            raise RuntimeError('Gamescope choice must be a root-owned immutable file')
        choice = json.loads(choice_path.read_text())
        if (set(choice) != {'format', 'binary', 'sha256'}
                or choice['format'] != 'mix-vr-gamescope-v1'
                or not re.fullmatch('[0-9a-f]{64}', choice['sha256'])):
            raise RuntimeError('Unexpected Gamescope choice format')
        binary = trusted_opt_path(choice['binary'])
        expected = choice['sha256']
    payload = binary.read_bytes()
    if payload[:6] != b'\x7fELF\x02\x01' or payload[18:20] != b'\xb7\x00':
        raise RuntimeError('Expected a native AArch64 Gamescope ELF')
    digest = hashlib.sha256(payload).hexdigest()
    if expected is not None and digest != expected:
        raise RuntimeError('Selected Gamescope checksum changed')
    return {'binary': str(binary), 'sha256': digest}


def current():
    info = CURRENT.lstat()
    if CURRENT.is_symlink() or info.st_uid != 0 or info.st_mode & 0o022:
        raise RuntimeError('Expected root-owned session state')
    data = json.loads(CURRENT.read_text())
    if not re.fullmatch(r'vr-shell-[a-zA-Z0-9_-]+', data['trial']):
        raise RuntimeError('Invalid trial name')
    if data['boot_id'] != Path('/proc/sys/kernel/random/boot_id').read_text().strip():
        raise RuntimeError('Session belongs to another boot')
    return data


def environment(data):
    env = dict(os.environ)
    for name in ('LD_PRELOAD', 'VR_DRIVER_PATH', 'STEAMVR_VRENV', 'VK_ICD_FILENAMES',
                 'LIBGL_DRIVERS_PATH', 'GBM_BACKENDS_PATH', '__EGL_VENDOR_LIBRARY_FILENAMES',
                 'LIBGL_ALWAYS_SOFTWARE', 'gamescope_vr_use_modifiers'):
        env.pop(name, None)
    state = Path(data['directory'])
    env.update({
        'HOME': str(SESSION_HOME), 'VR_OVERRIDE': str(STEAMVR),
        'STEAMVR_TOOLSDIR': str(STEAMVR),
        'VR_PATHREG_OVERRIDE': str(state / 'openvrpaths.vrpath'),
        'VR_CONFIG_PATH': str(state / 'config'), 'VR_LOG_PATH': str(state / 'logs'),
        'LD_LIBRARY_PATH': RUNTIME_LIBRARY_PATH,
        'VRCOMPOSITOR_LD_LIBRARY_PATH': RUNTIME_LIBRARY_PATH,
        'A081_BRINGUP': '1', 'A081_TRACKING_SOURCE': 'qvr-pose', 'A081_STEAMVR_EXTENDED': '0',
        'A081_QVR_POSE_PROFILE': 'android-portrait-v1',
        'A081_QVR_TRACKING_SOCKET': data['public_socket'],
        'XRT_COMPOSITOR_SCALE_PERCENTAGE': '100', 'XRT_LOG': 'info',
        'VK_DRIVER_FILES': KMS_ICD,
        'TU_A081_KMS_DEVICE': data['kms_device'], 'MESA_LOADER_DRIVER_OVERRIDE': 'zink',
        'QT_QPA_PLATFORM': 'xcb', 'XDG_RUNTIME_DIR': '/run/user/' + str(UID),
        'DBUS_SESSION_BUS_ADDRESS': 'unix:path=/run/user/' + str(UID) + '/bus',
        'MIX_VR_SHELL_TRIAL': data['trial'],
    })
    # Persisted root-owned session data reaches every real vrserver child,
    # including systemd launch; this does not rely on caller shell variables.
    for name in ('A081_CONTROLLER_POSE_SOCKET', 'A081_CONTROLLER_POSE_PROFILE'):
        env.pop(name, None)
    for hand in ('LEFT', 'RIGHT'):
        name = 'A081_CONTROLLER_RENDER_MODEL_' + hand
        env.pop(name, None)
        model = PROFILE.get('controller_render_models', {}).get(hand.lower())
        if model:
            env[name] = model
    if data.get('controller_pose_socket'):
        env.update(A081_CONTROLLER_POSE_SOCKET=data['controller_pose_socket'],
                   A081_CONTROLLER_POSE_PROFILE='openvr-head-raw-v1')
    # load_profile restricts this namespace to graphics choices. Session paths,
    # account identity and real tracking endpoint metadata cannot be replaced.
    env.pop('QIYU_TRACKING_RECOVERY_DISPLAY_GUARD', None)
    if PROFILE.get('tracking_recovery_display_guard'):
        env['QIYU_TRACKING_RECOVERY_DISPLAY_GUARD'] = '1'
    env.update(PROFILE.get('runtime_environment', {}))
    return env
