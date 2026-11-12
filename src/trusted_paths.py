# SPDX-License-Identifier: GPL-3.0-or-later
"""Immutable private assets, including openKylin's system /opt alias."""
import os
from pathlib import Path
import stat


def _immutable(path, directory=False):
    info = path.lstat()
    if (stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022 or
            (directory and not stat.S_ISDIR(info.st_mode)) or
            (not directory and not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)))):
        raise ValueError('Private asset path must be root-owned and immutable: ' + str(path))


def trusted_opt_path(path):
    """Permit only /opt -> /var/opt; reject links inside the private asset tree.

    Preserve the requested /opt spelling so argv, manifests and pinned digests
    do not change on distributions which relocate the entire system directory.
    """
    path = Path(path)
    if not path.is_absolute() or not path.is_relative_to('/opt') or '..' in path.parts:
        raise ValueError('Private asset must have an ordinary absolute /opt path')
    root = Path('/opt')
    _immutable(Path('/'), directory=True)
    root_info = root.lstat()
    if stat.S_ISLNK(root_info.st_mode):
        if root_info.st_uid != 0 or os.readlink(root) not in ('var/opt', '/var/opt'):
            raise ValueError('Only the root-owned system /opt -> /var/opt alias is permitted')
        physical_root = Path('/var/opt')
        _immutable(Path('/var'), directory=True)
        _immutable(physical_root, directory=True)
    else:
        _immutable(root, directory=True)
        physical_root = root
    relative = path.relative_to(root)
    current = root
    for index, component in enumerate(relative.parts):
        current /= component
        _immutable(current, directory=index < len(relative.parts) - 1)
    if path.resolve(strict=True) != physical_root / relative:
        raise ValueError('Private asset escaped its verified system directory')
    return path
