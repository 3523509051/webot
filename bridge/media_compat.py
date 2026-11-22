"""Local compatibility for WeChat media containers and renamed downloads."""
from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import Path
import re
import subprocess
import tempfile

from wechatauto.media import MediaDownloader


class BridgeMediaDownloader(MediaDownloader):
    def _wxgf_to_jpg(self, data: bytes):
        """WXGF can carry alpha and color as independent HEVC streams."""
        from PIL import Image
        exe, stream = self._ffmpeg_exe(), self._extract_hevc(data)
        if not exe or not stream:
            return None
        # A fresh decoder is required: concatenating the alpha stream with RGB can
        # retain the alpha stream's grayscale pixel format even for later frames.
        vps = [m.start() for m in re.finditer(b'\x00\x00(?:\x00)?\x01', stream)
               if m.end() < len(stream) and ((stream[m.end()] >> 1) & 63) == 32]
        if len(vps) > 1:
            stream = stream[vps[-1]:]
        try:
            with tempfile.TemporaryDirectory() as tmp:
                src = Path(tmp) / 'image.hevc'
                src.write_bytes(stream)
                result = subprocess.run(
                    [exe, '-y', '-v', 'error', '-i', str(src), '-frames:v', '1',
                     str(Path(tmp) / 'frame-%02d.png')],
                    capture_output=True, timeout=30, creationflags=0x08000000)
                frames = sorted(Path(tmp).glob('frame-*.png'))
                if result.returncode != 0 or not frames:
                    return None
                with Image.open(frames[0]) as image:
                    selected = image.convert('RGB')
                with BytesIO() as output:
                    selected.save(output, 'JPEG', quality=90)
                    return output.getvalue()
        except (OSError, subprocess.SubprocessError):
            return None


def copy_image_to_clipboard(path: str) -> bool:
    """Paste actual bitmap bytes, avoiding WeChat's CF_HDROP attachment dialog."""
    import ctypes
    import time
    from PIL import Image
    with Image.open(path) as image, BytesIO() as output:
        image.convert('RGB').save(output, 'BMP')
        dib = output.getvalue()[14:]
    u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
    u32.OpenClipboard.argtypes = [ctypes.c_void_p]
    u32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    u32.SetClipboardData.restype = ctypes.c_void_p
    k32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    k32.GlobalAlloc.restype = ctypes.c_void_p
    k32.GlobalLock.argtypes = [ctypes.c_void_p]
    k32.GlobalLock.restype = ctypes.c_void_p
    k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    k32.GlobalFree.argtypes = [ctypes.c_void_p]
    for _ in range(4):
        if u32.OpenClipboard(None):
            break
        time.sleep(0.15)
    else:
        return False
    handle = None
    try:
        if not u32.EmptyClipboard():
            return False
        handle = k32.GlobalAlloc(0x0042, len(dib))
        if not handle:
            return False
        pointer = k32.GlobalLock(handle)
        if not pointer:
            return False
        ctypes.memmove(pointer, dib, len(dib))
        k32.GlobalUnlock(handle)
        if not u32.SetClipboardData(8, handle):  # CF_DIB
            return False
        handle = None  # ownership transferred to Windows
        return True
    finally:
        u32.CloseClipboard()
        if handle:
            k32.GlobalFree(handle)


def find_download(base: Path, name: str, *, expected_size=0, expected_md5='', occurred_at=0):
    """Match WeChat's name(n).ext using source size/hash, never another old namesake."""
    filename = Path(name).name
    stem, suffix = Path(filename).stem, Path(filename).suffix
    pattern = re.compile(re.escape(stem) + r'(?:\(\d+\)|（\d+）)?' + re.escape(suffix), re.I)
    digest = str(expected_md5 or '').lower()
    if not re.fullmatch(r'[0-9a-f]{32}', digest):
        digest = ''
    matches = []
    if not base.is_dir():
        return None
    for folder in base.iterdir():
        # WeChat stores received files in month directories, not arbitrary nested trees.
        if not folder.is_dir():
            continue
        for candidate in folder.iterdir():
            if not candidate.is_file() or not pattern.fullmatch(candidate.name):
                continue
            stat = candidate.stat()
            if expected_size and stat.st_size != int(expected_size):
                continue
            if stat.st_size > 5_000_000:
                continue
            if digest:
                if hashlib.md5(candidate.read_bytes()).hexdigest() != digest:
                    continue
            else:
                # Renamed candidates need evidence linking them to this message.
                if candidate.name != filename and not (expected_size and occurred_at):
                    continue
                if occurred_at and abs(stat.st_mtime - float(occurred_at)) > 180:
                    continue
            matches.append(candidate)
    if len(matches) == 1:
        return str(matches[0])
    if digest and matches:
        return str(max(matches, key=lambda p: p.stat().st_mtime))
    return None
