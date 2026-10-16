"""Pure validation of a completed indexed item; no business DB access."""

import json
from pathlib import Path


def _header(path, size):
    with path.open("rb") as stream:
        return stream.read(size)


def reusable_aweme_is_complete(
    raw_item_path: str, content_key: str, *, outputs_root=None
) -> bool:
    """Validate the indexed payload and every media file it promises."""
    try:
        path = Path(str(raw_item_path))
        if outputs_root is not None:
            root = Path(outputs_root).resolve(strict=True)
            path = path.resolve(strict=True)
            if not path.is_relative_to(root) or not str(content_key).isdigit():
                return False
        if not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
            return False
        payload = json.loads(path.read_text(encoding="utf-8"))
        item = payload.get("item") if isinstance(payload, dict) else None
        if not isinstance(item, dict) or str(item.get("aweme_id") or "") != str(
            content_key
        ):
            return False
        if not str(item.get("aweme_url") or "").strip():
            return False
        if path.parent.name != "raw_items":
            return False
        output_root = path.parent.parent
        note_urls = item.get("note_download_url") or ""
        if isinstance(note_urls, str):
            note_urls = [value for value in note_urls.split(",") if value.strip()]
        elif isinstance(note_urls, list):
            note_urls = [value for value in note_urls if str(value).strip()]
        else:
            return False
        if note_urls:
            image_root = (
                output_root / "crawler" / "douyin" / "images" / str(content_key)
            )
            for index in range(len(note_urls)):
                media = image_root / f"{index:03d}.jpeg"
                if outputs_root is not None and not media.resolve().is_relative_to(
                    root
                ):
                    return False
                if not media.is_file() or media.stat().st_size == 0:
                    return False
                if _header(media, 2) != b"\xff\xd8":
                    return False
            return True
        if not str(item.get("video_download_url") or "").strip():
            return False
        media = (
            output_root
            / "crawler"
            / "douyin"
            / "videos"
            / str(content_key)
            / "video.mp4"
        )
        if outputs_root is not None and not media.resolve().is_relative_to(root):
            return False
        if not media.is_file() or media.stat().st_size == 0:
            return False
        return b"ftyp" in _header(media, 32)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return False
