import os

from core.errors import (
    PermanentError,
)

from core.plugin_logging import (
    plugin_log,
)


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

    # TODO: implement logic
    plugin_log(payload, "[yt_dlp_downloader] executed")
    return True
