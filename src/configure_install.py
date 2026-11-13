#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Prepare a new profile and private asset capsule from installed packages.

Does not start/stop services, select a boot target, or write a boot partition.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--capsule', type=Path, required=True)
    parser.add_argument('--root', type=Path, default=Path('/'))
    args = parser.parse_args()
    root = args.root.resolve()

    def inside(path):
        value = Path(path)
        if not value.is_absolute() or '..' in value.parts:
            raise ValueError('Expected an absolute target-root path')
        selected = root / value.relative_to('/')
        if root != Path('/') and not selected.resolve().is_relative_to(root):
            raise ValueError('Target-root symlink escapes the staging directory')
        return selected

    profile = json.loads(args.profile.read_text())
    def remap(value):
        if isinstance(value, str):
            value = value.replace('/opt/mix-native-interop/share/vulkan/icd.d/native-freedreno.json',
                                  '/usr/lib/aarch64-linux-gnu/xr-mesa/icd.d/freedreno_icd.aarch64.json')
            value = value.replace('/opt/mix-steamvr-turnip/share/vulkan/icd.d/freedreno_icd.aarch64.json',
                                  '/usr/lib/aarch64-linux-gnu/xr-mesa/icd.d/freedreno_icd.aarch64.json')
            return value.replace('/opt/mix-native-interop', '/usr/lib/aarch64-linux-gnu/xr/native-interop').replace(
                '/opt/mix-steamvr-turnip/lib', '/usr/lib/aarch64-linux-gnu/xr-mesa')
        if isinstance(value, list):return [remap(v) for v in value]
        if isinstance(value, dict):return {k:remap(v) for k,v in value.items()}
        return value
    profile = remap(profile)
    output = inside(args.output)
    capsule = inside(args.capsule)
    if output.exists() or capsule.exists():
        parser.error('Choose new profile and capsule paths; existing deployments remain intact')
    if not str(args.capsule).startswith('/opt/'):
        parser.error('The private asset capsule must remain below /opt')
    original_prefix = profile['prefix']
    original = inside(original_prefix)
    manifest = json.loads((original / 'manifest.json').read_text())
    forbidden = {'.py', '.pyc', '.sh', '.c', '.cc', '.cpp', '.h', '.hpp', '.patch'}

    def excluded(relative):
        relative = Path(relative)
        return (relative.parts[0] == 'monado' or 'runs' in relative.parts or
                '__pycache__' in relative.parts or relative.suffix in forbidden or
                str(relative) in ('manifest.json', 'SHA256SUMS'))

    for relative, expected in manifest['files'].items():
        if Path(relative).is_absolute() or '..' in Path(relative).parts:
            raise ValueError('Invalid imported asset path')
        # Historical scripts are neither imported nor executed. Their edits
        # must not prevent migration of independently checksum-locked assets.
        if excluded(relative):
            continue
        if sha(original / relative) != expected:
            raise ValueError('Imported asset checksum changed: ' + relative)
    def relocate(value):
        if isinstance(value, str):
            return value.replace(original_prefix + '/', str(args.capsule) + '/')
        if isinstance(value, list): return [relocate(v) for v in value]
        if isinstance(value, dict): return {k:relocate(v) for k,v in value.items()}
        return value
    original_plan = profile.get('controller_plan')
    profile = relocate(profile)
    for key in ('_path', '_sha256'): profile.pop(key, None)
    # Runtime data and external libraries can be imported by digest. Executable
    # project scripts always come from the installed session package.
    capsule.mkdir(parents=True)
    for source in original.rglob('*'):
        relative = source.relative_to(original)
        if excluded(relative):
            continue
        if source.is_file() and not source.is_symlink():
            target = capsule / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    library = Path('/usr/lib/aarch64-linux-gnu/xr')
    required = {
        'tracking/a081-qvr-stock-live-stream': library / 'head-tracking/a081-qvr-stock-live-stream',
        'tracking/liba081-qvr-client-live-stream.so': library / 'head-tracking/liba081-qvr-client-live-stream.so',
        'tracking/a081-tracking-observe': Path('/usr/bin/xr-head-observe'),
        'tracking/hexagonrpcd-a081': Path('/usr/bin/xr-hexagonrpcd'),
        'controller-sdk-pose': library / 'controller/controller-sdk-pose',
        'controller-radio': Path('/usr/bin/xr-controller-radio'),
        'controller-observe': Path('/usr/bin/xr-controller-observe'),
        'liba081-sdk-log.so': library / 'controller/liba081-sdk-log.so',
        'controller-dsp/libcdsprpc.so': library / 'controller/libcdsprpc.so',
        'controller-dsp/libcdsprpc_system.so': library / 'controller/libcdsprpc_system.so',
        'controller-dsp/liba081-snpe-runtime.so': library / 'controller/liba081-snpe-runtime.so',
    }
    imported = {}
    for relative, installed in required.items():
        source = inside(installed)
        if not source.is_file():
            raise FileNotFoundError('Install the owning component first: ' + str(installed))
        target = capsule / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        imported[relative] = dict(installed=str(installed), sha256=sha(source))
    driver = library / 'monado'
    binary = inside(driver / 'bin/linuxarm64/driver_monado.so')
    if not binary.is_file():
        raise FileNotFoundError('Install xr-monado-steamvr-driver first')
    profile.update(prefix=str(args.capsule), driver_directory=str(driver), driver_sha256=sha(binary),
                   head_tracking_plugin=str(library / 'head-tracking/liba081-qvr-client-live-stream.so'),
                   head_tracking_plugin_sha256=sha(inside(library / 'head-tracking/liba081-qvr-client-live-stream.so')),
                   utilities='/usr/lib/xr/session-manager/utilities')
    scene = profile.get('scene', {})
    if scene.get('mode') == 'command':
        argv = scene.get('argv', [])
        if argv and Path(argv[0]).name == 'xr-shell-session':
            argv[0] = '/usr/bin/xr-shell-session'
            if '--prefix' in argv:
                argv[argv.index('--prefix') + 1] = '/usr'
            else:
                argv.extend(['--prefix', '/usr'])
    plan_name = profile.get('controller_plan')
    if plan_name:
        plan = relocate(json.loads(inside(original_plan).read_text()))
        argv = plan['producer_argv']
        launcher = Path('/usr/lib/xr/session-manager/run-sdk-worker.sh')
        argv[0] = str(launcher)
        plan['producer_executable_sha256'] = sha(inside(launcher))
        if '--worker' in argv:
            worker_index = argv.index('--worker') + 1
            worker = library / 'controller/controller-sdk-pose'
            installed = inside(worker)
            if not installed.is_file():
                raise FileNotFoundError('Install xr-controller-worker-bionic first')
            destination = capsule / 'controller-sdk-pose'
            shutil.copy2(installed, destination)
            argv[worker_index] = str(args.capsule / destination.name)
            if '--worker-sha256' in argv:
                argv[argv.index('--worker-sha256') + 1] = sha(installed)
            imported[destination.name] = dict(installed=str(worker), sha256=sha(installed))
        if plan.get('radio_argv'):
            plan['radio_argv'][0] = '/usr/bin/xr-controller-radio'
            plan['radio_executable_sha256'] = sha(inside('/usr/bin/xr-controller-radio'))
        destination = output.with_suffix('.controller.json')
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(plan, indent=2) + '\n')
        profile['controller_plan'] = '/' + str(destination.relative_to(root))
    dsp = capsule / 'controller-dsp'
    dsp_sums = {str(p.relative_to(dsp)): sha(p) for p in dsp.rglob('*')
                if p.is_file() and p.name not in ('SHA256SUMS', 'manifest.json')}
    (dsp / 'SHA256SUMS').write_text(''.join(value + '  ' + name + '\n'
                                         for name,value in sorted(dsp_sums.items())))
    manifest['installed_components'] = imported
    manifest['files'] = {str(p.relative_to(capsule)): sha(p) for p in capsule.rglob('*') if p.is_file()}
    (capsule / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    files = {str(p.relative_to(capsule)): sha(p) for p in capsule.rglob('*')
             if p.is_file() and p.name != 'SHA256SUMS'}
    (capsule / 'SHA256SUMS').write_text(''.join(value + '  ' + name + '\n'
                                            for name, value in sorted(files.items())))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(profile, indent=2) + '\n')
    print(json.dumps(dict(profile=str(args.output), capsule=str(args.capsule),
                          services_changed=False, partitions_written=False)))


if __name__ == '__main__':
    main()
