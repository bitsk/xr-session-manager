# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit distribution/scene choices; the existing supervisor owns hardware."""
import hashlib
import json
import os
from pathlib import Path
import re
import stat


def load(path):
    path = Path(path)
    if not path.is_absolute():
        raise ValueError('Session profile must have an absolute path')
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise ValueError('Session profile must be a root-owned, immutable regular file')
    payload = path.read_bytes()
    data = json.loads(payload)
    required = {'format', 'id', 'user', 'uid', 'gid', 'home', 'rootfs_id', 'prefix',
                'runtime_directory', 'unit', 'tracking_unit', 'desktop_unit',
                'desktop_owner', 'steamvr', 'system_icd', 'kms_icd', 'utilities', 'scene'}
    if not required <= data.keys() or data['format'] != 'qiyu-vr-session-profile-v1':
        raise ValueError('Unexpected session profile schema')
    if not re.fullmatch('[a-z0-9][a-z0-9-]{0,47}', data['id']):
        raise ValueError('Invalid profile ID')
    if not re.fullmatch('[a-z_][a-z0-9_-]*', data['user']):
        raise ValueError('Invalid session user')
    if not all(isinstance(data[k], int) and data[k] > 0 for k in ('uid', 'gid')):
        raise ValueError('Session graphics must use an explicit non-root UID/GID')
    if not (info.st_mode & 0o004 or (info.st_gid == data['gid'] and info.st_mode & 0o040)):
        raise ValueError('Session profile must be readable by its graphics user')
    for name in ('home', 'prefix', 'runtime_directory', 'steamvr', 'system_icd', 'kms_icd', 'utilities'):
        if not isinstance(data[name], str) or not data[name].startswith('/') or any(c.isspace() for c in data[name]):
            raise ValueError('Expected absolute profile path: ' + name)
    library_paths = data.get('runtime_library_paths', [])
    if not isinstance(library_paths, list):
        raise ValueError('Runtime library paths must be an array')
    for library_path in library_paths:
        if (not isinstance(library_path, str) or not Path(library_path).is_absolute() or
                '\0' in library_path or ':' in library_path or any(c.isspace() for c in library_path) or
                '..' in Path(library_path).parts):
            raise ValueError('Expected ordinary absolute runtime library directory')
    if type(data.get('tracking_recovery_display_guard', False)) is not bool:
        raise ValueError('Tracking recovery display guard must be an explicit boolean')
    runtime_environment = data.get('runtime_environment', {})
    models = data.get('controller_render_models', {})
    if (not isinstance(models, dict) or
            (models and set(models) != {'left', 'right'}) or
            any(not isinstance(value, str) or
                not re.fullmatch(r'\{monado\}[a-zA-Z0-9_-]+', value)
                for value in models.values())):
        raise ValueError('Controller models require explicit left/right Monado resource names')
    if not isinstance(runtime_environment, dict):
        raise ValueError('Runtime environment must be an object')
    selected_fields = {'TU_A081_KMS_DEVICE', 'VK_DRIVER_FILES', 'VK_ICD_FILENAMES'}
    for key, value in runtime_environment.items():
        if key in selected_fields:
            raise ValueError('Runtime display/ICD selection belongs to the explicit profile fields')
        if (not re.fullmatch(r'(?:TU|VK|MESA)_[A-Z0-9_]+', key) or
                not isinstance(value, str) or '\0' in value):
            raise ValueError('Runtime overrides allow only uppercase TU_/VK_/MESA_ string fields')
    for name in ('unit', 'tracking_unit', 'desktop_unit', 'viewer_unit'):
        if data.get(name) and not re.fullmatch('[A-Za-z0-9_.@-]+[.]service', data[name]):
            raise ValueError('Expected service name: ' + name)
    consumers = data.get('sensor_consumers', [])
    if (not isinstance(consumers, list) or
            any(not isinstance(unit, str) or not re.fullmatch('[A-Za-z0-9_][A-Za-z0-9_.@-]*[.]service', unit)
                for unit in consumers) or len(set(consumers)) != len(consumers)):
        raise ValueError('Sensor consumers must be unique explicit system service names')
    owner = data['desktop_owner']
    if not isinstance(owner, dict) or not isinstance(owner.get('argv'), list) or not owner['argv']:
        raise ValueError('Exact desktop compositor argv is required')
    if any(not isinstance(v, str) or '\0' in v or '\n' in v for v in owner['argv']):
        raise ValueError('Invalid desktop owner argv')
    executable = owner.get('executable', owner['argv'][0])
    if (not isinstance(executable, str) or not Path(executable).is_absolute() or
            any(c.isspace() for c in executable) or '\0' in executable or '..' in Path(executable).parts):
        raise ValueError('Desktop compositor executable must be an ordinary absolute path')
    if owner.get('user_unit') and not re.fullmatch('[A-Za-z0-9_.@-]+[.]service', owner['user_unit']):
        raise ValueError('Expected desktop compositor user service name')
    if not isinstance(owner.get('environment', {}), dict):
        raise ValueError('Desktop owner environment must be an object')
    for key, value in owner.get('environment', {}).items():
        if (not re.fullmatch('[A-Z_][A-Z0-9_]*', key) or not isinstance(value, str) or
                '\0' in value or '\n' in value):
            raise ValueError('Invalid desktop owner environment')
    scene = data['scene']
    if scene.get('mode') not in ('steam', 'command'):
        raise ValueError('Scene mode must be steam or command')
    if type(scene.get('wait_for_first_tracking_packet', False)) is not bool:
        raise ValueError('First tracking packet gate must be an explicit boolean')
    if scene.get('wait_for_first_tracking_packet') and scene['mode'] != 'command':
        raise ValueError('First tracking packet gate applies only to command scenes')
    if scene['mode'] == 'command':
        if (not scene.get('argv') or not scene['argv'][0].startswith('/') or
                any(not isinstance(v, str) or '\0' in v for v in scene['argv'])):
            raise ValueError('Native scene requires explicit argv')
        if not isinstance(scene.get('environment', {}), dict):
            raise ValueError('Scene environment must be an object')
        for k, v in scene.get('environment', {}).items():
            if not re.fullmatch('[A-Z_][A-Z0-9_]*', k) or not isinstance(v, str) or '\0' in v:
                raise ValueError('Invalid native scene environment')
    data['_path'] = str(path)
    data['_sha256'] = hashlib.sha256(payload).hexdigest()
    return data


