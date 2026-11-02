"""Deterministic rights policy. It overrides any model decision (Jev included)."""

from __future__ import annotations

MEDIA_RIGHTS = ("metadata_only", "owned", "licensed", "permission_granted")
MEDIA_PROCESSING_RIGHTS = frozenset({"owned", "licensed", "permission_granted"})
# A short excerpt of public text (caption opening, on-screen hook) for
# classification. Longer third-party text is never stored.
MAX_PUBLIC_EXCERPT_CHARS = 300


class RightsError(PermissionError):
    pass


def can_process_media(media_rights: str) -> bool:
    """Download, transcription, cut detection: only for rights-cleared media."""
    return media_rights in MEDIA_PROCESSING_RIGHTS


def can_store_full_transcript(media_rights: str) -> bool:
    return media_rights in MEDIA_PROCESSING_RIGHTS


def require_media_rights(media_rights: str, rights_basis: str | None) -> None:
    if not can_process_media(media_rights):
        raise RightsError(
            f"media_rights={media_rights!r}: this source is metadata-only, so it cannot be "
            "downloaded, transcribed or cut-analysed. Register media you own or license instead."
        )
    if not (rights_basis or "").strip():
        raise RightsError("A rights_basis (who owns it / licence reference) is required for media analysis.")


def validate_rights_fields(media_rights: str, rights_basis: str | None, license_reference: str | None) -> None:
    if media_rights not in MEDIA_RIGHTS:
        raise RightsError(f"media_rights must be one of {', '.join(MEDIA_RIGHTS)}")
    if media_rights == "metadata_only":
        return
    if not (rights_basis or "").strip():
        raise RightsError(f"media_rights={media_rights!r} requires rights_basis")
    if media_rights in {"licensed", "permission_granted"} and not (license_reference or "").strip():
        raise RightsError(f"media_rights={media_rights!r} requires license_reference")
