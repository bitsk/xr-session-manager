#!/bin/bash
# Bounded original-library stages on the real A081; caller owns the device window.
set -euo pipefail
[[ $(id -u) == 0 && $(uname -m) == aarch64 ]]
case "$(cat /etc/a081-rootfs-id)" in
    a081-steamos-arm64|a081-openkylin-3.0-arm64) ;;
    *) exit 1 ;;
esac
grep -q A081 /proc/device-tree/model
base=$(readlink -f "${1:?Usage: run-cdsp.sh deployed-base version|init|health|imu-live [daemon] [plugin]}")
stage=${2:?Select version, init, health or imu-live}
case "$stage" in version|init|health|imu-live) ;; *) exit 2;; esac
if [[ "$stage" == imu-live ]]; then
    : "${A081_QVR_CONFIG6:?Absolute path to the original config6.xml}"
    : "${A081_QVR_CALIBRATION:?Absolute path to the original calibration.xml}"
    : "${A081_QVR_IIO_TO_ANDROID:?Verified signed IIO to Android axis permutation}"
    export A081_QVR_CONFIG6 A081_QVR_CALIBRATION A081_QVR_IIO_TO_ANDROID
fi
daemon=$(readlink -f "${3:-$base/hexagonrpcd-unmap}")
plugin=$(readlink -f "${4:-$base/tools/liba081-qvr-client.so}")
runtime=$base/stock-runtime/qcom
[[ -x "$daemon" && -f "$plugin" && -f "$runtime/dsp/cdsp/fastrpc_shell_3" ]]
exec 9>/run/mix-xr-stock-cdsp.lock
flock -n 9 || { echo 'Another XR CDSP session owns the lock' >&2; exit 1; }
# Refuse a concurrent CDSP owner; SDSP's existing sensor daemon stays active.
for fd in /proc/[0-9]*/fd/*; do
    target=$(readlink "$fd" 2>/dev/null || true)
    case "$target" in /dev/fastrpc-cdsp|/dev/fastrpc-cdsp-secure)
        echo "CDSP already open: $fd" >&2; exit 1;;
    esac
done
mkdir -p "$base/runs"
logdir=$(mktemp -d "$base/runs/$stage-XXXXXXXX")
export A081_QVR_OUTPUT_DIR="$logdir"
# Device-side lifetime check, after the owning processes have been reaped.
# Keep the raw exporter list as well as parsed results. Refuse a dirty starting
# point: it cannot prove ownership/reclamation of this bounded session.
dma_snapshot() {
    python3 - "$logdir" "$1" "${2:-0}" <<'PY'
import json, re, sys, time
from pathlib import Path

directory, phase = Path(sys.argv[1]), sys.argv[2]
deferred = phase == 'after' and sys.argv[3] == '1'
deadline = time.monotonic() + (3 if phase == 'after' and not deferred else 0)
attempts = 0
while True:
    attempts += 1
    raw = Path('/sys/kernel/debug/dma_buf/bufinfo').read_text()
    (directory / ('dma-' + phase + '.txt')).write_text(raw)
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
        (directory / ('dma-' + phase + '-first.txt')).write_text(raw)
        (directory / ('dma-' + phase + '-first.json')).write_text(json.dumps({'fastrpc_buffers': buffers, 'cdsp_fds': fds, 'zero': ok}, indent=2) + '\n')
    if ok or time.monotonic() >= deadline:
        break
    time.sleep(0.1)
result = {'phase': phase, 'rc': 0 if ok else 1, 'fastrpc_objects': len(buffers),
          'fastrpc_bytes': sum(b['bytes'] for b in buffers), 'buffers': buffers,
          'open_cdsp_fds': fds, 'attempts': attempts}
if deferred:
    result['deferred_to_controller_session'] = True
(directory / ('dma-' + phase + '.json')).write_text(json.dumps(result, indent=2) + '\n')
print('stage=session_dma_' + phase + ' rc=' + str(result['rc']) +
      ' objects=' + str(len(buffers)) + ' bytes=' + str(result['fastrpc_bytes']) +
      ' cdsp_fds=' + str(len(fds)) + ' attempts=' + str(attempts) + (' deferred=1' if deferred else ''))
raise SystemExit(0 if deferred else result['rc'])
PY
}
dma_snapshot before
root_timeout=410s
client_timeout=400s
if [[ ${A081_QVR_CONTINUOUS:-0} == 1 ]]; then root_timeout=0s; client_timeout=0s; fi
root_pid=
cleanup() {
    if [[ -n "$root_pid" ]]; then
        kill -TERM "$root_pid" 2>/dev/null || true
        wait "$root_pid" 2>/dev/null || true
        root_pid=
    fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
# Root PD must answer stock oemconfig/testsig lookups, including ENOENT.
# Do not add testsig, unsigned flags or a global replacement sensor daemon.
python3 "$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")/bounded_run.py" --log "$logdir/root.log" -- timeout --signal=TERM --kill-after=3s "$root_timeout" "$daemon" \
    -f /dev/fastrpc-cdsp -d cdsp -R "$runtime" &
root_pid=$!
set +e
A081_QVR_STAGE=$stage python3 "$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")/bounded_run.py" --log "$logdir/client.log" -- timeout --signal=TERM --kill-after=3s "$client_timeout" "$daemon" \
    -f /dev/fastrpc-cdsp -c "$runtime/dsp/cdsp/fastrpc_shell_3" \
    -d cdsp -R "$runtime" -P "$plugin"
rc=$?
client_rc=$rc
set -e
# The listener can terminate the private process before the plugin's C++
# cleanup runs (observed while opening cvpcpuRev). Replay only this client's
# saved IIO attributes after timeout has reaped it, with sensor locks held.
if [[ "$stage" == imu-live ]]; then
    journal_log="$logdir/client.log"
    [[ ! -e "$logdir/client.log.first" ]] || journal_log="$logdir/client.log.first"
    if python3 - "$journal_log" >"$logdir/iio-restore.log" 2>&1 <<'PY'
import fcntl, os, re, shlex, sys
from pathlib import Path

saved = {}
for line in Path(sys.argv[1]).read_text(errors='replace').splitlines():
    if not line.startswith('stage=iio_save rc=0 '):
        continue
    fields = dict(word.split('=', 1) for word in shlex.split(line) if '=' in word)
    detail = dict(word.split('=', 1) for word in fields['detail'].split())
    path = Path(detail['path'])
    match = re.fullmatch(r'/sys/bus/iio/devices/(iio:device[0-9]+)/(.*)', str(path))
    if not match:
        raise RuntimeError('Unexpected IIO journal path')
    device = Path('/sys/bus/iio/devices') / match[1]
    name = (device / 'name').read_text().strip()
    channel = {'qcom-see-accel': 'accel', 'qcom-see-gyro': 'anglvel'}.get(name)
    if not channel:
        raise RuntimeError('IIO device identity changed')
    allowed = {'current_timestamp_clock', 'buffer/enable', 'buffer/length',
               'buffer/watermark', 'buffer/hwfifo_timeout', 'scan_elements/in_timestamp_en'}
    allowed.add('in_' + channel + '_sampling_frequency')
    allowed.update('scan_elements/in_' + channel + '_' + axis + '_en' for axis in 'xyz')
    if match[2] not in allowed:
        raise RuntimeError('Unexpected IIO journal attribute')
    saved.setdefault(path, (detail['value'], device))

locks = []
if saved:
    lock_paths = {'/run/a081-iio.lock'}
    if os.environ.get('XDG_RUNTIME_DIR'):
        lock_paths.add(os.environ['XDG_RUNTIME_DIR'] + '/a081-iio.lock')
    for path in sorted(lock_paths):
        lock = open(path, 'a')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        locks.append(lock)
    for device in {device for _, device in saved.values()}:
        (device / 'buffer/enable').write_text('0')
    for path, (value, _) in reversed(list(saved.items())):
        path.write_text(value)
        if path.read_text().strip() != value:
            raise RuntimeError('IIO restore readback mismatch: ' + str(path))
        print('stage=wrapper_iio_restore rc=0 path=' + str(path) + ' value=' + value)
print('stage=wrapper_iio_restore_complete rc=0 attributes=' + str(len(saved)))
PY
    then
        cat "$logdir/iio-restore.log"
    else
        cat "$logdir/iio-restore.log" >&2
        rc=1
    fi
fi
# Stop the root-PD listener before examining process/session reclamation.
# In a shared controller session, retain this snapshot and perform the final
# zero check after ControllerSession has also reaped its separate SDK PD.
cleanup
if dma_snapshot after "${A081_CONTROLLER_SHARED_CDSP:-0}"; then
    if [[ "$stage" == imu-live && "$rc" == 3 ]]; then
        rc=0
    fi
else
    rc=1
fi
printf 'stage=%s exit=%d client_exit=%d logs=%s\n' "$stage" "$rc" "$client_rc" "$logdir"
grep -E '^stage=(open|driver_version|init|tracker_version|health_|share_|request_unmap64|deinit|close|plugin_exit|xml_|iio_|imu_|ring_|set_ring|enable_position|sensor_orientation|qtimer|start|stop|external_sensor|camera_|pose_|dl_)' "$logdir/client.log" || true
exit "$rc"
