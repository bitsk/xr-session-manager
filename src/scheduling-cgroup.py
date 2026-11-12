#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Set a utilization floor on this systemd service's own cgroup v2."""
import argparse
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import re
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--unit', required=True)
    parser.add_argument('--minimum', required=True, help='Utilization percentage, 0..100')
    args = parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9_.@-]+\.service', args.unit):
        parser.error('Expected an explicit service unit name')
    try:
        minimum = Decimal(args.minimum)
        if not minimum.is_finite() or not 0 <= minimum <= 100:
            raise ValueError('out of range')
    except (InvalidOperation, ValueError):
        parser.error('minimum must be a finite percentage from 0 to 100')
    if os.geteuid() != 0 or os.uname().machine != 'aarch64':
        raise RuntimeError('Apply only as root on the A081 target')
    if b'A081' not in Path('/proc/device-tree/model').read_bytes():
        raise RuntimeError('Target model is not A081')
    pam = subprocess.check_output(['systemctl', 'show', '--value', '--property=PAMName',
                                   args.unit], text=True).strip()
    if pam:
        raise RuntimeError('PAM can move children outside this service; bind the actual owned login scope first')
    groups = [line[3:] for line in Path('/proc/self/cgroup').read_text().splitlines()
              if line.startswith('0::')]
    if len(groups) != 1:
        raise RuntimeError('A unified cgroup v2 hierarchy is required')
    relative = Path(groups[0])
    if not relative.is_absolute() or '..' in relative.parts or relative.name != args.unit:
        raise RuntimeError('ExecStartPost must run in the named service cgroup')
    group = Path('/sys/fs/cgroup') / relative.relative_to('/')
    floor = group / 'cpu.uclamp.min'
    before = floor.read_text().strip()
    floor.write_text(format(minimum, '.2f') + '\n')
    after = floor.read_text().strip()
    # The kernel quantizes percentages to its integer capacity scale (1024).
    if abs(Decimal(after) - minimum) > Decimal('0.11'):
        floor.write_text(before + '\n')
        raise RuntimeError('Utilization floor readback mismatch; previous value restored')
    print(json.dumps({'format': 'qiyu-xr-cgroup-scheduling-v1', 'unit': args.unit,
                      'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                      'kernel': os.uname().release, 'cgroup': str(relative),
                      'minimum_before': before, 'minimum_after': after,
                      'effective_cpus': (group / 'cpuset.cpus.effective').read_text().strip(),
                      'cpu_weight': (group / 'cpu.weight').read_text().strip()}), flush=True)


if __name__ == '__main__':
    main()
