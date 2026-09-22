"""What Flower remembers between runs: where files go, and how the link is made.

Two things only — the directory the user last chose, and how to connect (proxy,
stream count). A download's own state does not belong here: it lives next to the
file being fetched, as the part plus the note beside it, so a run that dies takes
nothing with it. Settings are about how Flower behaves, not about what is half
downloaded.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path

APP_NAME = "Flower"
DEFAULT_PROXY_HOST = "127.0.0.1"  # the proxy is always local: only the port moves
DEFAULT_PROXY_PORT = "7897"  # Clash Verge's mixed port on this box
DEFAULT_STREAMS = 4
MIN_STREAMS = 1
MAX_STREAMS = 16


def settings_path() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / APP_NAME / "settings.json"


def default_save_dir() -> Path:
    return Path.home() / "Downloads"


@dataclass
class Settings:
    """The four choices the interface exposes, as they were left last time."""

    save_dir: str = ""
    use_proxy: bool = False
    proxy_host: str = DEFAULT_PROXY_HOST
    proxy_port: str = DEFAULT_PROXY_PORT
    streams: int = DEFAULT_STREAMS

    @classmethod
    def load(cls) -> Settings:
        """The stored settings, or the defaults: a broken file is not a failure.

        A hand-edited file can hold anything, so unknown keys are dropped and the
        stream count is pulled back into range rather than trusted.
        """
        try:
            raw = json.loads(settings_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        if not isinstance(raw, dict):
            return cls()
        known = {field.name for field in fields(cls)}
        settings = cls(**{key: value for key, value in raw.items() if key in known})
        settings.streams = clamp_streams(settings.streams)
        return settings

    def save(self) -> None:
        path = settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")

    def resolved_save_dir(self) -> Path:
        chosen = self.save_dir.strip()
        return Path(chosen) if chosen else default_save_dir()

    def proxy_url(self) -> str | None:
        """The proxy to use, or None for a direct connection.

        Never a fallback: a proxy the user turned on either works or the download
        fails loudly, because quietly going direct would look like the proxy was
        helping.
        """
        if not self.use_proxy:
            return None
        host = self.proxy_host.strip() or DEFAULT_PROXY_HOST
        port = self.proxy_port.strip() or DEFAULT_PROXY_PORT
        return f"http://{host}:{port}"


def clamp_streams(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        return DEFAULT_STREAMS
    return max(MIN_STREAMS, min(MAX_STREAMS, value))
