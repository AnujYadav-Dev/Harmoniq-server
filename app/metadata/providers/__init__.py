"""Metadata providers. Each one fills in the fields it knows; see app/metadata/resolver.py for the order."""

from __future__ import annotations

from typing import Protocol

from app.metadata.model import MetadataContext, TrackMetadata


class MetadataProvider(Protocol):
    name: str
    needs_enrich: bool  # True for optional third-party enrichment (skipped when a job sets enrich=false)

    def enrich(self, ctx: MetadataContext, current: TrackMetadata) -> TrackMetadata | None: ...
