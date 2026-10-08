"""Hash a delivered draft tree independently of platform-specific paths."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


def tree_identity(root: Path) -> dict:
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError('DELIVERY_ROOT_INVALID')
    rows = []
    for path in sorted(root.rglob('*'), key=lambda item: item.relative_to(root).as_posix().encode('utf-8')):
        if path.is_symlink():
            raise ValueError('DELIVERY_SYMLINK_REJECTED')
        relative = path.relative_to(root).as_posix()
        if '.recycle_bin' in Path(relative).parts or relative.endswith('.source.sha256'):
            continue
        if not path.is_file():
            continue
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
        rows.append({'path': relative, 'size': path.stat().st_size,
                     'sha256': digest.hexdigest()})
    if not rows:
        raise ValueError('DELIVERY_TREE_EMPTY')
    payload = json.dumps(rows, ensure_ascii=False, sort_keys=True,
                         separators=(',', ':'), allow_nan=False).encode('utf-8')
    return {'file_count': len(rows), 'files': rows,
            'sha256': hashlib.sha256(payload).hexdigest()}
