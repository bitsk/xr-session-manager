#!/usr/bin/env python3
"""One continuous real capture; source exit closes all consumers before VR stop."""
import json
import hashlib
import os
from pathlib import Path
import signal
import subprocess
import time
import traceback
from common import PROGRAMS
from common import PREFIX, UNIT, current, PROFILE
from hand_session import HandSession
from xr_controls import Controls
from controller_session import ControllerSession

assert os.geteuid() == 0
data = current()
raw = Path(data['raw'])
(raw / 'supervisor-started.json').write_text(json.dumps({
    'trial': data['trial'], 'pid': os.getpid(), 'started_monotonic_ns': time.monotonic_ns()}) + '\n')
tracking = PREFIX / 'tracking'
stopping = False
processes, logs = {}, []
result = {'trial': data['trial'], 'gate_seconds': data['seconds'], 'gain_v4l2': data['gain_v4l2'],
          'physical_accuracy_verified': False, 'controllers_started': False}
controller = ControllerSession(data, PREFIX)
hands = HandSession(PROFILE)
controls = Controls(PROFILE, data, hands)
control_started = False


def stop(_number, _frame):
    global stopping
    stopping = True


signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)


def spawn(name, argv, env=None):
    log = (raw / (name + '.log')).open('w')
    logs.append(log)
    process = subprocess.Popen(argv, env=env, stdout=log, stderr=subprocess.STDOUT)
    processes[name] = process
    return process


def finish(name, seconds):
    process = processes.get(name)
    if process is None:
        return
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=seconds)
    except subprocess.TimeoutExpired:
        result.setdefault('cleanup_errors', []).append(name + '_stop_timeout')
        process.kill()
        process.wait(timeout=3)
    result[name + '_exit'] = process.returncode


try:
    head_plugin = Path(PROFILE.get('head_tracking_plugin', str(tracking / 'liba081-qvr-client-live-stream.so')))
    if PROFILE.get('head_tracking_plugin'):
        expected = PROFILE['head_tracking_plugin_sha256']
        if hashlib.sha256(head_plugin.read_bytes()).hexdigest() != expected:
            raise RuntimeError('Selected head producer plugin checksum changed')
    controller_env = controller.start()
    result['controllers_started'] = controller.result['started']
    relay = spawn('relay', ['python3', str(PROGRAMS / 'relay.py'), '--source', data['raw_socket'],
                          '--output', data['public_socket'], '--seconds', '0' if data['seconds'] == 0 else '430'])
    observer = spawn('observer', [str(tracking / 'a081-tracking-observe'), data['raw_socket'],
                                  str(raw / 'packets.bin'), '0' if data['seconds'] == 0 else '420'])
    producer = spawn('producer', ['python3', str(PROGRAMS / 'run-camera-rig.py'), str(tracking),
            str(tracking / 'a081-qvr-stock-live-stream'), '--timing',
            '--gate-seconds', str(data['seconds']), '--gain', str(data['gain_v4l2']),
            '--visual-plugin', str(head_plugin),
            '--cdsp-runner', str(PROGRAMS / 'run-cdsp.sh'),
            '--cdsp-daemon', str(tracking / 'hexagonrpcd-a081')],
            dict(os.environ, A081_QVR_TRACKING_SOCKET=data['raw_socket'], **controller_env, **hands.environment()))
    source_ended = None
    while producer.poll() is None and not stopping:
        controller.check()
        if not control_started and (raw / 'packets.bin').exists() and (raw / 'packets.bin').stat().st_size:
            if hands.enabled and controls.preferences['hand_tracking_enabled']:
                try: hands.start()
                except Exception as error:
                    hands.result.update({'pass':False,'error':repr(error)})
                    controls.last_error=str(error)
                    controls.preferences['hand_tracking_enabled']=False
            controls.start()
            control_started = True
        # systemd stops the dependent hand unit before the head unit. The
        # session records shutdown intent before submitting that transaction.
        if hands.started and not current().get('shutdown_requested', False): hands.check()
        # A failed relay or evidence consumer is a failed trial, never a
        # reason to leave an apparently healthy UI with an old valid pose.
        for name, process in (('relay', relay), ('observer', observer)):
            if process.poll() is not None:
                if process.returncode != 0:
                    raise RuntimeError(name + ' failed while producer was running')
                # The actual stream closes before the wrapper finishes its
                # DMA/IIO/GPIO cleanup. Allow that bounded normal drain.
                if source_ended is None:
                    source_ended = time.monotonic()
        if source_ended is not None and time.monotonic() - source_ended > 55:
            raise RuntimeError('Producer did not finish after source disconnected')
        time.sleep(0.05)
    if data['seconds'] == 0 and not stopping:
        raise RuntimeError('Continuous producer ended without an explicit stop')
except BaseException as error:
    result['error'] = repr(error)
    result['traceback'] = traceback.format_exc()
finally:
    # SIGTERM goes to the Python gate owner first. It lowers FSIN, writes S to
    # the existing stop pipe and waits for the original QVR/DSP clean close.
    controls.close()
    result['hand_tracking'] = hands.stop()
    finish('producer', 55)
    result['controller'] = controller.stop()
    if not result['controller']['pass']:
        result.setdefault('cleanup_errors', []).append('controller_session')
    # Closing the output socket immediately clears the driver's full pose;
    # stale samples also expire independently after the unchanged 100 ms gate.
    finish('relay', 3)
    Path(data['producer_done']).write_text(str(time.monotonic_ns()) + '\n')
    observer = processes.get('observer')
    if observer is not None and observer.poll() is None:
        try:
            observer.wait(timeout=3)
        except subprocess.TimeoutExpired:
            finish('observer', 3)
    if observer is not None:
        result['observer_exit'] = observer.returncode
    result['stop_requested'] = stopping
    result['completed_monotonic_ns'] = time.monotonic_ns()
    result['pass'] = not result.get('error') and not result.get('cleanup_errors') and all(
        result.get(name + '_exit') == 0 for name in ('producer', 'relay', 'observer'))
    path = Path(data['tracking_result'])
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(result, indent=2) + '\n')
    temporary.replace(path)
    for log in logs:
        log.close()
    print(json.dumps(result), flush=True)
    if not stopping:
        # A background OpenVR observer remains connected during this interval
        # to establish real invalidation after the producer stopped.
        time.sleep(2)
        subprocess.run(['systemctl', '--no-block', 'stop', UNIT])

raise SystemExit(0 if result['pass'] else 1)
