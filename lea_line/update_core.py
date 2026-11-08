"""GitHub release validation and restart-only installation. No Blender access."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
import urllib.request
import uuid
import zipfile

REPOSITORY = 'trackk81/rea-pencil-for-blender'
API = 'https://api.github.com/repos/' + REPOSITORY
MAX_ZIP = 64 * 1024 * 1024
MAX_UNPACKED = 256 * 1024 * 1024

def version(text):
    match = re.fullmatch(r'v?(\d+)\.(\d+)\.(\d+)', str(text))
    if not match:
        raise ValueError('Invalid release version')
    return tuple(map(int, match.groups()))

def get_json(url):
    request = urllib.request.Request(url, headers={'Accept': 'application/vnd.github+json', 'User-Agent': 'rea-pencil-for-blender-updater'})
    with urllib.request.urlopen(request, timeout=15) as response:
        data = response.read(2 * 1024 * 1024 + 1)
    if len(data) > 2 * 1024 * 1024:
        raise ValueError('Release metadata too large')
    return json.loads(data)

def select_release(releases, current):
    choices = []
    for release in releases:
        if release.get('draft') or release.get('prerelease'):
            continue
        try:
            number = version(release['tag_name'])
        except (ValueError, KeyError):
            continue
        if number <= tuple(current):
            continue
        text = '.'.join(map(str, number))
        name = 'rea_pencil_for_blender-' + text + '.zip'
        for asset in release.get('assets', []):
            url = asset.get('browser_download_url', '')
            prefix = 'https://github.com/' + REPOSITORY + '/releases/download/' + release['tag_name'] + '/'
            digest = asset.get('digest', '') or ''
            if asset.get('name') != name or url != prefix + name:
                continue
            if not re.fullmatch(r'sha256:[0-9a-f]{64}', digest):
                continue
            if not 0 < asset.get('size', 0) <= MAX_ZIP:
                continue
            api_url = asset.get('url', '')
            if not re.fullmatch(re.escape(API) + r'/releases/assets/\d+', api_url):
                api_url = None
            choices.append({'version': number, 'url': url, 'api_url': api_url, 'sha256': digest[7:], 'size': asset['size'], 'name': name,
                            'release_url': 'https://github.com/' + REPOSITORY + '/releases/tag/' + release['tag_name']})
    return max(choices, key=lambda item: item['version']) if choices else None

def latest(current):
    return select_release(get_json(API + '/releases?per_page=100'), current)

def validate_zip(path, expected_version):
    with zipfile.ZipFile(path) as archive:
        seen = set()
        total = 0
        for item in archive.infolist():
            name = item.filename
            parts = PurePosixPath(name).parts
            if '\\' in name or ':' in name or not parts or parts[0] != 'lea_line' or '..' in parts or name.startswith('/'):
                raise ValueError('Invalid package path')
            if name.casefold() in seen:
                raise ValueError('Duplicate package path')
            seen.add(name.casefold())
            if stat.S_ISLNK(item.external_attr >> 16) or item.flag_bits & 1:
                raise ValueError('Unsupported package entry')
            total += item.file_size
            if total > MAX_UNPACKED or item.file_size > MAX_ZIP:
                raise ValueError('Package exceeds size limit')
        if archive.testzip() is not None:
            raise ValueError('Corrupt package')
        if not {'lea_line/release.json', 'lea_line/__init__.py'}.issubset(archive.namelist()):
            raise ValueError('Missing addon entry point or release manifest')
        metadata = json.loads(archive.read('lea_line/release.json'))
        if metadata.get('product') != 'rea-pencil-for-blender' or tuple(metadata.get('version', [])) != tuple(expected_version):
            raise ValueError('Wrong product or package version')
        if 'lea_line/__init__.py' not in archive.namelist():
            raise ValueError('Missing addon entry point')
    return metadata

def download(release, directory, blender_version):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (uuid.uuid4().hex + '.zip')
    # GitHub's public asset API avoids browser-download gateway failures.
    url = release.get('api_url')
    url = url + '?download=1' if url else release['url']
    request = urllib.request.Request(url, headers={'User-Agent': 'rea-pencil-for-blender-updater', 'Accept': 'application/octet-stream'})
    try:
        count = 0
        digest = hashlib.sha256()
        with urllib.request.urlopen(request, timeout=30) as response, path.open('wb') as stream:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                count += len(chunk)
                if count > MAX_ZIP:
                    raise ValueError('Download exceeds size limit')
                digest.update(chunk)
                stream.write(chunk)
        if count != release['size'] or digest.hexdigest() != release['sha256']:
            raise ValueError('Download checksum mismatch')
        metadata = validate_zip(path, release['version'])
        if tuple(blender_version) < tuple(metadata.get('blender_min', [4, 4, 0])):
            raise ValueError('This release requires a newer Blender')
        return str(path)
    except Exception:
        path.unlink(missing_ok=True)
        raise

def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, path)

def stage(path, release, directory, target, pid=None):
    path = Path(path).resolve()
    if hashlib.sha256(path.read_bytes()).hexdigest() != release['sha256']:
        raise ValueError('Staged checksum mismatch')
    validate_zip(path, release['version'])
    pending = {'state': 'ready', 'zip': str(path), 'sha256': release['sha256'], 'version': list(release['version']),
               'target': str(Path(target).resolve()), 'pid': os.getpid() if pid is None else pid}
    write_json(Path(directory) / 'pending.json', pending)
    return pending

def process_alive(pid):
    """Read-only process query; never use os.kill on Windows."""
    if not isinstance(pid, int) or pid <= 0:
        return True
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() != 87
        try:
            code = wintypes.DWORD()
            return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True

def other_blender_running(current_pid):
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        class Entry(ctypes.Structure):
            _fields_ = [('size', wintypes.DWORD), ('usage', wintypes.DWORD), ('pid', wintypes.DWORD),
                        ('heap', ctypes.c_size_t), ('module', wintypes.DWORD), ('threads', wintypes.DWORD),
                        ('parent', wintypes.DWORD), ('priority', wintypes.LONG), ('flags', wintypes.DWORD),
                        ('exe', wintypes.WCHAR * 260)]
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
        kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel.Process32FirstW.argtypes = kernel.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(Entry))
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel.CreateToolhelp32Snapshot(2, 0)
        if handle == wintypes.HANDLE(-1).value:
            return True
        try:
            entry = Entry();entry.size = ctypes.sizeof(Entry)
            ok = kernel.Process32FirstW(handle, ctypes.byref(entry))
            if not ok:
                return True
            while ok:
                if entry.pid != current_pid and entry.exe.casefold() == 'blender.exe':
                    return True
                ok = kernel.Process32NextW(handle, ctypes.byref(entry))
            return False
        finally:
            kernel.CloseHandle(handle)
    import subprocess
    try:
        result = subprocess.run(['ps', '-A', '-o', 'pid=,comm='], capture_output=True, text=True, timeout=3, check=True)
        for line in result.stdout.splitlines():
            fields = line.strip().split(None, 1)
            if len(fields) == 2 and int(fields[0]) != current_pid and Path(fields[1]).name.casefold() == 'blender':
                return True
        return False
    except (OSError, ValueError, subprocess.SubprocessError):
        return True

def apply_pending(directory, target, pid=None):
    """Swap only after the staging process exits; keep the previous folder intact."""
    marker = Path(directory) / 'pending.json'
    if not marker.exists():
        return False
    pending = json.loads(marker.read_text(encoding='utf-8'))
    target = Path(target).resolve()
    if pending.get('state') != 'ready' or pending.get('pid') == (os.getpid() if pid is None else pid):
        return False
    if Path(pending['target']).resolve() != target:
        return False
    if process_alive(pending.get('pid')):
        return False
    if other_blender_running(os.getpid() if pid is None else pid):
        return False
    path = Path(pending['zip'])
    if hashlib.sha256(path.read_bytes()).hexdigest() != pending['sha256']:
        raise ValueError('Pending checksum mismatch')
    validate_zip(path, pending['version'])
    temporary = Path(tempfile.mkdtemp(prefix='.lea-line-update-', dir=target.parent)).resolve()
    backup = target.parent / ('.lea-line-backup-' + uuid.uuid4().hex)
    swapped = False
    try:
        with zipfile.ZipFile(path) as archive:
            archive.extractall(temporary)
        os.replace(target, backup)
        swapped = True
        try:
            os.replace(temporary / 'lea_line', target)
        except Exception:
            os.replace(backup, target)
            swapped = False
            raise
        pending.update(state='applied', backup=str(backup))
        write_json(marker, pending)
        return True
    except Exception:
        if swapped and backup.exists():
            os.replace(target, temporary / 'failed-install')
            os.replace(backup, target)
        raise
    finally:
        # This is only our freshly created, verified temporary directory.
        if temporary.parent == target.parent and temporary.name.startswith('.lea-line-update-'):
            shutil.rmtree(temporary, ignore_errors=True)

def pending(directory):
    marker = Path(directory) / 'pending.json'
    return json.loads(marker.read_text(encoding='utf-8')) if marker.exists() else None
