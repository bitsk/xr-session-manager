#!/bin/bash
# Real ARM32 SDK worker in a private mount/PID/network namespace.
# No camera/Nordic ownership. CDSP is exposed only by the explicit DSP option.
set -euo pipefail
sdk_root= worker= worker_sha= input_socket= packet_fd= duration= candidate_profile= snapshot_dir= sdk_log_shim=
cdsp_runtime= cdsp_components= snpe_runtime=
while [[ $# -gt 0 ]]; do
    case "$1" in
        --root) sdk_root=${2:?}; shift 2;;
        --worker) worker=${2:?}; shift 2;;
        --worker-sha256) worker_sha=${2:?}; shift 2;;
        --input-socket) input_socket=${2:?}; shift 2;;
        --output-fd) packet_fd=${2:?}; shift 2;;
        --seconds) duration=${2:?}; shift 2;;
        --candidate-profile) candidate_profile=${2:?}; shift 2;;
        --snapshot-dir) snapshot_dir=${2:?}; shift 2;;
        --cdsp-runtime) cdsp_runtime=${2:?}; shift 2;;
        --snpe-runtime) snpe_runtime=${2:?}; shift 2;;
        *) echo "Unknown argument: $1" >&2; exit 64;;
    esac
done
[[ $(id -u) == 0 && $(uname -m) == aarch64 ]]
session_modules=$(dirname -- "$(readlink -e -- "${BASH_SOURCE[0]}")")
session_rootfs=$(python3 - "$session_modules" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
from common import ROOTFS
print(ROOTFS)
PY
)
[[ $(cat /etc/a081-rootfs-id) == "$session_rootfs" ]]
resolve_private_opt() {
    python3 - "$session_modules" "$1" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
from trusted_paths import trusted_opt_path
print(trusted_opt_path(sys.argv[2]).resolve(strict=True))
PY
}
private_opt=$(resolve_private_opt /opt)
grep -q A081 /proc/device-tree/model
[[ $sdk_root == /opt/* && $worker == /opt/* ]]
[[ $worker_sha =~ ^[0-9a-f]{64}$ && $packet_fd =~ ^[0-9]+$ && $packet_fd -ge 3 ]]
[[ $duration =~ ^[0-9]+$ && $duration -le 300 ]]
[[ -z $candidate_profile || $candidate_profile == sdk-world-to-portrait-v1 ]]
[[ -z $snpe_runtime || ( $snpe_runtime == dsp && -n $cdsp_runtime ) ]]
[[ $input_socket =~ ^/run/a081-controller-input-[a-zA-Z0-9_-]+/input.sock$ ]]
sdk_root=$(resolve_private_opt "$sdk_root")
worker=$(resolve_private_opt "$worker")
[[ $sdk_root == "$private_opt/"* && $worker == "$private_opt/"* ]]
input_directory=${input_socket%/*}
[[ -d $sdk_root && -f $sdk_root/manifest.json && -f $sdk_root/SHA256SUMS && -x $worker ]]
[[ -d $input_directory && ! -e $input_socket && ! -L $input_directory ]]
[[ -p /proc/self/fd/$packet_fd ]]
[[ $(sha256sum "$worker" | cut -d ' ' -f1) == "$worker_sha" ]]
(cd "$sdk_root" && sha256sum --quiet -c SHA256SUMS)
if [[ -n $cdsp_runtime ]]; then
    cdsp_runtime=$(resolve_private_opt "$cdsp_runtime")
    cdsp_components=${worker%/*}/controller-dsp
    [[ $cdsp_runtime == "$private_opt/"* && -c /dev/fastrpc-cdsp ]]
    [[ -f $cdsp_runtime/dsp/cdsp/fastrpc_shell_3 && -d $cdsp_components ]]
    (cd "$cdsp_runtime" && sha256sum --quiet -c SHA256SUMS)
    (cd "$cdsp_components" && sha256sum --quiet -c SHA256SUMS)
fi
if [[ -n $snapshot_dir ]]; then
    [[ $snapshot_dir == /opt/mix-vr-shell-controller/tracking/runs/*/controller-sdk-frames ]]
    sdk_log_shim=${worker%/*}/liba081-sdk-log.so
    [[ $(sha256sum "$sdk_log_shim" | cut -d ' ' -f1) == a4fdc4ceb3b3f7ac5ffaa2ae618196e4d0ea018367ab6462acbdc7aa7ef53297 ]]
    mkdir -m 700 -- "$snapshot_dir"
fi
unset LD_PRELOAD LD_LIBRARY_PATH LD_AUDIT
# exec preserves the explicitly inherited binary output pipe. --kill-child
# forwards parent lifecycle termination to the namespace worker on teardown.
exec unshare --mount --pid --net --fork --kill-child=TERM /bin/bash -eu -c '
    sdk_root=$1; worker=$2; input_directory=$3; input_socket=$4
    packet_fd=$5; duration=$6; candidate_profile=$7; snapshot_dir=$8; sdk_log_shim=$9
    cdsp_runtime=${10}; cdsp_components=${11}; snpe_runtime=${12}
    mount --make-rprivate /
    mount -t proc proc "$sdk_root/proc"
    # Proc device-tree is a symlink into sysfs. Bind only this read-only tree,
    # without exposing the rest of sysfs or any camera/radio device nodes.
    dt="$sdk_root/sys/firmware/devicetree/base"
    mkdir -p "$dt" "$sdk_root$input_directory"
    mount --bind /sys/firmware/devicetree/base "$dt"
    mount -o remount,bind,ro "$dt"
    mount --bind "$input_directory" "$sdk_root$input_directory"
    mount -t tmpfs -o mode=755,nosuid tmpfs "$sdk_root/dev"
    for spec in "null 1 3" "zero 1 5" "random 1 8" "urandom 1 9"; do
        read -r name major minor <<< "$spec"
        mknod -m 666 "$sdk_root/dev/$name" c "$major" "$minor"
    done
    target="$sdk_root/system/bin/a081-controller-sdk-probe"
    [[ -f $target ]]
    mount --bind "$worker" "$target"
    mount -o remount,bind,ro "$target"
    shim_env=()
    preloads=()
    library_path=/system/lib
    args=(--sdk /system/lib/libControllerSDK_EXPORT.so --input-socket "$input_socket"
          --output-fd "$packet_fd" --seconds "$duration")
    [[ -z $candidate_profile ]] || args+=(--candidate-profile "$candidate_profile")
    if [[ -n $snapshot_dir ]]; then
        # Bind the session raw directory, so snapshots survive namespace exit
        # and are retained by the existing per-session evidence archive.
        snapshots=/data/local/tmp/controller-sdk-frames
        mkdir -p "$sdk_root$snapshots"
        mount --bind "$snapshot_dir" "$sdk_root$snapshots"
        args+=(--snapshot-dir "$snapshots")
        touch "$sdk_root/tmp/liba081-sdk-log.so"
        mount --bind "$sdk_log_shim" "$sdk_root/tmp/liba081-sdk-log.so"
        mount -o remount,bind,ro "$sdk_root/tmp/liba081-sdk-log.so"
        preloads+=(/tmp/liba081-sdk-log.so)
    fi
    if [[ -n $cdsp_runtime ]]; then
        # SNPE reads the real SoC identity before loading its DSP runtime.
        soc_info="$sdk_root/sys/devices/soc0"
        mkdir -p "$soc_info"
        mount --bind /sys/devices/soc0 "$soc_info"
        mount -o remount,bind,ro "$soc_info"
        assets=/opt/a081-controller-dsp
        libraries=/opt/a081-controller-dsp-libs
        mkdir -p "$sdk_root$assets" "$sdk_root$libraries"
        mount --bind "$cdsp_runtime" "$sdk_root$assets"
        mount -o remount,bind,ro "$sdk_root$assets"
        mount --bind "$cdsp_components" "$sdk_root$libraries"
        mount -o remount,bind,ro "$sdk_root$libraries"
        touch "$sdk_root/dev/fastrpc-cdsp"
        mount --bind /dev/fastrpc-cdsp "$sdk_root/dev/fastrpc-cdsp"
        for name in libcdsprpc.so libcdsprpc_system.so; do
            [[ -e $sdk_root/system/lib/$name ]] || continue
            mount --bind "$cdsp_components/$name" "$sdk_root/system/lib/$name"
            mount -o remount,bind,ro "$sdk_root/system/lib/$name"
        done
        library_path=$libraries:/system/lib
        preloads+=("$libraries/libcdsprpc.so")
        shim_env+=(A081_CDSP_RUNTIME="$assets" A081_CDSP_DEVICE=/dev/fastrpc-cdsp)
        if [[ $snpe_runtime == dsp ]]; then
            preloads+=("$libraries/liba081-snpe-runtime.so")
            shim_env+=(A081_SNPE_RUNTIME=dsp)
        fi
    fi
    if [[ ${#preloads[@]} -gt 0 ]]; then
        preload=$(IFS=:; echo "${preloads[*]}")
        shim_env+=(LD_PRELOAD="$preload")
    fi
    exec env -i "${shim_env[@]}" LD_LIBRARY_PATH="$library_path" ICU_DATA=/apex/com.android.runtime/etc/icu \
        PATH=/system/bin HOME=/data/local/tmp TMPDIR=/tmp \
        /usr/sbin/chroot "$sdk_root" /system/bin/a081-controller-sdk-probe "${args[@]}"
' a081-controller-sdk "$sdk_root" "$worker" "$input_directory" "$input_socket" "$packet_fd" "$duration" "$candidate_profile" "$snapshot_dir" "$sdk_log_shim" "$cdsp_runtime" "$cdsp_components" "$snpe_runtime"
