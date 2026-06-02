import os
import re
import signal
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

from core.config import STATE_ROOT
from core.errors import (
    PermanentError,
    TransientError,
)

from core.plugin_logging import (
    plugin_log,
)

ALLOWED_OUTPUT_ROOT = Path(os.getenv("HANIS_DOWNLOAD_ROOT", "/var/lib/hanis/downloads"))
DEFAULT_OUTPUT_ROOT = ALLOWED_OUTPUT_ROOT / "yt_dlp_downloader"
YT_DLP_PATH = Path("/opt/hanis-tools/current/yt-dlp")
MAX_STDIO_CHARS = 4000
MAX_ERROR_CHARS = 1000
DEFAULT_OUTPUT_TEMPLATE = "%(title).200B [%(id)s].%(ext)s"

_CURRENT_PROC = None


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
    allowed_root = ALLOWED_OUTPUT_ROOT.resolve(strict=False)
    if not (resolved == allowed_root or allowed_root in resolved.parents):
        raise PermanentError(f"output_path must be under {allowed_root}")
    if resolved.exists() and not resolved.is_dir():
        raise PermanentError("output_path must be a directory")
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def _resolve_output_template(value: str | None) -> str:
    if value is None or not str(value).strip():
        return DEFAULT_OUTPUT_TEMPLATE
    template = str(value).strip()
    if "\x00" in template:
        raise PermanentError("output_template contains invalid null byte")
    if "/" in template or "\\" in template:
        raise PermanentError("output_template must be a filename template, not a path")
    if ".." in template:
        raise PermanentError("output_template must not contain '..'")
    if len(template) > 240:
        raise PermanentError("output_template is too long")
    if "%(" not in template:
        raise PermanentError("output_template must include yt-dlp fields such as %(title)s")
    return template


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


def _error_summary(value: str) -> str:
    if not value:
        return "yt-dlp failed without diagnostic output"
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    selected = []
    for line in lines:
        lower = line.lower()
        if "error:" in lower or "warning:" in lower or "unable to" in lower or "failed" in lower:
            selected.append(line)
    if not selected:
        selected = lines[-5:]
    return "\n".join(selected)[-MAX_ERROR_CHARS:]


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
        if path.suffix in {".part", ".ytdl", ".tmp"}:
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
            "title": path.stem,
            "ext": path.suffix.lstrip("."),
            "mtime": stat.st_mtime,
        })
    files.sort(key=lambda item: item["mtime"], reverse=True)
    return files


def _progress_path(job_id: str) -> Path:
    return STATE_ROOT / "jobs" / "progress" / f"{job_id}.json"


def _write_progress(payload: dict, progress: dict):
    job_id = payload.get("job_id")
    if not job_id:
        return
    progress = {
        "job_id": job_id,
        "plugin": "yt_dlp_downloader",
        "updated_at": time.time(),
        **progress,
    }
    path = _progress_path(job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    import json
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(progress, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


_PROGRESS_RE = re.compile(
    r"\[download\]\s+(?P<percent>\d+(?:\.\d+)?)%\s+of\s+"
    r"(?P<total>\S+)(?:\s+at\s+(?P<speed>\S+))?(?:\s+ETA\s+(?P<eta>\S+))?"
)


def _parse_progress_line(line: str) -> dict | None:
    match = _PROGRESS_RE.search(line)
    if match:
        data = match.groupdict()
        return {
            "status": "downloading",
            "percent": float(data["percent"]),
            "total": data.get("total"),
            "speed": data.get("speed"),
            "eta": data.get("eta"),
            "raw": line[-300:],
        }
    if "[download] Destination:" in line:
        return {
            "status": "destination",
            "destination": line.split("Destination:", 1)[1].strip(),
            "raw": line[-300:],
        }
    if "[download] 100%" in line or "has already been downloaded" in line:
        return {
            "status": "downloaded",
            "percent": 100.0,
            "raw": line[-300:],
        }
    return None


def _install_signal_handlers(payload: dict):
    previous_term = signal.getsignal(signal.SIGTERM)
    previous_int = signal.getsignal(signal.SIGINT)

    def _handle(signum, _frame):
        proc = _CURRENT_PROC
        if proc and proc.poll() is None:
            plugin_log(payload, f"[yt_dlp_downloader] received signal={signum}; terminating yt-dlp")
            try:
                proc.terminate()
            except Exception:
                pass
        raise TransientError("download interrupted")

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)
    return previous_term, previous_int


def _restore_signal_handlers(previous):
    previous_term, previous_int = previous
    signal.signal(signal.SIGTERM, previous_term)
    signal.signal(signal.SIGINT, previous_int)


def _run_yt_dlp(payload: dict, cmd: list[str], output_dir: Path, timeout: int) -> tuple[int, str, float]:
    global _CURRENT_PROC
    lines = []
    started = time.time()
    deadline = started + timeout
    previous_handlers = _install_signal_handlers(payload)
    _write_progress(payload, {"status": "starting", "percent": 0.0})
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env={
                "PATH": "/opt/hanis-tools/current:/usr/local/bin:/usr/bin:/bin",
                "HOME": str(output_dir),
                "LANG": "C",
                "LC_ALL": "C",
            },
        )
        _CURRENT_PROC = proc
        assert proc.stdout is not None
        for raw_line in proc.stdout:
            line = raw_line.rstrip("\n")
            lines.append(line)
            if len(lines) > 500:
                lines = lines[-500:]
            progress = _parse_progress_line(line)
            if progress:
                _write_progress(payload, progress)
                if progress.get("status") == "downloading":
                    plugin_log(
                        payload,
                        "[yt_dlp_downloader] progress "
                        f"percent={progress.get('percent')} speed={progress.get('speed')} eta={progress.get('eta')}",
                    )
            if time.time() > deadline:
                try:
                    proc.terminate()
                except Exception:
                    pass
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
                raise TransientError(f"yt-dlp timed out after {timeout}s")
        rc = proc.wait(timeout=5)
        return rc, "\n".join(lines), round(time.time() - started, 3)
    finally:
        _CURRENT_PROC = None
        _restore_signal_handlers(previous_handlers)


def _metadata_lookup(payload: dict, binary: str, url: str, output_dir: Path, timeout: int):
    cmd = [
        binary,
        "--dump-single-json",
        "--skip-download",
        "--no-warnings",
        url,
    ]
    plugin_log(payload, "[yt_dlp_downloader] metadata lookup start")
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
    except subprocess.TimeoutExpired:
        raise TransientError(f"yt-dlp metadata lookup timed out after {timeout}s")
    if result.returncode != 0:
        raise TransientError(
            "yt-dlp metadata lookup failed: "
            f"{_error_summary(result.stderr)}"
        )
    import json
    try:
        metadata = json.loads(result.stdout)
    except Exception:
        raise TransientError("yt-dlp metadata lookup returned invalid JSON")
    return {
        "success": True,
        "mode": "metadata",
        "metadata": {
            "id": metadata.get("id"),
            "title": metadata.get("title"),
            "webpage_url": metadata.get("webpage_url"),
            "extractor": metadata.get("extractor"),
            "duration": metadata.get("duration"),
            "playlist_count": len(metadata.get("entries") or []) if isinstance(metadata.get("entries"), list) else None,
        },
    }


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
    output_template_name = _resolve_output_template(payload.get("output_template"))
    binary = _resolve_yt_dlp()
    before_files = _snapshot_files(output_dir)

    timeout = int(payload.get("timeout") or 1800)
    timeout = max(1, min(timeout, 3600))
    if payload.get("metadata_only"):
        return _metadata_lookup(payload, binary, url, output_dir, timeout)

    output_template = str(output_dir / output_template_name)
    timeout = int(payload.get("timeout") or 1800)
    timeout = max(1, min(timeout, 3600))
    allow_playlist = bool(payload.get("allow_playlist"))

    cmd = [
        binary,
        "--newline",
        "--restrict-filenames",
        "-o",
        output_template,
    ]
    if not allow_playlist:
        cmd.append("--no-playlist")
    cmd.append(url)

    plugin_log(payload, f"[yt_dlp_downloader] start output_dir={output_dir} template={output_template_name}")
    rc, combined_output, duration = _run_yt_dlp(payload, cmd, output_dir, timeout)
    if rc != 0:
        plugin_log(payload, f"[yt_dlp_downloader] failed rc={rc}")
        _write_progress(payload, {"status": "failed", "error": _error_summary(combined_output)})
        raise TransientError(
            "yt-dlp failed: "
            f"{_error_summary(combined_output)}"
        )

    files = _collect_changed_files(output_dir, before_files)
    if not files:
        plugin_log(payload, "[yt_dlp_downloader] completed with no output files")
        raise TransientError("yt-dlp completed but no new or changed output files were found")

    plugin_log(payload, f"[yt_dlp_downloader] completed files={len(files)} duration={duration}s")
    _write_progress(payload, {"status": "completed", "percent": 100.0, "files": len(files)})
    return {
        "success": True,
        "mode": "download",
        "output_dir": str(output_dir),
        "output_template": output_template_name,
        "files": files[:20],
        "duration_seconds": duration,
        "diagnostic_tail": _tail(combined_output),
    }
