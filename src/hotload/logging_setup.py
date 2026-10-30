import hashlib
import json
import logging
import sys
import time


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + "Z",
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        out.update(getattr(record, "ctx", {}))
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)


def setup(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler._hotload = True  # type: ignore[attr-defined]
    root = logging.getLogger()
    # Replace only the handler a previous call installed. Clobbering every root handler would also remove ones this
    # code did not add (a host application's, or a test harness's log capture).
    root.handlers[:] = [h for h in root.handlers if not getattr(h, "_hotload", False)] + [handler]
    root.setLevel(level)
    # httpx logs full request URLs at INFO. Telegram puts the bot token in the URL path, so never let it through.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def url_ref(url: str) -> str:
    """Stable short hash of a URL for correlating log lines without storing the URL itself."""
    return hashlib.sha256(url.encode()).hexdigest()[:12]


def log(logger: logging.Logger, msg: str, level: int = logging.INFO, **ctx) -> None:
    logger.log(level, msg, extra={"ctx": ctx})
