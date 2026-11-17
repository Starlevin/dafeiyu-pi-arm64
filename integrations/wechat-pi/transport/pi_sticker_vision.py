#!/usr/bin/env python3
"""Bounded screenshots for static or animated WeChat sticker understanding.

No login, DB, memory, file-cache access. A dynamic image is summarized as a
3-frame contact sheet by the existing /api/vision model, not decoded from the
encrypted WeChat files.
"""
from io import BytesIO
import time

def capture_sticker(crop_once, *, animated=False, sleep=time.sleep):
    first=crop_once()
    if not first or not animated:
        return first
    frames=[first]
    for _ in range(2):
        sleep(0.16)
        shot=crop_once()
        if shot: frames.append(shot)
    if len(frames)<2:
        return first
    try:
        from PIL import Image, ImageOps
        images=[]
        for data in frames:
            with Image.open(BytesIO(data)) as im:
                if im.format!="PNG" or im.width>750 or im.height>800:
                    return first
                images.append(im.convert("RGB"))
        if len(set(frames))==1:
            return first
        tile_width=min(430,max(im.width for im in images))
        tile_height=min(650,max(im.height for im in images))
        output=Image.new("RGB",(tile_width*len(images),tile_height),(250,250,250))
        for idx,im in enumerate(images):
            im.thumbnail((tile_width,tile_height),Image.Resampling.LANCZOS)
            output.paste(im,(idx*tile_width+(tile_width-im.width)//2,(tile_height-im.height)//2))
        buff=BytesIO()
        output.save(buff,format="PNG",optimize=True)
        result=buff.getvalue()
        return result if len(result)<=3*1024*1024 else first
    except Exception:
        return first

DYNAMIC_VISION_PROMPT=(
  "以下为微信动态表情包在短时间内的若干帧拼图，从左到右按时间排列。"
  "请简洁描述人物或动物的动作、表情和表达的情绪；"
  "如果无法确定运动方向，请说明不确定。不要把多帧误认成多个人。"
)
