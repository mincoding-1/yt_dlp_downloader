import os
import re
import shutil
import signal
import subprocess
import time
import json
import hashlib
from pathlib import Path
from pathlib import PurePosixPath
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
DEFAULT_COLLECTION = "custom"
DEFAULT_RELATIVE_PATH = "yt_dlp_downloader"
ALLOWED_COLLECTIONS = {"plex", "shorts", "custom"}
YT_DLP_PATH = Path("/opt/hanis-tools/current/yt-dlp")
MAX_STDIO_CHARS = 4000
MAX_ERROR_CHARS = 1000
DEFAULT_OUTPUT_TEMPLATE = "%(uploader)s [%(channel_id)s]/%(playlist_index)s - %(title).200B [%(id)s].%(ext)s"
MAX_METADATA_ENTRIES = 100
DEFAULT_METADATA_ENTRIES = 50
ARCHIVE_ROOT = STATE_ROOT / "data" / "yt_dlp_downloader" / "archive"
SNAPSHOT_ROOT = STATE_ROOT / "data" / "yt_dlp_downloader" / "snapshots"
STATS_ROOT = STATE_ROOT / "data" / "yt_dlp_downloader" / "schedules"
ARCHIVE_LEASE_TTL = int(os.getenv("HANIS_YTDLP_ARCHIVE_LEASE_TTL", "7200"))
COLLECTION_RE = re.compile(r"^[a-z0-9_-]{1,64}$")
SAFE_ID_RE = re.compile(r"[^A-Za-z0-9._-]+")
FORMAT_PRESETS = {
    "best": "bestvideo+bestaudio/best",
    "1080p": "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
    "720p": "bestvideo[height<=720]+bestaudio/best[height<=720]/best",
    "480p": "bestvideo[height<=480]+bestaudio/best[height<=480]/best",
    "audio": "bestaudio/best",
}
JS_RUNTIME = shutil.which("node")

_CURRENT_PROC = None

ERROR_MAPPING = [
    (re.compile(r"private video|members-only|sign in to confirm|video unavailable|this video is unavailable", re.I), False, "unavailable"),
    (re.compile(r"unsupported url|no suitable extractor", re.I), False, "unsupported_url"),
    (re.compile(r"requested format is not available", re.I), False, "format_unavailable"),
    (re.compile(r"http error 429|too many requests|rate.?limit", re.I), True, "rate_limited"),
    (re.compile(r"timed out|temporary failure|connection reset|network is unreachable", re.I), True, "network"),
    (re.compile(r"ffmpeg.*not installed|ffmpeg.*not found", re.I), False, "missing_ffmpeg"),
]

SKIP_REASON_PATTERNS = [
    (re.compile(r"private video|sign in if you've been granted access", re.I), "private_video"),
    (re.compile(r"video unavailable|this video is unavailable", re.I), "unavailable"),
    (re.compile(r"has already been downloaded|download archive", re.I), "already_in_archive"),
    (re.compile(r"file already exists|has already been downloaded", re.I), "file_exists"),
    (re.compile(r"live event will begin|live stream offline|premieres in", re.I), "live_not_ready"),
    (re.compile(r"members-only", re.I), "members_only"),
]

PLAYLIST_NON_FATAL_REASONS = {"unavailable"}


def _validate_url(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PermanentError("url is required")
    url = value.strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise PermanentError("url must be an absolute http/https URL")
    return url


def _validate_collection(value: str | None) -> str:
    collection = str(value or DEFAULT_COLLECTION).strip().lower()
    if not COLLECTION_RE.fullmatch(collection) or collection not in ALLOWED_COLLECTIONS:
        allowed = ", ".join(sorted(ALLOWED_COLLECTIONS))
        raise PermanentError(f"collection must be one of: {allowed}")
    return collection


def _validate_relative_path(value: str | None) -> str:
    raw = str(value or DEFAULT_RELATIVE_PATH).strip()
    if not raw or raw == ".":
        raw = DEFAULT_RELATIVE_PATH
    if "\x00" in raw:
        raise PermanentError("relative_path contains invalid null byte")
    posix = PurePosixPath(raw)
    if posix.is_absolute() or any(part in {"", ".", ".."} for part in posix.parts):
        raise PermanentError("relative_path must be a safe relative POSIX path")
    if len(str(posix)) > 240:
        raise PermanentError("relative_path is too long")
    return str(posix)


def _resolve_output_dir(payload: dict) -> tuple[Path, str, str]:
    if payload.get("output_path"):
        raise PermanentError("output_path is no longer supported; use collection and relative_path")
    collection = _validate_collection(payload.get("collection"))
    relative_path = _validate_relative_path(payload.get("relative_path"))
    target = ALLOWED_OUTPUT_ROOT / collection / relative_path
    resolved = target.expanduser().resolve(strict=False)
    allowed_root = ALLOWED_OUTPUT_ROOT.resolve(strict=False)
    if not (resolved == allowed_root or allowed_root in resolved.parents):
        raise PermanentError(f"resolved output path must be under {allowed_root}")
    if resolved.exists() and not resolved.is_dir():
        raise PermanentError("resolved output path must be a directory")
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved, collection, relative_path


def _resolve_output_template(value: str | None) -> str:
    if value is None or not str(value).strip():
        return DEFAULT_OUTPUT_TEMPLATE
    template = str(value).strip()
    if "\x00" in template:
        raise PermanentError("output_template contains invalid null byte")
    if "\\" in template:
        raise PermanentError("output_template must use POSIX separators")
    posix = PurePosixPath(template)
    if posix.is_absolute() or any(part in {"", ".", ".."} for part in posix.parts):
        raise PermanentError("output_template must be a safe relative yt-dlp template")
    if len(template) > 240:
        raise PermanentError("output_template is too long")
    if "%(" not in template:
        raise PermanentError("output_template must include yt-dlp fields such as %(title)s")
    if "%(id)" not in template:
        raise PermanentError("output_template must include %(id)s to avoid filename collisions")
    return template


def _tool_env(output_dir: Path) -> dict:
    return {
        "PATH": "/opt/hanis-tools/current:/usr/local/bin:/usr/bin:/bin",
        "HOME": str(output_dir),
        "LANG": "C",
        "LC_ALL": "C",
    }


def _append_js_runtime(cmd: list[str]) -> None:
    if shutil.which("node"):
        cmd.extend(["--js-runtimes", "node"])


def _resolve_format_selector(payload: dict) -> tuple[str, str]:
    preset = str(payload.get("format_preset") or "").strip().lower()
    custom = str(payload.get("format_selector") or "").strip()
    mode = str(payload.get("download_mode") or "").strip().lower()

    if mode == "audio" and not preset:
        preset = "audio"
    if not preset:
        preset = "best"

    if preset == "custom":
        if not custom:
            raise PermanentError("format_selector is required when format_preset is custom")
        if len(custom) > 200:
            raise PermanentError("format_selector is too long")
        if any(ch in custom for ch in "\x00\r\n;&|`$<>"):
            raise PermanentError("format_selector contains unsafe characters")
        return preset, custom

    if preset not in FORMAT_PRESETS:
        raise PermanentError(f"unsupported format_preset: {preset}")
    return preset, FORMAT_PRESETS[preset]


def _resolve_selected_entries(value) -> str | None:
    if value in (None, "", []):
        return None

    if isinstance(value, str):
        selected = value.strip()
        if not selected:
            return None
        if len(selected) > 500 or not re.fullmatch(r"[0-9,\-\s]+", selected):
            raise PermanentError("selected_entries must be comma separated numeric playlist indexes")
        return ",".join(part.strip() for part in selected.split(",") if part.strip())

    if not isinstance(value, list):
        raise PermanentError("selected_entries must be a list or comma separated string")
    if len(value) > MAX_METADATA_ENTRIES:
        raise PermanentError(f"selected_entries may contain at most {MAX_METADATA_ENTRIES} items")

    indexes = []
    for item in value:
        try:
            index = int(item)
        except Exception:
            raise PermanentError("selected_entries must contain numeric playlist indexes")
        if index < 1 or index > 9999:
            raise PermanentError("selected_entries indexes must be between 1 and 9999")
        indexes.append(index)
    if not indexes:
        return None
    return ",".join(str(i) for i in sorted(set(indexes)))


def _metadata_limit(payload: dict) -> int:
    try:
        value = int(payload.get("metadata_limit") or DEFAULT_METADATA_ENTRIES)
    except Exception:
        value = DEFAULT_METADATA_ENTRIES
    return max(1, min(value, MAX_METADATA_ENTRIES))


def _resolve_yt_dlp() -> str:
    try:
        resolved = YT_DLP_PATH.resolve(strict=True)
    except FileNotFoundError:
        raise TransientError("yt-dlp binary is not installed")
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise TransientError("yt-dlp binary is not executable")
    return str(resolved)


def _safe_id(value: str) -> str:
    cleaned = SAFE_ID_RE.sub("_", str(value).strip()).strip("._-")
    return cleaned[:160] or "unknown"


def build_archive_key(extractor_key: str, entity_type: str, entity_id: str) -> str:
    return _safe_id(f"{extractor_key}_{entity_type}_{entity_id}")


def _entity_from_metadata(metadata: dict, url: str) -> tuple[str, str, str]:
    extractor = str(metadata.get("extractor_key") or metadata.get("extractor") or "unknown").split(":")[0].lower()
    extractor = _safe_id(extractor)
    playlist_id = metadata.get("playlist_id") or (metadata.get("id") if _classify_metadata(metadata) == "playlist" else None)
    if playlist_id:
        return extractor, "playlist", str(playlist_id)
    channel_id = metadata.get("channel_id")
    if channel_id:
        return extractor, "channel", str(channel_id)
    uploader_id = metadata.get("uploader_id")
    if uploader_id:
        return extractor, "uploader", str(uploader_id)
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    return extractor, "urlhash", digest


def _resolve_archive(payload: dict, metadata: dict, url: str) -> tuple[str | None, Path | None]:
    mode = str(payload.get("archive_mode") or "disabled").strip().lower()
    if mode in {"", "disabled", "off", "false"}:
        return None, None
    if mode not in {"auto", "custom"}:
        raise PermanentError("archive_mode must be auto, custom, or disabled")
    if mode == "custom":
        raw_archive_id = str(payload.get("archive_id") or "").strip()
        if not raw_archive_id:
            raise PermanentError("archive_id is required when archive_mode is custom")
        archive_id = _safe_id(raw_archive_id)
    else:
        archive_id = build_archive_key(*_entity_from_metadata(metadata, url))
    ARCHIVE_ROOT.mkdir(parents=True, exist_ok=True)
    return archive_id, ARCHIVE_ROOT / f"{archive_id}.txt"


def _archive_lease_id(archive_id: str) -> str:
    return f"archive_{archive_id}.json"


def _acquire_archive_lease(payload: dict, archive_id: str) -> str:
    from core.write_queue.coordination_lease import acquire_lease, break_stale_lease, read_lease

    lease_id = _archive_lease_id(archive_id)
    worker_id = str(payload.get("job_id") or payload.get("idempotency_key") or os.getpid())
    current = read_lease(lease_id)
    if current.get("valid") and time.time() - float(current.get("created_at", 0)) > ARCHIVE_LEASE_TTL:
        break_stale_lease(lease_id)
    ok = acquire_lease(lease_id, worker_id, {
        "archive_id": archive_id,
        "plugin": "yt_dlp_downloader",
        "ttl": ARCHIVE_LEASE_TTL,
    })
    if not ok:
        err = TransientError(f"archive is locked: {archive_id}")
        err.retryable = True
        raise err
    return lease_id


def _release_archive_lease(lease_id: str | None):
    if not lease_id:
        return
    from core.write_queue.coordination_lease import release_lease

    release_lease(lease_id)


def _classify_metadata(metadata: dict) -> str:
    if isinstance(metadata.get("entries"), list):
        extractor = str(metadata.get("extractor_key") or metadata.get("extractor") or "").lower()
        url = str(metadata.get("webpage_url") or metadata.get("original_url") or "").lower()
        if "channel" in extractor or "/channel/" in url or "/@" in url:
            return "channel"
        return "playlist"
    if metadata.get("_type") in {"playlist", "multi_video"}:
        return "playlist"
    if metadata.get("id") or metadata.get("title"):
        return "video"
    return "unknown"


def _entry_url(entry: dict) -> str | None:
    for key in ("webpage_url", "url"):
        value = entry.get(key)
        if isinstance(value, str) and value:
            if value.startswith("http://") or value.startswith("https://"):
                return value
    ie_key = entry.get("ie_key")
    entry_id = entry.get("id")
    if ie_key == "Youtube" and entry_id:
        return f"https://www.youtube.com/watch?v={entry_id}"
    return None


def _normalize_entry(entry: dict, index: int) -> dict:
    return {
        "index": index,
        "id": entry.get("id"),
        "title": entry.get("title") or entry.get("id") or f"Item {index}",
        "duration": entry.get("duration"),
        "url": _entry_url(entry),
        "thumbnail": entry.get("thumbnail"),
        "uploader": entry.get("uploader") or entry.get("channel"),
    }


def _normalize_metadata(metadata: dict, limit: int) -> dict:
    raw_entries = metadata.get("entries")
    entries = []
    if isinstance(raw_entries, list):
        for index, entry in enumerate(raw_entries[:limit], start=1):
            if isinstance(entry, dict):
                entries.append(_normalize_entry(entry, index))

    return {
        "kind": _classify_metadata(metadata),
        "id": metadata.get("id"),
        "title": metadata.get("title"),
        "webpage_url": metadata.get("webpage_url") or metadata.get("original_url"),
        "extractor": metadata.get("extractor") or metadata.get("extractor_key"),
        "thumbnail": metadata.get("thumbnail"),
        "duration": metadata.get("duration"),
        "uploader": metadata.get("uploader") or metadata.get("channel"),
        "entries": entries,
        "entries_count": len(raw_entries) if isinstance(raw_entries, list) else 0,
        "entries_truncated": isinstance(raw_entries, list) and len(raw_entries) > len(entries),
        "entries_limit": limit,
    }


def _archive_contains(archive_path: Path | None, video_id: str | None) -> bool:
    if not archive_path or not video_id or not archive_path.exists():
        return False
    needle = str(video_id)
    try:
        with open(archive_path, "r", encoding="utf-8") as f:
            for line in f:
                parts = line.strip().split()
                if parts and parts[-1] == needle:
                    return True
    except OSError:
        return False
    return False


def _preview_items(metadata: dict, archive_path: Path | None, limit: int) -> dict:
    raw_entries = metadata.get("entries")
    if isinstance(raw_entries, list):
        entries = [_normalize_entry(entry, index) for index, entry in enumerate(raw_entries[:limit], start=1) if isinstance(entry, dict)]
    else:
        entries = [{
            "index": 1,
            "id": metadata.get("id"),
            "title": metadata.get("title") or metadata.get("id") or "Item 1",
            "duration": metadata.get("duration"),
            "url": metadata.get("webpage_url") or metadata.get("original_url"),
            "thumbnail": metadata.get("thumbnail"),
            "uploader": metadata.get("uploader") or metadata.get("channel"),
        }]
    will_download = []
    will_skip = []
    for entry in entries:
        reason = _entry_skip_reason(entry)
        if reason:
            will_skip.append({**entry, "reason": reason})
        elif _archive_contains(archive_path, entry.get("id")):
            will_skip.append({**entry, "reason": "already_in_archive"})
        else:
            will_download.append(entry)
    return {
        "will_download": will_download,
        "will_skip": will_skip,
    }


def _write_snapshot(payload: dict, metadata: dict, archive_id: str | None, counts: dict):
    schedule_id = str(payload.get("schedule_id") or payload.get("idempotency_key") or payload.get("job_id") or "manual")
    safe_schedule = _safe_id(schedule_id)
    target_dir = SNAPSHOT_ROOT / safe_schedule
    target_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "checked_at": time.time(),
        "archive_id": archive_id,
        "playlist_id": metadata.get("playlist_id") or metadata.get("id"),
        "kind": _classify_metadata(metadata),
        "entries": len(metadata.get("entries") or []),
        **counts,
    }
    latest = target_dir / "latest.json"
    tmp = latest.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, latest)
    rotated = target_dir / f"{int(data['checked_at'])}.json"
    with open(rotated, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    old = sorted(p for p in target_dir.glob("*.json") if p.name != "latest.json")
    for path in old[:-10]:
        try:
            path.unlink()
        except OSError:
            pass


def _update_schedule_failures(payload: dict, failed: bool, retryable: bool):
    schedule_id = payload.get("schedule_id")
    if not schedule_id:
        return
    safe_schedule = _safe_id(str(schedule_id))
    STATS_ROOT.mkdir(parents=True, exist_ok=True)
    path = STATS_ROOT / f"{safe_schedule}.json"
    data = {}
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
    if failed and not retryable:
        data["consecutive_failures"] = int(data.get("consecutive_failures") or 0) + 1
        data["last_failure_at"] = time.time()
    elif not failed:
        data["consecutive_failures"] = 0
        data["last_success_at"] = time.time()
    data["schedule_id"] = str(schedule_id)
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    if int(data.get("consecutive_failures") or 0) >= 5:
        try:
            from core.scheduler import set_schedule_enabled

            set_schedule_enabled(str(schedule_id), False, reason="yt_dlp_downloader consecutive permanent failures")
        except Exception:
            plugin_log(payload, "[yt_dlp_downloader] failed to disable schedule after permanent failures")


def _classify_yt_dlp_error(output: str) -> tuple[bool, str]:
    for pattern, retryable, reason in ERROR_MAPPING:
        if pattern.search(output or ""):
            return retryable, reason
    return True, "yt_dlp_failed"


def _entry_skip_reason(entry: dict) -> str | None:
    availability = str(entry.get("availability") or "").lower()
    live_status = str(entry.get("live_status") or "").lower()
    title = str(entry.get("title") or "").lower()
    if availability in {"private", "premium_only", "subscriber_only", "needs_auth"}:
        return "private_video"
    if availability in {"unavailable", "deleted"}:
        return "unavailable"
    if live_status in {"is_upcoming", "is_live"}:
        return "live_not_ready"
    if "[private video]" in title:
        return "private_video"
    if "[deleted video]" in title:
        return "unavailable"
    return None


def _parse_skipped_items(output: str, metadata: dict | None = None, limit: int = 50) -> list[dict]:
    skipped = []
    seen = set()
    for line in (output or "").splitlines():
        line = line.strip()
        if not line:
            continue
        reason = None
        for pattern, mapped in SKIP_REASON_PATTERNS:
            if pattern.search(line):
                reason = mapped
                break
        if not reason:
            continue
        item_id = None
        match = re.search(r"\[youtube\]\s+([A-Za-z0-9_-]{6,})", line)
        if match:
            item_id = match.group(1)
        key = (item_id or line[-120:], reason)
        if key in seen:
            continue
        seen.add(key)
        skipped.append({
            "id": item_id,
            "title": item_id or "unknown",
            "reason": reason,
            "detail": line[-240:],
        })
        if len(skipped) >= limit:
            return skipped

    if metadata:
        raw_entries = metadata.get("entries")
        if isinstance(raw_entries, list):
            for index, entry in enumerate(raw_entries[:limit], start=1):
                if not isinstance(entry, dict):
                    continue
                reason = _entry_skip_reason(entry)
                if not reason:
                    continue
                normalized = _normalize_entry(entry, index)
                key = (normalized.get("id") or index, reason)
                if key in seen:
                    continue
                seen.add(key)
                skipped.append({**normalized, "reason": reason})
                if len(skipped) >= limit:
                    break
    return skipped


def _count_archive_skips(output: str) -> int:
    return len(re.findall(r"has already been downloaded|download archive", output or "", flags=re.I))


def _playlist_partial_result(
    payload: dict,
    *,
    metadata: dict,
    archive_id: str | None,
    output_dir: Path,
    collection: str,
    relative_path: str,
    files: list[dict],
    combined_output: str,
    duration: float,
    download_mode: str,
    format_preset: str,
    format_selector: str,
    selected_entries: str | None,
    output_template_name: str,
) -> dict:
    skipped = _parse_skipped_items(combined_output, metadata, _metadata_limit(payload))
    archive_skips = _count_archive_skips(combined_output)
    skipped_count = max(len(skipped), archive_skips)
    status = "partial" if files else "no_new_items"
    plugin_log(
        payload,
        f"[yt_dlp_downloader] playlist completed status={status} files={len(files)} skipped={skipped_count}",
    )
    _write_progress(payload, {
        "status": "completed",
        "percent": 100.0,
        "files": len(files),
        "skipped": skipped_count,
        "result_status": status,
    })
    _update_schedule_failures(payload, failed=False, retryable=False)
    _write_snapshot(payload, metadata, archive_id, {
        "downloaded_count": len(files),
        "skipped_count": skipped_count,
    })
    return {
        "success": True,
        "mode": "download",
        "status": status,
        "collection": collection,
        "relative_path": relative_path,
        "archive_id": archive_id,
        "download_mode": download_mode,
        "format_preset": format_preset,
        "format_selector": format_selector,
        "selected_entries": selected_entries,
        "output_dir": str(output_dir),
        "output_template": output_template_name,
        "files": files[:20],
        "downloaded_count": len(files),
        "skipped_count": skipped_count,
        "skipped": skipped[:20],
        "duration_seconds": duration,
        "diagnostic_tail": _tail(combined_output),
    }


def _raise_yt_dlp_error(payload: dict, output: str):
    retryable, reason = _classify_yt_dlp_error(output)
    summary = _error_summary(output)
    exc_cls = TransientError if retryable else PermanentError
    err = exc_cls(f"yt-dlp failed ({reason}): {summary}")
    err.retryable = retryable
    _update_schedule_failures(payload, failed=True, retryable=retryable)
    raise err


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
    for path in output_dir.rglob("*"):
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
    for path in output_dir.rglob("*"):
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
                **_tool_env(output_dir),
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
    limit = _metadata_limit(payload)
    cmd = [
        binary,
        "--dump-single-json",
        "--skip-download",
        "--no-warnings",
        "--flat-playlist",
        "--playlist-end",
        str(limit),
        url,
    ]
    _append_js_runtime(cmd)
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
                **_tool_env(output_dir),
            },
        )
    except subprocess.TimeoutExpired:
        raise TransientError(f"yt-dlp metadata lookup timed out after {timeout}s")
    if result.returncode != 0:
        _raise_yt_dlp_error(payload, result.stderr)
    try:
        metadata = json.loads(result.stdout)
    except Exception:
        raise TransientError("yt-dlp metadata lookup returned invalid JSON")
    normalized = _normalize_metadata(metadata, limit)
    _write_progress(payload, {"status": "completed", "percent": 100.0, "mode": "metadata"})
    return {
        "success": True,
        "mode": "metadata",
        "metadata": normalized,
    }


def _extract_metadata(payload: dict, binary: str, url: str, output_dir: Path, timeout: int) -> dict:
    limit = _metadata_limit(payload)
    cmd = [
        binary,
        "--dump-single-json",
        "--skip-download",
        "--no-warnings",
        "--flat-playlist",
        "--playlist-end",
        str(limit),
        url,
    ]
    _append_js_runtime(cmd)
    try:
        result = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            env={
                **_tool_env(output_dir),
            },
        )
    except subprocess.TimeoutExpired:
        err = TransientError(f"yt-dlp metadata lookup timed out after {timeout}s")
        err.retryable = True
        raise err
    if result.returncode != 0:
        _raise_yt_dlp_error(payload, result.stderr)
    try:
        return json.loads(result.stdout)
    except Exception:
        err = TransientError("yt-dlp metadata lookup returned invalid JSON")
        err.retryable = True
        raise err


def execute(payload: dict):
    """
    payload example:
{
    "url": "example",
    "collection": "custom",
    "relative_path": "yt_dlp_downloader"
}
    """

    # 기본 검증
    if not isinstance(payload, dict):
        raise PermanentError("payload must be dict")

    url = _validate_url(payload.get("url"))
    output_dir, collection, relative_path = _resolve_output_dir(payload)
    output_template_name = _resolve_output_template(payload.get("output_template"))
    binary = _resolve_yt_dlp()
    before_files = _snapshot_files(output_dir)

    timeout = int(payload.get("timeout") or 1800)
    timeout = max(1, min(timeout, 3600))
    if payload.get("metadata_only"):
        return _metadata_lookup(payload, binary, url, output_dir, timeout)

    metadata = _extract_metadata(payload, binary, url, output_dir, min(timeout, 300))
    archive_id, archive_path = _resolve_archive(payload, metadata, url)
    if payload.get("dry_run"):
        preview = _preview_items(metadata, archive_path, _metadata_limit(payload))
        normalized = _normalize_metadata(metadata, _metadata_limit(payload))
        _write_snapshot(
            payload,
            metadata,
            archive_id,
            {
                "downloaded_count": 0,
                "skipped_count": len(preview["will_skip"]),
                "will_download_count": len(preview["will_download"]),
            },
        )
        return {
            "success": True,
            "mode": "dry_run",
            "status": "no_new_items" if not preview["will_download"] else "preview",
            "collection": collection,
            "relative_path": relative_path,
            "output_dir": str(output_dir),
            "archive_id": archive_id,
            "metadata": normalized,
            **preview,
        }

    output_template = str(output_dir / output_template_name)
    timeout = int(payload.get("timeout") or 1800)
    timeout = max(1, min(timeout, 3600))
    allow_playlist = bool(payload.get("allow_playlist"))
    selected_entries = _resolve_selected_entries(payload.get("selected_entries"))
    if selected_entries:
        allow_playlist = True
    format_preset, format_selector = _resolve_format_selector(payload)
    download_mode = str(payload.get("download_mode") or "video").strip().lower()
    audio_format = str(payload.get("audio_format") or "source").strip().lower()
    if download_mode not in {"video", "audio"}:
        raise PermanentError("download_mode must be video or audio")
    if audio_format not in {"source", "mp3", "m4a", "opus", "flac", "wav"}:
        raise PermanentError("unsupported audio_format")

    cmd = [
        binary,
        "--newline",
        "--restrict-filenames",
        "-f",
        format_selector,
        "-o",
        output_template,
    ]
    if archive_path:
        cmd.extend(["--download-archive", str(archive_path)])
    _append_js_runtime(cmd)
    if selected_entries:
        cmd.extend(["--playlist-items", selected_entries])
    if not allow_playlist:
        cmd.append("--no-playlist")
    else:
        cmd.append("--ignore-errors")
    if download_mode == "audio" and audio_format != "source":
        cmd.extend(["--extract-audio", "--audio-format", audio_format])
    cmd.append(url)

    plugin_log(
        payload,
        "[yt_dlp_downloader] start "
        f"collection={collection} relative_path={relative_path} output_dir={output_dir} template={output_template_name} "
        f"format_preset={format_preset} selected_entries={selected_entries or '-'}",
    )
    lease_id = None
    try:
        if archive_id:
            lease_id = _acquire_archive_lease(payload, archive_id)
        rc, combined_output, duration = _run_yt_dlp(payload, cmd, output_dir, timeout)
        if rc != 0:
            plugin_log(payload, f"[yt_dlp_downloader] failed rc={rc}")
            _write_progress(payload, {"status": "failed", "error": _error_summary(combined_output)})
            retryable, reason = _classify_yt_dlp_error(combined_output)
            if allow_playlist and reason in PLAYLIST_NON_FATAL_REASONS:
                files = _collect_changed_files(output_dir, before_files)
                return _playlist_partial_result(
                    payload,
                    metadata=metadata,
                    archive_id=archive_id,
                    output_dir=output_dir,
                    collection=collection,
                    relative_path=relative_path,
                    files=files,
                    combined_output=combined_output,
                    duration=duration,
                    download_mode=download_mode,
                    format_preset=format_preset,
                    format_selector=format_selector,
                    selected_entries=selected_entries,
                    output_template_name=output_template_name,
                )
            _raise_yt_dlp_error(payload, combined_output)
    finally:
        _release_archive_lease(lease_id)

    files = _collect_changed_files(output_dir, before_files)
    skipped = _parse_skipped_items(combined_output, metadata, _metadata_limit(payload))
    archive_skips = _count_archive_skips(combined_output)
    if allow_playlist and (skipped or archive_skips) and files:
        return _playlist_partial_result(
            payload,
            metadata=metadata,
            archive_id=archive_id,
            output_dir=output_dir,
            collection=collection,
            relative_path=relative_path,
            files=files,
            combined_output=combined_output,
            duration=duration,
            download_mode=download_mode,
            format_preset=format_preset,
            format_selector=format_selector,
            selected_entries=selected_entries,
            output_template_name=output_template_name,
        )
    if not files:
        if "has already been downloaded" in combined_output or archive_path or skipped:
            plugin_log(payload, "[yt_dlp_downloader] completed with no new items")
            _write_progress(payload, {"status": "completed", "percent": 100.0, "files": 0})
            _update_schedule_failures(payload, failed=False, retryable=False)
            skipped_count = max(len(skipped), archive_skips)
            _write_snapshot(payload, metadata, archive_id, {"downloaded_count": 0, "skipped_count": skipped_count})
            return {
                "success": True,
                "mode": "download",
                "status": "no_new_items",
                "collection": collection,
                "relative_path": relative_path,
                "archive_id": archive_id,
                "output_dir": str(output_dir),
                "files": [],
                "downloaded_count": 0,
                "skipped_count": skipped_count,
                "skipped": skipped[:20],
                "duration_seconds": duration,
                "diagnostic_tail": _tail(combined_output),
            }
        err = TransientError("yt-dlp completed but no new or changed output files were found")
        err.retryable = True
        raise err

    plugin_log(payload, f"[yt_dlp_downloader] completed files={len(files)} duration={duration}s")
    _write_progress(payload, {"status": "completed", "percent": 100.0, "files": len(files)})
    _update_schedule_failures(payload, failed=False, retryable=False)
    _write_snapshot(payload, metadata, archive_id, {"downloaded_count": len(files), "skipped_count": 0})
    return {
        "success": True,
        "mode": "download",
        "status": "success",
        "collection": collection,
        "relative_path": relative_path,
        "archive_id": archive_id,
        "download_mode": download_mode,
        "format_preset": format_preset,
        "format_selector": format_selector,
        "selected_entries": selected_entries,
        "output_dir": str(output_dir),
        "output_template": output_template_name,
        "files": files[:20],
        "downloaded_count": len(files),
        "skipped_count": 0,
        "skipped": [],
        "duration_seconds": duration,
        "diagnostic_tail": _tail(combined_output),
    }
