from core.plugin_runtime import plugin_entrypoint
from .internal.handler import execute


@plugin_entrypoint
def run(payload: dict):
    return execute(payload)
