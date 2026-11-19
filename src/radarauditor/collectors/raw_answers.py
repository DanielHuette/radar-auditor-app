import logging
import re
from collections.abc import Callable
from contextvars import ContextVar

from radarauditor.errors import StorageError

logger = logging.getLogger(__name__)

RawSink = Callable[[str, bytes], None]

_sink: ContextVar[RawSink | None] = ContextVar("raw_answer_sink", default=None)


def bind(sink: RawSink | None) -> None:
    _sink.set(sink)


def file_name(step: str, supplier: str, what: str, extension: str = "json") -> str:
    safe = re.sub(r"[^A-Za-z0-9_.~-]+", "_", what).strip("_")
    return f"{step}-{supplier}-{safe}.{extension}"


def keep(name: str, body: bytes | str) -> None:
    sink = _sink.get()
    if sink is None:
        return
    data = body.encode("utf-8") if isinstance(body, str) else body
    try:
        sink(name, data)
    except StorageError as error:
        logger.warning("Raw answer %s not stored: %s", name, error)
