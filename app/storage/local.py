"""Finished files on disk: `data/files/{key}.{ext}` + `{key}.jpg` + `{key}.json` sidecar.

The sidecars are the dedupe index, so cached files survive restarts.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("harmoniq.storage")

AUDIO_EXTS = ("m4a", "opus", "mp3")


@dataclass
class StoredFile:
    key: str
    audio: Path
    artwork: Path | None
    meta: dict  # the sidecar contents

    @property
    def size(self) -> int:
        return self.meta.get("size_bytes", 0)


class LocalFileStore:
    def __init__(self, data_dir: Path) -> None:
        self.files_dir = data_dir / "files"
        self.tmp_dir = data_dir / "tmp"
        self.files_dir.mkdir(parents=True, exist_ok=True)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    # -- lookups -------------------------------------------------------------------------------
    def _sidecar(self, key: str) -> Path:
        return self.files_dir / f"{key}.json"

    def get(self, key: str) -> StoredFile | None:
        sidecar = self._sidecar(key)
        try:
            meta = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        audio = self.files_dir / f"{key}.{meta.get('ext')}"
        if not audio.is_file():
            return None
        art = self.files_dir / f"{key}.jpg"
        return StoredFile(key, audio, art if art.is_file() else None, meta)

    def touch(self, key: str) -> None:
        with self._lock:
            stored = self.get(key)
            if stored:
                stored.meta["last_access"] = time.time()
                self._write_json(self._sidecar(key), stored.meta)

    # -- writes --------------------------------------------------------------------------------
    def new_tmp(self, name: str) -> Path:
        path = self.tmp_dir / name
        shutil.rmtree(path, ignore_errors=True)
        path.mkdir(parents=True)
        return path

    def publish(self, key: str, audio_src: Path, ext: str, artwork: bytes | None, meta: dict) -> StoredFile:
        with self._lock:
            audio_dst = self.files_dir / f"{key}.{ext}"
            os.replace(audio_src, audio_dst)
            if artwork:
                art_tmp = self.files_dir / f"{key}.jpg.tmp"
                art_tmp.write_bytes(artwork)
                os.replace(art_tmp, self.files_dir / f"{key}.jpg")
            now = time.time()
            meta = {**meta, "ext": ext, "created_at": now, "last_access": now}
            self._write_json(self._sidecar(key), meta)
        stored = self.get(key)
        assert stored is not None
        return stored

    def delete(self, key: str) -> None:
        with self._lock:
            for path in self.files_dir.glob(f"{key}.*"):
                path.unlink(missing_ok=True)

    @staticmethod
    def _write_json(path: Path, data: dict) -> None:
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)

    # -- maintenance ---------------------------------------------------------------------------
    def usage(self) -> int:
        return sum(p.stat().st_size for p in self.files_dir.iterdir() if p.is_file())

    def free_bytes(self) -> int:
        return shutil.disk_usage(self.files_dir).free

    def sweep(self, ttl_seconds: int, max_bytes: int, keep: set[str] = frozenset()) -> int:
        """Delete expired/orphaned files, then the least recently used ones above `max_bytes`."""
        with self._lock:
            return self._sweep(ttl_seconds, max_bytes, keep)

    def _sweep(self, ttl_seconds: int, max_bytes: int, keep: set[str]) -> int:
        now = time.time()
        removed = 0
        entries: list[tuple[float, str, int]] = []
        keys = {p.name.split(".", 1)[0] for p in self.files_dir.iterdir() if p.is_file()}
        for key in keys:
            stored = self.get(key)
            if stored is None:
                self.delete(key)  # orphaned pieces or broken sidecar
                removed += 1
                continue
            last = stored.meta.get("last_access", 0)
            if now - last > ttl_seconds and key not in keep:
                self.delete(key)
                removed += 1
                continue
            size = sum(p.stat().st_size for p in self.files_dir.glob(f"{key}.*"))
            entries.append((last, key, size))
        total = sum(e[2] for e in entries)
        for last, key, size in sorted(entries):
            if total <= max_bytes:
                break
            if key in keep:
                continue
            self.delete(key)
            total -= size
            removed += 1
        if removed:
            log.info("Sweeper removed %d cached file(s)", removed)
        return removed

    def clean_tmp(self, older_than: float, keep: set[str] = frozenset()) -> None:
        now = time.time()
        for path in self.tmp_dir.iterdir():
            if path.name in keep:
                continue
            try:
                if now - path.stat().st_mtime > older_than:
                    shutil.rmtree(path, ignore_errors=True) if path.is_dir() else path.unlink(missing_ok=True)
            except OSError:
                pass
