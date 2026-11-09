
"""Stage 8 — publish videos through Buffer or existing providers."""
from __future__ import annotations

import time
from pathlib import Path

from .config import env
from .utils import log


def publish(cfg: dict, video: Path, meta: dict) -> dict:
    provider = cfg["publish"]["provider"]
    platforms = cfg["publish"]["platforms"]
    caption = _caption(meta)

    if cfg["publish"].get("queue"):
        log("publish: QUEUED for review (not posted)", "warn")
        return {"status": "queued", "platforms": platforms}

    if provider == "dry_run":
        log(f"publish: [DRY-RUN] would post to {', '.join(platforms)}", "warn")
        log(f"title: {meta.get('title')}", "info")
        return {"status": "dry_run", "platforms": platforms, "caption": caption}

    if provider == "buffer":
        return _buffer(video, meta, caption)

    if provider == "ayrshare":
        return _ayrshare(cfg, video, meta, caption, platforms)

    if provider == "upload_post":
        return _upload_post(cfg, video, meta, caption, platforms)

    raise ValueError(f"Unknown publish.provider: {provider}")


def _caption(meta: dict) -> str:
    tags = " ".join(meta.get("hashtags", []))
    return f"{meta.get('description', meta.get('title', ''))}\n\n{tags}".strip()


def _buffer(video: Path, meta: dict, caption: str) -> dict:
    import requests

    cloud = env("CLOUDINARY_CLOUD_NAME")
    api_key = env("CLOUDINARY_API_KEY")
    api_secret = env("CLOUDINARY_API_SECRET")
    preset = env("CLOUDINARY_UPLOAD_PRESET")
    buffer_key = env("BUFFER_API_KEY")
    channel_id = env("BUFFER_CHANNEL_ID")

    required = {
        "CLOUDINARY_CLOUD_NAME": cloud,
        "CLOUDINARY_API_KEY": api_key,
        "CLOUDINARY_API_SECRET": api_secret,
        "CLOUDINARY_UPLOAD_PRESET": preset,
        "BUFFER_API_KEY": buffer_key,
        "BUFFER_CHANNEL_ID": channel_id,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise SystemExit("Missing environment variables: " + ", ".join(missing))

    # 1. Upload the generated MP4 to Cloudinary.
    upload_url = f"https://api.cloudinary.com/v1_1/{cloud}/video/upload"

    with video.open("rb") as file_handle:
        upload = requests.post(
            upload_url,
            auth=(api_key, api_secret),
            data={"upload_preset": preset},
            files={"file": (video.name, file_handle, "video/mp4")},
            timeout=600,
        )

    upload.raise_for_status()
    media_url = upload.json().get("secure_url")
    if not media_url:
        raise RuntimeError("Cloudinary did not return a secure video URL")

    log("publish: video uploaded to Cloudinary", "ok")

    # 2. Create a YouTube post in Buffer's automatic queue.
    title = str(meta.get("title") or "Built Different")[:100]

    query = """
    mutation CreatePost($input: CreatePostInput!) {
      createPost(input: $input) {
        ... on PostActionSuccess {
          post {
            id
            text
            dueAt
          }
        }
        ... on MutationError {
          message
        }
      }
    }
    """

    payload = {
        "query": query,
        "variables": {
            "input": {
                "channelId": channel_id,
                "text": caption,
                "schedulingType": "automatic",
                "mode": "addToQueue",
                "assets": [
                    {"video": {"url": media_url}}
                ],
                "metadata": {
                    "youtube": {
                        "title": title,
                        "categoryId": "22",
                        "privacy": "public",
                        "madeForKids": False
                    }
                }
            }
        }
    }

    response = requests.post(
        "https://api.buffer.com",
        headers={
            "Authorization": f"Bearer {buffer_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=120,
    )
    response.raise_for_status()

    result = response.json()

    if result.get("errors"):
        raise RuntimeError(f"Buffer GraphQL error: {result['errors']}")

    action = result.get("data", {}).get("createPost", {})
    if action.get("message"):
        raise RuntimeError(f"Buffer rejected the post: {action['message']}")

    post = action.get("post")
    if not post:
        raise RuntimeError(f"Unexpected Buffer response: {result}")

    log(f"publish: Buffer queued YouTube post {post['id']}", "ok")

    return {
        "status": "queued",
        "provider": "buffer",
        "channel": "youtube",
        "post_id": post["id"],
        "due_at": post.get("dueAt"),
        "media_url": media_url,
    }


def _ayrshare(cfg, video, meta, caption, platforms) -> dict:
    import requests

    key = env("AYRSHARE_API_KEY")
    if not key:
        raise SystemExit("AYRSHARE_API_KEY missing in .env")

    with video.open("rb") as handle:
        up = requests.post(
            "https://api.ayrshare.com/api/media/upload",
            headers={"Authorization": f"Bearer {key}"},
            files={"file": handle},
            timeout=300,
        )
    up.raise_for_status()
    media_url = up.json().get("url") or up.json().get("mediaUrl")

    results = {}
    for i, platform in enumerate(platforms):
        if i:
            time.sleep(cfg["publish"].get("stagger_seconds", 0))
        payload = {
            "post": caption,
            "platforms": [platform],
            "mediaUrls": [media_url],
            "isVideo": True,
        }
        if platform == "youtube":
            payload["youTubeOptions"] = {
                "title": meta["title"][:100],
                "shorts": True,
            }
        if cfg["compliance"].get("ai_disclosure"):
            payload.setdefault("tikTokOptions", {})["aiGenerated"] = True

        r = requests.post(
            "https://api.ayrshare.com/api/post",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=120,
        )
        ok = r.status_code < 300
        results[platform] = "posted" if ok else f"error {r.status_code}: {r.text[:200]}"
        log(f"publish→{platform}: {results[platform]}", "ok" if ok else "err")

    return {"status": "posted", "results": results}


def _upload_post(cfg, video, meta, caption, platforms) -> dict:
    import requests

    key = env("UPLOAD_POST_API_KEY")
    user = env("UPLOAD_POST_USER")
    if not key:
        raise SystemExit("UPLOAD_POST_API_KEY missing in .env")

    with video.open("rb") as handle:
        r = requests.post(
            "https://api.upload-post.com/api/upload",
            headers={"Authorization": f"Apikey {key}"},
            data={
                "user": user,
                "title": meta["title"][:100],
                "caption": caption,
                "platform[]": platforms,
            },
            files={"video": handle},
            timeout=300,
        )

    ok = r.status_code < 300
    log(
        f"publish: upload-post {'ok' if ok else 'error'} ({r.status_code})",
        "ok" if ok else "err",
    )
    return {
        "status": "posted" if ok else "error",
        "response": r.text[:300],
    }
