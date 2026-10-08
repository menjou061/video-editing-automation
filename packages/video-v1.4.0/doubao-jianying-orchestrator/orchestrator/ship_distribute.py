# -*- coding: utf-8 -*-
"""定稿分发（ship & distribute）：一步完成
  1. 素材路径可移植化改写（media/ 中真实存在的文件才改写）
  2. 注入本地化工具 素材本地化修复.bat + fix_local.ps1（v3 位置无关版）
  3. 复制到 NAS：测试产出\<草稿名> 与 测试产出\分发草稿\<草稿名>（--style unc 时）

用法：
  python orchestrator/ship_distribute.py --draft <草稿目录> [--style unc|local|mac] [--skip-dist]
风格：
  unc   改写为 NAS UNC 并复制到 NAS（默认，给剪辑同学）
  local 只改写为本机 media 绝对路径（不出货）
  mac   改写为 Mac 预览库路径（不出货）
"""
import argparse, io, json, os, shutil, subprocess, sys
from pathlib import Path

NAS_SHARE_ENV = 'JY_NAS_SHARE'
NAS_USER_ENV = 'JY_NAS_USER'
NAS_PASSWORD_ENV = 'NAS_PASSWORD'
MAC_DRAFT_ROOT_ENV = 'JY_MAC_DRAFT_ROOT'
TEMPLATES = Path(__file__).resolve().parent.parent / 'templates'
TOOLS = ('素材本地化修复.bat', 'fix_local.ps1')


def log(m):
    try:
        print(m, flush=True)
    except UnicodeEncodeError:
        print(m.encode('unicode_escape').decode('ascii'), flush=True)


def rewrite(root, style):
    """改写 draft_info.json / draft_content.json 素材路径；返回改写条数。"""
    base_name = os.path.basename(os.path.abspath(root))
    sep = '\\' if style != 'mac' else '/'
    if style == 'unc':
        nas_share = os.environ.get(NAS_SHARE_ENV, '').strip()
        if not nas_share:
            raise RuntimeError(NAS_SHARE_ENV + '_MISSING')
        output_root = nas_share.rstrip('\\/') + '\\测试产出'
        prefix = output_root + '\\' + base_name + '\\media\\'
        root_prefix = output_root + '\\' + base_name
        root_dir_prefix = output_root
    elif style == 'local':
        prefix = os.path.abspath(root) + os.sep + 'media' + os.sep
        root_prefix = os.path.abspath(root)
        root_dir_prefix = os.path.dirname(os.path.abspath(root))
    else:  # mac
        mac_base = os.environ.get(MAC_DRAFT_ROOT_ENV, '').strip()
        if not mac_base:
            raise RuntimeError(MAC_DRAFT_ROOT_ENV + '_MISSING')
        prefix = mac_base + '/' + base_name + '/media/'
        root_prefix = mac_base + '/' + base_name
        root_dir_prefix = mac_base

    n = 0
    for fn in ('draft_info.json', 'draft_content.json'):
        p = os.path.join(root, fn)
        if not os.path.isfile(p):
            continue
        with io.open(p, encoding='utf-8-sig') as f:
            d = json.load(f)
        for kind in ('videos', 'audios', 'images', 'gifs'):
            for m in d.get('materials', {}).get(kind, []):
                name = m.get('name') or m.get('material_name') or ''
                if name and os.path.exists(os.path.join(root, 'media', name)):
                    m['path'] = prefix + name
                    n += 1
        with io.open(p, 'w', encoding='utf-8') as f:
            json.dump(d, f, ensure_ascii=False, indent=2)

    meta_p = os.path.join(root, 'draft_meta_info.json')
    if os.path.isfile(meta_p):
        with io.open(meta_p, encoding='utf-8-sig') as f:
            md = json.load(f)
        md['draft_fold_path'] = root_prefix
        md['draft_root_path'] = root_dir_prefix
        md['draft_json_file'] = root_prefix + sep + 'draft_info.json'
        with io.open(meta_p, 'w', encoding='utf-8') as f:
            json.dump(md, f, ensure_ascii=False, indent=2)
    return n


def inject_tools(root):
    """把 skill 打包的 v3 本地化工具注入草稿目录（覆盖旧版）。"""
    injected = []
    for t in TOOLS:
        src = TEMPLATES / t
        if src.is_file():
            shutil.copyfile(str(src), os.path.join(root, t))
            injected.append(t)
    return injected


def mount_nas():
    nas_share = os.environ.get(NAS_SHARE_ENV, '').strip()
    nas_user = os.environ.get(NAS_USER_ENV, '').strip()
    if not nas_share:
        raise RuntimeError(NAS_SHARE_ENV + '_MISSING')
    if not nas_user:
        raise RuntimeError(NAS_USER_ENV + '_MISSING')
    password = os.environ.get(NAS_PASSWORD_ENV, '').strip()
    if not password:
        raise RuntimeError(NAS_PASSWORD_ENV + '_MISSING')
    r = subprocess.run(['net', 'use', nas_share, password, '/user:' + nas_user],
                       capture_output=True, text=True)
    return r.returncode


def robocopy(src, dst):
    r = subprocess.run(['robocopy', src, dst, '/E', '/NJH', '/NJS', '/NFL', '/NDL', '/NP',
                        '/XD', '.recycle_bin', '/XF', '*.source.sha256'],
                       capture_output=True, text=True)
    return r.returncode  # 0-7 = 成功


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--draft', required=True)
    ap.add_argument('--style', default='unc', choices=('unc', 'local', 'mac'))
    ap.add_argument('--skip-dist', action='store_true')
    args = ap.parse_args()

    root = os.path.abspath(args.draft)
    if not os.path.isdir(os.path.join(root, 'media')):
        log(json.dumps({'status': 'FAIL', 'reason': 'media dir missing: ' + root}, ensure_ascii=False))
        return 2

    n = rewrite(root, args.style)
    injected = inject_tools(root)
    name = os.path.basename(root)

    dist = False
    dist_skipped = False
    root_copied = False
    if args.style == 'unc' and not args.skip_dist:
        if mount_nas() != 0:
            log(json.dumps({'status': 'FAIL', 'reason': 'net use failed'}, ensure_ascii=False))
            return 3
        nas_share = os.environ.get(NAS_SHARE_ENV, '').strip()
        if not nas_share:
            raise RuntimeError(NAS_SHARE_ENV + '_MISSING')
        output_root = nas_share.rstrip('\\/') + '\\测试产出'
        nas_root = output_root + '\\' + name
        nas_dist = output_root + '\\分发草稿\\' + name
        rc = robocopy(root, nas_root)
        if rc >= 8:
            log(json.dumps({'status': 'FAIL', 'reason': 'robocopy root failed rc=%d' % rc}, ensure_ascii=False))
            return 4
        root_copied = True
        if os.path.isdir(nas_dist):
            dist_skipped = True
        else:
            rc = robocopy(root, nas_dist)
            if rc >= 8:
                log(json.dumps({'status': 'FAIL', 'reason': 'robocopy dist failed rc=%d' % rc}, ensure_ascii=False))
                return 5
            dist = True

    log(json.dumps({
        'status': 'OK',
        'draft': name,
        'style': args.style,
        'rewrote': n,
        'injected': injected,
        'root_copied': root_copied,
        'dist_copied': dist,
        'dist_skipped': dist_skipped,
        'dist_target': (os.environ.get(NAS_SHARE_ENV, '').rstrip('\\/') + '\\测试产出\\分发草稿\\' + name) if args.style == 'unc' else None,
    }, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
