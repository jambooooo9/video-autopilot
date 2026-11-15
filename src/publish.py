
"""Stage 8 — publish the same video to multiple platforms through Buffer."""
from __future__ import annotations

import time
from pathlib import Path

from .config import env
from .utils import log


BUFFER_URL = "https://api.buffer.com"


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
        return _buffer(video, meta, caption, platforms, cfg)

    if provider == "ayrshare":
        return _ayrshare(cfg, video, meta, caption, platforms)

    if provider == "upload_post":
        return _upload_post(cfg, video, meta, caption, platforms)

    raise ValueError(f"Unknown publish.provider: {provider}")


def _caption(meta: dict) -> str:
    tags = " ".join(meta.get("hashtags", []))
    return f"{meta.get('description', meta.get('title', ''))}\n\n{tags}".strip()


def _buffer_request(buffer_key: str, query: str, variables: dict | None = None) -> dict:
    import requests

    response = requests.post(
        BUFFER_URL,
        headers={
            "Authorization": f"Bearer {buffer_key}",
            "Content-Type": "application/json",
        },
        json={"query": query, "variables": variables or {}},
        timeout=120,
    )

    response.raise_for_status()
    result = response.json()

    if result.get("errors"):
        raise RuntimeError(f"Buffer GraphQL error: {result['errors']}")

    return result.get("data", {})


def _buffer_channels(buffer_key: str) -> dict:
    """Find the three connected channels in the same Buffer organization."""

    account_query = """
    query {
      account {
        organizations {
          id
          name
        }
      }
    }
    """

    account = _buffer_request(buffer_key, account_query)
    organizations = account.get("account", {}).get("organizations", [])

    if not organizations:
        raise RuntimeError("No Buffer organization was found.")

    channels_query = """
    query GetChannels($organizationId: OrganizationId!) {
      channels(input: { organizationId: $organizationId }) {
        id
        name
        service
        isDisconnected
        isLocked
      }
    }
    """

    required = {"youtube", "tiktok", "instagram"}
    matching_organizations = []

    for organization in organizations:
        data = _buffer_request(
            buffer_key,
            channels_query,
            {"organizationId": organization["id"]},
        )

        channels = data.get("channels", [])
        available = {}

        for channel in channels:
            service = str(channel.get("service", "")).lower()

            if (
                service in required
                and not channel.get("isDisconnected")
                and not channel.get("isLocked")
            ):
                available[service] = channel

        if required.issubset(available):
            matching_organizations.append((organization, available))

    if len(matching_organizations) == 0:
        raise RuntimeError(
            "Could not find YouTube, TikTok and Instagram together "
            "in one active Buffer organization. Check your connected channels."
        )

    if len(matching_organizations) > 1:
        raise RuntimeError(
            "More than one Buffer organization has all three channels. "
            "Please configure one organization before publishing."
        )

    organization, channels = matching_organizations[0]

    log(f"Buffer organization: {organization['name']}", "ok")

    return channels


def _buffer(video: Path, meta: dict, caption: str,
            platforms: list, cfg: dict) -> dict:
    import requests

    cloud = env("CLOUDINARY_CLOUD_NAME")
    api_key = env("CLOUDINARY_API_KEY")
    api_secret = env("CLOUDINARY_API_SECRET")
    preset = env("CLOUDINARY_UPLOAD_PRESET")
    buffer_key = env("BUFFER_API_KEY")

    required = {
        "CLOUDINARY_CLOUD_NAME": cloud,
        "CLOUDINARY_API_KEY": api_key,
        "CLOUDINARY_API_SECRET": api_secret,
        "CLOUDINARY_UPLOAD_PRESET": preset,
        "BUFFER_API_KEY": buffer_key,
    }

    missing = [name for name, value in required.items() if not value]

    if missing:
        raise SystemExit(
            "Missing environment variables: " + ", ".join(missing)
        )

    # 1. Upload the video ONCE to Cloudinary.
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
        raise RuntimeError("Cloudinary did not return a secure video URL.")

    log("Video uploaded to Cloudinary once.", "ok")

    # 2. Find the connected Buffer channels.
    channels = _buffer_channels(buffer_key)

    # 3. Prepare the same video asset for every platform.
    title = str(meta.get("title") or "Built Different")[:100]

    video_asset = [
        {"video": {"url": media_url}}
    ]

    # Only publish to the requested three platforms.
    requested = {str(p).lower() for p in platforms}
    targets = ["youtube", "tiktok", "instagram"]

    targets = [p for p in targets if p in requested]

    if set(targets) != {"youtube", "tiktok", "instagram"}:
        raise RuntimeError(
            "Publishing is configured to require youtube, tiktok and "
            "instagram. Update config/settings.yaml first."
        )

    mutation = """
    mutation CreatePost($input: CreatePostInput!) {
      createPost(input: $input) {
        ... on PostActionSuccess {
          post {
            id
            text
            dueAt
            status
          }
        }
        ... on MutationError {
          message
        }
      }
    }
    """

    results = {}
    failures = {}

    # 4. Create one post per platform using the SAME video URL.
    for index, platform in enumerate(targets):

        channel = channels[platform]

        metadata = {}

        if platform == "youtube":
            metadata["youtube"] = {
                "title": title,
                "categoryId": "22",
                "privacy": "public",
                "madeForKids": False,
                "isAiGenerated": bool(
                    cfg.get("compliance", {}).get("ai_disclosure", False)
        ),
    }            

        elif platform == "tiktok":
            metadata["tiktok"] = {
                "isAiGenerated": bool(
                    cfg.get("compliance", {}).get("ai_disclosure", False)
                )
            }

        elif platform == "instagram":
            metadata["instagram"] = {
                "type": "reel",
                "shouldShareToFeed": True,
                "isAiGenerated": bool(
                    cfg.get("compliance", {}).get("ai_disclosure", False)
                ),
            }

        post_input = {
            "channelId": channel["id"],
            "saveToDraft": False,
            "text": caption,
            "schedulingType": "automatic",
            "mode": "shareNow",
            "assets": video_asset,
            "metadata": metadata,
            "aiAssisted": True,
        }

        try:
            data = _buffer_request(
                buffer_key,
                mutation,
                {"input": post_input},
            )

            action = data.get("createPost", {})

            if action.get("message"):
                raise RuntimeError(action["message"])

            post = action.get("post")

            if not post:
                raise RuntimeError("Buffer returned no post confirmation.")

            results[platform] = {
                "post_id": post["id"],
                "status": post.get("status"),
                "due_at": post.get("dueAt"),
            }

            log(
                f"Buffer {platform}: post submitted ({post['id']})",
                "ok",
            )

        except Exception as error:
            failures[platform] = str(error)
            log(f"Buffer {platform}: FAILED — {error}", "err")

        if index < len(targets) - 1:
            time.sleep(cfg["publish"].get("stagger_seconds", 5))

    # 5. Report the result without hiding partial failures.
    if failures:
        raise RuntimeError(
            "Some Buffer posts failed. Successful posts: "
            f"{list(results.keys())}. Failed posts: {failures}. "
            "Check Buffer before retrying to avoid duplicate posts."
        )

    log("Same video submitted successfully to all three platforms.", "ok")

    return {
        "status": "submitted",
        "provider": "buffer",
        "results": results,
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

        log(
            f"publish→{platform}: {results[platform]}",
            "ok" if ok else "err",
        )

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
