#!/usr/bin/env python3
"""视频人工标注工具。

播放 webm/mp4 等视频，空格暂停，在画面上拖拽圈选区域；
每个暂停点与所圈区域会被记录到 JSON 标注文件，
随后由 export_video.py 合成新视频（标记处画面冻结若干秒并画出圈选区域）。
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

import cv2
import tkinter as tk
from tkinter import colorchooser, filedialog, messagebox, ttk

from PIL import Image, ImageTk

TOOLS: list[tuple[str, str]] = [
    ("矩形", "rect"),
    ("椭圆", "ellipse"),
    ("自由笔", "freehand"),
    ("箭头", "arrow"),
]
TOOL_LABELS = {code: name for name, code in TOOLS}
PALETTE = ["#FF3B30", "#FFD60A", "#34C759", "#0A84FF", "#FF2D95", "#FFFFFF"]
SPEEDS = ["0.25x", "0.5x", "1.0x", "1.5x", "2.0x"]
TEXT_ENTRY_CLASSES = {"Entry", "TEntry", "Spinbox", "TSpinbox", "Text", "TCombobox"}


def format_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    minutes, sec = divmod(seconds, 60)
    hours, minutes = divmod(int(minutes), 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{sec:06.3f}"
    return f"{minutes:02d}:{sec:06.3f}"


class Decoder(threading.Thread):
    """后台解码线程：负责顺序播放与跳转，解码结果通过队列交给 UI 线程。"""

    def __init__(self, path: str, out_queue: "queue.Queue[tuple[str, int, Any]]") -> None:
        super().__init__(daemon=True)
        self.path = path
        self.out_queue = out_queue
        self.cap = cv2.VideoCapture(path)
        if not self.cap.isOpened():
            raise RuntimeError(f"无法打开视频：{path}")
        fps = self.cap.get(cv2.CAP_PROP_FPS)
        self.fps = float(fps) if fps and fps > 0 else 25.0
        self.width = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.height = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        count = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT))
        self.frame_count = max(1, count)

        self._lock = threading.Lock()
        self._seek_to: Optional[int] = None
        self._playing = False
        self._speed = 1.0
        self._stop = threading.Event()
        self._next_index = 0

    # ---- 控制接口（UI 线程调用）----
    def play(self) -> None:
        with self._lock:
            self._playing = True

    def pause(self) -> None:
        with self._lock:
            self._playing = False

    @property
    def playing(self) -> bool:
        with self._lock:
            return self._playing

    def set_speed(self, speed: float) -> None:
        with self._lock:
            self._speed = max(0.05, speed)

    def seek(self, frame_index: int) -> None:
        with self._lock:
            self._seek_to = int(frame_index)

    def shutdown(self) -> None:
        self._stop.set()

    # ---- 线程主体 ----
    def run(self) -> None:
        next_due = time.perf_counter()
        while not self._stop.is_set():
            with self._lock:
                seek_to, self._seek_to = self._seek_to, None
                playing = self._playing
                speed = self._speed

            if seek_to is not None:
                self._do_seek(seek_to)
                next_due = time.perf_counter()
                continue

            if not playing:
                time.sleep(0.005)
                continue

            now = time.perf_counter()
            if now < next_due:
                time.sleep(min(0.004, next_due - now))
                continue

            index = self._next_index
            ok, frame = self.cap.read()
            if not ok:
                with self._lock:
                    self._playing = False
                self._emit(("end", index, None))
                continue
            self._next_index = index + 1
            self._emit(("frame", index, frame))

            next_due += 1.0 / (self.fps * speed)
            if next_due < now - 0.25:
                next_due = now

        self.cap.release()

    def _do_seek(self, index: int) -> None:
        index = max(0, min(index, self.frame_count - 1))
        self.cap.set(cv2.CAP_PROP_POS_FRAMES, index)
        ok, frame = self.cap.read()
        if ok:
            self._next_index = index + 1
            self._emit(("frame", index, frame))
        else:
            self._next_index = index
            self._emit(("end", index, None))

    def _emit(self, item: tuple[str, int, Any]) -> None:
        while not self._stop.is_set():
            try:
                self.out_queue.put(item, timeout=0.1)
                return
            except queue.Full:
                continue


class AnnotatorApp(tk.Tk):
    def __init__(self, video_path: str, max_width: int, max_height: int) -> None:
        super().__init__()
        self.video_path = str(Path(video_path).resolve())
        self.title(f"视频标注 - {Path(self.video_path).name}")

        self.frame_queue: "queue.Queue[tuple[str, int, Any]]" = queue.Queue(maxsize=4)
        self.decoder = Decoder(self.video_path, self.frame_queue)

        self.scale = min(
            max_width / self.decoder.width,
            max_height / self.decoder.height,
            1.0,
        )
        self.disp_w = max(1, int(round(self.decoder.width * self.scale)))
        self.disp_h = max(1, int(round(self.decoder.height * self.scale)))

        self.marks: dict[int, dict[str, Any]] = {}
        self.annotation_path = Path(str(Path(self.video_path).with_suffix("")) + ".annotations.json")

        self.current_index = 0
        self.current_frame = None
        self._photo: Optional[ImageTk.PhotoImage] = None
        self._timeline_guard = False
        self._drag: Optional[dict[str, Any]] = None
        self._sorted_frames: list[int] = []
        self._dirty = False

        self._build_ui()
        self.decoder.start()
        self.decoder.seek(0)
        self.after(10, self._pump)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        if self.annotation_path.exists():
            self._load_annotations(self.annotation_path, silent=True)

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=6)
        root.pack(fill=tk.BOTH, expand=True)

        left = ttk.Frame(root)
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.canvas = tk.Canvas(
            left,
            width=self.disp_w,
            height=self.disp_h,
            bg="#101010",
            highlightthickness=0,
            cursor="crosshair",
        )
        self.canvas.pack(fill=tk.BOTH, expand=False)
        self.image_item = self.canvas.create_image(0, 0, anchor=tk.NW)
        self.canvas.bind("<ButtonPress-1>", self._on_draw_start)
        self.canvas.bind("<B1-Motion>", self._on_draw_move)
        self.canvas.bind("<ButtonRelease-1>", self._on_draw_end)
        self.canvas.bind("<Button-3>", lambda _e: self._undo_shape())

        timeline = ttk.Frame(left)
        timeline.pack(fill=tk.X, pady=(6, 2))
        self.timeline = ttk.Scale(
            timeline,
            from_=0,
            to=max(1, self.decoder.frame_count - 1),
            orient=tk.HORIZONTAL,
            command=self._on_timeline,
        )
        self.timeline.pack(fill=tk.X)

        transport = ttk.Frame(left)
        transport.pack(fill=tk.X, pady=2)
        ttk.Button(transport, text="⏮ -1秒", width=8,
                   command=lambda: self._step(-int(self.decoder.fps))).pack(side=tk.LEFT)
        ttk.Button(transport, text="◀ 帧", width=6,
                   command=lambda: self._step(-1)).pack(side=tk.LEFT, padx=2)
        self.play_btn = ttk.Button(transport, text="▶ 播放 (空格)", width=14, command=self._toggle_play)
        self.play_btn.pack(side=tk.LEFT, padx=4)
        ttk.Button(transport, text="帧 ▶", width=6,
                   command=lambda: self._step(1)).pack(side=tk.LEFT, padx=2)
        ttk.Button(transport, text="+1秒 ⏭", width=8,
                   command=lambda: self._step(int(self.decoder.fps))).pack(side=tk.LEFT)

        ttk.Label(transport, text="  速度").pack(side=tk.LEFT)
        self.speed_box = ttk.Combobox(transport, values=SPEEDS, width=6, state="readonly")
        self.speed_box.set("1.0x")
        self.speed_box.bind("<<ComboboxSelected>>", self._on_speed)
        self.speed_box.pack(side=tk.LEFT, padx=4)

        self.status = ttk.Label(left, text="", anchor=tk.W)
        self.status.pack(fill=tk.X, pady=(4, 0))

        right = ttk.Frame(root, padding=(10, 0, 0, 0))
        right.pack(side=tk.LEFT, fill=tk.Y)

        tool_box = ttk.LabelFrame(right, text="圈选工具", padding=6)
        tool_box.pack(fill=tk.X)
        self.tool_var = tk.StringVar(value="rect")
        for name, code in TOOLS:
            ttk.Radiobutton(tool_box, text=name, value=code, variable=self.tool_var).pack(anchor=tk.W)

        style_box = ttk.LabelFrame(right, text="样式", padding=6)
        style_box.pack(fill=tk.X, pady=6)
        self.color_var = tk.StringVar(value=PALETTE[0])
        swatches = ttk.Frame(style_box)
        swatches.pack(fill=tk.X)
        for color in PALETTE:
            tk.Button(
                swatches, bg=color, width=2, relief=tk.RIDGE,
                command=lambda c=color: self._set_color(c),
            ).pack(side=tk.LEFT, padx=1)
        ttk.Button(style_box, text="自定义颜色", command=self._pick_color).pack(fill=tk.X, pady=(4, 2))
        self.color_preview = tk.Label(style_box, text="当前颜色", bg=PALETTE[0], fg="#000000")
        self.color_preview.pack(fill=tk.X, pady=(0, 4))

        width_row = ttk.Frame(style_box)
        width_row.pack(fill=tk.X)
        ttk.Label(width_row, text="线宽").pack(side=tk.LEFT)
        self.width_var = tk.IntVar(value=max(2, self.decoder.height // 240))
        ttk.Spinbox(width_row, from_=1, to=30, textvariable=self.width_var, width=5).pack(side=tk.RIGHT)

        label_row = ttk.Frame(style_box)
        label_row.pack(fill=tk.X, pady=(4, 0))
        ttk.Label(label_row, text="标签").pack(side=tk.LEFT)
        self.label_var = tk.StringVar(value="")
        ttk.Entry(label_row, textvariable=self.label_var, width=14).pack(side=tk.RIGHT)

        hold_row = ttk.Frame(style_box)
        hold_row.pack(fill=tk.X, pady=(4, 0))
        ttk.Label(hold_row, text="停顿秒数").pack(side=tk.LEFT)
        self.hold_var = tk.DoubleVar(value=1.0)
        ttk.Spinbox(hold_row, from_=0.1, to=30.0, increment=0.5,
                    textvariable=self.hold_var, width=5).pack(side=tk.RIGHT)

        mark_box = ttk.LabelFrame(right, text="标记点", padding=6)
        mark_box.pack(fill=tk.BOTH, expand=True, pady=6)
        list_frame = ttk.Frame(mark_box)
        list_frame.pack(fill=tk.BOTH, expand=True)
        scroll = ttk.Scrollbar(list_frame, orient=tk.VERTICAL)
        self.mark_list = tk.Listbox(list_frame, height=12, width=32, activestyle="dotbox",
                                    yscrollcommand=scroll.set, exportselection=False)
        scroll.config(command=self.mark_list.yview)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.mark_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.mark_list.bind("<<ListboxSelect>>", self._on_mark_select)

        ttk.Button(mark_box, text="撤销最后一笔 (Ctrl+Z)", command=self._undo_shape).pack(fill=tk.X, pady=(6, 2))
        ttk.Button(mark_box, text="删除选中标记 (Del)", command=self._delete_selected_mark).pack(fill=tk.X, pady=2)
        ttk.Button(mark_box, text="应用停顿秒数到选中标记", command=self._apply_hold_to_selected).pack(fill=tk.X, pady=2)
        ttk.Button(mark_box, text="清空全部标记", command=self._clear_marks).pack(fill=tk.X, pady=2)

        file_box = ttk.LabelFrame(right, text="文件", padding=6)
        file_box.pack(fill=tk.X)
        ttk.Button(file_box, text="保存标注 (Ctrl+S)", command=self._save_annotations).pack(fill=tk.X, pady=2)
        ttk.Button(file_box, text="载入标注", command=self._choose_and_load).pack(fill=tk.X, pady=2)
        ttk.Button(file_box, text="导出新视频…", command=self._export_video).pack(fill=tk.X, pady=2)

        self.bind_all("<space>", self._on_space)
        self.bind_all("<Left>", lambda e: self._key_step(e, -1))
        self.bind_all("<Right>", lambda e: self._key_step(e, 1))
        self.bind_all("<Shift-Left>", lambda e: self._key_step(e, -int(self.decoder.fps)))
        self.bind_all("<Shift-Right>", lambda e: self._key_step(e, int(self.decoder.fps)))
        self.bind_all("<Control-s>", lambda e: self._save_annotations())
        self.bind_all("<Control-z>", lambda e: self._undo_shape())
        self.bind_all("<Delete>", self._on_delete_key)

    # -------------------------------------------------------------- 播放控制
    def _focus_in_text(self) -> bool:
        widget = self.focus_get()
        return widget is not None and widget.winfo_class() in TEXT_ENTRY_CLASSES

    def _on_space(self, _event=None) -> Optional[str]:
        if self._focus_in_text():
            return None
        self._toggle_play()
        return "break"

    def _key_step(self, _event, delta: int) -> Optional[str]:
        if self._focus_in_text():
            return None
        self._step(delta)
        return "break"

    def _on_delete_key(self, _event=None) -> Optional[str]:
        if self._focus_in_text():
            return None
        self._delete_selected_mark()
        return "break"

    def _toggle_play(self) -> None:
        if self.decoder.playing:
            self.decoder.pause()
        else:
            if self.current_index >= self.decoder.frame_count - 1:
                self.decoder.seek(0)
            self.decoder.play()
        self._update_status()

    def _step(self, delta: int) -> None:
        self.decoder.pause()
        self.decoder.seek(self.current_index + delta)
        self._update_status()

    def _on_speed(self, _event=None) -> None:
        self.decoder.set_speed(float(self.speed_box.get().rstrip("x")))

    def _on_timeline(self, value: str) -> None:
        if self._timeline_guard:
            return
        target = int(float(value))
        if target != self.current_index:
            self.decoder.pause()
            self.decoder.seek(target)

    # ------------------------------------------------------------ 帧渲染循环
    def _pump(self) -> None:
        latest: Optional[tuple[str, int, Any]] = None
        try:
            while True:
                latest = self.frame_queue.get_nowait()
        except queue.Empty:
            pass

        if latest is not None:
            kind, index, frame = latest
            if kind == "frame":
                self.current_index = index
                self.current_frame = frame
                self._render_frame(frame)
                self._render_overlay()
                self._update_timeline()
            self._update_status()
        self.after(10, self._pump)

    def _render_frame(self, frame) -> None:
        small = cv2.resize(frame, (self.disp_w, self.disp_h), interpolation=cv2.INTER_AREA)
        rgb = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)
        self._photo = ImageTk.PhotoImage(Image.fromarray(rgb))
        self.canvas.itemconfig(self.image_item, image=self._photo)

    def _render_overlay(self) -> None:
        self.canvas.delete("ann")
        mark = self.marks.get(self.current_index)
        shapes = list(mark["shapes"]) if mark else []
        if self._drag is not None:
            shapes = shapes + [self._drag]
        for shape in shapes:
            self._draw_shape_on_canvas(shape)

    def _draw_shape_on_canvas(self, shape: dict[str, Any]) -> None:
        pts = [(x * self.scale, y * self.scale) for x, y in shape["points"]]
        if not pts:
            return
        color = shape.get("color", "#FF3B30")
        width = max(1, int(round(shape.get("width", 4) * self.scale)))
        kind = shape.get("type", "rect")
        if kind in ("rect", "ellipse") and len(pts) >= 2:
            x0, y0 = pts[0]
            x1, y1 = pts[1]
            box = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
            if kind == "rect":
                self.canvas.create_rectangle(*box, outline=color, width=width, tags="ann")
            else:
                self.canvas.create_oval(*box, outline=color, width=width, tags="ann")
        elif kind == "arrow" and len(pts) >= 2:
            self.canvas.create_line(*pts[0], *pts[1], fill=color, width=width,
                                    arrow=tk.LAST,
                                    arrowshape=(width * 4, width * 5, width * 2),
                                    tags="ann")
        elif kind == "freehand" and len(pts) >= 2:
            flat = [c for p in pts for c in p]
            self.canvas.create_line(*flat, fill=color, width=width, joinstyle=tk.ROUND,
                                    capstyle=tk.ROUND, smooth=True, tags="ann")
        label = shape.get("label", "")
        if label:
            lx = min(p[0] for p in pts)
            ly = min(p[1] for p in pts)
            self.canvas.create_text(lx + 2, ly - 10, text=label, fill=color,
                                    anchor=tk.NW, font=("Microsoft YaHei", 11, "bold"), tags="ann")

    def _update_timeline(self) -> None:
        self._timeline_guard = True
        try:
            self.timeline.set(self.current_index)
        finally:
            self._timeline_guard = False

    def _update_status(self) -> None:
        t = self.current_index / self.decoder.fps
        total = self.decoder.frame_count / self.decoder.fps
        state = "播放中" if self.decoder.playing else "已暂停"
        self.play_btn.config(text="⏸ 暂停 (空格)" if self.decoder.playing else "▶ 播放 (空格)")
        dirty = "  *未保存" if self._dirty else ""
        self.status.config(
            text=(f"{state}  |  时间 {format_time(t)} / {format_time(total)}"
                  f"  |  帧 {self.current_index} / {self.decoder.frame_count - 1}"
                  f"  |  标记 {len(self.marks)} 个{dirty}")
        )

    # ------------------------------------------------------------------ 圈选
    def _canvas_to_video(self, x: float, y: float) -> tuple[float, float]:
        vx = min(max(x / self.scale, 0.0), self.decoder.width - 1.0)
        vy = min(max(y / self.scale, 0.0), self.decoder.height - 1.0)
        return round(vx, 1), round(vy, 1)

    def _on_draw_start(self, event) -> None:
        if self.decoder.playing:
            self.decoder.pause()
            self._update_status()
        point = self._canvas_to_video(event.x, event.y)
        self._drag = {
            "type": self.tool_var.get(),
            "points": [point, point],
            "color": self.color_var.get(),
            "width": int(self.width_var.get()),
            "label": self.label_var.get().strip(),
        }
        self._render_overlay()

    def _on_draw_move(self, event) -> None:
        if self._drag is None:
            return
        point = self._canvas_to_video(event.x, event.y)
        if self._drag["type"] == "freehand":
            last = self._drag["points"][-1]
            if abs(point[0] - last[0]) + abs(point[1] - last[1]) >= 2:
                self._drag["points"].append(point)
        else:
            self._drag["points"][1] = point
        self._render_overlay()

    def _on_draw_end(self, _event) -> None:
        if self._drag is None:
            return
        shape = self._drag
        self._drag = None
        pts = shape["points"]
        if shape["type"] == "freehand":
            valid = len(pts) >= 3
        elif shape["type"] == "arrow":
            valid = abs(pts[1][0] - pts[0][0]) + abs(pts[1][1] - pts[0][1]) >= 10
        else:
            valid = abs(pts[1][0] - pts[0][0]) >= 3 and abs(pts[1][1] - pts[0][1]) >= 3
        if not valid:
            self._render_overlay()
            return
        mark = self.marks.get(self.current_index)
        if mark is None:
            mark = {"frame": self.current_index, "hold": float(self.hold_var.get()), "shapes": []}
            self.marks[self.current_index] = mark
        mark["shapes"].append(shape)
        self._dirty = True
        self._refresh_mark_list(select_frame=self.current_index)
        self._render_overlay()
        self._update_status()

    def _undo_shape(self) -> None:
        mark = self.marks.get(self.current_index)
        if not mark or not mark["shapes"]:
            return
        mark["shapes"].pop()
        if not mark["shapes"]:
            del self.marks[self.current_index]
        self._dirty = True
        self._refresh_mark_list(select_frame=self.current_index)
        self._render_overlay()
        self._update_status()

    # -------------------------------------------------------------- 标记管理
    def _refresh_mark_list(self, select_frame: Optional[int] = None) -> None:
        self._sorted_frames = sorted(self.marks)
        self.mark_list.delete(0, tk.END)
        for frame in self._sorted_frames:
            mark = self.marks[frame]
            kinds = "/".join(dict.fromkeys(TOOL_LABELS.get(s["type"], s["type"]) for s in mark["shapes"]))
            labels = [s.get("label", "") for s in mark["shapes"] if s.get("label")]
            suffix = f"  {labels[0]}" if labels else ""
            self.mark_list.insert(
                tk.END,
                f"{format_time(frame / self.decoder.fps)}  #{frame}  停{mark['hold']:g}s  {kinds}{suffix}",
            )
        if select_frame is not None and select_frame in self.marks:
            idx = self._sorted_frames.index(select_frame)
            self.mark_list.selection_clear(0, tk.END)
            self.mark_list.selection_set(idx)
            self.mark_list.see(idx)

    def _selected_frame(self) -> Optional[int]:
        selection = self.mark_list.curselection()
        if not selection:
            return None
        return self._sorted_frames[selection[0]]

    def _on_mark_select(self, _event=None) -> None:
        frame = self._selected_frame()
        if frame is None or frame == self.current_index:
            return
        self.decoder.pause()
        self.decoder.seek(frame)

    def _delete_selected_mark(self) -> None:
        frame = self._selected_frame()
        if frame is None:
            frame = self.current_index
        if frame not in self.marks:
            return
        del self.marks[frame]
        self._dirty = True
        self._refresh_mark_list()
        self._render_overlay()
        self._update_status()

    def _apply_hold_to_selected(self) -> None:
        frame = self._selected_frame()
        if frame is None:
            frame = self.current_index
        if frame not in self.marks:
            return
        self.marks[frame]["hold"] = float(self.hold_var.get())
        self._dirty = True
        self._refresh_mark_list(select_frame=frame)

    def _clear_marks(self) -> None:
        if not self.marks:
            return
        if not messagebox.askyesno("确认", f"确定清空全部 {len(self.marks)} 个标记？"):
            return
        self.marks.clear()
        self._dirty = True
        self._refresh_mark_list()
        self._render_overlay()
        self._update_status()

    def _set_color(self, color: str) -> None:
        self.color_var.set(color)
        self.color_preview.config(bg=color)

    def _pick_color(self) -> None:
        _rgb, hex_color = colorchooser.askcolor(color=self.color_var.get(), title="选择颜色")
        if hex_color:
            self._set_color(hex_color)

    # ------------------------------------------------------------------ 存取
    def _annotation_payload(self) -> dict[str, Any]:
        marks = []
        for frame in sorted(self.marks):
            mark = self.marks[frame]
            marks.append({
                "frame": frame,
                "time": round(frame / self.decoder.fps, 4),
                "hold": float(mark["hold"]),
                "shapes": [
                    {
                        "type": s["type"],
                        "points": [[float(x), float(y)] for x, y in s["points"]],
                        "color": s.get("color", "#FF3B30"),
                        "width": int(s.get("width", 4)),
                        "label": s.get("label", ""),
                    }
                    for s in mark["shapes"]
                ],
            })
        return {
            "version": 1,
            "video": self.video_path,
            "fps": self.decoder.fps,
            "width": self.decoder.width,
            "height": self.decoder.height,
            "frame_count": self.decoder.frame_count,
            "marks": marks,
        }

    def _save_annotations(self, path: Optional[Path] = None) -> Path:
        target = Path(path) if path else self.annotation_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self._annotation_payload(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self.annotation_path = target
        self._dirty = False
        self._update_status()
        self.status.config(text=f"已保存标注：{target}")
        return target

    def _choose_and_load(self) -> None:
        path = filedialog.askopenfilename(
            title="载入标注文件",
            filetypes=[("标注 JSON", "*.json"), ("所有文件", "*.*")],
            initialdir=str(Path(self.video_path).parent),
        )
        if path:
            self._load_annotations(Path(path))

    def _load_annotations(self, path: Path, silent: bool = False) -> None:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            if not silent:
                messagebox.showerror("载入失败", str(exc))
            return
        self.marks = {}
        for mark in data.get("marks", []):
            frame = int(mark["frame"])
            self.marks[frame] = {
                "frame": frame,
                "hold": float(mark.get("hold", 1.0)),
                "shapes": [
                    {
                        "type": s.get("type", "rect"),
                        "points": [(float(p[0]), float(p[1])) for p in s.get("points", [])],
                        "color": s.get("color", "#FF3B30"),
                        "width": int(s.get("width", 4)),
                        "label": s.get("label", ""),
                    }
                    for s in mark.get("shapes", [])
                ],
            }
        self.annotation_path = Path(path)
        self._dirty = False
        self._refresh_mark_list()
        self._render_overlay()
        self._update_status()

    # ------------------------------------------------------------------ 导出
    def _export_video(self) -> None:
        if not self.marks:
            messagebox.showwarning("无标记", "还没有任何标记点，请先暂停并圈选区域。")
            return
        self.decoder.pause()
        annotation_file = self._save_annotations()
        default_name = Path(self.video_path).stem + "_annotated.mp4"
        output = filedialog.asksaveasfilename(
            title="导出新视频",
            defaultextension=".mp4",
            initialfile=default_name,
            initialdir=str(Path(self.video_path).parent),
            filetypes=[("MP4 视频", "*.mp4"), ("WebM 视频", "*.webm")],
        )
        if not output:
            return
        script = Path(__file__).with_name("export_video.py")
        cmd = [sys.executable, "-u", str(script), str(annotation_file), "--output", output]
        self._run_export(cmd)

    def _run_export(self, cmd: list[str]) -> None:
        window = tk.Toplevel(self)
        window.title("导出进度")
        text = tk.Text(window, width=110, height=28, bg="#111111", fg="#d8d8d8")
        text.pack(fill=tk.BOTH, expand=True)
        text.insert(tk.END, " ".join(cmd) + "\n\n")

        log_queue: "queue.Queue[Optional[str]]" = queue.Queue()

        def reader() -> None:
            env = dict(os.environ, PYTHONIOENCODING="utf-8")
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1, env=env,
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                log_queue.put(line)
            proc.wait()
            log_queue.put(f"\n=== 进程结束，退出码 {proc.returncode} ===\n")
            log_queue.put(None)

        def drain() -> None:
            try:
                while True:
                    line = log_queue.get_nowait()
                    if line is None:
                        return
                    text.insert(tk.END, line)
                    text.see(tk.END)
            except queue.Empty:
                pass
            window.after(120, drain)

        threading.Thread(target=reader, daemon=True).start()
        drain()

    def _on_close(self) -> None:
        if self._dirty and self.marks:
            answer = messagebox.askyesnocancel("退出", "标注尚未保存，是否保存后退出？")
            if answer is None:
                return
            if answer:
                self._save_annotations()
        self.decoder.shutdown()
        self.destroy()


def main() -> int:
    parser = argparse.ArgumentParser(description="视频暂停圈选标注工具")
    parser.add_argument("video", nargs="?", help="视频文件路径（webm/mp4/...）")
    parser.add_argument("--max-width", type=int, default=1280, help="画面显示最大宽度")
    parser.add_argument("--max-height", type=int, default=720, help="画面显示最大高度")
    args = parser.parse_args()

    video = args.video
    if not video:
        root = tk.Tk()
        root.withdraw()
        video = filedialog.askopenfilename(
            title="选择视频",
            filetypes=[("视频文件", "*.webm *.mp4 *.mkv *.mov *.avi"), ("所有文件", "*.*")],
        )
        root.destroy()
    if not video:
        print("未选择视频。", file=sys.stderr)
        return 1
    if not Path(video).exists():
        print(f"文件不存在：{video}", file=sys.stderr)
        return 1

    app = AnnotatorApp(video, args.max_width, args.max_height)
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
