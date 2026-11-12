#!/usr/bin/env python3
"""Lifecycle glue for a real SDK worker, packet relay and shared-camera input.

Imported only by the isolated controller candidate's root tracking supervisor.
The caller provides an immutable plan pinning its actual SDK launcher and
optional Nordic feeder. This module never opens cameras or radio itself.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import time
from trusted_paths import trusted_opt_path

NORDIC_RESET = Path('/sys/bus/spi/devices/spi3.0/reset_asserted')


def trusted_file(path):
    return trusted_opt_path(path)


def prepare_sdk_mountpoints(plan):
    """Restore empty SDK mount directories omitted by file-only collectors."""
    argv = plan['producer_argv']
    if Path(argv[0]).name != 'run-sdk-worker.sh':
        return []
    if argv.count('--root') != 1 or argv.index('--root') + 1 >= len(argv):
        raise ValueError('SDK launcher needs one explicit trusted root directory')
    root = trusted_opt_path(argv[argv.index('--root') + 1])
    if not root.is_dir():
        raise ValueError('SDK root must be an existing private directory')
    result = []
    for name, mode in (('proc', 0o755), ('dev', 0o755), ('tmp', 0o1777)):
        path = root / name
        created = not os.path.lexists(path)
        if created:
            path.mkdir(mode=0o755)
        info = path.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or
                (info.st_mode & 0o022 and not (name == 'tmp' and stat.S_IMODE(info.st_mode) == 0o1777))):
            raise ValueError('Untrusted SDK mount directory: ' + str(path))
        path.chmod(mode)
        info = path.lstat()
        if info.st_uid != 0 or stat.S_IMODE(info.st_mode) != mode or not stat.S_ISDIR(info.st_mode):
            raise RuntimeError('SDK mount directory readback failed: ' + str(path))
        result.append({'path': str(path), 'created': created, 'mode': oct(mode)})
    return result


def shared_cdsp_after(directory):
    """The existing global DMA/FD check, after both head and SDK PDs exit."""
    directory = Path(directory)
    deadline = time.monotonic() + 3
    attempts = 0
    while True:
        attempts += 1
        raw = Path('/sys/kernel/debug/dma_buf/bufinfo').read_text()
        (directory / 'dma-after.txt').write_text(raw)
        if 'exp_name' not in raw or not re.search(r'^Total \d+ objects, \d+ bytes$', raw, re.M):
            raise RuntimeError('Unknown DMA exporter report; cannot verify reclamation')
        buffers = []
        for line in raw.splitlines():
            fields = line.split()
            if len(fields) >= 6 and fields[4] == 'fastrpc':
                buffers.append({'bytes': int(fields[0], 10), 'inode': int(fields[5], 10)})
        fds = []
        for fd in Path('/proc').glob('[0-9]*/fd/*'):
            try:
                if str(fd.readlink()) in ('/dev/fastrpc-cdsp', '/dev/fastrpc-cdsp-secure'):
                    fds.append(str(fd))
            except FileNotFoundError:
                pass
        ok = not buffers and not fds
        if attempts == 1:
            (directory / 'dma-after-first.txt').write_text(raw)
            (directory / 'dma-after-first.json').write_text(json.dumps(
                {'fastrpc_buffers': buffers, 'cdsp_fds': fds, 'zero': ok}, indent=2) + '\n')
        if ok or time.monotonic() >= deadline:
            break
        time.sleep(0.1)
    result = {'phase': 'after', 'rc': 0 if ok else 1, 'fastrpc_objects': len(buffers),
              'fastrpc_bytes': sum(b['bytes'] for b in buffers), 'buffers': buffers,
              'open_cdsp_fds': fds, 'attempts': attempts}
    (directory / 'dma-after.json').write_text(json.dumps(result, indent=2) + '\n')
    return result


def read_plan(path, session_seconds=None):
    path = trusted_file(path)
    data = path.read_bytes()
    plan = json.loads(data)
    required = {'format', 'producer_argv', 'producer_executable_sha256', 'input_socket', 'camera_env'}
    if not required <= set(plan) or set(plan) - required - {'radio_argv', 'radio_executable_sha256'}:
        raise ValueError('Unexpected controller plan fields')
    if plan['format'] != 'a081-controller-session-plan-v1':
        raise ValueError('Unexpected controller plan version')
    input_socket = plan['input_socket']
    if not re.fullmatch(r'/run/a081-controller-input-[a-zA-Z0-9_-]+/input.sock', input_socket):
        raise ValueError('Input socket must be a private per-trial /run endpoint')
    env = plan['camera_env']
    startup_ae_environment = {'A081_QVR_STARTUP_AE', 'A081_STOCK_ISO100_REAL_GAIN'}
    if not isinstance(env, dict) or any((not re.fullmatch(r'A081_CONTROLLER_[A-Z0-9_]+', k) and k not in startup_ae_environment) or
                                       not isinstance(v, str) or '\0' in v for k, v in env.items()):
        raise ValueError('Unexpected camera input environment')
    if env.get('A081_CONTROLLER_INPUT_SOCKET') != input_socket:
        raise ValueError('Camera and SDK must use the same input socket')
    for role in ('producer', 'radio'):
        argv = plan.get(role + '_argv')
        if role == 'radio' and argv is None:
            continue
        if not isinstance(argv, list) or not argv or any(not isinstance(v, str) or '\0' in v for v in argv):
            raise ValueError('Expected explicit controller argv')
        program = trusted_file(argv[0])
        if hashlib.sha256(program.read_bytes()).hexdigest() != plan[role + '_executable_sha256']:
            raise ValueError('Controller launcher digest changed: ' + str(program))
        if role == 'producer' and argv.count('{packet_fd}') != 1:
            raise ValueError('SDK launcher must take exactly one {packet_fd} argument')
        if role == 'radio' and any('{' in value or '}' in value for value in argv):
            raise ValueError('No substitutions in radio argv')
        if session_seconds is not None:
            # These workers begin before SteamVR startup and remain alive
            # until shared-camera cleanup. Cover the owned unit's complete
            # RuntimeMaxSec window, not just the visible observation period.
            if argv.count('--seconds') != 1:
                raise ValueError('Controller worker requires an explicit lifetime')
            index = argv.index('--seconds') + 1
            if index == len(argv) or not argv[index].isdigit():
                raise ValueError('Controller worker lifetime must be numeric')
            lifetime = int(argv[index])
            if session_seconds == 0 and lifetime != 0:
                raise ValueError('Continuous controller session requires continuous workers')
            if lifetime and lifetime < session_seconds + 145:
                raise ValueError('Controller worker lifetime must cover session seconds + 145')
    return plan, hashlib.sha256(data).hexdigest()


class ControllerSession:
    def __init__(self, data, prefix):
        self.data, self.prefix = data, Path(prefix)
        self.processes, self.logs = {}, []
        self.plan = None
        self.input_directory = None
        self.result = {'started': False, 'physical_position_accepted': False}

    def spawn(self, name, argv, pass_fds=()):
        log = (Path(self.data['raw']) / ('controller-' + name + '.log')).open('wb')
        self.logs.append(log)
        process = subprocess.Popen(argv, pass_fds=pass_fds, stdout=log, stderr=subprocess.STDOUT,
                                   start_new_session=True)
        self.processes[name] = process
        self.result.setdefault('owned_process_groups', {})[name] = process.pid
        return process

    def signal_group(self, name, sig):
        process = self.processes[name]
        # A live, unreaped Popen child pins its PID. Each child called setsid
        # before exec, so this group contains only the owned launcher and its
        # descendants, including unshare's actual ARM32 namespace worker.
        if process.poll() is not None:
            return
        try:
            if os.getpgid(process.pid) != process.pid:
                raise RuntimeError('Owned controller process left its private group: ' + name)
            os.killpg(process.pid, sig)
            self.result.setdefault('group_signals', []).append({'name': name, 'signal': int(sig),
                                                               'monotonic_ns': time.monotonic_ns()})
        except ProcessLookupError:
            pass

    def start(self):
        if not self.data.get('controller_plan'):
            return {}
        self.plan, digest = read_plan(self.data['controller_plan'])
        if digest != self.data['controller_plan_sha256']:
            raise ValueError('Controller plan changed after session creation')
        self.result['sdk_mountpoints'] = prepare_sdk_mountpoints(self.plan)
        # The camera owner releases reset only after camera-active/HWGP1 are
        # high. Radio may open its transport now and wait for the real DRDY.
        if NORDIC_RESET.read_text().strip() != '1':
            raise RuntimeError('Nordic reset must be held before controller startup')
        self.result['nordic_reset_held_before_start'] = True
        directory = Path(self.data['controller_pose_socket']).parent
        input_directory = Path(self.plan['input_socket']).parent
        input_directory.mkdir(mode=0o700)
        self.input_directory = input_directory
        read_fd, write_fd = os.pipe2(os.O_CLOEXEC)
        try:
            relay = ['python3', str(self.prefix / 'controller-relay.py'),
                '--directory', str(directory), '--input-fd', str(read_fd), '--seconds', '0']
            # Continuous operation publishes live packets without an unbounded
            # raw recording. Bounded validation retains its full packet log.
            if self.data['seconds']:
                relay += ['--record', str(Path(self.data['raw']) / 'controller-packets.bin')]
            self.spawn('relay', relay, (read_fd,))
            substitutions = {'{packet_fd}': str(write_fd),
                             '{snapshot_directory}': str(Path(self.data['raw']) / 'controller-sdk-frames')}
            argv = [substitutions.get(value, value) for value in self.plan['producer_argv']]
            self.spawn('sdk', argv, (write_fd,))
        finally:
            os.close(read_fd); os.close(write_fd)
        deadline = time.monotonic() + 15
        input_path = Path(self.plan['input_socket'])
        output_path = Path(self.data['controller_pose_socket'])
        while time.monotonic() < deadline:
            for name, process in self.processes.items():
                if process.poll() is not None:
                    raise RuntimeError('Controller ' + name + ' exited during startup')
            if input_path.exists() and output_path.exists():
                for path in (input_path, output_path):
                    info = path.lstat()
                    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != 0:
                        raise ValueError('Unexpected controller endpoint owner/type')
                break
            time.sleep(.05)
        else:
            raise TimeoutError('Controller SDK/relay socket readiness')
        if self.plan.get('radio_argv'):
            self.spawn('radio', self.plan['radio_argv'])
        self.result.update(started=True, plan_sha256=digest, input_socket=str(input_path),
                           output_socket=str(output_path), radio_started='radio' in self.processes)
        return dict(self.plan['camera_env'])

    def check(self):
        if not self.plan:
            return
        for name, process in self.processes.items():
            if process.poll() is not None:
                raise RuntimeError('Controller ' + name + ' exited while QVR camera owner was running')

    def stop(self):
        # The caller first stops the shared camera/QVR producer. Only then
        # stop Nordic, close SDK input, and drain its packet output to EOF.
        if self.plan:
            try:
                held = NORDIC_RESET.read_text().strip() == '1'
                self.result['nordic_reset_held_after_camera_stop'] = held
                if not held:
                    self.result.setdefault('cleanup_errors', []).append('nordic_reset_not_held_after_camera_stop')
            except OSError as error:
                self.result.setdefault('cleanup_errors', []).append('nordic_reset_readback: ' + str(error))
        for name in ('radio', 'sdk', 'relay'):
            process = self.processes.get(name)
            if process is None:
                continue
            if process.poll() is None:
                if name == 'relay':
                    try: process.wait(timeout=2)
                    except subprocess.TimeoutExpired: self.signal_group(name, signal.SIGTERM)
                else:
                    self.signal_group(name, signal.SIGTERM)
            try:
                process.wait(timeout=8 if name == 'sdk' else 3)
            except subprocess.TimeoutExpired:
                self.result.setdefault('cleanup_errors', []).append(name + '_stop_timeout')
                self.signal_group(name, signal.SIGKILL)
                process.wait(timeout=3)
            self.result[name + '_exit'] = process.returncode
        if self.plan and self.plan['camera_env'].get('A081_CONTROLLER_SHARED_CDSP') == '1':
            try:
                final_cdsp = shared_cdsp_after(self.data['raw'])
                self.result['shared_cdsp_after'] = final_cdsp
                if final_cdsp['rc']:
                    self.result.setdefault('cleanup_errors', []).append('shared_cdsp_not_reclaimed')
            except (OSError, ValueError, RuntimeError) as error:
                self.result.setdefault('cleanup_errors', []).append('shared_cdsp_after: ' + str(error))
        if self.input_directory is not None:
            input_path = self.input_directory / 'input.sock'
            try:
                if input_path.exists():
                    info = input_path.lstat()
                    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != 0:
                        raise ValueError('Input socket ownership changed')
                    input_path.unlink()
                self.input_directory.rmdir()
            except (OSError, ValueError) as error:
                self.result.setdefault('cleanup_errors', []).append('input_socket_cleanup: ' + str(error))
        for log in self.logs:
            log.close()
        self.result['pass'] = (not self.plan or (self.result['started'] and not self.result.get('cleanup_errors') and
            all(self.result.get(name + '_exit') == 0 for name in self.processes)))
        self.result['completed_monotonic_ns'] = time.monotonic_ns()
        (Path(self.data['raw']) / 'controller-session-result.json').write_text(json.dumps(self.result, indent=2) + '\n')
        return self.result
