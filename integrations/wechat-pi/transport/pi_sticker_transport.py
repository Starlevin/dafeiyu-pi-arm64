#!/usr/bin/env python3
"""Fail-closed Linux WeChat sticker bridge for the existing Whale Girl core.

Not a WeChat-native sticker API. Sends a verified local gallery image through
the already-tested image attachment path, to the locally configured sticker group only.
No arbitrary URLs, no database access, no login handling and no retries.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
from runtime_paths import configured_group
import re
import shutil
import stat
import tempfile
import time
import urllib.request
import urllib.error

APPROVED_GROUP = configured_group("STICKER_GROUP")
MAX_BYTES = 4 * 1024 * 1024
MAX_DIMENSION = 4096
IMAGE_TYPES = {".png": b"\x89PNG\r\n\x1a\n", ".jpg": b"\xff\xd8\xff",
               ".jpeg": b"\xff\xd8\xff", ".gif": b"GIF8"}
UUID_RE = re.compile(r"^[0-9a-fA-F-]{36}$")
CORE = "http://127.0.0.1:19301"

class StickerError(Exception):
    pass

def validate_image(path, library, *, limit=MAX_BYTES):
    """Only an existing ordinary image inside the configured gallery is usable."""
    root = Path(library).absolute()
    file = Path(path)
    if not file.is_absolute() or file.suffix.lower() not in IMAGE_TYPES:
        raise StickerError("invalid_file_path")
    if file.is_symlink():
        raise StickerError("unsafe_symlink")
    # Both root and intermediate components must be real directories.
    for parent in (root, *list(file.parents)[:-1]):
        if not parent.exists():
            continue
        if parent.is_symlink():
            raise StickerError("unsafe_symlink")
    try:
        normalized = file.resolve(strict=True)
        base = root.resolve(strict=True)
        if not normalized.is_relative_to(base) or normalized == base:
            raise StickerError("outside_gallery")
        cur = normalized.parent
        while cur != base:
            if cur.is_symlink():
                raise StickerError("unsafe_parent")
            cur = cur.parent
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(normalized, flags)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= limit:
                raise StickerError("unsafe_file_size")
            with os.fdopen(fd, "rb", closefd=False) as f:
                raw = f.read(limit + 1)
            if len(raw) != info.st_size or len(raw) > limit:
                raise StickerError("file_changed")
        finally:
            os.close(fd)
        suffix = normalized.suffix.lower()
        if not raw.startswith(IMAGE_TYPES[suffix]):
            raise StickerError("invalid_image_magic")
        if suffix == ".gif" and not raw.startswith((b"GIF87a", b"GIF89a")):
            raise StickerError("invalid_gif_header")
        # PIL is available on this Pi; verify image decoding and dimensions,
        # including the first and last frame for animated GIFs.
        try:
            from PIL import Image
            from io import BytesIO
            with Image.open(BytesIO(raw)) as im:
                if im.format not in ("PNG","JPEG","GIF"):
                    raise StickerError("unsupported_decoded_type")
                if im.width > MAX_DIMENSION or im.height > MAX_DIMENSION or im.width < 1 or im.height < 1:
                    raise StickerError("image_dimensions")
                if getattr(im,"n_frames",1)>120:
                    raise StickerError("excessive_gif_frames")
                im.load()
                if getattr(im,"n_frames",1)>1:
                    im.seek(im.n_frames-1); im.load()
        except StickerError:
            raise
        except Exception as exc:
            raise StickerError("image_decode_failed") from exc
        return raw, suffix, hashlib.sha256(raw).hexdigest()
    except StickerError:
        raise
    except (OSError,ValueError) as exc:
        raise StickerError("image_unavailable") from exc

def stage_approved_image(data, suffix, digest, staging_root):
    """Create a one-job self-validating approved-media directory, no shared manifest mutation."""
    directory = Path(staging_root)
    if directory.is_symlink():
        raise StickerError("unsafe_stage")
    directory.mkdir(mode=0o700,parents=True,exist_ok=True)
    tmp = tempfile.TemporaryDirectory(prefix="sticker-",dir=directory)
    base=Path(tmp.name)
    name="weixin-sticker"+suffix
    dest=base/name
    with dest.open("xb") as f:
        f.write(data)
    dest.chmod(0o600)
    manifest={"files":{name:{"kind":"image","sha256":digest}}}
    (base/"approved-media.json").write_text(json.dumps(manifest),encoding="utf8")
    (base/"approved-media.json").chmod(0o600)
    return tmp,{"kind":"image","name":name}

def core_request(path, token, *, body=None, timeout=4):
    if not isinstance(token,str) or not token or not path.startswith("/api/stickers/"):
        raise StickerError("bad_api_request")
    payload = None if body is None else json.dumps(body).encode("utf8")
    req=urllib.request.Request(CORE+path,data=payload,
        method="GET" if payload is None else "POST",
        headers={"x-bot-token":token,**({"content-type":"application/json"} if payload is not None else {})})
    # Bypass system proxy for local bridge.
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req,timeout=timeout) as response:
            obj=json.load(response)
            if not isinstance(obj,dict) or not obj.get("ok"):
                raise StickerError("core_rejected")
            return obj
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise StickerError("core_unavailable") from exc

class StickerCooldown:
    def __init__(self,clock=time.monotonic,min_gap=150):
        self.clock=clock
        self.gap=min_gap
        self.last=float("-inf")
    def permitted(self):
        return self.clock()-self.last>=self.gap
    def commit(self):
        self.last=self.clock()

def process_sticker(job, target, token, on_submit, media, bridge, xi, *,
                    library, stage_root, cooldown=None, requester=core_request):
    """One-shot candidate. Return (status, reserved).

    The result is not assumed confirmed unless the native UI exhibits an
    outgoing image. Claim is one-shot; all claimed tasks are acked terminal.
    """
    if target != APPROVED_GROUP or not isinstance(job,dict):
        return "not_authorized",False
    jobid=job.get("id")
    if not isinstance(jobid,str) or not UUID_RE.fullmatch(jobid):
        return "invalid_job",False
    if job.get("chat") != target:
        return "target_mismatch",False
    if cooldown is not None and not cooldown.permitted():
        return "cooldown",False
    # No UI operation until server verifies the reserved job.
    claimed=False
    sent=False
    result="not_claimed"
    try:
        response=requester("/api/stickers/claim",token,body={"id":jobid})
        item=response.get("item")
        # A mismatched claimed chat must be terminally acked as failed, not
        # left in sending status until timeout.
        claimed=isinstance(item,dict) and item.get("id")==jobid
        if not claimed or item.get("chat")!=target:
            raise StickerError("claimed_target_mismatch")
        data,suffix,digest=validate_image(item.get("path"),library)
        if cooldown is not None:
            cooldown.commit()
        temp,spec=stage_approved_image(data,suffix,digest,stage_root)
        try:
            def reserve():
                nonlocal sent
                on_submit()
                sent=True
            result=media.send_media(target,spec,temp.name,reserve,bridge,xi)
        finally:
            temp.cleanup()
    except StickerError as exc:
        result=str(exc)
    except Exception:
        result="sticker_exception"
    finally:
        if claimed:
            ack="confirmed" if result=="visible_outgoing" else "unconfirmed" if sent else "failed"
            try:
                requester("/api/stickers/ack",token,body={"id":jobid,"status":ack,"error":"" if ack=="confirmed" else result[:80]})
            except Exception:
                pass  # Never retry an uncertain send, including ack failure.
    return result,sent

def classify_placeholder(text):
    if not isinstance(text,str):return None
    body=text.strip()
    if body in ("[动画表情]","动画表情","[动态表情]","[GIF]","[gif]"):
        return "animated"
    if body in ("表情","[表情]","[表情包]","[贴图]","[Sticker]","Sticker",
                "图片","[图片]","Image","[Image]",""):
        return "static"
    return None
