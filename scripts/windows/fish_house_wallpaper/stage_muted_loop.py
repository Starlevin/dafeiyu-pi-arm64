#!/usr/bin/env python3
"""Stage a video-only standby variant for the muted fish-house player.

Run on the Windows build machine with FFmpeg 7+. The input theme and assets
remain intact. The output contains the updated theme and only the new asset.
"""

import argparse
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile


def decoded_frames(ffmpeg: str, path: Path) -> tuple[Fraction, list[tuple[str, ...]]]:
    result = subprocess.check_output([
        ffmpeg, "-v", "error", "-nostdin", "-i", str(path), "-map", "0:v:0",
        "-fps_mode", "passthrough", "-enc_time_base", "demux", "-f", "framemd5", "-",
    ], encoding="ascii", timeout=90)
    timebase = next(line.split(": ", 1)[1] for line in result.splitlines()
                    if line.startswith("#tb 0: "))
    frames = [tuple(field.strip() for field in line.split(","))
              for line in result.splitlines() if line and not line.startswith("#")]
    if not frames:
        raise ValueError("The video contains no decodable frames")
    return Fraction(timebase), frames


def stage_loop(ffmpeg: str, theme_path: Path, node_id: str, output: Path) -> dict:
    if output.exists():
        raise ValueError("Choose a new output directory")
    theme = json.loads(theme_path.read_text(encoding="utf-8"))
    nodes = theme["config"]["flow"]["nodes"]
    matching = [node for node in nodes if node["id"] == node_id]
    if (len(matching) != 1 or matching[0].get("loop") is not True
            or matching[0].get("role") != "wait"):
        raise ValueError("Select a single looping standby node")
    standby = matching[0]
    original_video = standby["video"]
    if not re.fullmatch(r"videos/[A-Za-z0-9_-][A-Za-z0-9_.-]*\.mp4", original_video):
        raise ValueError("The standby video must be under the theme's videos directory")
    media_id = standby["media_id"]
    library = [asset for asset in theme["config"]["library"]
               if asset["id"] == media_id or asset["video"] == original_video]
    if (len(library) != 1 or library[0]["id"] != media_id or library[0]["video"] != original_video
            or sum(node.get("media_id") == media_id or node.get("video") == original_video
                   for node in nodes) != 1):
        raise ValueError("The selected standby asset must belong only to the selected node")
    source = theme_path.parent / original_video
    if not source.is_file() or source.resolve().parent != (theme_path.parent.resolve() / "videos"):
        raise ValueError("The original video path is missing or resolves outside the theme")
    with tempfile.TemporaryDirectory(prefix="fish-house-loop-", dir=output.parent) as temp:
        staged = Path(temp) / "theme"
        (staged / "videos").mkdir(parents=True)
        candidate = staged / "videos" / "standby.mp4"
        subprocess.run([
            ffmpeg, "-v", "error", "-nostdin", "-n", "-i", str(source), "-map", "0:v:0",
            "-c:v", "copy", "-an", "-movflags", "+faststart", str(candidate),
        ], check=True, timeout=90)
        original_frames = decoded_frames(ffmpeg, source)
        timebase, frames = decoded_frames(ffmpeg, candidate)
        if original_frames != (timebase, frames) or int(frames[0][2]) != 0:
            raise ValueError("The candidate changed decoded frames or their timing")
        duration = max(int(frame[2]) + int(frame[3]) for frame in frames) * timebase
        if duration <= 0:
            raise ValueError("The candidate duration must be positive")
        with candidate.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        new_video = f"videos/{source.stem}-silent-{digest[:12]}.mp4"
        candidate.rename(staged / new_video)
        for entry in (standby, library[0]):
            entry.update(video=new_video, duration=float(duration))
        (staged / "theme.json").write_text(
            json.dumps(theme, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
        staged.rename(output)
    return {"node": node_id, "video": new_video, "duration": float(duration),
            "framesMatched": len(frames), "sha256": digest}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("theme", type=Path)
    parser.add_argument("node")
    parser.add_argument("output", type=Path)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    args = parser.parse_args()
    print(json.dumps(stage_loop(args.ffmpeg, args.theme, args.node, args.output)))


if __name__ == "__main__":
    main()
