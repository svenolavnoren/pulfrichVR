#!/usr/bin/env python3
"""
# sync010.py
# Copyright 2026 Sven-Olav Norén
# Licensed under the Apache License, Version 2.0
# SPDX-License-Identifier: Apache-2.0

Maud Sync Tool
--------------
Manual L/R sync helper for dual Insta360 sources (INSV or MOV/MP4 exports).

Main ideas:
- Pick left/right video.
- Build short preview frame stacks from BOTH lenses -> equirect.
- Step frames independently until sync looks right.
- Generate a clean ffmpeg .sh script for full render.
- Persist the tweakable values between sessions.
- Optionally generate a short test render script using -t.

Preview notes:
- Preview reflects FOV and yaw values from the UI.
- Preview uses both lens streams from each INSV.
- Preview exports numbered JPG frames once, then frame stepping is instant.

Render notes:
- Final script derives crop geometry from the detected input width/height.
- This allows 5760x2880 and 7680x3840 style source sizes without changing the app.
- Target resolution only affects the final output scaling, as intended.

Requires:
- Python 3.10+
- PySide6
- ffmpeg / ffprobe in PATH
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QProcess, Qt
from PySide6.QtGui import QAction, QImage, QKeySequence, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QSlider,
    QSpinBox,
    QDoubleSpinBox,
    QVBoxLayout,
    QWidget,
)

APP_TITLE = "Maud Sync Tool"
SETTINGS_FILE = ".maud_sync_tool.json"
DEFAULT_FPS = 24.0
DEFAULT_TARGET_RES = 5760
DEFAULT_FOV = 198.0
DEFAULT_YAW_LEFT = 0.0
DEFAULT_YAW_RIGHT = 0.0
DEFAULT_OUTPUT_PREFIX = ""
DEFAULT_DURATION_TEST = 20
DEFAULT_PREVIEW_SECONDS = 2
DEFAULT_PREVIEW_HEIGHT = 720


class ToolError(RuntimeError):
    pass


def is_insv(path: Path) -> bool:
    return path.suffix.lower() == ".insv"


def source_mode_for_pair(left: Path, right: Path) -> str:
    left_is_insv = is_insv(left)
    right_is_insv = is_insv(right)
    if left_is_insv != right_is_insv:
        raise ToolError("Left/right must both be INSV or both be MOV/MP4 style files.")
    return "insv" if left_is_insv else "flat"


@dataclass
class VideoInfo:
    path: Path
    width: int
    height: int
    nb_frames: Optional[int]
    fps: float


@dataclass
class PreviewSet:
    dir_path: Path
    fps: float
    frame_count: int
    width: int
    height: int
    season_start_seconds: float

    @property
    def frame_step(self) -> int:
        return 1

    def absolute_frame_index(self, local_index: int) -> int:
        season_start_frame = int(round(self.season_start_seconds * self.fps))
        return season_start_frame + local_index * self.frame_step

    def local_seconds(self, local_index: int) -> float:
        return local_index / max(self.fps, 0.001)

    def absolute_seconds(self, local_index: int) -> float:
        return self.absolute_frame_index(local_index) / max(self.fps, 0.001)

    def frame_path(self, index_1based: int) -> Path:
        return self.dir_path / f"frame_{index_1based:06d}.jpg"


def run_checked(cmd: list[str]) -> str:
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        raise ToolError(
            f"Command failed: {' '.join(shlex.quote(c) for c in cmd)}\n\n"
            f"stdout:\n{proc.stdout}\n\nstderr:\n{proc.stderr}"
        )
    return proc.stdout


def ffprobe_video_info(path: Path, stream: str = "v:0") -> VideoInfo:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        stream,
        "-show_entries",
        "stream=width,height,r_frame_rate,nb_frames",
        "-of",
        "json",
        str(path),
    ]
    raw = run_checked(cmd)
    data = json.loads(raw)
    streams = data.get("streams", [])
    if not streams:
        raise ToolError(f"No stream {stream} found in {path}")
    s = streams[0]
    fps = parse_rate(s.get("r_frame_rate", "24/1"))
    nb_frames = s.get("nb_frames")
    return VideoInfo(
        path=path,
        width=int(s["width"]),
        height=int(s["height"]),
        nb_frames=int(nb_frames) if nb_frames and str(nb_frames).isdigit() else None,
        fps=fps,
    )


def parse_rate(rate: str) -> float:
    if not rate or rate == "0/0":
        return DEFAULT_FPS
    if "/" in rate:
        a, b = rate.split("/", 1)
        af = float(a)
        bf = float(b)
        return af / bf if bf else DEFAULT_FPS
    return float(rate)


def count_preview_frames(directory: Path) -> int:
    return len(list(directory.glob("frame_*.jpg")))


def preview_cache_tag(value: float) -> str:
    return f"{value:.3f}".replace("-", "m").replace(".", "p")


def build_preview_dir_name(
    src: Path,
    preview_seconds: int,
    preview_height: int,
    fov: float,
    yaw: float,
    use_insv_pipeline: bool,
    season_start_seconds: float,
) -> str:
    mode_tag = "insv" if use_insv_pipeline else "flat"
    return (
        f"{src.stem}__{mode_tag}__ps{preview_seconds}"
        f"__ss{preview_cache_tag(season_start_seconds)}"
        f"__h{preview_height}__fov{preview_cache_tag(fov)}__yaw{preview_cache_tag(yaw)}__frames"
    )


def preview_cache_metadata(
    src: Path,
    preview_seconds: int,
    preview_height: int,
    fov: float,
    yaw: float,
    use_insv_pipeline: bool,
    season_start_seconds: float,
) -> dict[str, object]:
    return {
        "src_name": src.name,
        "src_mtime_ns": src.stat().st_mtime_ns,
        "preview_seconds": preview_seconds,
        "preview_height": preview_height,
        "fov": round(fov, 6),
        "yaw": round(yaw, 6),
        "use_insv_pipeline": use_insv_pipeline,
        "season_start_seconds": round(season_start_seconds, 6),
    }


def build_preview_frames(
    src: Path,
    out_dir: Path,
    preview_seconds: int,
    fov: float,
    yaw: float,
    preview_height: int,
    use_insv_pipeline: bool,
    season_start_seconds: float,
) -> PreviewSet:
    out_dir.mkdir(parents=True, exist_ok=True)
    info = ffprobe_video_info(src, stream="v:0")
    fps = info.fps or DEFAULT_FPS
    cmd = build_preview_command(
        src=src,
        out_dir=out_dir,
        preview_seconds=preview_seconds,
        fov=fov,
        yaw=yaw,
        preview_height=preview_height,
        use_insv_pipeline=use_insv_pipeline,
        season_start_seconds=season_start_seconds,
    )
    run_checked(cmd)
    return finalize_preview_set(src, out_dir, fps, season_start_seconds)


def build_preview_command(
    src: Path,
    out_dir: Path,
    preview_seconds: int,
    fov: float,
    yaw: float,
    preview_height: int,
    use_insv_pipeline: bool,
    season_start_seconds: float,
) -> list[str]:
    return [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-nostats",
        "-progress",
        "pipe:1",
        "-ss",
        f"{season_start_seconds:.6f}",
        "-t",
        str(preview_seconds),
        "-i",
        str(src),
        "-filter_complex",
        (
            (
                f"[0:v:0][0:v:1]hstack[df];"
                f"[df]v360=dfisheye:e:ih_fov={fov}:iv_fov={fov}:yaw={yaw},"
                f"scale=-2:{preview_height}[v]"
            )
            if use_insv_pipeline
            else f"[0:v:0]scale=-2:{preview_height}[v]"
        ),
        "-map",
        "[v]",
        "-an",
        str(out_dir / "frame_%06d.jpg"),
    ]


def finalize_preview_set(src: Path, out_dir: Path, fps: float, season_start_seconds: float) -> PreviewSet:
    first = out_dir / "frame_000001.jpg"
    if not first.exists():
        raise ToolError(f"Preview build created no frames for {src.name}")
    frame_info = ffprobe_video_info(first)
    frame_count = count_preview_frames(out_dir)
    return PreviewSet(
        dir_path=out_dir,
        fps=fps,
        frame_count=frame_count,
        width=frame_info.width,
        height=frame_info.height,
        season_start_seconds=season_start_seconds,
    )

def qpixmap_from_file(path: Path) -> QPixmap:
    img = QImage(str(path))
    if img.isNull():
        raise ToolError(f"Could not load image: {path}")
    return QPixmap.fromImage(img)


def extract_number_triplet(name: str) -> Optional[str]:
    m = re.search(r"_(\d{3})\.(?:insv|mp4|mov)$", name, flags=re.IGNORECASE)
    if m:
        return m.group(1)
    m = re.search(r"_(\d{3})(?:_|\.)", name)
    return m.group(1) if m else None


def extract_date_yyyymmdd(name: str) -> Optional[str]:
    m = re.search(r"VID_(\d{8})_", name)
    return m.group(1) if m else None


def infer_output_stem(left_path: Path, right_path: Path, prefix: str = DEFAULT_OUTPUT_PREFIX) -> str:
    nums_left = extract_number_triplet(left_path.name)
    nums_right = extract_number_triplet(right_path.name)
    date = extract_date_yyyymmdd(left_path.name) or extract_date_yyyymmdd(right_path.name) or "00000000"
    parts = []
    if prefix.strip():
        parts.append(prefix.strip())
    parts.append(date)
    if nums_left and nums_right:
        parts.append(f"{nums_left}-{nums_right}")
    else:
        parts.append(f"{left_path.stem}-{right_path.stem}")
    return "-".join(parts)


def with_left_frame_suffix(stem: str, left_frame: int) -> str:
    base = re.sub(r"__L\d+$", "", stem)
    return f"{base}__L{left_frame:06d}"


def shell_quote(path: str) -> str:
    return shlex.quote(path)


def metadata_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def build_embedded_meta1(recipe_cmd: str) -> str:
    return f"MYC_META1:{recipe_cmd}"


def build_embedded_meta2() -> str:
    return ""


def read_jpg_comment(path: Path) -> str:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format_tags=comment",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    return run_checked(cmd).strip()


def build_flat_tb_graph(
    left_chain: str,
    right_chain: str,
    target_res: int,
    use_rgb24: bool,
) -> str:
    left_filters = f"{left_chain},format=rgb24" if use_rgb24 else left_chain
    right_filters = f"{right_chain},format=rgb24" if use_rgb24 else right_chain
    return (
        f"[0:v:0]{left_filters},split=3[lv0][lv12][lv3]; "
        f"[1:v:0]{right_filters},split=3[rv0][rv12][rv3]; "
        "[lv0]crop='iw/4':ih:0:0[lq0]; "
        "[lv12]crop='iw/2':ih:'iw/4':0[lmid]; "
        "[lv3]crop='iw/4':ih:'3*iw/4':0[lq3]; "
        "[rv0]crop='iw/4':ih:0:0[rq0]; "
        "[rv12]crop='iw/2':ih:'iw/4':0[rmid]; "
        "[rv3]crop='iw/4':ih:'3*iw/4':0[rq3]; "
        "[rq0][lmid][rq3]hstack=inputs=3[top]; "
        "[lq0][rmid][lq3]hstack=inputs=3[bot]; "
        f"[top][bot]vstack,scale={target_res}:{target_res}[v]"
    )


def build_image_script_text(
    left_file: Path,
    right_file: Path,
    output_jpg: str,
    left_frame_index: int,
    right_frame_index: int,
    fps: float,
    src_left_width: int,
    src_left_height: int,
    src_right_width: int,
    src_right_height: int,
    target_res: int,
    fov_left: float,
    fov_right: float,
    yaw_left: float,
    yaw_right: float,
    use_insv_pipeline: bool,
) -> str:
    left_ts = left_frame_index / max(fps, 0.001)
    right_ts = right_frame_index / max(fps, 0.001)

    if use_insv_pipeline:
        filter_graph = (
            f"[0:v:0]select='eq(n,{left_frame_index})',setpts=PTS-STARTPTS,format=rgb24[f0a]; "
            f"[0:v:1]select='eq(n,{left_frame_index})',setpts=PTS-STARTPTS,format=rgb24[f0b]; "
            f"[f0a][f0b]hstack[dfL]; "
            f"[dfL]v360=dfisheye:e:ih_fov={fov_left}:iv_fov={fov_left}:yaw={yaw_left}:pitch=0:roll=0,split=3[lv0][lv12][lv3]; "
            f"[1:v:0]select='eq(n,{right_frame_index})',setpts=PTS-STARTPTS,format=rgb24[g0a]; "
            f"[1:v:1]select='eq(n,{right_frame_index})',setpts=PTS-STARTPTS,format=rgb24[g0b]; "
            f"[g0a][g0b]hstack[dfR]; "
            f"[dfR]v360=dfisheye:e:ih_fov={fov_right}:iv_fov={fov_right}:yaw={yaw_right}:pitch=0:roll=0,split=3[rv0][rv12][rv3]; "
            "[lv0]crop='iw/4':ih:0:0[r1]; "
            "[lv12]crop='iw/2':ih:'iw/4':0[r23]; "
            "[lv3]crop='iw/4':ih:'3*iw/4':0[r4]; "
            "[rv0]crop='iw/4':ih:0:0[r5]; "
            "[rv12]crop='iw/2':ih:'iw/4':0[r67]; "
            "[rv3]crop='iw/4':ih:'3*iw/4':0[r8]; "
            "[r5][r23][r8]hstack=inputs=3[top]; "
            "[r1][r67][r4]hstack=inputs=3[bot]; "
            f"[top][bot]vstack,scale={target_res}:{target_res}[v]"
        )
    else:
        filter_graph = build_flat_tb_graph(
            left_chain=f"select='eq(n,{left_frame_index})',setpts=PTS-STARTPTS",
            right_chain=f"select='eq(n,{right_frame_index})',setpts=PTS-STARTPTS",
            target_res=target_res,
            use_rgb24=True,
        )

    recipe_cmd = " ".join(
        [
            "ffmpeg -hide_banner",
            f"-i {shlex.quote(left_file.name)}",
            f"-i {shlex.quote(right_file.name)}",
            f'-filter_complex {shlex.quote(filter_graph)}',
            '-map "[v]"',
            "-frames:v 1",
            "-update 1",
            "-q:v 1",
            shlex.quote(output_jpg),
        ]
    )
    meta1 = metadata_escape(build_embedded_meta1(recipe_cmd))
    meta2 = metadata_escape(build_embedded_meta2())

    lines = f'''#!/usr/bin/env bash
set -euo pipefail

# Auto-generated by Maud Sync Tool
# 3D360 still image from exact chosen preview frames
# Left : {left_file.name} frame {left_frame_index}
# Right: {right_file.name} frame {right_frame_index}
# FPS used for sync: {fps:.6f}

LEFT_FILE={shell_quote(left_file.name)}
RIGHT_FILE={shell_quote(right_file.name)}
OUT_JPG={shell_quote(output_jpg)}

SRC_W_LEFT={src_left_width}
SRC_H_LEFT={src_left_height}
SRC_W_RIGHT={src_right_width}
SRC_H_RIGHT={src_right_height}
TARGET_RES={target_res}
FOV_LEFT={fov_left}
FOV_RIGHT={fov_right}
YAW_LEFT={yaw_left}
YAW_RIGHT={yaw_right}
FPS_SYNC={fps:.6f}
LEFT_FRAME={left_frame_index}
RIGHT_FRAME={right_frame_index}
LEFT_TS={left_ts:.6f}
RIGHT_TS={right_ts:.6f}

ffmpeg -hide_banner \
  -i "$LEFT_FILE" \
  -i "$RIGHT_FILE" \
  -filter_complex "\
{filter_graph}" \
  -map "[v]" \
  -frames:v 1 \
  -update 1 \
  -metadata comment="{meta1}" \
  -metadata description="{meta2}" \
  -q:v 1 \
  "$OUT_JPG"
'''
    return lines

def build_fast_batch_image_dump_script_text(
    left_file: Path,
    right_file: Path,
    output_dir_name: str,
    stem: str,
    season_start_seconds: float,
    preview_seconds: int,
    target_res: int,
    fov_left: float,
    fov_right: float,
    yaw_left: float,
    yaw_right: float,
    use_insv_pipeline: bool,
) -> str:
    if use_insv_pipeline:
        filter_graph = (
            "[0:v:0]format=rgb24[f0a]; "
            "[0:v:1]format=rgb24[f0b]; "
            "[f0a][f0b]hstack[dfL]; "
            f"[dfL]v360=dfisheye:e:ih_fov={fov_left}:iv_fov={fov_left}:yaw={yaw_left}:pitch=0:roll=0,split=3[lv0][lv12][lv3]; "
            "[1:v:0]format=rgb24[g0a]; "
            "[1:v:1]format=rgb24[g0b]; "
            "[g0a][g0b]hstack[dfR]; "
            f"[dfR]v360=dfisheye:e:ih_fov={fov_right}:iv_fov={fov_right}:yaw={yaw_right}:pitch=0:roll=0,split=3[rv0][rv12][rv3]; "
            "[lv0]crop='iw/4':ih:0:0[r1]; "
            "[lv12]crop='iw/2':ih:'iw/4':0[r23]; "
            "[lv3]crop='iw/4':ih:'3*iw/4':0[r4]; "
            "[rv0]crop='iw/4':ih:0:0[r5]; "
            "[rv12]crop='iw/2':ih:'iw/4':0[r67]; "
            "[rv3]crop='iw/4':ih:'3*iw/4':0[r8]; "
            "[r5][r23][r8]hstack=inputs=3[top]; "
            "[r1][r67][r4]hstack=inputs=3[bot]; "
            f"[top][bot]vstack,scale={target_res}:{target_res}[v]"
        )
    else:
        filter_graph = build_flat_tb_graph(
            left_chain="null",
            right_chain="null",
            target_res=target_res,
            use_rgb24=False,
        )

    lines = f'''#!/usr/bin/env bash
set -euo pipefail

# Auto-generated by Maud Sync Tool
# Fast raw dump from preview duration.
# Use this for timelapse-style workflows where frame-for-frame synced dump is not needed.

LEFT_FILE={shell_quote(left_file.name)}
RIGHT_FILE={shell_quote(right_file.name)}
OUT_DIR={shell_quote(output_dir_name)}
STEM={shell_quote(stem)}
SEASON_START_SECONDS={season_start_seconds:.6f}
PREVIEW_SECONDS={preview_seconds}
TARGET_RES={target_res}
FOV_LEFT={fov_left}
FOV_RIGHT={fov_right}
YAW_LEFT={yaw_left}
YAW_RIGHT={yaw_right}

mkdir -p "$OUT_DIR"

ffmpeg -hide_banner -y -ss "$SEASON_START_SECONDS" -t "$PREVIEW_SECONDS" \
  -i "$LEFT_FILE" \
  -i "$RIGHT_FILE" \
  -filter_complex "\
{filter_graph}" \
  -map "[v]" \
  -q:v 1 \
  "$OUT_DIR/$STEM"_%06d.jpg
'''
    return lines

def build_batch_image_dump_script_text(
    left_file: Path,
    right_file: Path,
    output_dir_name: str,
    stem: str,
    season_start_seconds: float,
    offset_frames: int,
    fps: float,
    src_left_width: int,
    src_left_height: int,
    src_right_width: int,
    src_right_height: int,
    target_res: int,
    fov_left: float,
    fov_right: float,
    yaw_left: float,
    yaw_right: float,
    left_preview_count: int,
    right_preview_count: int,
    use_insv_pipeline: bool,
) -> str:
    season_start_frame = int(round(season_start_seconds * fps))
    if offset_frames >= 0:
        left_start = 0
        right_start = offset_frames
        pair_count = min(left_preview_count, max(0, right_preview_count - offset_frames))
    else:
        left_start = -offset_frames
        right_start = 0
        pair_count = min(max(0, left_preview_count + offset_frames), right_preview_count)

    if use_insv_pipeline:
        filter_graph = (
            "[0:v:0]select='eq(n,'$LEFT_FRAME')',setpts=PTS-STARTPTS,format=rgb24[f0a]; "
            "[0:v:1]select='eq(n,'$LEFT_FRAME')',setpts=PTS-STARTPTS,format=rgb24[f0b]; "
            "[f0a][f0b]hstack[dfL]; "
            f"[dfL]v360=dfisheye:e:ih_fov={fov_left}:iv_fov={fov_left}:yaw={yaw_left}:pitch=0:roll=0,split=3[lv0][lv12][lv3]; "
            "[1:v:0]select='eq(n,'$RIGHT_FRAME')',setpts=PTS-STARTPTS,format=rgb24[g0a]; "
            "[1:v:1]select='eq(n,'$RIGHT_FRAME')',setpts=PTS-STARTPTS,format=rgb24[g0b]; "
            "[g0a][g0b]hstack[dfR]; "
            f"[dfR]v360=dfisheye:e:ih_fov={fov_right}:iv_fov={fov_right}:yaw={yaw_right}:pitch=0:roll=0,split=3[rv0][rv12][rv3]; "
            "[lv0]crop='iw/4':ih:0:0[r1]; "
            "[lv12]crop='iw/2':ih:'iw/4':0[r23]; "
            "[lv3]crop='iw/4':ih:'3*iw/4':0[r4]; "
            "[rv0]crop='iw/4':ih:0:0[r5]; "
            "[rv12]crop='iw/2':ih:'iw/4':0[r67]; "
            "[rv3]crop='iw/4':ih:'3*iw/4':0[r8]; "
            "[r5][r23][r8]hstack=inputs=3[top]; "
            "[r1][r67][r4]hstack=inputs=3[bot]; "
            f"[top][bot]vstack,scale={target_res}:{target_res}[v]"
        )
    else:
        filter_graph = build_flat_tb_graph(
            left_chain="select='eq(n,'$LEFT_FRAME')',setpts=PTS-STARTPTS",
            right_chain="select='eq(n,'$RIGHT_FRAME')',setpts=PTS-STARTPTS",
            target_res=target_res,
            use_rgb24=True,
        )

    lines = f'''#!/usr/bin/env bash
set -euo pipefail

# Auto-generated by Maud Sync Tool
# Batch dump of synced stills from original source files
# Preview sync offset decides which L/R frame pairs are used.

LEFT_FILE={shell_quote(left_file.name)}
RIGHT_FILE={shell_quote(right_file.name)}
OUT_DIR={shell_quote(output_dir_name)}
STEM={shell_quote(stem)}
PAIR_COUNT={pair_count}
LEFT_START={left_start}
RIGHT_START={right_start}
SEASON_START_SECONDS={season_start_seconds:.6f}
FPS_SYNC={fps:.6f}
TARGET_RES={target_res}
FOV_LEFT={fov_left}
FOV_RIGHT={fov_right}
YAW_LEFT={yaw_left}
YAW_RIGHT={yaw_right}

mkdir -p "$OUT_DIR"

SEASON_START_FRAME={season_start_frame}

for ((i=0; i<PAIR_COUNT; i++)); do
  LEFT_FRAME=$((SEASON_START_FRAME + LEFT_START + i))
  RIGHT_FRAME=$((SEASON_START_FRAME + RIGHT_START + i))
  OUT_JPG=$(printf "%s/%s_%06d.jpg" "$OUT_DIR" "$STEM" "$i")

  ffmpeg -hide_banner -y \
    -i "$LEFT_FILE" \
    -i "$RIGHT_FILE" \
    -filter_complex "\
{filter_graph}" \
    -map "[v]" \
    -frames:v 1 \
    -update 1 \
    -q:v 1 \
    "$OUT_JPG"

done
'''
    return lines


def build_ffmpeg_script_text(
    left_file: Path,
    right_file: Path,
    output_mp4: str,
    offset_frames: int,
    fps: float,
    src_left_width: int,
    src_left_height: int,
    src_right_width: int,
    src_right_height: int,
    target_res: int,
    fov_left: float,
    fov_right: float,
    yaw_left: float,
    yaw_right: float,
    test_seconds: int,
    include_test_duration: bool,
    use_insv_pipeline: bool,
) -> str:
    trim_seconds = abs(offset_frames) / max(fps, 0.001)
    trim_str = f"{trim_seconds:.6f}"

    if offset_frames >= 0:
        right_trim_v0 = f"trim=start={trim_str},setpts=PTS-STARTPTS,"
        right_trim_v1 = f"trim=start={trim_str},setpts=PTS-STARTPTS,"
        left_trim_v0 = ""
        left_trim_v1 = ""
        right_trim_a = f",atrim=start={trim_str},asetpts=PTS-STARTPTS"
        left_trim_a = ""
    else:
        right_trim_v0 = ""
        right_trim_v1 = ""
        left_trim_v0 = f"trim=start={trim_str},setpts=PTS-STARTPTS,"
        left_trim_v1 = f"trim=start={trim_str},setpts=PTS-STARTPTS,"
        right_trim_a = ""
        left_trim_a = f",atrim=start={trim_str},asetpts=PTS-STARTPTS"

    duration_line = '  -t "$TEST_SECONDS" \\\n' if include_test_duration else ""

    if use_insv_pipeline:
        video_graph = (
            f"[0:v:0]{left_trim_v0}format=rgb24[f0a]; "
            f"[0:v:1]{left_trim_v1}format=rgb24[f0b]; "
            "[f0a][f0b]hstack[dfL]; "
            f"[dfL]v360=dfisheye:e:ih_fov={fov_left}:iv_fov={fov_left}:yaw={yaw_left}:pitch=0:roll=0,split=3[lv0][lv12][lv3]; "
            f"[1:v:0]{right_trim_v0}format=rgb24[g0a]; "
            f"[1:v:1]{right_trim_v1}format=rgb24[g0b]; "
            "[g0a][g0b]hstack[dfR]; "
            f"[dfR]v360=dfisheye:e:ih_fov={fov_right}:iv_fov={fov_right}:yaw={yaw_right}:pitch=0:roll=0,split=3[rv0][rv12][rv3]; "
            "[lv0]crop='iw/4':ih:0:0[r1]; "
            "[lv12]crop='iw/2':ih:'iw/4':0[r23]; "
            "[lv3]crop='iw/4':ih:'3*iw/4':0[r4]; "
            "[rv0]crop='iw/4':ih:0:0[r5]; "
            "[rv12]crop='iw/2':ih:'iw/4':0[r67]; "
            "[rv3]crop='iw/4':ih:'3*iw/4':0[r8]; "
            "[r5][r23][r8]hstack=inputs=3[top]; "
            "[r1][r67][r4]hstack=inputs=3[bot]; "
            f"[top][bot]vstack,scale={target_res}:{target_res}[v]"
        )
    else:
        video_graph = build_flat_tb_graph(
            left_chain=f"{left_trim_v0}null".rstrip(","),
            right_chain=f"{right_trim_v0}null".rstrip(","),
            target_res=target_res,
            use_rgb24=True,
        )

    audio_filter = ""
    audio_map = ""
    if use_insv_pipeline:
        audio_filter = (
            f"[0:a]pan=mono|c0=c0{left_trim_a}[a0]; "
            f"[1:a]pan=mono|c0=c0{right_trim_a}[a1]; "
            "[a0][a1]join=inputs=2:channel_layout=stereo[a]"
        )
        audio_map = ' -map "[a]"'

    filter_complex = video_graph if not audio_filter else f"{video_graph}; {audio_filter}"
    audio_codec_block = '  -c:a aac -b:a 192k \\\n' if use_insv_pipeline else ""

    lines = f'''#!/usr/bin/env bash
set -euo pipefail

# Auto-generated by Maud Sync Tool
# Left : {left_file.name}
# Right: {right_file.name}
# FPS used for sync: {fps:.6f}
# Offset (frames): {offset_frames}
# Offset (seconds): {trim_str}

LEFT_FILE={shell_quote(left_file.name)}
RIGHT_FILE={shell_quote(right_file.name)}
OUT_MP4={shell_quote(output_mp4)}
TEST_SECONDS={test_seconds}

SRC_W_LEFT={src_left_width}
SRC_H_LEFT={src_left_height}
SRC_W_RIGHT={src_right_width}
SRC_H_RIGHT={src_right_height}
TARGET_RES={target_res}
FOV_LEFT={fov_left}
FOV_RIGHT={fov_right}
YAW_LEFT={yaw_left}
YAW_RIGHT={yaw_right}
FPS_SYNC={fps:.6f}
OFFSET_FRAMES={offset_frames}
OFFSET_SECONDS={trim_str}

# Derived source geometry per eye after v360 equirect.
# Expected common case:
#   5760x2880 -> quarter=1440, half=2880
#   7680x3840 -> quarter=1920, half=3840
EYE_W_LEFT=$((SRC_W_LEFT/4))
EYE_H_LEFT=$((SRC_H_LEFT))
MID_W_LEFT=$((SRC_W_LEFT/2))
THREE_Q_LEFT=$((3*SRC_W_LEFT/4))

EYE_W_RIGHT=$((SRC_W_RIGHT/4))
EYE_H_RIGHT=$((SRC_H_RIGHT))
MID_W_RIGHT=$((SRC_W_RIGHT/2))
THREE_Q_RIGHT=$((3*SRC_W_RIGHT/4))

ffmpeg -hide_banner \
{duration_line}  -i "$LEFT_FILE" \
  -i "$RIGHT_FILE" \
  -filter_complex "\
{filter_complex}" \
  -map "[v]"{audio_map} \
  -c:v libsvtav1 \
{audio_codec_block}  -shortest \
  "$OUT_MP4"
'''
    return lines


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.resize(1500, 980)

        self.work_dir = Path.cwd()
        self.settings_path = self.work_dir / SETTINGS_FILE
        self.left_info: Optional[VideoInfo] = None
        self.right_info: Optional[VideoInfo] = None
        self.left_preview: Optional[PreviewSet] = None
        self.right_preview: Optional[PreviewSet] = None
        self.preview_process: Optional[QProcess] = None
        self.preview_queue: list[dict[str, object]] = []
        self.current_preview_job: Optional[dict[str, object]] = None
        self.preview_expected_frames = 0

        self._build_ui()
        self.load_settings()
        self.refresh_file_lists()

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)

        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(4, 4, 4, 4)
        left_layout.setSpacing(6)

        top_row = QHBoxLayout()
        left_layout.addLayout(top_row)

        self.dir_label = QLineEdit(str(self.work_dir))
        self.dir_label.setReadOnly(True)
        btn_dir = QPushButton("Choose folder…")
        btn_dir.clicked.connect(self.choose_folder)
        top_row.addWidget(QLabel("Folder:"))
        top_row.addWidget(self.dir_label, 1)
        top_row.addWidget(btn_dir)

        pick_row = QGridLayout()
        left_layout.addLayout(pick_row)

        self.left_combo = QComboBox()
        self.right_combo = QComboBox()
        btn_refresh = QPushButton("Refresh")
        btn_refresh.clicked.connect(self.refresh_file_lists)
        btn_build = QPushButton("Preview")
        btn_build.clicked.connect(self.build_previews)
        self.btn_stop_preview = QPushButton("Stop")
        self.btn_stop_preview.setEnabled(False)
        self.btn_stop_preview.clicked.connect(self.stop_preview_build)

        pick_row.addWidget(QLabel("Left video:"), 0, 0)
        pick_row.addWidget(self.left_combo, 0, 1)
        pick_row.addWidget(QLabel("Right video:"), 1, 0)
        pick_row.addWidget(self.right_combo, 1, 1)
        pick_row.addWidget(btn_refresh, 0, 2)
        pick_row.addWidget(btn_build, 1, 2)
        pick_row.addWidget(self.btn_stop_preview, 1, 3)
        pick_row.setColumnStretch(1, 1)

        preview_box = QGroupBox("Preview (Top/Bottom)")
        left_layout.addWidget(preview_box, 1)
        preview_layout = QVBoxLayout(preview_box)
        preview_layout.setContentsMargins(6, 6, 6, 6)
        preview_layout.setSpacing(6)

        self.left_caption = QLabel("Left preview")
        self.left_caption.setStyleSheet("font-weight: bold;")
        preview_layout.addWidget(self.left_caption)

        self.left_image = QLabel("Left preview")
        self.left_image.setAlignment(Qt.AlignCenter)
        self.left_image.setMinimumSize(720, 300)
        self.left_image.setStyleSheet("background:#111;color:#ddd;border:1px solid #555;")
        preview_layout.addWidget(self.left_image, 1)

        self.right_caption = QLabel("Right preview")
        self.right_caption.setStyleSheet("font-weight: bold;")
        preview_layout.addWidget(self.right_caption)

        self.right_image = QLabel("Right preview")
        self.right_image.setAlignment(Qt.AlignCenter)
        self.right_image.setMinimumSize(720, 300)
        self.right_image.setStyleSheet("background:#111;color:#ddd;border:1px solid #555;")
        preview_layout.addWidget(self.right_image, 1)

        self.preview_progress_label = QLabel("Preview progress: idle")
        preview_layout.addWidget(self.preview_progress_label)

        self.preview_scope_label = QLabel("Window start: 0.000 s")
        preview_layout.addWidget(self.preview_scope_label)

        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(4, 4, 4, 4)
        right_layout.setSpacing(8)

        controls = QGroupBox("Sync controls")
        right_layout.addWidget(controls)
        controls_layout = QGridLayout(controls)

        self.fps_box = QDoubleSpinBox()
        self.fps_box.setRange(1.0, 240.0)
        self.fps_box.setDecimals(6)
        self.fps_box.setValue(DEFAULT_FPS)
        self.fps_box.editingFinished.connect(self.save_settings)

        self.step_spin = QSpinBox()
        self.step_spin.setRange(1, 500)
        self.step_spin.setValue(1)
        self.step_spin.editingFinished.connect(self.save_settings)

        self.link_checkbox = QCheckBox("Move both together")
        self.link_checkbox.setChecked(False)

        self.left_frame_box = QSpinBox()
        self.left_frame_box.setRange(0, 10_000_000)
        self.left_frame_box.valueChanged.connect(self.on_left_frame_changed)

        self.right_frame_box = QSpinBox()
        self.right_frame_box.setRange(0, 10_000_000)
        self.right_frame_box.valueChanged.connect(self.on_right_frame_changed)
        btn_l_prev = QPushButton("L -")
        btn_l_next = QPushButton("L +")
        btn_r_prev = QPushButton("R -")
        btn_r_next = QPushButton("R +")
        btn_both_prev = QPushButton("Both -")
        btn_both_next = QPushButton("Both +")

        btn_l_prev.clicked.connect(lambda: self.bump_frame("left", -self.step_spin.value()))
        btn_l_next.clicked.connect(lambda: self.bump_frame("left", +self.step_spin.value()))
        btn_r_prev.clicked.connect(lambda: self.bump_frame("right", -self.step_spin.value()))
        btn_r_next.clicked.connect(lambda: self.bump_frame("right", +self.step_spin.value()))
        btn_both_prev.clicked.connect(lambda: self.bump_both(-self.step_spin.value()))
        btn_both_next.clicked.connect(lambda: self.bump_both(+self.step_spin.value()))

        self.offset_label = QLabel("Offset: 0 frames (0.000000 s)")
        self.offset_label.setStyleSheet("font-weight:bold;")
        self.absolute_label = QLabel("Absolute frames: L 0, R 0")
        self.window_slider = QSlider(Qt.Horizontal)
        self.window_slider.setRange(0, 0)
        self.window_slider.valueChanged.connect(self.on_window_slider_changed)
        self.window_slider_label = QLabel("Window frame: 0 / 0")

        controls_layout.addWidget(QLabel("FPS:"), 0, 0)
        controls_layout.addWidget(self.fps_box, 0, 1)
        controls_layout.addWidget(QLabel("Step:"), 0, 2)
        controls_layout.addWidget(self.step_spin, 0, 3)
        controls_layout.addWidget(self.link_checkbox, 0, 4)

        controls_layout.addWidget(QLabel("Left frame:"), 1, 0)
        controls_layout.addWidget(self.left_frame_box, 1, 1)
        controls_layout.addWidget(btn_l_prev, 1, 2)
        controls_layout.addWidget(btn_l_next, 1, 3)

        controls_layout.addWidget(QLabel("Right frame:"), 2, 0)
        controls_layout.addWidget(self.right_frame_box, 2, 1)
        controls_layout.addWidget(btn_r_prev, 2, 2)
        controls_layout.addWidget(btn_r_next, 2, 3)

        controls_layout.addWidget(self.window_slider_label, 3, 0, 1, 5)

        controls_layout.addWidget(QLabel("Slide:"), 4, 0)
        controls_layout.addWidget(self.window_slider, 4, 1, 1, 4)
        controls_layout.addWidget(btn_both_prev, 5, 2)
        controls_layout.addWidget(btn_both_next, 5, 3)
        controls_layout.addWidget(self.offset_label, 5, 0, 1, 2)
        controls_layout.addWidget(self.absolute_label, 6, 0, 1, 5)

        gen = QGroupBox("Generate script")
        right_layout.addWidget(gen)
        gen_form = QFormLayout(gen)

        self.output_stem_edit = QLineEdit()
        self.output_stem_edit.editingFinished.connect(self.save_settings)

        self.target_res_box = QSpinBox()
        self.target_res_box.setRange(512, 16384)
        self.target_res_box.setValue(DEFAULT_TARGET_RES)
        self.target_res_box.editingFinished.connect(self.save_settings)

        self.left_fov_box = QDoubleSpinBox(); self.left_fov_box.setRange(1.0, 360.0); self.left_fov_box.setDecimals(3); self.left_fov_box.setValue(DEFAULT_FOV); self.left_fov_box.editingFinished.connect(self.save_settings)
        self.right_fov_box = QDoubleSpinBox(); self.right_fov_box.setRange(1.0, 360.0); self.right_fov_box.setDecimals(3); self.right_fov_box.setValue(DEFAULT_FOV); self.right_fov_box.editingFinished.connect(self.save_settings)
        self.left_yaw_box = QDoubleSpinBox(); self.left_yaw_box.setRange(-360.0, 360.0); self.left_yaw_box.setDecimals(3); self.left_yaw_box.setValue(DEFAULT_YAW_LEFT); self.left_yaw_box.editingFinished.connect(self.save_settings)
        self.right_yaw_box = QDoubleSpinBox(); self.right_yaw_box.setRange(-360.0, 360.0); self.right_yaw_box.setDecimals(3); self.right_yaw_box.setValue(DEFAULT_YAW_RIGHT); self.right_yaw_box.editingFinished.connect(self.save_settings)
        self.preview_seconds_box = QSpinBox(); self.preview_seconds_box.setRange(1, 10000); self.preview_seconds_box.setValue(DEFAULT_PREVIEW_SECONDS); self.preview_seconds_box.editingFinished.connect(self.save_settings)
        self.preview_height_box = QSpinBox(); self.preview_height_box.setRange(120, 2160); self.preview_height_box.setSingleStep(120); self.preview_height_box.setValue(DEFAULT_PREVIEW_HEIGHT); self.preview_height_box.editingFinished.connect(self.save_settings)
        self.season_start_box = QDoubleSpinBox(); self.season_start_box.setRange(0.0, 24 * 3600.0); self.season_start_box.setDecimals(3); self.season_start_box.setSingleStep(1.0); self.season_start_box.setValue(0.0); self.season_start_box.editingFinished.connect(self.save_settings)
        self.test_seconds_box = QSpinBox(); self.test_seconds_box.setRange(1, 3600); self.test_seconds_box.setValue(DEFAULT_DURATION_TEST); self.test_seconds_box.editingFinished.connect(self.save_settings)
        self.use_test_duration_box = QCheckBox("Use -t TEST_SECONDS in generated script")
        self.use_test_duration_box.stateChanged.connect(self.save_settings)

        self.synced_dump_box = QCheckBox("Synced dump")
        self.synced_dump_box.setChecked(False)
        self.synced_dump_box.stateChanged.connect(self.save_settings)

        btn_fill_name = QPushButton("Guess names")
        btn_fill_name.clicked.connect(self.fill_output_name)
        btn_generate = QPushButton("Generate video .sh")
        btn_generate.clicked.connect(self.generate_script)
        btn_generate_image = QPushButton("Generate image .sh")
        btn_generate_image.clicked.connect(self.generate_image_script)
        btn_dump_preview = QPushButton("Generate jpg dump .sh")
        btn_dump_preview.clicked.connect(self.generate_batch_image_dump_script)

        gen_form.addRow("Output stem:", self.output_stem_edit)
        gen_form.addRow("Target resolution:", self.target_res_box)
        gen_form.addRow("Left FOV:", self.left_fov_box)
        gen_form.addRow("Right FOV:", self.right_fov_box)
        gen_form.addRow("Left yaw:", self.left_yaw_box)
        gen_form.addRow("Right yaw:", self.right_yaw_box)
        gen_form.addRow("Window start (sec):", self.season_start_box)
        gen_form.addRow("Preview seconds:", self.preview_seconds_box)
        gen_form.addRow("Preview height:", self.preview_height_box)
        gen_form.addRow("Test seconds const:", self.test_seconds_box)
        gen_form.addRow("Short test render:", self.use_test_duration_box)
        gen_form.addRow("Dump mode:", self.synced_dump_box)

        gen_buttons = QHBoxLayout()
        gen_buttons.addWidget(btn_fill_name)
        gen_buttons.addWidget(btn_generate)
        gen_buttons.addWidget(btn_generate_image)
        gen_buttons.addWidget(btn_dump_preview)
        gen_form.addRow(gen_buttons)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(1000)
        self.log.setMinimumWidth(360)
        right_layout.addWidget(self.log, 1)

        root.addWidget(left_panel, 10)
        root.addWidget(right_panel, 6)

        self._shortcut_actions = []
        for text, key, func in [
            ("Left -1", Qt.Key_A, lambda: self.bump_frame("left", -1)),
            ("Left +1", Qt.Key_D, lambda: self.bump_frame("left", +1)),
            ("Right -1", Qt.Key_J, lambda: self.bump_frame("right", -1)),
            ("Right +1", Qt.Key_L, lambda: self.bump_frame("right", +1)),
            ("Both -1", Qt.Key_Left, lambda: self.bump_both(-1)),
            ("Both +1", Qt.Key_Right, lambda: self.bump_both(+1)),
        ]:
            action = QAction(text, self)
            action.setShortcut(QKeySequence(key))
            action.triggered.connect(func)
            self.addAction(action)
            self._shortcut_actions.append(action)

    def log_msg(self, text: str) -> None:
        self.log.appendPlainText(text)


    def load_settings(self) -> None:
        if not self.settings_path.exists():
            return
        try:
            data = json.loads(self.settings_path.read_text(encoding="utf-8"))
            self.fps_box.setValue(float(data.get("fps", DEFAULT_FPS)))
            self.step_spin.setValue(int(data.get("step", 1)))
            self.target_res_box.setValue(int(data.get("target_res", DEFAULT_TARGET_RES)))
            self.left_fov_box.setValue(float(data.get("left_fov", DEFAULT_FOV)))
            self.right_fov_box.setValue(float(data.get("right_fov", DEFAULT_FOV)))
            self.left_yaw_box.setValue(float(data.get("left_yaw", DEFAULT_YAW_LEFT)))
            self.right_yaw_box.setValue(float(data.get("right_yaw", DEFAULT_YAW_RIGHT)))
            self.season_start_box.setValue(float(data.get("season_start_seconds", 0.0)))
            self.preview_seconds_box.setValue(int(data.get("preview_seconds", DEFAULT_PREVIEW_SECONDS)))
            self.preview_height_box.setValue(int(data.get("preview_height", DEFAULT_PREVIEW_HEIGHT)))
            self.test_seconds_box.setValue(int(data.get("test_seconds", DEFAULT_DURATION_TEST)))
            self.use_test_duration_box.setChecked(bool(data.get("use_test_duration", False)))
            self.synced_dump_box.setChecked(bool(data.get("synced_dump", False)))
            self.output_stem_edit.setText(str(data.get("output_stem", "")))
        except Exception as e:
            self.log_msg(f"Could not load settings: {e}")

    def save_settings(self) -> None:
        try:
            data = {
                "fps": self.fps_box.value(),
                "step": self.step_spin.value(),
                "target_res": self.target_res_box.value(),
                "left_fov": self.left_fov_box.value(),
                "right_fov": self.right_fov_box.value(),
                "left_yaw": self.left_yaw_box.value(),
                "right_yaw": self.right_yaw_box.value(),
                "season_start_seconds": self.season_start_box.value(),
                "preview_seconds": self.preview_seconds_box.value(),
                "preview_height": self.preview_height_box.value(),
                "test_seconds": self.test_seconds_box.value(),
                "use_test_duration": self.use_test_duration_box.isChecked(),
                "synced_dump": self.synced_dump_box.isChecked(),
                "output_stem": self.output_stem_edit.text(),
            }
            self.settings_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception as e:
            self.log_msg(f"Could not save settings: {e}")

    def choose_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose folder", str(self.work_dir))
        if folder:
            self.work_dir = Path(folder)
            self.settings_path = self.work_dir / SETTINGS_FILE
            self.dir_label.setText(str(self.work_dir))
            self.load_settings()
            self.refresh_file_lists()

    def refresh_file_lists(self) -> None:
        files = sorted(
            [p for p in self.work_dir.iterdir() if p.is_file() and p.suffix.lower() in {".insv", ".mov", ".mp4"}],
            key=lambda p: p.name.lower(),
        )
        names = [p.name for p in files]
        self.left_combo.clear()
        self.right_combo.clear()
        self.left_combo.addItems(names)
        self.right_combo.addItems(names)
        if len(names) >= 2:
            self.left_combo.setCurrentIndex(0)
            self.right_combo.setCurrentIndex(1)
            if not self.output_stem_edit.text().strip():
                self.fill_output_name()
        self.log_msg(f"Found {len(names)} video file(s) (.insv/.mov/.mp4) in {self.work_dir}")

    def current_left_path(self) -> Path:
        return self.work_dir / self.left_combo.currentText()

    def current_right_path(self) -> Path:
        return self.work_dir / self.right_combo.currentText()

    def set_preview_progress(self, text: str) -> None:
        self.preview_progress_label.setText(f"Preview progress: {text}")

    def _cleanup_preview_process(self) -> None:
        if self.preview_process is not None:
            try:
                self.preview_process.readyReadStandardOutput.disconnect(self.on_preview_process_output)
            except Exception:
                pass
            try:
                self.preview_process.readyReadStandardError.disconnect(self.on_preview_process_stderr)
            except Exception:
                pass
            try:
                self.preview_process.finished.disconnect(self.on_preview_process_finished)
            except Exception:
                pass
            self.preview_process.deleteLater()
        self.preview_process = None

    def stop_preview_build(self) -> None:
        self.preview_queue.clear()
        self.current_preview_job = None
        self.preview_expected_frames = 0
        if self.preview_process is not None:
            self.log_msg("Stopping preview build …")
            self.preview_process.kill()
        self._cleanup_preview_process()
        self.btn_stop_preview.setEnabled(False)
        self.set_preview_progress("stopped")

    def on_preview_process_output(self) -> None:
        if self.preview_process is None or self.current_preview_job is None:
            return
        text = bytes(self.preview_process.readAllStandardOutput()).decode("utf-8", errors="replace")
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line.startswith("frame="):
                continue
            try:
                frame = int(line.split("=", 1)[1])
            except ValueError:
                continue
            side = str(self.current_preview_job["side"]).upper()
            expected = max(1, self.preview_expected_frames)
            shown = min(frame, expected)
            self.set_preview_progress(f"{side} {shown}/{expected}")

    def on_preview_process_stderr(self) -> None:
        if self.preview_process is None:
            return
        chunk = bytes(self.preview_process.readAllStandardError()).decode("utf-8", errors="replace")
        self.preview_stderr_buffer += chunk

    def start_next_preview_build(self) -> None:
        if not self.preview_queue:
            self.current_preview_job = None
            self._cleanup_preview_process()
            self.btn_stop_preview.setEnabled(False)
            self.left_frame_box.setRange(0, max(0, self.left_preview.frame_count - 1) if self.left_preview else 0)
            self.right_frame_box.setRange(0, max(0, self.right_preview.frame_count - 1) if self.right_preview else 0)
            self.window_slider.blockSignals(True)
            self.window_slider.setRange(0, max(0, self.left_preview.frame_count - 1) if self.left_preview else 0)
            self.window_slider.setValue(0)
            self.window_slider.blockSignals(False)
            self.fill_output_name()
            self.refresh_previews()
            if self.left_preview and self.right_preview:
                fps = min(self.left_preview.fps, self.right_preview.fps) or DEFAULT_FPS
                self.set_preview_progress("done")
                self.log_msg(
                    f"Preview ready. Left frames: {self.left_preview.frame_count}, "
                    f"Right frames: {self.right_preview.frame_count}, FPS: {fps:.6f}"
                )
            return

        self.current_preview_job = self.preview_queue.pop(0)
        side = str(self.current_preview_job["side"]).upper()
        src = self.current_preview_job["src"]
        preview_seconds = int(self.current_preview_job["preview_seconds"])
        preview_height = int(self.current_preview_job["preview_height"])
        season_start_seconds = float(self.current_preview_job["season_start_seconds"])
        fov = float(self.current_preview_job["fov"])
        yaw = float(self.current_preview_job["yaw"])
        fps = float(self.current_preview_job["fps"])
        self.preview_expected_frames = max(1, int(round(fps * preview_seconds)))
        self.set_preview_progress(f"{side} 0/{self.preview_expected_frames}")
        self.log_msg(
            f"Building {side} preview (season_start={season_start_seconds:.3f}s, "
            f"duration={preview_seconds}s, h={preview_height}, fov={fov}, yaw={yaw}) …"
        )

        out_dir = self.current_preview_job["out_dir"]
        out_dir.mkdir(parents=True, exist_ok=True)

        self.preview_stderr_buffer = ""
        process = QProcess(self)
        process.setProgram("ffmpeg")
        cmd = build_preview_command(
            src=src,  # type: ignore[arg-type]
            out_dir=self.current_preview_job["out_dir"],  # type: ignore[arg-type]
            preview_seconds=preview_seconds,
            fov=fov,
            yaw=yaw,
            preview_height=preview_height,
            use_insv_pipeline=bool(self.current_preview_job["use_insv_pipeline"]),
            season_start_seconds=season_start_seconds,
        )
        process.setArguments(cmd[1:])
        process.readyReadStandardOutput.connect(self.on_preview_process_output)
        process.readyReadStandardError.connect(self.on_preview_process_stderr)
        process.finished.connect(self.on_preview_process_finished)
        self.preview_process = process
        self.btn_stop_preview.setEnabled(True)
        process.start()

    def on_preview_process_finished(self, exit_code: int, exit_status) -> None:  # type: ignore[override]
        if self.current_preview_job is None or self.preview_process is None:
            self._cleanup_preview_process()
            self.btn_stop_preview.setEnabled(False)
            return

        stderr = getattr(self, "preview_stderr_buffer", "")
        if exit_code != 0:
            err = ToolError(
                f"Preview build failed for {self.current_preview_job['src']}\n\nstderr:\n{stderr}"
            )
            self._cleanup_preview_process()
            self.btn_stop_preview.setEnabled(False)
            self.preview_queue.clear()
            self.current_preview_job = None
            self.set_preview_progress("failed")
            self.show_error(err)
            return

        try:
            preview = finalize_preview_set(
                self.current_preview_job["src"],  # type: ignore[arg-type]
                self.current_preview_job["out_dir"],  # type: ignore[arg-type]
                float(self.current_preview_job["fps"]),
                float(self.current_preview_job["season_start_seconds"]),
            )
            if self.current_preview_job["side"] == "left":
                self.left_preview = preview
            else:
                self.right_preview = preview
        except Exception as e:
            self._cleanup_preview_process()
            self.btn_stop_preview.setEnabled(False)
            self.preview_queue.clear()
            self.current_preview_job = None
            self.set_preview_progress("failed")
            self.show_error(e)
            return

        self._cleanup_preview_process()
        self.start_next_preview_build()

    def build_previews(self) -> None:
        try:
            if self.preview_process is not None:
                raise ToolError("Preview build already running. Stop it first if you want to restart.")
            left = self.current_left_path()
            right = self.current_right_path()
            if not left.exists() or not right.exists():
                raise ToolError("Please choose valid left/right video files.")
            if left == right:
                raise ToolError("Left and right files must be different.")

            use_insv_pipeline = source_mode_for_pair(left, right) == "insv"

            self.left_info = ffprobe_video_info(left)
            self.right_info = ffprobe_video_info(right)
            fps = min(self.left_info.fps, self.right_info.fps) or DEFAULT_FPS
            self.fps_box.setValue(fps)

            preview_dir = self.work_dir / ".maud_preview"
            preview_targets = {
                "left": preview_dir / build_preview_dir_name(
                    left,
                    self.preview_seconds_box.value(),
                    self.preview_height_box.value(),
                    self.left_fov_box.value(),
                    self.left_yaw_box.value(),
                    use_insv_pipeline,
                    self.season_start_box.value(),
                ),
                "right": preview_dir / build_preview_dir_name(
                    right,
                    self.preview_seconds_box.value(),
                    self.preview_height_box.value(),
                    self.right_fov_box.value(),
                    self.right_yaw_box.value(),
                    use_insv_pipeline,
                    self.season_start_box.value(),
                ),
            }

            self.save_settings()

            preview_seconds = self.preview_seconds_box.value()
            preview_height = self.preview_height_box.value()
            season_start_seconds = self.season_start_box.value()
            left_fov = self.left_fov_box.value()
            right_fov = self.right_fov_box.value()
            left_yaw = self.left_yaw_box.value()
            right_yaw = self.right_yaw_box.value()
            self.left_preview = None
            self.right_preview = None

            self.left_frame_box.blockSignals(True)
            self.right_frame_box.blockSignals(True)
            self.left_frame_box.setValue(0)
            self.right_frame_box.setValue(0)
            self.left_frame_box.blockSignals(False)
            self.right_frame_box.blockSignals(False)
            self.left_frame_box.setRange(0, 0)
            self.right_frame_box.setRange(0, 0)
            self.window_slider.blockSignals(True)
            self.window_slider.setRange(0, 0)
            self.window_slider.setValue(0)
            self.window_slider.blockSignals(False)

            self.preview_queue = [
                {
                    "side": "left",
                    "src": left,
                    "out_dir": preview_targets["left"],
                    "preview_seconds": preview_seconds,
                    "preview_height": preview_height,
                    "season_start_seconds": season_start_seconds,
                    "fov": left_fov,
                    "yaw": left_yaw,
                    "fps": fps,
                    "use_insv_pipeline": use_insv_pipeline,
                },
                {
                    "side": "right",
                    "src": right,
                    "out_dir": preview_targets["right"],
                    "preview_seconds": preview_seconds,
                    "preview_height": preview_height,
                    "season_start_seconds": season_start_seconds,
                    "fov": right_fov,
                    "yaw": right_yaw,
                    "fps": fps,
                    "use_insv_pipeline": use_insv_pipeline,
                },
            ]
            self.start_next_preview_build()
        except Exception as e:
            self.show_error(e)

    def on_left_frame_changed(self, value: int) -> None:
        self.window_slider.blockSignals(True)
        self.window_slider.setValue(value)
        self.window_slider.blockSignals(False)
        self.refresh_previews()

    def on_right_frame_changed(self, value: int) -> None:
        self.refresh_previews()

    def on_window_slider_changed(self, value: int) -> None:
        current_left = self.left_frame_box.value()
        current_right = self.right_frame_box.value()
        delta = value - current_left
        self.left_frame_box.setValue(max(0, min(self.left_frame_box.maximum(), value)))
        self.right_frame_box.setValue(
            max(0, min(self.right_frame_box.maximum(), current_right + delta))
        )

    def bump_frame(self, side: str, delta: int) -> None:
        if side == "left":
            self.left_frame_box.setValue(max(0, min(self.left_frame_box.maximum(), self.left_frame_box.value() + delta)))
            if self.link_checkbox.isChecked():
                self.right_frame_box.setValue(max(0, min(self.right_frame_box.maximum(), self.right_frame_box.value() + delta)))
        else:
            self.right_frame_box.setValue(max(0, min(self.right_frame_box.maximum(), self.right_frame_box.value() + delta)))
            if self.link_checkbox.isChecked():
                self.left_frame_box.setValue(max(0, min(self.left_frame_box.maximum(), self.left_frame_box.value() + delta)))

    def bump_both(self, delta: int) -> None:
        self.left_frame_box.setValue(max(0, min(self.left_frame_box.maximum(), self.left_frame_box.value() + delta)))
        self.right_frame_box.setValue(max(0, min(self.right_frame_box.maximum(), self.right_frame_box.value() + delta)))

    def current_preview_set(self, side: str) -> Optional[PreviewSet]:
        return self.left_preview if side == "left" else self.right_preview

    def current_absolute_frame(self, side: str) -> int:
        preview = self.current_preview_set(side)
        local_index = self.left_frame_box.value() if side == "left" else self.right_frame_box.value()
        if preview is not None:
            return preview.absolute_frame_index(local_index)
        return local_index

    def refresh_previews(self) -> None:
        try:
            if not self.left_preview or not self.right_preview:
                self.update_offset_label()
                return

            left_index = self.left_frame_box.value() + 1
            right_index = self.right_frame_box.value() + 1
            left_path = self.left_preview.frame_path(left_index)
            right_path = self.right_preview.frame_path(right_index)
            if not left_path.exists() or not right_path.exists():
                self.update_offset_label()
                return

            left_pm = qpixmap_from_file(left_path)
            right_pm = qpixmap_from_file(right_path)
            self.left_caption.setText(
                f"Left preview — local {self.left_frame_box.value()} / {self.left_frame_box.maximum()} "
                f"→ absolute frame {self.left_preview.absolute_frame_index(self.left_frame_box.value())}"
            )
            self.right_caption.setText(
                f"Right preview — local {self.right_frame_box.value()} / {self.right_frame_box.maximum()} "
                f"→ absolute frame {self.right_preview.absolute_frame_index(self.right_frame_box.value())}"
            )
            self.left_image.setPixmap(left_pm.scaled(self.left_image.size(), Qt.KeepAspectRatio, Qt.FastTransformation))
            self.right_image.setPixmap(right_pm.scaled(self.right_image.size(), Qt.KeepAspectRatio, Qt.FastTransformation))
            self.update_offset_label()
        except Exception as e:
            self.show_error(e)

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        for lbl in (self.left_image, self.right_image):
            pm = lbl.pixmap()
            if pm is not None and not pm.isNull():
                lbl.setPixmap(pm.scaled(lbl.size(), Qt.KeepAspectRatio, Qt.FastTransformation))

    def update_offset_label(self) -> None:
        offset = self.right_frame_box.value() - self.left_frame_box.value()
        seconds = offset / max(self.fps_box.value(), 0.001)
        side = "trim RIGHT" if offset >= 0 else "trim LEFT"
        left_abs = self.current_absolute_frame("left")
        right_abs = self.current_absolute_frame("right")
        left_abs_sec = left_abs / max(self.fps_box.value(), 0.001)
        right_abs_sec = right_abs / max(self.fps_box.value(), 0.001)
        self.offset_label.setText(f"Offset: {offset} frames ({seconds:.6f} s) → {side}")
        self.absolute_label.setText(
            f"Absolute frames: L {left_abs} ({left_abs_sec:.3f}s), "
            f"R {right_abs} ({right_abs_sec:.3f}s)"
        )
        self.preview_scope_label.setText(f"Window start: {self.season_start_box.value():.3f} s")
        self.window_slider_label.setText(
            f"Window frame: {self.left_frame_box.value()} / {self.left_frame_box.maximum()}"
        )

    def fill_output_name(self) -> None:
        try:
            left = self.current_left_path()
            right = self.current_right_path()
            stem = infer_output_stem(left, right)
            if self.left_preview is not None:
                stem = with_left_frame_suffix(stem, self.current_absolute_frame("left"))
            self.output_stem_edit.setText(stem)
            self.save_settings()
        except Exception:
            pass

    def generate_image_script(self) -> None:
        try:
            left = self.current_left_path()
            right = self.current_right_path()
            if not left.exists() or not right.exists():
                raise ToolError("Choose valid left/right files first.")
            if self.left_info is None:
                self.left_info = ffprobe_video_info(left)
            if self.right_info is None:
                self.right_info = ffprobe_video_info(right)

            stem = self.output_stem_edit.text().strip()
            if not stem:
                raise ToolError("Output stem is empty.")

            left_frame = self.current_absolute_frame("left")
            stem_with_frame = with_left_frame_suffix(stem, left_frame)
            script_name = f"{stem_with_frame}__image.sh"
            jpg_name = f"{stem_with_frame}.jpg"
            script_path = self.work_dir / script_name

            self.save_settings()

            text = build_image_script_text(
                left_file=left,
                right_file=right,
                output_jpg=jpg_name,
                left_frame_index=self.current_absolute_frame("left"),
                right_frame_index=self.current_absolute_frame("right"),
                fps=self.fps_box.value(),
                src_left_width=self.left_info.width,
                src_left_height=self.left_info.height,
                src_right_width=self.right_info.width,
                src_right_height=self.right_info.height,
                target_res=self.target_res_box.value(),
                fov_left=self.left_fov_box.value(),
                fov_right=self.right_fov_box.value(),
                yaw_left=self.left_yaw_box.value(),
                yaw_right=self.right_yaw_box.value(),
                use_insv_pipeline=source_mode_for_pair(left, right) == "insv",
            )
            script_path.write_text(text, encoding="utf-8")
            script_path.chmod(0o755)
            self.log_msg(f"Generated image script: {script_path.name}")
            QMessageBox.information(
                self,
                APP_TITLE,
                f"Generated:\n{script_path}\n\nOutput image:\n{jpg_name}",
            )
        except Exception as e:
            self.show_error(e)

    def generate_batch_image_dump_script(self) -> None:
        try:
            if not self.left_preview or not self.right_preview:
                raise ToolError("Build preview first. The dump uses the current sync offset and preview frame counts.")

            left = self.current_left_path()
            right = self.current_right_path()
            if not left.exists() or not right.exists():
                raise ToolError("Choose valid left/right files first.")
            if self.left_info is None:
                self.left_info = ffprobe_video_info(left)
            if self.right_info is None:
                self.right_info = ffprobe_video_info(right)

            stem = self.output_stem_edit.text().strip()
            if not stem:
                raise ToolError("Output stem is empty.")
    
            out_dir_name = stem
            mode_tag = "synced" if self.synced_dump_box.isChecked() else "fast"
            script_name = f"{stem}__dump_preview_to_jpg__{mode_tag}.sh"
            script_path = self.work_dir / script_name

            self.save_settings()

            if self.synced_dump_box.isChecked():
                text = build_batch_image_dump_script_text(
                   left_file=left,
                    right_file=right,
                    output_dir_name=out_dir_name,
                    stem=stem,
                    season_start_seconds=self.season_start_box.value(),
                    offset_frames=self.right_frame_box.value() - self.left_frame_box.value(),
                    fps=self.fps_box.value(),
                    src_left_width=self.left_info.width,
                    src_left_height=self.left_info.height,
                    src_right_width=self.right_info.width,
                    src_right_height=self.right_info.height,
                    target_res=self.target_res_box.value(),
                    fov_left=self.left_fov_box.value(),
                    fov_right=self.right_fov_box.value(),
                    yaw_left=self.left_yaw_box.value(),
                    yaw_right=self.right_yaw_box.value(),
                    left_preview_count=self.left_preview.frame_count,
                    right_preview_count=self.right_preview.frame_count,
                    use_insv_pipeline=source_mode_for_pair(left, right) == "insv",
                )
            else:
                text = build_fast_batch_image_dump_script_text(
                    left_file=left,
                    right_file=right,
                    output_dir_name=out_dir_name,
                    stem=stem,
                    season_start_seconds=self.season_start_box.value(),
                    preview_seconds=self.preview_seconds_box.value(),
                    target_res=self.target_res_box.value(),
                    fov_left=self.left_fov_box.value(),
                    fov_right=self.right_fov_box.value(),
                    yaw_left=self.left_yaw_box.value(),
                    yaw_right=self.right_yaw_box.value(),
                    use_insv_pipeline=source_mode_for_pair(left, right) == "insv",
                )
 
            script_path.write_text(text, encoding="utf-8")
            script_path.chmod(0o755)
            self.log_msg(f"Generated batch image dump script: {script_path.name}")
            QMessageBox.information(
                self,
                APP_TITLE,
                f"Generated:\n{script_path}\n\nOutput folder:\n{self.work_dir / out_dir_name}",
            )
        except Exception as e:
            self.show_error(e)

    def generate_script(self) -> None:
        try:
            left = self.current_left_path()
            right = self.current_right_path()
            if not left.exists() or not right.exists():
                raise ToolError("Choose valid left/right files first.")
            if self.left_info is None:
                self.left_info = ffprobe_video_info(left)
            if self.right_info is None:
                self.right_info = ffprobe_video_info(right)

            stem = self.output_stem_edit.text().strip()
            if not stem:
                raise ToolError("Output stem is empty.")

            script_name = f"{stem}.sh"
            mp4_name = f"{stem}.mp4"
            script_path = self.work_dir / script_name

            self.save_settings()

            text = build_ffmpeg_script_text(
                left_file=left,
                right_file=right,
                output_mp4=mp4_name,
                offset_frames=self.right_frame_box.value() - self.left_frame_box.value(),
                fps=self.fps_box.value(),
                src_left_width=self.left_info.width,
                src_left_height=self.left_info.height,
                src_right_width=self.right_info.width,
                src_right_height=self.right_info.height,
                target_res=self.target_res_box.value(),
                fov_left=self.left_fov_box.value(),
                fov_right=self.right_fov_box.value(),
                yaw_left=self.left_yaw_box.value(),
                yaw_right=self.right_yaw_box.value(),
                test_seconds=self.test_seconds_box.value(),
                include_test_duration=self.use_test_duration_box.isChecked(),
                use_insv_pipeline=source_mode_for_pair(left, right) == "insv",
            )
            script_path.write_text(text, encoding="utf-8")
            script_path.chmod(0o755)
            self.log_msg(f"Generated script: {script_path.name}")
            QMessageBox.information(self, APP_TITLE, f"Generated:\n{script_path}\n\nOutput video:\n{mp4_name}")
        except Exception as e:
            self.show_error(e)

    def show_error(self, err: Exception) -> None:
        self.log_msg(f"ERROR: {err}")
        QMessageBox.critical(self, APP_TITLE, str(err))


def main() -> int:
    app = QApplication(sys.argv)
    w = MainWindow()
    w.showMaximized()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
