import os
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

from core.errors import (
    PermanentError,
    TransientError,
)

from core.plugin_logging import (
    plugin_log,
)

DEFAULT_OUTPUT_ROOT = Path("/var/lib/hanis/downloads/yt_dlp_downloader")
YT_DLP_PATH = Path("/opt/hanis-tools/current/yt-dlp")
MAX_STDIO_CHARS = 4000


def _validate_url(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PermanentError("url is required")
    url = value.strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise PermanentError("url must be an absolute http/https URL")
    return url


def _resolve_output_dir(value: str | None) -> Path:
    if value is None or not str(value).strip():
        target = DEFAULT_OUTPUT_ROOT
    else:
        raw = str(value).strip()
        if "\x00" in raw:
            raise PermanentError("output_path contains invalid null byte")
        target = Path(raw)
        if not target.is_absolute():
            target = DEFAULT_OUTPUT_ROOT / target

    resolved = target.expanduser().resolve(strict=False)
    if any(part == ".." for part in target.parts):
        raise PermanentError("output_path must not contain '..'")
    if resolved.exists() and not resolved.is_dir():
        raise PermanentError("output_path must be a directory")
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def _resolve_yt_dlp() -> str:
    try:
        resolved = YT_DLP_PATH.resolve(strict=True)
    except FileNotFoundError:
        raise TransientError("yt-dlp binary is not installed")
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise TransientError("yt-dlp binary is not executable")
    return str(resolved)


def _tail(value: str) -> str:
    if not value:
        return ""
    return value[-MAX_STDIO_CHARS:]


def _snapshot_files(output_dir: Path) -> dict[str, tuple[int, int]]:
    files = {}
    for path in output_dir.iterdir():
        if not path.is_file():
            continue
        try:
            stat = path.stat()
            files[str(path)] = (stat.st_size, stat.st_mtime_ns)
        except OSError:
            continue
    return files


def _collect_changed_files(output_dir: Path, before: dict[str, tuple[int, int]]) -> list[dict]:
    files = []
    for path in output_dir.iterdir():
        if not path.is_file():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        key = str(path)
        current = (stat.st_size, stat.st_mtime_ns)
        if before.get(key) == current:
            continue
        files.append({
            "path": key,
            "size": stat.st_size,
            "mtime": stat.st_mtime,
        })
    files.sort(key=lambda item: item["mtime"], reverse=True)
    return files


def execute(payload: dict):
    """
    payload example:
{
    "url": "example",
    "output_path": "example"
}
    """

    # 기본 검증
    if not isinstance(payload, dict):
        raise PermanentError("payload must be dict")

    url = _validate_url(payload.get("url"))
    output_dir = _resolve_output_dir(payload.get("output_path"))
    binary = _resolve_yt_dlp()
    before_files = _snapshot_files(output_dir)

    output_template = str(output_dir / "%(title).200B [%(id)s].%(ext)s")
    timeout = int(payload.get("timeout") or 1800)
    timeout = max(1, min(timeout, 3600))

    cmd = [
        binary,
        "--no-progress",
        "--restrict-filenames",
        "--no-playlist",
        "-o",
        output_template,
        url,
    ]

    plugin_log(payload, f"[yt_dlp_downloader] start output_dir={output_dir}")
    started = time.time()
    try:
        result = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            env={
                "PATH": "/opt/hanis-tools/current:/usr/local/bin:/usr/bin:/bin",
                "HOME": str(output_dir),
                "LANG": "C",
                "LC_ALL": "C",
            },
        )
    except subprocess.TimeoutExpired as e:
        plugin_log(payload, "[yt_dlp_downloader] timeout")
        raise TransientError(f"yt-dlp timed out after {timeout}s: {_tail(e.stderr or '')}")

    duration = round(time.time() - started, 3)
    if result.returncode != 0:
        plugin_log(payload, f"[yt_dlp_downloader] failed rc={result.returncode}")
        raise TransientError(f"yt-dlp failed rc={result.returncode}: {_tail(result.stderr)}")

    files = _collect_changed_files(output_dir, before_files)
    if not files:
        plugin_log(payload, "[yt_dlp_downloader] completed with no output files")
        raise TransientError("yt-dlp completed but no new or changed output files were found")

    plugin_log(payload, f"[yt_dlp_downloader] completed files={len(files)} duration={duration}s")
    return {
        "success": True,
        "output_dir": str(output_dir),
        "files": files[:20],
        "duration_seconds": duration,
        "stdout_tail": _tail(result.stdout),
        "stderr_tail": _tail(result.stderr),
    }
