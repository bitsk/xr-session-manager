#!/usr/bin/python3
"""Bounded real A081 camera bring-up using the kernel-validated GPIO barrier.

Reuses the recipe verified in camera-barrier-r1/capture-barrier.py, with
the XR V4L2 input component owning image buffers. No exposure time is invented.
Profile-selected isolated session; input contracts unchanged.
Run only inside the coordinator-owned hardware window.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import platform
import re
import signal
import stat
import struct
import subprocess
import tempfile
import time
import traceback

from stock_ov7251_ae import StockOv7251Ae, stereo_luma
from passthrough_graph import PassthroughGraph
from common import PREFIX, ROOTFS, PROFILE
import hashlib

import gpiod
from gpiod.line import Direction, Value

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('base', type=Path)
parser.add_argument('capture', type=Path)
parser.add_argument('--timing', action='store_true', help='Use the kernel-accepted QCFT ancillary nodes')
parser.add_argument('--visual-plugin', type=Path, help='Original QVR consumer in the private CDSP process')
parser.add_argument('--cdsp-runner', type=Path)
parser.add_argument('--cdsp-daemon', type=Path)
parser.add_argument('--gate-seconds', type=float, default=2, help='Bounded real FSIN gate duration, 0 until explicit stop, or at most 300 seconds')
parser.add_argument('--gain', type=int, choices=(16, 64), default=16)
args = parser.parse_args()
os.environ.pop('A081_PASSTHROUGH_CAMERA_NODES', None)
passthrough_mode = os.environ.get('A081_PASSTHROUGH', '0')
if passthrough_mode not in ('0', '1'):
    parser.error('A081_PASSTHROUGH must be 0 or 1')

startup_ae = os.environ.get('A081_QVR_STARTUP_AE', '')
if startup_ae not in ('', 'stock-ov7251'):
    parser.error('Unknown startup AE policy')
# stock-sensor-exposure.jsonl: ISO200 -> real2/reg32; ISO862 -> real8.62/reg137.
iso100_real_gain = float(os.environ.get('A081_STOCK_ISO100_REAL_GAIN', '1'))
if startup_ae and iso100_real_gain != 1:
    parser.error('Startup AE requires the measured stock ISO100 real gain of 1')
controller_exposure_profile = os.environ.get('A081_CONTROLLER_EXPOSURE_PROFILE', '')
if controller_exposure_profile not in ('', 'stock-40us-nearest-lines', 'kernel-three-phase'):
    parser.error('Unknown controller exposure profile')
# Existing mode: row time=(640+288)/48MHz =19.333us. The nearest whole
# integration to the observed stock40us is2rows=38666ns, not exactly40us.
controller_exposure_lines = 2 if controller_exposure_profile else 250
controller_gain = 32 if controller_exposure_profile else args.gain
three_phase = controller_exposure_profile == 'kernel-three-phase'
pattern_phase2_lines, pattern_phase2_gain = 250, args.gain
stop_requested = False
class StopRequested(Exception):
    pass

def request_stop(_signum, _frame):
    global stop_requested
    stop_requested = True

signal.signal(signal.SIGTERM, request_stop)
signal.signal(signal.SIGINT, request_stop)
# Controller acquisition is deliberately unavailable until head 6DoF acceptance.
args.controller_helper = args.controller_runner = None
base, executable = args.base.resolve(), args.capture.resolve()
assert os.geteuid() == 0 and platform.machine() == 'aarch64'
assert Path('/etc/a081-rootfs-id').read_text().strip() == ROOTFS
assert b'A081' in Path('/proc/device-tree/model').read_bytes()
assert Path('/sys/module/qcom_camss/parameters/frame_headers').read_text().strip() == ('Y' if args.timing else 'N')
assert base == (PREFIX / 'tracking').resolve(strict=True)
assert executable.is_file() and os.access(executable, os.X_OK)
assert bool(args.controller_helper) == bool(args.controller_runner)
visual = bool(args.visual_plugin)
if controller_exposure_profile:
    assert visual and args.timing, 'Short controller exposure requires the shared visual QCFT candidate'
assert args.gate_seconds == 0 or 0.25 <= args.gate_seconds <= 300
assert visual == bool(args.cdsp_runner) == bool(args.cdsp_daemon)
if visual:
    assert args.timing
    for name in ('visual_plugin', 'cdsp_runner', 'cdsp_daemon'):
        path = getattr(args, name).resolve()
        assert path.is_file()
        if name == 'visual_plugin' and PROFILE.get('head_tracking_plugin'):
            assert path == Path(PROFILE['head_tracking_plugin']).resolve()
            assert hashlib.sha256(path.read_bytes()).hexdigest() == PROFILE['head_tracking_plugin_sha256']
            info = path.stat()
            assert info.st_uid == 0 and not info.st_mode & 0o022
        else:
            assert path.is_relative_to(base)
        setattr(args, name, path)
if args.controller_helper:
    args.controller_helper = args.controller_helper.resolve()
    args.controller_runner = args.controller_runner.resolve()
    assert args.controller_helper.is_file() and args.controller_runner.is_file()

def camss_devices():
    """Identify the camera graph independently of V4L2 node numbering.

    Venus also allocates /dev/video nodes. Only CAMSS owns the sensor graph,
    including its otherwise unlinked QCFT metadata nodes and all subdevices.
    """
    found = []
    for node in sorted(Path('/dev').glob('media[0-9]*')):
        graph = subprocess.check_output(['media-ctl', '-d', str(node), '-p'],
                                        text=True, stderr=subprocess.STDOUT, timeout=10)
        if re.search(r'^driver\s+qcom-camss\s*$', graph, re.M):
            found.append((node, graph))
    if len(found) != 1:
        raise RuntimeError('Expected one CAMSS media device: ' + repr([str(n) for n, _ in found]))
    node, graph = found[0]
    if not re.search(r'^bus info\s+platform:ac6a000\.camss\s*$', graph, re.M):
        raise RuntimeError('Unexpected A081 CAMSS platform')
    device = node.stat()
    if not stat.S_ISCHR(device.st_mode):
        raise RuntimeError('CAMSS media node is not a character device')
    platform_path = Path(f'/sys/dev/char/{os.major(device.st_rdev)}:{os.minor(device.st_rdev)}').resolve(strict=True).parent
    if platform_path.name != 'ac6a000.camss':
        raise RuntimeError('CAMSS media sysfs identity disagrees with its graph')
    nodes = set(re.findall(r'^\s*device node name (/dev/(?:video[0-9]+|v4l-subdev[0-9]+))\s*$', graph, re.M))
    if not nodes:
        raise RuntimeError('CAMSS graph has no V4L2 device nodes')
    nodes.add(str(node))
    # Also cover a CAMSS node not yet represented by a media entity. Sensors
    # have I2C parents, so their membership comes from the graph above.
    for entry in Path('/sys/class/video4linux').iterdir():
        if (entry / 'device').resolve(strict=True).is_relative_to(platform_path):
            nodes.add('/dev/' + entry.name)
    devices = {}
    for name in sorted(nodes):
        info = Path(name).stat()
        if not stat.S_ISCHR(info.st_mode):
            raise RuntimeError('CAMSS node is not a character device: ' + name)
        devices.setdefault(info.st_rdev, []).append(name)
    return str(node), graph, devices


def camera_fd_owners():
    owners = []
    for fd in Path('/proc').glob('[0-9]*/fd/*'):
        try:
            info = fd.stat()
            if not stat.S_ISCHR(info.st_mode) or info.st_rdev not in camera_devices:
                continue
            target = str(fd.readlink())
        except (FileNotFoundError, ProcessLookupError):
            continue
        owners.append(dict(pid=int(fd.parts[2]), fd=int(fd.name), target=target,
                           device=f'{os.major(info.st_rdev)}:{os.minor(info.st_rdev)}',
                           nodes=camera_devices[info.st_rdev]))
    return owners


media_device, initial_graph, camera_devices = camss_devices()
camera_owners_before = camera_fd_owners()
if camera_owners_before:
    raise RuntimeError('CAMSS devices already owned: ' + json.dumps(camera_owners_before))

(base / 'runs').mkdir(exist_ok=True)
out = Path(tempfile.mkdtemp(prefix='camera-rig-', dir=base / 'runs'))
(out / 'frames').mkdir()
# Stock cam0/1/3 mode tables set bit2 in both 0x3820 and 0x3821;
# cam2 clears both. These are sensor controls, not a software pixel rotation.
# Evidence: camera-debian-r1/stock/sensor-settings-decoded.json,
# each OV7251 module's resolutionData register_tables[21]. Slot/PHY identities
# retain the existing electrical mapping; this is not a new lens observation.
slots = [dict(id=i, phy=phy, csid=i, vfe=i,
              horizontal_flip=0 if i == 2 else 1, vertical_flip=0 if i == 2 else 1)
         for i, phy in enumerate((5, 4, 2, 3))]
saved_control_names = ('exposure', 'gain', 'frame_synchronization', 'vertical_blanking',
                       'horizontal_flip', 'vertical_flip')
result = dict(kernel=platform.release(), slots=slots, events=[],
              optical_sync_verified=False, exposure_start_verified=False,
              qvr_submissions=0, nordic_power_reset_untouched=True,
              nordic_power_untouched=True)
result['qcft_requested'] = args.timing
result['camera_ownership'] = dict(media_device=media_device,
    device_nodes={f'{os.major(dev)}:{os.minor(dev)}': names for dev, names in camera_devices.items()},
    owners_before=camera_owners_before)
(out / 'graph-preflight.txt').write_text(initial_graph)
(out / 'camera-ownership-before.json').write_text(json.dumps(result['camera_ownership'], indent=2) + '\n')
result['original_visual_requested'] = visual
result['gate_seconds'] = args.gate_seconds
result['continuous_until_stop'] = args.gate_seconds == 0
result['sensor_orientation_profile'] = 'stock_ov7251_cam013_hv1_cam2_hv0'
result['sensor_fixed_control_profile'] = dict(name=f'ov7251-e250-g{args.gain}-fixed-comparison',
    exposure_lines=250, gain_v4l2=args.gain, nominal_linear_gain=args.gain / 16.0, gain_denominator=16,
    comparison_baseline_gain_v4l2=64, exposure_and_qcft_timing_unchanged=not bool(controller_exposure_profile),
    exposure_lines_by_slot=[250,250,controller_exposure_lines,controller_exposure_lines], dynamic_ae=False)
result['sensor_fixed_control_profile']['gain_v4l2_by_slot'] = [args.gain] * 2 + [controller_gain] * 2
result['software_pixel_rotation'] = False
result['requested_sensor_mode_fps'] = 75 if three_phase else 90
result['controller_exposure_candidate'] = dict(
    enabled=bool(controller_exposure_profile), profile=controller_exposure_profile,
    target_stock_ns=40000, controller_lines=controller_exposure_lines,
    controller_gain_v4l2=controller_gain, stock_three_phase_reproduced=False,
    tracking_lines=250, exact_stock_duration=False, hardware_verified=False)
handles, watchers, saved_controls, saved_intervals = [], {}, {}, {}
request = capture = controller = None
passthrough_graph = None
nordic = Path('/sys/bus/spi/devices/spi3.0')
nordic_reset_owned = False
stop_read, stop_write = os.pipe()
debug = Path('/sys/module/qcom_camss/parameters/timestamp_debug')
old_debug = debug.read_text()
printk = Path('/proc/sys/kernel/printk')
old_printk = printk.read_text()


def run(argv):
    if argv[0] == 'media-ctl':
        argv = [argv[0], '-d', media_device, *argv[1:]]
    return subprocess.check_output(argv, text=True, stderr=subprocess.STDOUT, timeout=10)


def record(name, argv):
    value = run(argv)
    (out / name).write_text(value)
    return value


def event(kind, **fields):
    result['events'].append(dict(kind=kind, monotonic_ns=time.monotonic_ns(),
                                boottime_ns=time.clock_gettime_ns(time.CLOCK_BOOTTIME), **fields))


def start_nordic(purpose, temporary_gate=False):
    global nordic_reset_owned
    assert (nordic / 'driver').resolve(strict=True) == Path('/sys/bus/spi/drivers/a081-nordic')
    reset = nordic / 'reset_asserted'
    assert reset.read_text().strip() == '1', 'Nordic must start held in reset'
    irq_before = int((nordic / 'irq_count').read_text())
    nordic_reset_owned = True
    # OEM STREAMON precedes HWGP1 and reset release. Startup AE retains its
    # separate early clock supply; the final capture releases after STREAMON.
    if temporary_gate:
        request.set_value(144, Value.ACTIVE)
    assert request.get_value(144) == Value.ACTIVE
    event('nordic_reset_release_before', gpio15=request.get_value(15).value,
          gpio144=request.get_value(144).value, irq_count=irq_before, purpose=purpose)
    try:
        result['nordic_power_reset_untouched'] = False
        reset.write_text('0\n')
        assert reset.read_text().strip() == '0'
        event('nordic_reset_released', purpose=purpose)
        deadline = time.monotonic() + 2
        while True:
            if stop_requested:
                raise StopRequested()
            irq_after = int((nordic / 'irq_count').read_text())
            if irq_after > irq_before:
                event('nordic_drdy_observed', irq_count_before=irq_before, irq_count=irq_after,
                      purpose=purpose, firmware_ready_proven=False)
                break
            if time.monotonic() >= deadline:
                raise TimeoutError('No Nordic DRDY after reset release')
            time.sleep(.002)
    finally:
        if temporary_gate:
            request.set_value(144, Value.INACTIVE)
            event('gate_low_after_nordic_startup', purpose=purpose)


def sensors_by_phy(graph):
    """Resolve physical sensor links, independent of Linux I2C probe order."""
    found = {}
    blocks = re.split(r'^- entity \d+: ', graph, flags=re.M)[1:]
    for block in blocks:
        name = block.split(' (', 1)[0]
        sensor = re.fullmatch(r'ov7251 (\d+)-0060', name)
        if not sensor:
            continue
        nodes = re.findall(r'^\s*device node name (/dev/v4l-subdev\d+)\s*$', block, re.M)
        links = re.findall(r'^\s*-> "msm_csiphy(\d+)":0 \[([^\]]*)\]', block, re.M)
        if len(nodes) != 1 or len(links) != 1 or not {'ENABLED', 'IMMUTABLE'} <= set(links[0][1].split(',')):
            raise RuntimeError('Ambiguous or inactive OV7251 sensor link: ' + name)
        phy = int(links[0][0])
        if phy in found:
            raise RuntimeError('Multiple OV7251 sensors connected to CSIPHY ' + str(phy))
        found[phy] = dict(sensor_entity=name, sensor_subdev=nodes[0], bus=int(sensor[1]))
    if set(found) != {5, 4, 2, 3} or len({s['sensor_subdev'] for s in found.values()}) != 4:
        raise RuntimeError('Incomplete or ambiguous physical OV7251 topology: ' + repr(found))
    return found


def interval(node, saved=None):
    # Standard 48-byte v4l2_subdev_frame_interval, pad0/stream0/ACTIVE.
    # Preserve the exact numerator/denominator rather than assuming 30fps.
    words = [0] * 12
    words[4] = 1
    if saved is not None:
        words[1:3] = saved
    value = bytearray(struct.pack('=12I', *words))
    fd = os.open(node, os.O_RDWR | os.O_CLOEXEC)
    try:
        fcntl.ioctl(fd, 0xc0305615 if saved is None else 0xc0305616, value, True)
    finally:
        os.close(fd)
    returned = struct.unpack('=12I', value)[1:3]
    assert all(returned), returned
    return returned


def preconverge_tracking():
    """Bounded original AE startup, with a full stop between exposure epochs.

    These pixels never enter QVR. The final session gets fixed, read-back
    controls and timing derived from those controls, not from AE requests.
    """
    ae = StockOv7251Ae()
    rounds = []
    result['startup_ae'] = dict(policy=startup_ae, continuous_ae=False,
        iso100_real_gain=iso100_real_gain, rounds=rounds, converged=False)
    for attempt in range(8):
        if stop_requested:
            raise StopRequested()
        setting = ae.request()
        # Nearest integration supported by Linux's measured 928/48MHz row
        # time. Stock observations idx279/299 use 244/442 rows; retain the
        # actual Linux duration below rather than claiming exact requested ns.
        lines = max(1, (setting['exposure_ns'] * 48000000 + 464 * 10**9) // (928 * 10**9))
        # Stock CalculateExposure truncates real gain * 16 to a register value.
        gain = max(16, min(1023, int(setting['iso_gain'] * iso100_real_gain / 100 * 16)))
        for slot in slots[:2]:
            run(['v4l2-ctl', '-d', slot['sensor_subdev'], '-c', f'exposure={lines},gain={gain}'])
            values = run(['v4l2-ctl', '-d', slot['sensor_subdev'], '-C', 'exposure,gain'])
            actual = dict((a, int(b)) for a, b in re.findall(r'^(\w+): (\d+)', values, re.M))
            assert actual == dict(exposure=lines, gain=gain), actual
            slot.update(fixed_nominal_exposure_ns=lines * 928 * 10**9 // 48000000,
                        fixed_gain_v4l2=gain, fixed_nominal_linear_gain=gain / 16.0)
        epoch = out / f'ae-{attempt:02d}'
        epoch.mkdir()
        marker = epoch / 'gate-raised'
        read_fd, write_fd = os.pipe()
        phase_capture = None
        phase_watchers = []
        phase_handles = []
        try:
            for slot in slots:
                handle = (epoch / f"slot{slot['id']}-barrier.log").open('w')
                phase_handles.append(handle)
                phase_watchers.append(subprocess.Popen(['stdbuf', '-oL', '-eL', 'v4l2-ctl',
                    '-d', slot['csid_subdev'], '--wait-for-event=frame_sync', '--verbose'],
                    stdout=handle, stderr=subprocess.STDOUT))
            time.sleep(.15)
            assert all(p.poll() is None for p in phase_watchers)
            phase_env = dict(os.environ, A081_QVR_CAMERA_NODES=','.join(s['video'] for s in slots),
                A081_QVR_OUTPUT_DIR=str(epoch), A081_QVR_CAMERA_STOP_FD=str(read_fd),
                A081_CAMERA_AE_GATE_FILE=str(marker))
            phase_env.pop('A081_QVR_CAMERA_TIMING_NODES', None)
            phase_env.pop('A081_QVR_CONTINUOUS', None)
            handle = (epoch / 'capture.log').open('w')
            phase_handles.append(handle)
            phase_capture = subprocess.Popen([str(executable), '--camera-capture'], env=phase_env,
                pass_fds=(read_fd,), stdout=handle, stderr=subprocess.STDOUT, start_new_session=True)
            deadline = time.monotonic() + 5
            while not all(p.poll() == 0 for p in phase_watchers[1:]):
                if stop_requested:
                    raise StopRequested()
                assert phase_capture.poll() is None, 'Startup AE capture exited before barrier'
                if time.monotonic() > deadline:
                    raise TimeoutError('Startup AE first-frame barrier incomplete')
                time.sleep(.002)
            request.set_value(144, Value.ACTIVE)
            marker.write_text(str(time.clock_gettime_ns(time.CLOCK_BOOTTIME)))
            images = [epoch / f'camera-slot{i}-first.y8' for i in range(2)]
            while not all(p.exists() and p.stat().st_size == 640 * 480 for p in images):
                if stop_requested:
                    raise StopRequested()
                assert phase_capture.poll() is None, 'Startup AE capture exited before images'
                if time.monotonic() > deadline:
                    raise TimeoutError('Startup AE fresh pixel timeout')
                time.sleep(.01)
        finally:
            phase_cleanup_errors = []
            def phase_cleanup(name, action):
                try:
                    action()
                except BaseException as error:
                    phase_cleanup_errors.append(name + ': ' + repr(error))

            def phase_stop_capture():
                if phase_capture.poll() is None:
                    try:
                        os.write(write_fd, b'S')
                        phase_capture.wait(timeout=3)
                    except (BrokenPipeError, subprocess.TimeoutExpired):
                        os.killpg(phase_capture.pid, signal.SIGTERM)
                        try:
                            phase_capture.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            os.killpg(phase_capture.pid, signal.SIGKILL)
                            phase_capture.wait(timeout=2)

            def phase_stop_watcher(proc):
                if proc.poll() is None:
                    proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2)

            phase_cleanup('gate_low', lambda: request.set_value(144, Value.INACTIVE))
            if phase_capture is not None:
                phase_cleanup('capture_stop', phase_stop_capture)
            for index, proc in enumerate(phase_watchers):
                phase_cleanup(f'watcher_{index}', lambda proc=proc: phase_stop_watcher(proc))
            for index, handle in enumerate(phase_handles):
                phase_cleanup(f'log_{index}', handle.close)
            phase_cleanup('pipe_read_close', lambda: os.close(read_fd))
            phase_cleanup('pipe_write_close', lambda: os.close(write_fd))
            if phase_cleanup_errors:
                result['startup_ae'].setdefault('cleanup_errors', []).extend(phase_cleanup_errors)
                raise RuntimeError('Startup AE cleanup failed: ' + '; '.join(phase_cleanup_errors))
        assert phase_capture.returncode == 0, ('Startup AE capture failed', epoch)
        luma, eye_means = stereo_luma(*(p.read_bytes() for p in images))
        next_setting = ae.update(luma, 1)
        rounds.append(dict(**setting, exposure_lines=lines, gain_v4l2=gain,
            actual_exposure_ns=slots[0]['fixed_nominal_exposure_ns'], eye_luma=eye_means,
            selected_luma=luma, next_index=next_setting['index'], evidence=str(epoch)))
        event('startup_ae_epoch', **rounds[-1])
        if next_setting['index'] == setting['index']:
            result['startup_ae']['converged'] = abs(luma - ae.table['luma_target']) <= ae.table['luma_tolerance']
            result['startup_ae']['table_limit'] = not result['startup_ae']['converged']
            break
    result['startup_ae']['final_applied'] = rounds[-1]
    for slot in slots[:2]:
        record(f"slot{slot['id']}-post-ae-controls.txt", ['v4l2-ctl', '-d',
            slot['sensor_subdev'], '-C', 'exposure,gain,frame_synchronization,horizontal_blanking,pixel_rate'])
    result['sensor_fixed_control_profile'].update(
        name='stock-ov7251-startup-ae-held', exposure_lines=rounds[-1]['exposure_lines'],
        gain_v4l2=rounds[-1]['gain_v4l2'], nominal_linear_gain=rounds[-1]['gain_v4l2'] / 16.0,
        gain_v4l2_by_slot=[rounds[-1]['gain_v4l2']] * 2 + [controller_gain] * 2,
        exposure_lines_by_slot=[rounds[-1]['exposure_lines']] * 2 + [controller_exposure_lines] * 2,
        exposure_and_qcft_timing_unchanged=False)


try:
    with gpiod.Chip('/dev/gpiochip3') as chip:
        assert chip.get_info().label == 'f100000.pinctrl'
        assert not chip.get_line_info(15).used and not chip.get_line_info(144).used
    graph = record('graph-before.txt', ['media-ctl', '-p'])
    sensors = sensors_by_phy(graph)
    for slot in slots:
        slot.update(sensors[slot['phy']])
        assert run(['media-ctl', '-e', slot['sensor_entity']]).strip() == slot['sensor_subdev']
    result['sensor_binding'] = 'immutable_sensor_to_csiphy_media_link'
    if passthrough_mode == '1':
        # Resolve all extra resources before changing the four tracking paths.
        try:
            passthrough_graph = PassthroughGraph(run, graph, out)
        except Exception as error:
            result['passthrough_error'] = repr(error)
            event('passthrough_unavailable', detail=repr(error))

    request = gpiod.request_lines('/dev/gpiochip3', consumer='a081-xr-camera-rig', config={
        144: gpiod.LineSettings(direction=Direction.OUTPUT, output_value=Value.INACTIVE),
        15: gpiod.LineSettings(direction=Direction.OUTPUT, output_value=Value.ACTIVE)})
    event('gate_low_before_setup')
    if startup_ae:
        start_nordic('startup_ae', temporary_gate=True)
    run(['media-ctl', '-r'])
    for slot in slots:
        i, phy = slot['id'], slot['phy']
        entity, sd = slot['sensor_entity'], slot['sensor_subdev']
        slot.update(video=run(['media-ctl', '-e', f'msm_vfe{i}_video0']).strip(),
                    csid_subdev=run(['media-ctl', '-e', f'msm_csid{i}']).strip())
        if args.timing:
            slot['timing'] = run(['media-ctl', '-e', f'msm_vfe{i}_video0_timing']).strip()
        text = record(f'slot{i}-controls-before.txt', ['v4l2-ctl', '-d', sd, '-C',
                      ','.join(saved_control_names)])
        saved_controls[i] = dict((a, int(b)) for a, b in re.findall(r'^(\w+): (\d+)', text, re.M))
        assert set(saved_controls[i]) == set(saved_control_names)
        saved_intervals[i] = interval(sd)
        run(['media-ctl', '-l', f'"msm_csiphy{phy}":1 -> "msm_csid{i}":0 [1], '
                              f'"msm_csid{i}":1 -> "msm_vfe{i}_rdi0":0 [1]'])
        formats = ', '.join(f'"{name}":{pad} [fmt:Y10_1X10/640x480 field:none]'
                            for name, pad in [(entity, 0), (f'msm_csiphy{phy}', 0),
                                              (f'msm_csid{i}', 0), (f'msm_csid{i}', 1),
                                              (f'msm_vfe{i}_rdi0', 0)])
        run(['media-ctl', '-V', formats])
        run(['v4l2-ctl', '-d', slot['video'], '--set-fmt-video=width=640,height=480,pixelformat=Y10P'])
        mode_fps = 75 if three_phase else 90
        run(['v4l2-ctl', '-d', sd, f'--set-subdev-fps=pad=0,stream=0,fps={mode_fps}'])
        if three_phase:
            # The A081 stock-mode0 candidate reports its real nominal period,
            # 690*928/48MHz =667/50000 seconds. Reject an older kernel silently
            # selecting60/90fps for this request. Physical FSIN is measured
            # later from QCFT; this is only the standard mode readback.
            selected = interval(sd)
            assert selected[0] * 50000 == selected[1] * 667, selected
            slot['selected_mode_interval'] = list(selected)
            event('stock75_mode_selected', slot=i, interval_numerator=selected[0],
                  interval_denominator=selected[1], frame_length_lines=690)

        exposure_lines = controller_exposure_lines if i >= 2 else 250
        gain = controller_gain if i >= 2 else args.gain
        run(['v4l2-ctl', '-d', sd, '-c', f'frame_synchronization=2,exposure={exposure_lines},gain={gain},'
             f"horizontal_flip={slot['horizontal_flip']},vertical_flip={slot['vertical_flip']}"])
        # The driver applies cached standard controls after the mode register
        # table and before sensor STREAMON. Verify the requested values here;
        # this is a control readback, not an independent I2C register dump.
        orientation = record(f'slot{i}-orientation-before-stream.txt',
                             ['v4l2-ctl', '-d', sd, '-C', 'horizontal_flip,vertical_flip'])
        orientation = dict((a, int(b)) for a, b in re.findall(r'^(\w+): (\d+)', orientation, re.M))
        assert orientation == {name: slot[name] for name in ('horizontal_flip', 'vertical_flip')}, orientation
        event('sensor_orientation_pre_stream', slot=i, **orientation)
        record(f'slot{i}-controls-active.txt', ['v4l2-ctl', '-d', sd, '--list-ctrls'])
        if args.timing:
            values = record(f'slot{i}-fixed-timing-controls.txt', ['v4l2-ctl', '-d', sd, '-C',
                            'exposure,gain,frame_synchronization,horizontal_blanking,pixel_rate'])
            values = dict((a, int(b)) for a, b in re.findall(r'^(\w+): (\d+)', values, re.M))
            assert values == dict(exposure=exposure_lines, gain=gain, frame_synchronization=2,
                                  horizontal_blanking=288, pixel_rate=48000000), values
            slot['fixed_nominal_exposure_ns'] = values['exposure'] * (640 + values['horizontal_blanking']) * 10**9 // values['pixel_rate']
            slot['fixed_gain_v4l2'] = values['gain']
            slot['fixed_nominal_linear_gain'] = values['gain'] / 16.0
    record('graph-active.txt', ['media-ctl', '-p'])
    record('dmesg-before.txt', ['dmesg'])
    printk.write_text('4\n')
    debug.write_text('N\n' if args.timing else 'Y\n')
    if startup_ae:
        preconverge_tracking()
        # Every AE capture and watcher has exited. Hold reset through the
        # final sensor setup/STREAMON, before any exposure epoch is armed.
        (nordic / 'reset_asserted').write_text('1\n')
        assert (nordic / 'reset_asserted').read_text().strip() == '1'
        event('nordic_reset_asserted_after_startup_ae')
    if three_phase:
        # Hold the actual AE result for phase2. Establish a real, equal-period
        # short-exposure stream before selecting the common QCFT SOF anchor.
        if startup_ae:
            held = result['startup_ae']['final_applied']
            pattern_phase2_lines, pattern_phase2_gain = held['exposure_lines'], held['gain_v4l2']
        assert 1 <= pattern_phase2_lines <= 670 and 16 <= pattern_phase2_gain <= 1023
        for slot in slots:
            run(['v4l2-ctl', '-d', slot['sensor_subdev'], '-c',
                 'vertical_blanking=210,exposure=2,gain=32'])
            text = record(f"slot{slot['id']}-pattern-pre-stream.txt", ['v4l2-ctl', '-d',
                slot['sensor_subdev'], '-C', 'vertical_blanking,exposure,gain'])
            actual = dict((a, int(b)) for a, b in re.findall(r'^(\w+): (\d+)', text, re.M))
            assert actual == dict(vertical_blanking=210, exposure=2, gain=32), actual
            slot['fixed_nominal_exposure_ns'] = 2 * 928 * 10**9 // 48000000
        result['kernel_exposure_pattern'] = dict(
            phase2_lines=pattern_phase2_lines, phase2_gain=pattern_phase2_gain,
            short_lines=2, short_gain=32, frame_length_lines=690,
            nominal_fps=48000000 / (928 * 690),
            anchor_source='four_matched_real_QCFT_SOFs_in_capture_owner',
            period_source='consecutive_real_QCFT_SOFs', optical_phase_verified=False)
    for slot in slots:
        log = (out / f"slot{slot['id']}-first-event.log").open('w')
        handles.append(log)
        watchers[slot['id']] = subprocess.Popen(['stdbuf', '-oL', '-eL', 'v4l2-ctl', '-d',
            slot['csid_subdev'], '--wait-for-event=frame_sync', '--verbose'], stdout=log, stderr=subprocess.STDOUT)
    time.sleep(.15)
    assert all(p.poll() is None for p in watchers.values())
    if args.controller_helper:
        # Observe the actual radio while camera-active/gate signals are live,
        # not only after the rig has returned the MCU to its idle condition.
        log = (out / 'controller.log').open('w')
        handles.append(log)
        controller = subprocess.Popen(['bash', str(args.controller_runner), str(base),
            str(args.controller_helper)], stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        event('controller_input_started')
    if passthrough_graph:
        try:
            passthrough_graph.configure()
            result['passthrough'] = passthrough_graph.slots
        except Exception as error:
            # A preview failure must not prevent the accepted tracking startup.
            result['passthrough_error'] = repr(error)
            event('passthrough_configuration_failed', detail=repr(error))
    env = dict(os.environ, A081_QVR_CAMERA_NODES=','.join(s['video'] for s in slots),
               A081_QVR_OUTPUT_DIR=str(out / 'frames'), A081_QVR_CAMERA_STOP_FD=str(stop_read))
    if args.gate_seconds == 0:
        env['A081_QVR_CONTINUOUS'] = '1'
    else:
        env.pop('A081_QVR_CONTINUOUS', None)
    if args.timing:
        assert slots[0]['fixed_nominal_exposure_ns'] == slots[1]['fixed_nominal_exposure_ns']
        assert slots[2]['fixed_nominal_exposure_ns'] == slots[3]['fixed_nominal_exposure_ns']
        env.update(A081_QVR_CAMERA_TIMING_NODES=','.join(s['timing'] for s in slots),
                   A081_QVR_CAMERA_EXPOSURE_NS=str(slots[0]['fixed_nominal_exposure_ns']),
                   A081_CONTROLLER_CAMERA_EXPOSURE_NS=str(slots[2]['fixed_nominal_exposure_ns']))
    if three_phase:
        env.update(A081_CONTROLLER_PATTERN_CSID_NODES=','.join(s['csid_subdev'] for s in slots),
                   A081_CONTROLLER_PATTERN_PHASE2_LINES=str(pattern_phase2_lines),
                   A081_CONTROLLER_PATTERN_PHASE2_GAIN=str(pattern_phase2_gain))
    env.pop('A081_PASSTHROUGH_CAMERA_NODES', None)
    if passthrough_graph and passthrough_graph.ready:
        env['A081_PASSTHROUGH_CAMERA_NODES'] = ','.join(slot['video'] for slot in passthrough_graph.slots)
        export_dir = Path('/run/qiyu-passthrough')
        export_dir.mkdir(mode=0o750, exist_ok=True)
        assert export_dir.is_dir() and not export_dir.is_symlink()
        os.chown(export_dir, 0, int(os.environ.get('A081_PASSTHROUGH_READER_GID', '1000')))
        export_dir.chmod(0o750)
        env['A081_PASSTHROUGH_EXPORT_DIRECTORY'] = str(export_dir)
    log = (out / 'capture.log').open('w')
    handles.append(log)
    command = [str(executable), '--camera-capture']
    if visual:
        env.update(A081_QVR_VISUAL='qcft', A081_QVR_CVP_MODE='disabled', A081_QVR_IMU_HZ='500',
                   A081_QVR_IIO_TO_ANDROID='1,2,3', A081_QVR_CONFIG6=str(base / 'config6-cvp-disabled.xml'),
                   A081_QVR_CALIBRATION=str(base / 'input-config/calibration.xml'))
        command = ['bash', str(args.cdsp_runner), str(base), 'imu-live',
                   str(args.cdsp_daemon), str(args.visual_plugin)]
    capture = subprocess.Popen(command, env=env, pass_fds=(stop_read,),
                               stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    os.close(stop_read)
    stop_read = -1
    event('capture_started')
    # The real cold QVR wrapper + synchronous DSP initialization took 27.4s
    # before camera STREAMON in first-failed/camera-rig-el_h_wkg. Its previous
    # 20s budget stopped a successful initialization before the GPIO barrier.
    # Keep AE/nonvisual windows separate and retain a bounded startup wait.
    startup_budget = 45 if visual else 3
    startup_begin = time.monotonic()
    limit, seen = startup_begin + startup_budget, 0
    event('first_frame_wait_begin', budget_seconds=startup_budget, visual=visual)
    while seen & 0xe != 0xe:
        if stop_requested:
            raise StopRequested()
        assert capture.poll() is None, ('capture exited', capture.returncode)
        for i, watcher in watchers.items():
            if watcher.poll() is not None:
                assert watcher.returncode == 0, (i, watcher.returncode)
                if not seen & (1 << i):
                    seen |= 1 << i
                    event('first_input_sof_notification', slot=i, seen_mask=seen,
                          startup_elapsed_seconds=time.monotonic() - startup_begin)
        if time.monotonic() >= limit:
            event('first_frame_wait_timeout', seen_mask=seen, budget_seconds=startup_budget,
                  startup_elapsed_seconds=time.monotonic() - startup_begin)
            raise TimeoutError('Real first-frame barrier incomplete: ' + hex(seen))
        if seen & 0xe != 0xe:
            time.sleep(.002)
    if stop_requested:
        raise StopRequested()
    event('first_frame_wait_complete', budget_seconds=startup_budget,
          startup_elapsed_seconds=time.monotonic() - startup_begin)
    event('gate_raise_call_before', seen_mask=seen)
    request.set_value(144, Value.ACTIVE)
    event('gate_raise_call_after')
    gate_after_ns = result['events'][-1]['boottime_ns']
    start_nordic('final_capture')
    if visual:
        # Atomic pipe record: this is a measured GPIO-call boundary used only
        # for startup rejection, never a substitute for each image's QCFT SOF.
        assert os.write(stop_write, b'G' + struct.pack('=Q', gate_after_ns)) == 9
    result['gate_barrier_passed'] = True
    end = time.monotonic() + args.gate_seconds
    while not stop_requested and (args.gate_seconds == 0 or time.monotonic() < end):
        assert capture.poll() is None, ('capture ended with gate high', capture.returncode)
        time.sleep(.02)
    result['bounded_capture_completed'] = not stop_requested
    result['requested_stop'] = stop_requested
except StopRequested:
    result['requested_stop'] = True
except BaseException as error:
    result['error'] = repr(error)
    result['traceback'] = traceback.format_exc()
finally:
    # Keep every cleanup step independent so a GPIO library error cannot skip
    # camera controls, media links, raw evidence or the final result record.
    def cleanup_step(name, action):
        try:
            return action()
        except BaseException as error:
            result.setdefault('cleanup_errors', []).append(name + ': ' + repr(error))
            return None

    def lower_gate():
        request.set_value(144, Value.INACTIVE)
        event('gate_low_before_stop_request')

    def stop_capture():
        if capture.poll() is None:
            try:
                os.write(stop_write, b'S')
                event('stop_requested_after_gate_low')
                capture.wait(timeout=35 if visual else 3)
            except (BrokenPipeError, subprocess.TimeoutExpired):
                os.killpg(capture.pid, signal.SIGTERM)
                try:
                    capture.wait(timeout=5 if visual else 2)
                except subprocess.TimeoutExpired:
                    os.killpg(capture.pid, signal.SIGKILL)
                    capture.wait()
        result['capture_exit'] = capture.returncode

    def stop_watcher(watcher):
        if watcher.poll() is None:
            watcher.terminate()
        watcher.wait(timeout=2)

    if request:
        cleanup_step('gate_low', lower_gate)
    if capture:
        cleanup_step('capture_stop', stop_capture)
    for i, watcher in watchers.items():
        cleanup_step('watcher_' + str(i), lambda watcher=watcher: stop_watcher(watcher))
    for handle in handles:
        cleanup_step('log_close', handle.close)
    for fd in (stop_read, stop_write):
        if fd >= 0:
            cleanup_step('pipe_close', lambda fd=fd: os.close(fd))

    def check_camera_exit():
        owners = camera_fd_owners()
        result['camera_ownership']['owners_after_capture_stop'] = owners
        (out / 'camera-ownership-after.json').write_text(json.dumps(owners, indent=2) + '\n')
        if owners:
            raise RuntimeError('CAMSS devices remain owned: ' + json.dumps(owners))
        return True

    def hold_nordic_reset():
        (nordic / 'reset_asserted').write_text('1\n')
        assert (nordic / 'reset_asserted').read_text().strip() == '1'
        result['nordic_reset_held_after_camera_stop'] = True
        event('nordic_reset_asserted_after_camera_stop')

    camera_stopped = cleanup_step('camera_fd_owners', check_camera_exit)
    if nordic_reset_owned and camera_stopped:
        cleanup_step('nordic_reset_hold', hold_nordic_reset)
    cleanup_step('timestamp_debug_restore', lambda: debug.write_text(old_debug))
    cleanup_step('printk_restore', lambda: printk.write_text(old_printk))

    def restore_slot(slot):
        i = slot['id']
        assert interval(slot['sensor_subdev'], saved_intervals[i]) == saved_intervals[i]
        run(['v4l2-ctl', '-d', slot['sensor_subdev'], '-c',
             ','.join(f'{k}={v}' for k, v in saved_controls[i].items())])
        after = record(f'slot{i}-controls-after.txt', ['v4l2-ctl', '-d', slot['sensor_subdev'], '-C',
                       ','.join(saved_control_names)])
        restored = dict((a, int(b)) for a, b in re.findall(r'^(\w+): (\d+)', after, re.M))
        assert restored == saved_controls[i], (i, saved_controls[i], restored)
    for slot in slots:
        if slot['id'] in saved_intervals:
            cleanup_step('slot_restore_' + str(slot['id']), lambda slot=slot: restore_slot(slot))

    if request:
        cleanup_step('camera_active_low', lambda: request.set_value(15, Value.INACTIVE))
        cleanup_step('release_output_gpio', request.release)
        def restore_gpio_inputs():
            # SteamOS libgpiod 2.0.1 returned EINVAL when reconfiguring this
            # existing two-line OUTPUT request to INPUT. Release the already
            # low lines and request the same unowned lines as INPUT instead.
            with gpiod.Chip('/dev/gpiochip3') as chip:
                assert chip.get_info().label == 'f100000.pinctrl'
                assert all(not chip.get_line_info(line).used for line in (15, 144))
            with gpiod.request_lines('/dev/gpiochip3', consumer='a081-xr-camera-idle',
                    config={(15, 144): gpiod.LineSettings(direction=Direction.INPUT)}):
                with gpiod.Chip('/dev/gpiochip3') as chip:
                    assert all(chip.get_line_info(line).direction == Direction.INPUT for line in (15, 144))
            with gpiod.Chip('/dev/gpiochip3') as chip:
                result['gpio_idle'] = {str(line): {'used': chip.get_line_info(line).used,
                    'direction': str(chip.get_line_info(line).direction)} for line in (15, 144)}
                assert all(not chip.get_line_info(line).used and chip.get_line_info(line).direction == Direction.INPUT
                           for line in (15, 144))
        cleanup_step('gpio_input_restore', restore_gpio_inputs)
    if passthrough_graph:
        cleanup_step('passthrough_controls_restore', passthrough_graph.restore)
    cleanup_step('media_links_reset', lambda: run(['media-ctl', '-r']))
    cleanup_step('dmesg_after', lambda: record('dmesg-after.txt', ['dmesg']))
    cleanup_step('graph_after', lambda: record('graph-after.txt', ['media-ctl', '-p']))
    cleanup_step('gpio_after', lambda: (out / 'gpio-after.txt').write_text(Path('/sys/kernel/debug/gpio').read_text()))
    if visual and capture:
        def collect_visual_result():
            log_text = (out / 'capture.log').read_text(errors='replace')
            found = re.search(r'^stage=imu-live exit=\d+ client_exit=\d+ logs=(\S+)$', log_text, re.M)
            if found:
                dsp_logs = Path(found[1])
                assert dsp_logs.is_relative_to(base / 'runs')
                result['dsp_logs'] = str(dsp_logs)
                client = (dsp_logs / 'client.log').read_text(errors='replace')
                summaries = re.findall(r'^stage=imu_input_summary .*?camera_rpc_success=(\d+)', client, re.M)
                assert summaries, 'Missing final real camera RPC counter'
                result['qvr_submissions'] = int(summaries[-1])
            else:
                raise RuntimeError('Missing CDSP wrapper completion record')
            result['visual_6dof_verified'] = False
            if result['qvr_submissions'] == 0:
                result.setdefault('error', 'No successful original visual RPC observed')
        cleanup_step('visual_result', collect_visual_result)
    result['saved_intervals'] = saved_intervals
    result['saved_controls'] = saved_controls
    (out / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(dict(logs=str(out), **result)), flush=True)
raise SystemExit(1 if result.get('error') or result.get('cleanup_errors') or result.get('capture_exit') != 0 else 0)
