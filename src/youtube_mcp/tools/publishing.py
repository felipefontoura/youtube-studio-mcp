"""Video publishing tools — upload, update metadata, thumbnails, delete."""

import os
import shutil
import tempfile
import urllib.error
import urllib.request
from contextlib import contextmanager
from urllib.parse import urlparse

from googleapiclient.http import MediaFileUpload

from youtube_mcp.server import auth, mcp, quota

_DOWNLOAD_CHUNK = 1024 * 1024  # 1 MiB


def _is_url(path: str) -> bool:
    return path.startswith("http://") or path.startswith("https://")


@contextmanager
def _local_file(file_path: str):
    """Resolve `file_path` to a local, seekable path for MediaFileUpload.

    This server routinely runs on a different host than its caller (e.g. behind a
    remote MCP gateway), so a bare local path from the caller is often unreachable.
    `MediaFileUpload`/`MediaIoBaseUpload` need a seekable local file to do a
    resumable upload (Google's client library calls `.seek()` to retry chunks), so
    an http(s) `file_path` can't be streamed straight from the response body —
    it's downloaded to a temp file first instead. The temp file is removed on exit
    either way.
    """
    if not _is_url(file_path):
        if not os.path.exists(file_path):
            raise FileNotFoundError(file_path)
        yield file_path
        return

    suffix = os.path.splitext(urlparse(file_path).path)[1]
    fd, tmp_path = tempfile.mkstemp(suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as tmp_file:
            with urllib.request.urlopen(file_path) as response:
                shutil.copyfileobj(response, tmp_file, _DOWNLOAD_CHUNK)
        yield tmp_path
    finally:
        os.unlink(tmp_path)


@mcp.tool()
def youtube_upload_video(
    file_path: str,
    title: str,
    description: str = "",
    tags: list[str] | None = None,
    category_id: str = "22",
    privacy_status: str = "private",
    publish_at: str | None = None,
) -> dict:
    """Upload a video to YouTube.

    Costs 1,600 quota units. Video is uploaded as private by default.

    Args:
        file_path: Absolute path to the video file, or an http(s) URL to it
        title: Video title (max 100 characters)
        description: Video description (max 5,000 characters)
        tags: List of tags
        category_id: YouTube category ID (default "22" = People & Blogs)
        privacy_status: "private", "public", or "unlisted"
        publish_at: ISO 8601 datetime to schedule publishing (requires privacy_status="private")
    """
    try:
        with _local_file(file_path) as local_path:
            quota.consume("video_insert")
            youtube = auth.build_youtube_service()

            body = {
                "snippet": {
                    "title": title[:100],
                    "description": description[:5000],
                    "tags": tags or [],
                    "categoryId": category_id,
                },
                "status": {
                    "privacyStatus": privacy_status,
                    "selfDeclaredMadeForKids": False,
                },
            }

            if publish_at and privacy_status == "private":
                body["status"]["publishAt"] = publish_at

            media = MediaFileUpload(local_path, resumable=True)

            request = youtube.videos().insert(
                part="snippet,status",
                body=body,
                media_body=media,
            )

            response = request.execute()
    except FileNotFoundError:
        return {"error": f"File not found: {file_path}"}
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        return {"error": f"Could not download video from {file_path}: {e}"}

    return {
        "id": response["id"],
        "title": response["snippet"]["title"],
        "privacy": response["status"]["privacyStatus"],
        "publish_at": response["status"].get("publishAt"),
        "url": f"https://www.youtube.com/watch?v={response['id']}",
        "quota_cost": 1600,
    }


@mcp.tool()
def youtube_update_video(
    video_id: str,
    title: str | None = None,
    description: str | None = None,
    tags: list[str] | None = None,
    category_id: str | None = None,
    privacy_status: str | None = None,
) -> dict:
    """Update metadata for an existing video.

    Only provided fields are updated; others remain unchanged.

    Args:
        video_id: YouTube video ID
        title: New title (max 100 characters)
        description: New description (max 5,000 characters)
        tags: New tags (replaces existing tags)
        category_id: New category ID
        privacy_status: "private", "public", or "unlisted"
    """
    quota.consume("list")
    youtube = auth.build_youtube_service()

    # Fetch current video data first
    current = youtube.videos().list(part="snippet,status", id=video_id).execute()
    items = current.get("items", [])
    if not items:
        return {"error": f"Video not found: {video_id}"}

    video = items[0]
    snippet = video["snippet"]
    status = video["status"]

    # Update only provided fields
    if title is not None:
        snippet["title"] = title[:100]
    if description is not None:
        snippet["description"] = description[:5000]
    if tags is not None:
        snippet["tags"] = tags
    if category_id is not None:
        snippet["categoryId"] = category_id

    body = {"id": video_id, "snippet": snippet}

    if privacy_status is not None:
        body["status"] = {"privacyStatus": privacy_status}
        parts = "snippet,status"
    else:
        parts = "snippet"

    quota.consume("update")
    response = youtube.videos().update(part=parts, body=body).execute()

    # The API only returns the parts we asked for: without privacy_status the
    # response has no "status", so fall back to the value fetched above.
    privacy = response.get("status", status).get("privacyStatus")

    return {
        "id": response["id"],
        "title": response["snippet"]["title"],
        "privacy": privacy,
        "updated": True,
    }


@mcp.tool()
def youtube_set_thumbnail(video_id: str, file_path: str) -> dict:
    """Upload a custom thumbnail for a video.

    Args:
        video_id: YouTube video ID
        file_path: Absolute path to the thumbnail image (JPEG, PNG, GIF, BMP; max 2MB), or an
            http(s) URL to it
    """
    try:
        with _local_file(file_path) as local_path:
            quota.consume("thumbnail_set")
            youtube = auth.build_youtube_service()

            media = MediaFileUpload(local_path)
            response = youtube.thumbnails().set(videoId=video_id, media_body=media).execute()
    except FileNotFoundError:
        return {"error": f"File not found: {file_path}"}
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        return {"error": f"Could not download thumbnail from {file_path}: {e}"}

    items = response.get("items", [])
    if items:
        return {
            "video_id": video_id,
            "thumbnail_url": items[0].get("default", {}).get("url"),
            "updated": True,
        }

    return {"video_id": video_id, "updated": True}


@mcp.tool()
def youtube_delete_video(video_id: str) -> dict:
    """Delete a video. This action is irreversible.

    Args:
        video_id: YouTube video ID to delete
    """
    quota.consume("delete")
    youtube = auth.build_youtube_service()

    youtube.videos().delete(id=video_id).execute()

    return {"video_id": video_id, "deleted": True}
