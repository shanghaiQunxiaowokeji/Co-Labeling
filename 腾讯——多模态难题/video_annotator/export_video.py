#!/usr/bin/env python3
"""根据标注 JSON 合成新视频。

在每个人工标记处，画面冻结指定秒数（默认 1 秒）并画出人工圈选的区域，
音频同步插入等长静音，其余部分保持原样。

做法：把原视频按标记点切成若干段分别重编码为 MPEG-TS，
冻结帧单独编码成等长 TS 片段，最后用 concat 解复用器拼接。
相比一次性 filter_complex（trim+split+concat）不会出现长视频的内存爆涨。
"""
from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from fractions import Fraction
from pathlib import Path
from typing import Any, Optional

from PIL import Image, ImageDraw, ImageFont

FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\arialbd.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]


def log(message: str) -> None:
    print(message, flush=True)


def require_tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise SystemExit(f"找不到 {name}，请先安装 ffmpeg 并加入 PATH。")
    return path


def run(cmd: list[str]) -> None:
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        log(result.stdout)
        raise SystemExit(f"命令失败（退出码 {result.returncode}）：{' '.join(cmd)}")


def probe(video: Path) -> dict[str, Any]:
    cmd = [
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_streams", "-show_format", str(video),
    ]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise SystemExit(f"ffprobe 失败：{result.stderr.strip()}")
    data = json.loads(result.stdout)
    video_stream = next((s for s in data["streams"] if s["codec_type"] == "video"), None)
    if video_stream is None:
        raise SystemExit("视频中没有视频流。")
    has_audio = any(s["codec_type"] == "audio" for s in data["streams"])
    rate = video_stream.get("r_frame_rate") or video_stream.get("avg_frame_rate") or "25/1"
    if rate in ("0/0", "0/1"):
        rate = "25/1"
    return {
        "fps": Fraction(rate),
        "width": int(video_stream["width"]),
        "height": int(video_stream["height"]),
        "has_audio": has_audio,
        "duration": float(data["format"].get("duration", 0.0)),
    }


# --------------------------------------------------------------------- 绘图
def load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for candidate in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    return ImageFont.load_default()


def normalize_box(points: list[list[float]]) -> tuple[float, float, float, float]:
    (x0, y0), (x1, y1) = points[0], points[1]
    return min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)


def draw_arrow(draw: ImageDraw.ImageDraw, points: list[list[float]], color: str, width: int) -> None:
    (x0, y0), (x1, y1) = points[0], points[1]
    angle = math.atan2(y1 - y0, x1 - x0)
    head = max(12.0, width * 5.0)
    spread = math.radians(24)
    tip = (x1, y1)
    left = (x1 - head * math.cos(angle - spread), y1 - head * math.sin(angle - spread))
    right = (x1 - head * math.cos(angle + spread), y1 - head * math.sin(angle + spread))
    shaft_end = (x1 - head * 0.75 * math.cos(angle), y1 - head * 0.75 * math.sin(angle))
    draw.line([(x0, y0), shaft_end], fill=color, width=width)
    draw.polygon([tip, left, right], fill=color)


def draw_shapes(image: Image.Image, shapes: list[dict[str, Any]]) -> Image.Image:
    canvas = image.convert("RGB")
    draw = ImageDraw.Draw(canvas)
    for shape in shapes:
        points = [[float(p[0]), float(p[1])] for p in shape.get("points", [])]
        if not points:
            continue
        color = shape.get("color", "#FF3B30")
        width = max(1, int(shape.get("width", 4)))
        kind = shape.get("type", "rect")
        if kind == "rect" and len(points) >= 2:
            draw.rectangle(normalize_box(points), outline=color, width=width)
        elif kind == "ellipse" and len(points) >= 2:
            draw.ellipse(normalize_box(points), outline=color, width=width)
        elif kind == "arrow" and len(points) >= 2:
            draw_arrow(draw, points, color, width)
        elif kind == "freehand" and len(points) >= 2:
            draw.line([tuple(p) for p in points], fill=color, width=width, joint="curve")
            radius = width / 2
            for px, py in (points[0], points[-1]):
                draw.ellipse((px - radius, py - radius, px + radius, py + radius), fill=color)

        label = (shape.get("label") or "").strip()
        if label:
            font = load_font(max(18, int(canvas.height / 28)))
            lx = min(p[0] for p in points)
            ly = min(p[1] for p in points)
            ty = max(4.0, ly - canvas.height / 24)
            draw.text((lx, ty), label, font=font, fill=color,
                      stroke_width=max(2, width // 2), stroke_fill="#000000")
    return canvas


# ----------------------------------------------------------------- 片段编码
def video_encode_args(fps: Fraction, crf: int, preset: str, has_audio: bool) -> list[str]:
    args = [
        "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
        "-pix_fmt", "yuv420p", "-fps_mode", "cfr", "-r", str(fps),
    ]
    if has_audio:
        args += ["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"]
    else:
        args += ["-an"]
    return args


def encode_source_segment(src: Path, start: float, duration: Optional[float], out: Path,
                          info: dict[str, Any], crf: int, preset: str) -> None:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-accurate_seek", "-ss", f"{start:.6f}"]
    if duration is not None:
        cmd += ["-t", f"{duration:.6f}"]
    cmd += ["-i", str(src), "-vf", "setsar=1"]
    cmd += video_encode_args(info["fps"], crf, preset, info["has_audio"])
    if info["has_audio"]:
        cmd += ["-af", "aresample=async=1:first_pts=0"]
    cmd += ["-f", "mpegts", str(out)]
    run(cmd)


def encode_freeze_segment(still: Path, hold: float, out: Path,
                          info: dict[str, Any], crf: int, preset: str) -> None:
    fps = info["fps"]
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-loop", "1", "-framerate", str(fps), "-t", f"{hold:.6f}", "-i", str(still)]
    if info["has_audio"]:
        cmd += ["-f", "lavfi", "-t", f"{hold:.6f}",
                "-i", "anullsrc=r=48000:cl=stereo"]
    cmd += ["-vf", "setsar=1"]
    cmd += video_encode_args(fps, crf, preset, info["has_audio"])
    cmd += ["-shortest", "-f", "mpegts", str(out)]
    run(cmd)


def extract_frame(src: Path, time_s: float, out: Path) -> None:
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-accurate_seek", "-ss", f"{time_s:.6f}", "-i", str(src),
         "-frames:v", "1", str(out)])


# --------------------------------------------------------------------- 主流程
def build_plan(marks: list[dict[str, Any]], fps: Fraction, duration: float,
               default_hold: float) -> list[dict[str, Any]]:
    seen: set[int] = set()
    cleaned: list[dict[str, Any]] = []
    for mark in sorted(marks, key=lambda m: int(m["frame"])):
        frame = int(mark["frame"])
        if frame in seen:
            continue
        seen.add(frame)
        time_s = frame / float(fps)
        if duration and time_s >= duration - 1e-6:
            log(f"[警告] 标记帧 {frame}（{time_s:.3f}s）超出视频时长，已跳过。")
            continue
        if not mark.get("shapes"):
            log(f"[警告] 标记帧 {frame} 没有圈选区域，已跳过。")
            continue
        cleaned.append({
            "frame": frame,
            "time": time_s,
            "hold": float(mark.get("hold") or default_hold),
            "shapes": mark["shapes"],
        })

    plan: list[dict[str, Any]] = []
    prev = 0.0
    for mark in cleaned:
        if mark["time"] - prev > 1e-6:
            plan.append({"kind": "video", "start": prev, "duration": mark["time"] - prev})
        plan.append({"kind": "freeze", "time": mark["time"], "hold": mark["hold"],
                     "shapes": mark["shapes"], "frame": mark["frame"]})
        prev = mark["time"]
    if not duration or duration - prev > 1e-6:
        plan.append({"kind": "video", "start": prev, "duration": None})
    return plan


def main() -> int:
    parser = argparse.ArgumentParser(description="根据标注合成带停顿与圈选的新视频")
    parser.add_argument("annotations", help="annotator.py 保存的标注 JSON")
    parser.add_argument("--video", help="覆盖标注里记录的源视频路径")
    parser.add_argument("--output", "-o", help="输出视频路径（.mp4 或 .webm）")
    parser.add_argument("--hold", type=float, default=1.0, help="标注未指定时的默认停顿秒数")
    parser.add_argument("--crf", type=int, default=20, help="H.264 画质，越小越好（默认 20）")
    parser.add_argument("--preset", default="veryfast", help="x264 预设（默认 veryfast）")
    parser.add_argument("--jobs", type=int, default=2, help="并行编码的片段数（默认 2）")
    parser.add_argument("--keep-temp", action="store_true", help="保留中间文件便于排查")
    parser.add_argument("--dry-run", action="store_true", help="只打印合成计划，不实际编码")
    args = parser.parse_args()

    require_tool("ffmpeg")
    require_tool("ffprobe")

    ann_path = Path(args.annotations).resolve()
    data = json.loads(ann_path.read_text(encoding="utf-8"))
    src = Path(args.video or data["video"])
    if not src.is_absolute():
        src = (ann_path.parent / src).resolve()
    if not src.exists():
        raise SystemExit(f"源视频不存在：{src}")

    info = probe(src)
    fps = info["fps"]
    log(f"源视频：{src.name}  {info['width']}x{info['height']}  "
        f"{float(fps):.3f} fps  {info['duration']:.2f}s  音频：{'有' if info['has_audio'] else '无'}")

    plan = build_plan(data.get("marks", []), fps, info["duration"], args.hold)
    freezes = [p for p in plan if p["kind"] == "freeze"]
    if not freezes:
        raise SystemExit("标注中没有可用的标记点。")
    extra = sum(p["hold"] for p in freezes)
    log(f"标记点 {len(freezes)} 个，共增加 {extra:.2f}s 停顿，"
        f"预计输出时长 {info['duration'] + extra:.2f}s")

    output = Path(args.output) if args.output else src.with_name(src.stem + "_annotated.mp4")
    output = output.resolve()

    if args.dry_run:
        for i, part in enumerate(plan):
            if part["kind"] == "video":
                dur = "到结尾" if part["duration"] is None else f"{part['duration']:.3f}s"
                log(f"  [{i:03d}] 原片 从 {part['start']:.3f}s 起 {dur}")
            else:
                log(f"  [{i:03d}] 冻结 帧#{part['frame']} @ {part['time']:.3f}s "
                    f"停 {part['hold']:g}s，{len(part['shapes'])} 个标记图形")
        return 0

    work = Path(tempfile.mkdtemp(prefix="colabel_export_", dir=str(output.parent)))
    log(f"中间文件目录：{work}")
    try:
        # 1) 抽取并绘制冻结帧
        for i, part in enumerate(plan):
            if part["kind"] != "freeze":
                continue
            raw = work / f"still_{i:03d}_raw.png"
            extract_frame(src, part["time"], raw)
            annotated = work / f"still_{i:03d}.png"
            with Image.open(raw) as image:
                draw_shapes(image, part["shapes"]).save(annotated)
            part["still"] = annotated
            log(f"  已绘制冻结帧 #{part['frame']} @ {part['time']:.3f}s")

        # 2) 逐段编码
        segments: list[Path] = [work / f"seg_{i:03d}.ts" for i in range(len(plan))]
        tasks = [(i, part, segments[i]) for i, part in enumerate(plan)]

        def encode(task) -> str:
            i, part, seg = task
            if part["kind"] == "video":
                encode_source_segment(src, part["start"], part["duration"], seg,
                                      info, args.crf, args.preset)
                dur = "到结尾" if part["duration"] is None else f"{part['duration']:.3f}s"
                return f"  [{i + 1}/{len(plan)}] 原片段 {part['start']:.3f}s + {dur}"
            encode_freeze_segment(part["still"], part["hold"], seg, info, args.crf, args.preset)
            return f"  [{i + 1}/{len(plan)}] 冻结段 帧#{part['frame']} 停 {part['hold']:g}s"

        log(f"开始编码 {len(plan)} 个片段（并行 {max(1, args.jobs)}）…")
        with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
            for message in pool.map(encode, tasks):
                log(message)

        # 3) 拼接
        list_file = work / "concat.txt"
        list_file.write_text(
            "".join(f"file '{seg.as_posix()}'\n" for seg in segments),
            encoding="utf-8",
        )
        log("拼接中…")
        concat = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                  "-f", "concat", "-safe", "0", "-i", str(list_file)]
        if output.suffix.lower() == ".webm":
            concat += ["-c:v", "libvpx-vp9", "-crf", "32", "-b:v", "0", "-row-mt", "1",
                       "-pix_fmt", "yuv420p"]
            concat += ["-c:a", "libopus", "-b:a", "128k"] if info["has_audio"] else ["-an"]
        else:
            concat += ["-c", "copy", "-movflags", "+faststart"]
        concat += [str(output)]
        run(concat)
        log(f"完成：{output}")
    finally:
        if args.keep_temp:
            log(f"已保留中间文件：{work}")
        else:
            shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
