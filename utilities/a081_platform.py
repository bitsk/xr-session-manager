# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared identity selection for the existing A081 permission/cleanup helpers."""
import json
import os
from pathlib import Path
import platform
import pwd
import re
import stat


ROOTS = {
    'a081-steamos-arm64': {'user': 'steamos', 'desktop_unit': 'a081-steamos-desktop.service'},
    'a081-openkylin-3.0-arm64': {'user': 'mix', 'desktop_unit': 'lightdm.service'},
}


def identity():
    if os.geteuid() != 0 or platform.machine() != 'aarch64':
        raise RuntimeError('Run as root on the native A081 installation')
    if b'A081' not in Path('/proc/device-tree/model').read_bytes():
        raise RuntimeError('Expected the A081 device tree')
    marker = Path('/etc/a081-rootfs-id').read_text().strip()
    if marker not in ROOTS:
        raise RuntimeError('Unsupported independent A081 rootfs')
    selected = dict(ROOTS[marker], uid=1000, gid=1000, rootfs_id=marker,
                    utilities='/opt/mix-steamvr/bin')
    profile_path = os.environ.get('QIYU_VR_SESSION_PROFILE')
    if profile_path:
        path = Path(profile_path)
        if not path.is_absolute():
            raise RuntimeError('Session profile path must be absolute')
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise RuntimeError('Session profile must be a root-owned immutable regular file')
        profile = json.loads(path.read_text())
        if (profile.get('format') != 'qiyu-vr-session-profile-v1' or
                profile.get('rootfs_id') != marker or
                profile.get('uid') != 1000 or profile.get('gid') != 1000):
            raise RuntimeError('Session profile does not match this root or graphics identity')
        for key in ('user', 'desktop_unit', 'utilities'):
            if not isinstance(profile.get(key), str):
                raise RuntimeError('Missing platform field: ' + key)
            selected[key] = profile[key]
    if not re.fullmatch('[a-z_][a-z0-9_-]*', selected['user']):
        raise RuntimeError('Invalid graphics user')
    user = pwd.getpwnam(selected['user'])
    if (user.pw_uid, user.pw_gid) != (1000, 1000):
        raise RuntimeError('Expected the selected graphics account at UID/GID 1000')
    if not re.fullmatch('[A-Za-z0-9_.@-]+[.]service', selected['desktop_unit']):
        raise RuntimeError('Invalid maintenance desktop service')
    if (not selected['utilities'].startswith('/') or
            any(c.isspace() for c in selected['utilities']) or
            '..' in Path(selected['utilities']).parts):
        raise RuntimeError('Expected an absolute utilities directory')
    return selected
