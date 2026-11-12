#!/usr/bin/env python3
"""Stop only the configured managed compositor, including its PAM orphan."""
import json
import os
from pathlib import Path
import re
import select
import signal
import subprocess
import time
from common import PROFILE, DESKTOP, USER, UID, SESSION_HOME, ROOTFS

UNIT = DESKTOP
ARGV = PROFILE.get('desktop_owner', {}).get('argv', [
    '/opt/mix-steamos-graphics/bin/kwin_wayland', '--drm', '--xwayland', '--no-lockscreen',
    '--exit-with-session', '/usr/local/bin/mix-steam desktop -nobigpicture'])
OWNER_ENV = PROFILE.get('desktop_owner', {}).get('environment',
    {'HOME': str(SESSION_HOME), 'XDG_VTNR': '7', 'MIX_STEAMOS_DESKTOP_MODE': 'steam'})
EXECUTABLE = PROFILE.get('desktop_owner', {}).get('executable', ARGV[0])
USER_UNIT = PROFILE.get('desktop_owner', {}).get('user_unit')
USER_CGROUP = ('/user.slice/user-' + str(UID) + '.slice/user@' + str(UID) +
               '.service/session.slice/' + USER_UNIT) if USER_UNIT else None


def user_systemctl(*arguments):
    return ['runuser', '-u', USER, '--', 'env', 'XDG_RUNTIME_DIR=/run/user/' + str(UID),
            'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/' + str(UID) + '/bus',
            'systemctl', '--user', *arguments]


def verify_user_unit(record):
    output = subprocess.check_output(user_systemctl(
        'show', USER_UNIT, '--no-pager', '--property=ControlGroup,MainPID,ActiveState'),
        text=True, timeout=10)
    state = dict(line.split('=', 1) for line in output.splitlines() if '=' in line)
    if (state.get('ControlGroup') != USER_CGROUP or state.get('ActiveState') != 'active' or
            state.get('MainPID') != str(record['pid'])):
        raise RuntimeError('Desktop user service does not own the pinned compositor')
    if identity(record['pid']) != record:
        raise RuntimeError('Pinned desktop process identity changed')
    return state


def identity(pid):
    path = Path('/proc') / str(pid)
    try:
        if path.stat().st_uid != UID:
            return None
        argv = [s.decode() for s in (path / 'cmdline').read_bytes().rstrip(b'\0').split(b'\0')]
        if argv != ARGV or str((path / 'exe').readlink()) != EXECUTABLE:
            return None
        env = dict(item.split(b'=', 1) for item in (path / 'environ').read_bytes().split(b'\0') if b'=' in item)
        if any(env.get(k.encode()) != v.encode() for k, v in OWNER_ENV.items()):
            return None
        if USER_UNIT:
            if '0::' + USER_CGROUP not in (path / 'cgroup').read_text().splitlines():
                return None
            ownership = {'user_unit': USER_UNIT, 'cgroup': USER_CGROUP}
        else:
            session = env.get(b'XDG_SESSION_ID', b'').decode()
            if not re.fullmatch('[a-zA-Z0-9]+', session):
                return None
            scope = '/user.slice/user-' + str(UID) + '.slice/session-' + session + '.scope'
            if scope not in (path / 'cgroup').read_text().splitlines()[0]:
                return None
            ownership = {'session': session}
        fields = (path / 'stat').read_text().rsplit(')', 1)[1].split()
        if fields[0] == 'Z':
            return None
        return {'pid': int(pid), 'start_ticks': fields[19], 'argv': argv, **ownership}
    except (FileNotFoundError, ProcessLookupError):
        return None


def owners():
    found = []
    for path in Path('/proc').glob('[0-9]*'):
        try:
            if path.stat().st_uid != UID or (path / 'comm').read_text().strip() != Path(EXECUTABLE).name[:15]:
                continue
            record = identity(path.name)
            if record is None:
                # Refuse an unrelated compositor; never broaden the PID match.
                fields = (path / 'stat').read_text().rsplit(')', 1)[1].split()
                if fields[0] != 'Z':
                    raise RuntimeError('Unrecognized desktop owner: ' + path.name)
            else:
                found.append(record)
        except (FileNotFoundError, ProcessLookupError):
            continue
    if len(found) > 1:
        raise RuntimeError('Multiple managed desktop sessions need explicit inspection')
    return found


def stop_desktop():
    result = {'desktop_owners_before_stop': owners(), 'signals': []}
    for record in result['desktop_owners_before_stop']:
        if USER_UNIT:
            result['desktop_user_unit_before_stop'] = verify_user_unit(record)
            subprocess.run(user_systemctl('stop', USER_UNIT), check=True, timeout=25)
            if owners():
                raise RuntimeError('Managed desktop user service still owns a compositor after stop')
            result['desktop_user_unit_stopped'] = USER_UNIT
            continue
        pid = record['pid']
        descriptor = os.pidfd_open(pid)
        try:
            waiter = select.poll()
            waiter.register(descriptor, select.POLLIN)
            for number, wait_ms in ((signal.SIGTERM, 6000), (signal.SIGKILL, 3000)):
                if waiter.poll(0):
                    break
                if identity(pid) != record:
                    raise RuntimeError('Pinned desktop process identity changed')
                # Let the compositor release KMS before its PAM/tty service.
                # Forced fallback is limited to this exact pinned process.
                signal.pidfd_send_signal(descriptor, number)
                result['signals'].append({'pid': pid, 'signal': number.name})
                if waiter.poll(wait_ms):
                    break
            else:
                raise RuntimeError('Owned desktop compositor did not exit after bounded stop')
        finally:
            os.close(descriptor)
    subprocess.run(['systemctl', 'stop', UNIT], check=True, timeout=25)
    if owners():
        raise RuntimeError('Managed desktop still exists after stop')
    result['desktop_processes_gone'] = True
    print(json.dumps(result), flush=True)
    return result


def restore_desktop():
    subprocess.run(['systemctl', 'start', UNIT], check=True, timeout=20)
    deadline, stable_since = time.monotonic() + 10, None
    pending_owner = None
    while time.monotonic() < deadline:
        state = subprocess.check_output(['systemctl', 'is-active', UNIT], text=True).strip()
        try:
            current_owners = owners() if state == 'active' else []
        except RuntimeError as error:
            if not (str(error).startswith('Unrecognized desktop owner: ') or
                    str(error) == 'Multiple managed desktop sessions need explicit inspection'):
                raise
            # A newly spawned compositor can change identity during /proc reads
            # or briefly fork a child before that child execs Xwayland.
            # Only this restore window retries; stop_desktop remains strict.
            pending_owner = str(error)
            current_owners = []
        if state == 'active' and current_owners:
            if USER_UNIT:
                try:
                    verify_user_unit(current_owners[0])
                except (RuntimeError, subprocess.CalledProcessError):
                    stable_since = None
                    time.sleep(0.2)
                    continue
            stable_since = stable_since or time.monotonic()
            if time.monotonic() - stable_since >= 2:
                return {'desktop_restore_active_seconds': 2,
                        'desktop_owner': current_owners[0]}
        else:
            stable_since = None
        time.sleep(0.2)
    raise RuntimeError('Desktop did not remain active with its real compositor process' +
                       (': ' + pending_owner if pending_owner else ''))


if __name__ == '__main__':
    assert os.geteuid() == 0 and os.uname().machine == 'aarch64'
    assert Path('/etc/a081-rootfs-id').read_text().strip() == ROOTFS
    assert b'A081' in Path('/proc/device-tree/model').read_bytes()
    stop_desktop()
