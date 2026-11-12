#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit filesystem-only boot selection; never starts VR or changes boot slots."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import shlex
from profile import load

HEADER = '# Managed by qiyu-vr-session profile boot selector\n'


def inside(root, path):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('Expected an absolute path within target root')
    return root / path.relative_to('/')


def systemctl(root, *args, check=True):
    return subprocess.run(['systemctl', '--root', str(root), *args], check=check,
                          capture_output=True, text=True)


def enable_state(root, unit):
    result = systemctl(root, 'is-enabled', unit, check=False)
    return result.stdout.strip()


def set_enabled(root, unit, enabled):
    systemctl(root, 'enable' if enabled else 'disable', unit)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('/'))
    parser.add_argument('--profile', type=Path, required=True,
                        help='Absolute profile name inside the target root')
    parser.add_argument('--mode', choices=('vr', 'desktop', 'restore'), required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('Boot selection requires root')
    root = args.root.resolve()
    profile = load(inside(root, args.profile))
    unit = 'qiyu-' + profile['id'] + '-boot.service'
    unit_file = inside(root, '/etc/systemd/system/' + unit)
    state_file = inside(root, '/var/lib/qiyu-vr-session/' + profile['id'] + '-boot.json')
    program = Path(profile['prefix']) / 'session.py'
    if not inside(root, program).is_file():
        raise RuntimeError('Install the complete session package before boot selection')
    if unit_file.exists() and not unit_file.read_text().startswith(HEADER):
        raise RuntimeError('Refusing to replace an unrelated boot unit')
    if not state_file.exists():
        if args.mode == 'restore':
            raise RuntimeError('No previous boot configuration was saved')
        state_file.parent.mkdir(parents=True, exist_ok=True)
        previous = {'profile_id': profile['id'], 'unit': unit,
                    'desktop_unit': profile['desktop_unit'],
                    'desktop_enabled': enable_state(root, profile['desktop_unit']),
                    'default_target': systemctl(root, 'get-default').stdout.strip(),
                    'boot_enabled': enable_state(root, unit),
                    'boot_unit_before': unit_file.read_text() if unit_file.exists() else None}
        state_file.write_text(json.dumps(previous, indent=2) + '\n')
        state_file.chmod(0o600)
    previous = json.loads(state_file.read_text())
    if previous['unit'] != unit or previous['desktop_unit'] != profile['desktop_unit']:
        raise RuntimeError('Saved boot rollback does not match this profile')
    if args.mode == 'restore':
        systemctl(root, 'disable', unit, check=False)
        if previous['boot_unit_before'] is None:
            unit_file.unlink(missing_ok=True)
        else:
            unit_file.write_text(previous['boot_unit_before'])
        set_enabled(root, profile['desktop_unit'], previous['desktop_enabled'] == 'enabled')
        systemctl(root, 'set-default', previous.get('default_target', 'graphical.target'))
        if previous['boot_enabled'] == 'enabled':
            set_enabled(root, unit, True)
    else:
        argv = ['/usr/bin/python3', str(program), 'start', '--seconds', '0', '--gain', '16']
        if profile.get('controller_plan'):
            argv += ['--controller-plan', profile['controller_plan']]
        if profile.get('driver_directory'):
            argv += ['--driver-directory', profile['driver_directory'],
                     '--driver-sha256', profile['driver_sha256']]
        # Paths in the profile are restricted to ordinary absolute filenames;
        # quote each argument for systemd's Exec* parser, never invoke a shell.
        command = shlex.join(argv)
        unit_file.parent.mkdir(parents=True, exist_ok=True)
        unit_file.write_text(HEADER + f'''[Unit]
Description=Primary native XR session ({profile['id']})
After=systemd-user-sessions.service systemd-logind.service a081-sensors-files.service
Requires=systemd-logind.service a081-sensors-files.service

[Service]
Type=oneshot
RemainAfterExit=yes
Environment=QIYU_VR_SESSION_PROFILE={args.profile}
ExecStart={command}
ExecStop=/usr/bin/python3 {program} stop
TimeoutStartSec=120
TimeoutStopSec=180

[Install]
WantedBy=multi-user.target
''')
        if args.mode == 'vr':
            # Disable first so two display owners cannot be pulled into boot.
            # A runtime startup failure still has its explicit desktop fallback.
            set_enabled(root, profile['desktop_unit'], False)
            try:
                set_enabled(root, unit, True)
                systemctl(root, 'set-default', 'multi-user.target')
            except BaseException:
                set_enabled(root, profile['desktop_unit'], previous['desktop_enabled'] == 'enabled')
                raise
        else:
            set_enabled(root, unit, False)
            set_enabled(root, profile['desktop_unit'], True)
            systemctl(root, 'set-default', 'graphical.target')
    print(json.dumps({'profile_id': profile['id'], 'mode': args.mode, 'unit': unit,
                      'root': str(root), 'active_sessions_changed': False,
                      'boot_partitions_changed': False,
                      'default_target': systemctl(root, 'get-default').stdout.strip(),
                      'desktop_enabled': enable_state(root, profile['desktop_unit']),
                      'xr_boot_enabled': enable_state(root, unit)}, indent=2))


if __name__ == '__main__':
    main()
