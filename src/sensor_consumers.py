# SPDX-License-Identifier: GPL-3.0-or-later
"""Pause explicit desktop IIO consumers without changing sensor attributes."""
from pathlib import Path
import subprocess
import time


def _masked(unit, base='/run/systemd/system'):
    path = Path(base) / unit
    return path.is_symlink() and str(path.readlink()) == '/dev/null'


def _state(unit):
    output = subprocess.check_output(['systemctl', 'show', unit, '--no-pager',
        '--property=ActiveState,LoadState,UnitFileState'], text=True, timeout=10)
    state = dict(line.split('=', 1) for line in output.splitlines() if '=' in line)
    if state.get('LoadState') not in ('loaded', 'masked'):
        raise RuntimeError('Configured sensor consumer is not installed: ' + unit)
    enabled = subprocess.run(['systemctl', 'is-enabled', unit], text=True,
                             capture_output=True, timeout=10)
    state.update(enabled=enabled.stdout.strip(), runtime_mask=_masked(unit),
                 persistent_mask=_masked(unit, '/etc/systemd/system'))
    return state


def _run(*arguments):
    subprocess.run(['systemctl', *arguments], check=True, timeout=25)


def pause(data, checkpoint, units):
    """Journal original state before masking/stopping each explicit service."""
    records = data.setdefault('sensor_consumers_state', [])
    if records:
        raise RuntimeError('Sensor consumers were already paused for this session')
    for unit in units:
        before = _state(unit)
        record = {'unit': unit, 'before': before,
                  'active_before': before['ActiveState'] == 'active',
                  'mask_requested': False, 'restored': False}
        records.append(record)
        checkpoint(data)
        if not before['runtime_mask'] and not before['persistent_mask']:
            # The runtime mask is installed before systemd stops the process,
            # so a desktop D-Bus activation cannot reopen IIO in the gap.
            record['mask_requested'] = True
            checkpoint(data)
            _run('mask', '--runtime', '--now', unit)
        else:
            _run('stop', unit)
        after = _state(unit)
        if after['ActiveState'] not in ('inactive', 'failed'):
            raise RuntimeError('Sensor consumer did not stop: ' + unit)
        if not after['runtime_mask'] and not after['persistent_mask']:
            raise RuntimeError('Sensor consumer can still be D-Bus activated: ' + unit)
        record.update(paused=True, after_pause=after, paused_monotonic_ns=time.monotonic_ns())
        checkpoint(data)


def restore(data, checkpoint):
    """Undo only this session's runtime mask, preserving enablement."""
    errors = []
    for record in reversed(data.get('sensor_consumers_state', [])):
        if record.get('restored'):
            continue
        unit, before = record['unit'], record['before']
        try:
            if record['mask_requested'] and not before['runtime_mask'] and _masked(unit):
                _run('unmask', '--runtime', unit)
            temporary_unmask = record['active_before'] and before['runtime_mask']
            if temporary_unmask:
                _run('unmask', '--runtime', unit)
            try:
                if record['active_before']:
                    _run('start', unit)
            finally:
                if temporary_unmask:
                    _run('mask', '--runtime', unit)
            after = _state(unit)
            if ((after['ActiveState'] == 'active') != record['active_before'] or
                    after['enabled'] != before['enabled'] or
                    after['runtime_mask'] != before['runtime_mask'] or
                    after['persistent_mask'] != before['persistent_mask']):
                raise RuntimeError('Sensor consumer did not regain its original state')
            record.update(restored=True, after_restore=after,
                          restored_monotonic_ns=time.monotonic_ns())
        except Exception as error:
            record['restore_error'] = repr(error)
            errors.append('sensor_consumer_restore ' + unit + ': ' + str(error))
        checkpoint(data)
    return errors
