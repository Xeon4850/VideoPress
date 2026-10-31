# -*- coding: utf-8 -*-
"""视频压缩工具：Windows / FFmpeg / NVENC / QSV / CPU"""
from __future__ import annotations

import json
import math
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    BaseTk = TkinterDnD.Tk
    HAS_DND = True
except ImportError:
    DND_FILES = None
    BaseTk = tk.Tk
    HAS_DND = False

APP_NAME = "视频压缩工具"
VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".flv", ".wmv", ".m4v", ".ts", ".mts", ".m2ts", ".webm", ".mpg", ".mpeg", ".3gp", ".vob"}
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def app_dir() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def find_executable(name: str) -> Optional[str]:
    exe = name + (".exe" if os.name == "nt" else "")
    candidates = [app_dir() / exe, app_dir() / "ffmpeg" / "bin" / exe,
                  app_dir().parent / "ffmpeg" / "bin" / exe]
    for p in candidates:
        if p.is_file():
            return str(p)
    return shutil.which(name)


def safe_stem(name: str) -> str:
    """只清理 Windows 非法字符，保留 Unicode、空格、括号及多数复杂名称。"""
    stem = Path(name).stem
    stem = INVALID_CHARS.sub("_", stem).strip().rstrip(". ")
    if not stem:
        stem = "未命名视频"
    if stem.upper() in RESERVED_NAMES:
        stem = "_" + stem
    return stem[:180].rstrip(". ") or "未命名视频"


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for i in range(2, 10000):
        candidate = path.with_name(f"{path.stem}_{i}{path.suffix}")
        if not candidate.exists():
            return candidate
    return path.with_name(f"{path.stem}_{uuid.uuid4().hex[:8]}{path.suffix}")


def human_size(value: float) -> str:
    if not value or value < 0:
        return "—"
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    while value >= 1024 and i < len(units) - 1:
        value /= 1024
        i += 1
    return f"{value:.1f} {units[i]}"


def human_rate(value: Optional[float]) -> str:
    return "—" if not value else f"{value / 1_000_000:.2f} Mbps"


def human_time(seconds: float) -> str:
    if not math.isfinite(seconds) or seconds < 0:
        return "—"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def split_drop_paths(data: str) -> list[str]:
    """解析 Tk DND 列表；花括号用于保护包含空格的路径。"""
    paths, token, brace = [], [], False
    for ch in data:
        if ch == "{" and not brace:
            brace = True
        elif ch == "}" and brace:
            brace = False
        elif ch.isspace() and not brace:
            if token:
                paths.append("".join(token))
                token = []
        else:
            token.append(ch)
    if token:
        paths.append("".join(token))
    return paths


@dataclass
class VideoInfo:
    duration: float = 0
    size: int = 0
    bitrate: Optional[int] = None
    width: int = 0
    height: int = 0
    codec: str = "—"
    fps: str = "—"


@dataclass
class VideoItem:
    path: Path
    info: VideoInfo = field(default_factory=VideoInfo)
    status: str = "等待"
    progress: float = 0
    output: Optional[Path] = None
    ratio_text: str = "—"
    error: str = ""
    encoder: str = ""


class FFmpegService:
    def __init__(self):
        self.builds: list[dict] = []
        self.ffmpeg = None
        self.ffprobe = None
        self.available: list[str] = []
        self.detect_error = ""
        self.version = "未知版本"
        self._discover_builds()

    def _discover_builds(self):
        exe = ".exe" if os.name == "nt" else ""
        candidates = [
            ("latest", app_dir() / "ffmpeg-latest" / "bin"),
            ("current", app_dir() / "ffmpeg" / "bin"),
            ("compat", app_dir() / "ffmpeg-compat" / "bin"),
            ("local", app_dir()),
        ]
        seen = set()
        for name, directory in candidates:
            ffmpeg, ffprobe = directory / f"ffmpeg{exe}", directory / f"ffprobe{exe}"
            key = os.path.normcase(str(ffmpeg))
            if ffmpeg.is_file() and ffprobe.is_file() and key not in seen:
                self.builds.append({"name": name, "ffmpeg": str(ffmpeg), "ffprobe": str(ffprobe),
                                    "version": "未知版本", "available": []})
                seen.add(key)
        if not self.builds:
            ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
            if ffmpeg and ffprobe:
                self.builds.append({"name": "system", "ffmpeg": ffmpeg, "ffprobe": ffprobe,
                                    "version": "未知版本", "available": []})
        if self.builds:
            self.select_build(self.builds[0])

    def select_build(self, build: dict):
        self.ffmpeg, self.ffprobe = build["ffmpeg"], build["ffprobe"]

    def validate(self):
        if not self.builds:
            raise RuntimeError("未找到 FFmpeg。请先运行 setup.bat。")

    def run(self, args: list[str], timeout=30) -> subprocess.CompletedProcess:
        return subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, creationflags=CREATE_NO_WINDOW)

    def detect_encoders(self):
        self.validate()
        self.available = []
        self.detect_error = ""
        versions, nvenc_failures = [], []
        diagnostics = ["VideoPress hardware detection", ""]
        # 同时记录 NVIDIA 驱动状态；找不到 nvidia-smi 不等于没有显卡，但日志会明确显示。
        nvidia_smi = shutil.which("nvidia-smi")
        if not nvidia_smi and os.name == "nt":
            candidate = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "nvidia-smi.exe"
            if candidate.is_file():
                nvidia_smi = str(candidate)
        diagnostics.append(f"nvidia-smi: {nvidia_smi or 'NOT FOUND'}")
        if nvidia_smi:
            try:
                smi = self.run([nvidia_smi], timeout=10)
                diagnostics.extend([smi.stdout, smi.stderr])
            except Exception as exc:
                diagnostics.append(f"nvidia-smi error: {exc}")
        tests = [("NVENC（NVIDIA）", "h264_nvenc"), ("QSV（Intel 核显）", "h264_qsv")]
        for build in self.builds:
            self.select_build(build)
            version_result = self.run([self.ffmpeg, "-version"], timeout=10)
            version_output = (version_result.stdout or version_result.stderr).strip()
            lines = version_output.splitlines()
            first_line = lines[0] if lines else "未知版本"
            match = re.search(r"ffmpeg version\s+([^\s]+)", first_line)
            build["version"] = match.group(1) if match else first_line
            versions.append(build["version"])
            build["available"] = []
            diagnostics.extend(["", "=" * 60, f'BUILD: {build["name"]}', f'PATH: {self.ffmpeg}', version_output])
            # 实际编码一帧，完整记录 stdout/stderr 和退出码。
            for label, encoder in tests:
                cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "verbose", "-f", "lavfi",
                       "-i", "testsrc2=size=128x128:rate=1", "-frames:v", "1", "-c:v", encoder,
                       "-f", "null", "-"]
                diagnostics.extend(["", f'TEST: {encoder}', f'COMMAND: {subprocess.list2cmdline(cmd)}'])
                try:
                    result = self.run(cmd, timeout=15)
                    diagnostics.extend([f"EXIT: {result.returncode}", "STDOUT:", result.stdout, "STDERR:", result.stderr])
                    if result.returncode == 0:
                        build["available"].append(label)
                    elif encoder == "h264_nvenc":
                        nvenc_failures.append(f'FFmpeg {build["version"]}: {result.stderr.strip()[-500:]}')
                except (subprocess.TimeoutExpired, OSError) as exc:
                    diagnostics.append(f"EXCEPTION: {exc}")
                    if encoder == "h264_nvenc":
                        nvenc_failures.append(f'FFmpeg {build["version"]}: {exc}')
        nvenc_ok = any("NVENC（NVIDIA）" in b["available"] for b in self.builds)
        # NVENC 始终显示，允许手动强制尝试；自动模式仍只使用通过实测的构建。
        self.available.append("NVENC（NVIDIA）")
        if any("QSV（Intel 核显）" in b["available"] for b in self.builds):
            self.available.append("QSV（Intel 核显）")
        self.available.append("CPU（libx264）")
        self.version = " / ".join(dict.fromkeys(versions))
        if not nvenc_ok and nvenc_failures:
            self.detect_error = ("两套 FFmpeg 的 NVENC 快速测试均未通过，但自动模式仍会正式尝试 NVENC；"
                                 "也可手动选择 NVENC 强制重试。\n"
                                 "完整原因已写入 hardware_detection.log，请将该文件发给我。\n\n" + "\n".join(nvenc_failures))
        try:
            log_dir = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else app_dir()
            (log_dir / "hardware_detection.log").write_text("\n".join(diagnostics), encoding="utf-8")
        except OSError:
            pass
        return self.available

    def probe(self, path: Path) -> VideoInfo:
        self.validate()
        cmd = [self.ffprobe, "-v", "error", "-show_entries",
               "format=duration,size,bit_rate:stream=index,codec_type,codec_name,width,height,avg_frame_rate,bit_rate,duration",
               "-of", "json", str(path)]
        result = self.run(cmd, timeout=60)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip()[-800:] or "无法读取视频属性")
        data = json.loads(result.stdout)
        fmt = data.get("format", {})
        streams = data.get("streams", [])
        video = next((s for s in streams if s.get("codec_type") == "video"), None)
        if not video:
            raise RuntimeError("文件中没有视频流")
        def number(value, default=0.0):
            try:
                return float(value)
            except (TypeError, ValueError):
                return default
        duration = number(fmt.get("duration"))
        if duration <= 0:
            durations = [number(s.get("duration")) for s in streams]
            duration = max(durations, default=0)
        size = int(number(fmt.get("size"), path.stat().st_size))
        bitrate = int(number(fmt.get("bit_rate"))) or None
        fps_raw = video.get("avg_frame_rate", "0/1")
        try:
            a, b = fps_raw.split("/")
            fps = f"{float(a) / float(b):.3f}".rstrip("0").rstrip(".") if float(b) else "—"
        except Exception:
            fps = "—"
        return VideoInfo(duration=duration, size=size, bitrate=bitrate,
                         width=int(number(video.get("width"))), height=int(number(video.get("height"))),
                         codec=video.get("codec_name", "—"), fps=fps)

    def encoder_args(self, label: str, bitrate_k: int, cpu_threads: int = 0) -> list[str]:
        rate = [f"-b:v", f"{bitrate_k}k", "-maxrate", f"{int(bitrate_k * 1.4)}k",
                "-bufsize", f"{bitrate_k * 2}k"]
        if "NVENC" in label:
            return ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", *rate]
        if "QSV" in label:
            return ["-c:v", "h264_qsv", "-preset", "medium", *rate]
        return ["-c:v", "libx264", "-preset", "medium", *(["-threads", str(cpu_threads)] if cpu_threads else []), *rate]

    def build_command(self, item: VideoItem, output: Path, ratio: int, encoder: str, cpu_threads: int = 0) -> list[str]:
        info = item.info
        audio_k = 128
        if info.duration > 0:
            target_bits = info.size * 8 * ratio / 100
            video_k = int(target_bits / info.duration / 1000) - audio_k
        else:
            source_k = int((info.bitrate or 4_000_000) / 1000)
            video_k = int(source_k * ratio / 100) - audio_k
        video_k = max(100, min(video_k, 50_000))
        cmd = [self.ffmpeg, "-hide_banner", "-y", "-i", str(item.path), "-map", "0:v:0", "-map", "0:a?",
               *self.encoder_args(encoder, video_k, cpu_threads), "-c:a", "aac", "-b:a", f"{audio_k}k",
               "-movflags", "+faststart", "-max_muxing_queue_size", "1024", "-progress", "pipe:1",
               "-nostats", str(output)]
        return cmd


class VideoCompressorApp(BaseTk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1180x760")
        self.minsize(980, 650)
        self.service = FFmpegService()
        self.items: list[VideoItem] = []
        self.item_by_iid: dict[str, VideoItem] = {}
        self.events = queue.Queue()
        self.worker: Optional[threading.Thread] = None
        self.current_processes: set[subprocess.Popen] = set()
        self.process_lock = threading.Lock()
        self.output_lock = threading.Lock()
        self.cancel_current = threading.Event()
        self.stop_after_current = threading.Event()
        self.running = False
        self.output_mode = tk.StringVar(value="source")
        self.custom_dir = tk.StringVar()
        self.ratio = tk.IntVar(value=45)
        self.recursive = tk.BooleanVar(value=True)
        self.encoder = tk.StringVar(value="自动")
        self.parallel_enabled = tk.BooleanVar(value=True)
        self.cpu_workers = tk.IntVar(value=2)
        self.hardware_text = tk.StringVar(value="正在检测编码器…")
        self.summary = tk.StringVar(value="拖入视频或目录，或点击上方按钮添加")
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(100, self._poll_events)
        self.after(150, self._init_engine)

    def _build_ui(self):
        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        main = ttk.Frame(self, padding=14)
        main.pack(fill="both", expand=True)
        top = ttk.Frame(main)
        top.pack(fill="x")
        ttk.Button(top, text="添加视频", command=self.add_files).pack(side="left")
        ttk.Button(top, text="添加目录", command=self.add_folder).pack(side="left", padx=(8, 0))
        ttk.Button(top, text="移除选中", command=self.remove_selected).pack(side="left", padx=(8, 0))
        ttk.Button(top, text="清空列表", command=self.clear_items).pack(side="left", padx=(8, 0))
        ttk.Checkbutton(top, text="递归扫描子目录", variable=self.recursive).pack(side="left", padx=18)
        ttk.Label(top, textvariable=self.hardware_text, foreground="#246b45").pack(side="right")

        drop = ttk.Label(main, text="将视频文件或整个目录拖到这里\n支持本地磁盘、映射网络驱动器及 \\\\NAS\\共享路径",
                         anchor="center", justify="center", padding=18, relief="groove")
        drop.pack(fill="x", pady=12)
        if HAS_DND:
            for widget in (drop, self):
                widget.drop_target_register(DND_FILES)
                widget.dnd_bind("<<Drop>>", self.on_drop)
        else:
            drop.configure(text="请使用“添加视频／添加目录”按钮\n（安装 tkinterdnd2 后支持拖拽）")

        columns = ("name", "resolution", "bitrate", "size", "status", "progress", "ratio")
        self.tree = ttk.Treeview(main, columns=columns, show="headings", selectmode="extended")
        headings = {"name": "文件", "resolution": "分辨率 / 编码", "bitrate": "原码率", "size": "原大小",
                    "status": "状态 / 输出", "progress": "进度", "ratio": "压缩后 / 原文件"}
        widths = {"name": 310, "resolution": 130, "bitrate": 95, "size": 90, "status": 250, "progress": 90, "ratio": 120}
        for col in columns:
            self.tree.heading(col, text=headings[col])
            self.tree.column(col, width=widths[col], minwidth=70, anchor="w")
        scroll = ttk.Scrollbar(main, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="top", fill="both", expand=True)
        scroll.place(in_=self.tree, relx=1.0, rely=0, relheight=1.0, anchor="ne")
        self.tree.tag_configure("done", foreground="#18743a")
        self.tree.tag_configure("error", foreground="#b3261e")

        settings = ttk.LabelFrame(main, text="压缩设置", padding=10)
        settings.pack(fill="x", pady=(12, 0))
        ttk.Label(settings, text="目标大小：").grid(row=0, column=0, sticky="w")
        self.ratio_label = ttk.Label(settings, text="原文件的 45%", width=16)
        self.ratio_label.grid(row=0, column=1, sticky="w")
        scale = ttk.Scale(settings, from_=10, to=90, variable=self.ratio, command=self._ratio_changed)
        scale.grid(row=0, column=2, sticky="ew", padx=8)
        ttk.Label(settings, text="编码器：").grid(row=0, column=3, padx=(12, 4))
        self.encoder_box = ttk.Combobox(settings, textvariable=self.encoder, state="readonly", width=22, values=["自动"])
        self.encoder_box.grid(row=0, column=4, sticky="w")
        ttk.Checkbutton(settings, text="自动模式三路并行", variable=self.parallel_enabled).grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Label(settings, text="CPU 任务数：").grid(row=1, column=2, sticky="e", padx=8, pady=(8, 0))
        ttk.Spinbox(settings, from_=1, to=4, textvariable=self.cpu_workers, width=5).grid(row=1, column=3, sticky="w", pady=(8, 0))
        ttk.Label(settings, text="NVENC ×1 ＋ QSV ×1 ＋ CPU ×N").grid(row=1, column=4, sticky="w", pady=(8, 0))
        settings.columnconfigure(2, weight=1)

        out = ttk.Frame(main)
        out.pack(fill="x", pady=(10, 0))
        ttk.Radiobutton(out, text="输出到各视频原目录下的 output 文件夹", variable=self.output_mode, value="source").pack(side="left")
        ttk.Radiobutton(out, text="自定义目录", variable=self.output_mode, value="custom").pack(side="left", padx=(16, 4))
        self.dir_entry = ttk.Entry(out, textvariable=self.custom_dir, width=45)
        self.dir_entry.pack(side="left", fill="x", expand=True)
        ttk.Button(out, text="浏览…", command=self.choose_output).pack(side="left", padx=(6, 0))

        bottom = ttk.Frame(main)
        bottom.pack(fill="x", pady=(12, 0))
        self.start_btn = ttk.Button(bottom, text="开始压缩", command=self.start)
        self.start_btn.pack(side="left")
        self.skip_btn = ttk.Button(bottom, text="跳过运行中", command=self.skip_current, state="disabled")
        self.skip_btn.pack(side="left", padx=(8, 0))
        self.stop_btn = ttk.Button(bottom, text="停止队列", command=self.stop_queue, state="disabled")
        self.stop_btn.pack(side="left", padx=(8, 0))
        ttk.Label(bottom, textvariable=self.summary).pack(side="left", padx=16)
        self.total_progress = ttk.Progressbar(main, mode="determinate", maximum=100)
        self.total_progress.pack(fill="x", pady=(10, 0))

    def _ratio_changed(self, _=None):
        self.ratio_label.configure(text=f"原文件的 {int(float(self.ratio.get()))}%")

    def _init_engine(self):
        def task():
            try:
                available = self.service.detect_encoders()
                self.events.put(("engine", available, ""))
            except Exception as exc:
                self.events.put(("engine", ["CPU（libx264）"], str(exc)))
        threading.Thread(target=task, daemon=True).start()

    def on_drop(self, event):
        self.add_paths(split_drop_paths(event.data))

    def add_files(self):
        paths = filedialog.askopenfilenames(title="选择视频", filetypes=[("视频文件", " ".join("*" + e for e in VIDEO_EXTS)), ("所有文件", "*.*")])
        self.add_paths(list(paths))

    def add_folder(self):
        path = filedialog.askdirectory(title="选择视频目录")
        if path:
            self.add_paths([path])

    def add_paths(self, paths: list[str]):
        if self.running:
            messagebox.showinfo(APP_NAME, "压缩进行中，请停止队列后再修改列表。")
            return
        found: list[Path] = []
        for raw in paths:
            p = Path(raw)
            try:
                if p.is_dir():
                    iterator = p.rglob("*") if self.recursive.get() else p.glob("*")
                    found.extend(x for x in iterator if x.is_file() and x.suffix.lower() in VIDEO_EXTS and not x.stem.endswith("_compressed") and ".partial" not in x.stem)
                elif p.is_file() and p.suffix.lower() in VIDEO_EXTS and not p.stem.endswith("_compressed") and ".partial" not in p.stem:
                    found.append(p)
            except OSError:
                continue
        existing = {os.path.normcase(str(x.path)) for x in self.items}
        added = 0
        for path in found:
            key = os.path.normcase(str(path))
            if key not in existing:
                item = VideoItem(path=path)
                self.items.append(item)
                self.item_by_iid[self.tree.insert("", "end", values=(path.name, "读取中…", "—", "—", "等待", "0%", "—"))] = item
                existing.add(key)
                added += 1
                threading.Thread(target=self._probe_item, args=(item,), daemon=True).start()
        self.summary.set(f"共 {len(self.items)} 个视频，本次新增 {added} 个")
        if not added and paths:
            self.summary.set("没有发现可添加的视频（也可能是文件已在列表中）")

    def _find_iid(self, item):
        return next((iid for iid, value in self.item_by_iid.items() if value is item), None)

    def _probe_item(self, item):
        try:
            info = self.service.probe(item.path)
            self.events.put(("probe", item, info, ""))
        except Exception as exc:
            self.events.put(("probe", item, None, str(exc)))

    def _update_row(self, item):
        iid = self._find_iid(item)
        if not iid:
            return
        info = item.info
        resolution = f"{info.width}×{info.height} / {info.codec}" if info.width else "—"
        status = item.status + (f" → {item.output.name}" if item.output and item.status == "完成" else "")
        tags = ("done",) if item.status == "完成" else (("error",) if item.status == "失败" else ())
        self.tree.item(iid, values=(item.path.name, resolution, human_rate(info.bitrate), human_size(info.size),
                                    status, f"{item.progress:.1f}%", item.ratio_text), tags=tags)

    def remove_selected(self):
        if self.running:
            return
        for iid in self.tree.selection():
            item = self.item_by_iid.pop(iid)
            self.items.remove(item)
            self.tree.delete(iid)
        self.summary.set(f"共 {len(self.items)} 个视频")

    def clear_items(self):
        if self.running:
            return
        self.tree.delete(*self.tree.get_children())
        self.items.clear()
        self.item_by_iid.clear()
        self.total_progress["value"] = 0
        self.summary.set("列表已清空")

    def choose_output(self):
        path = filedialog.askdirectory(title="选择输出目录")
        if path:
            self.custom_dir.set(path)
            self.output_mode.set("custom")

    def output_for(self, item: VideoItem) -> Path:
        directory = Path(self.custom_dir.get()) if self.output_mode.get() == "custom" else item.path.parent / "output"
        directory.mkdir(parents=True, exist_ok=True)
        return unique_path(directory / f"{safe_stem(item.path.name)}_compressed.mp4")

    def start(self):
        if self.running:
            return
        pending = [x for x in self.items if x.status in {"等待", "失败", "已取消"}]
        if not pending:
            messagebox.showinfo(APP_NAME, "请先添加待压缩视频。")
            return
        if not self.service.ffmpeg:
            messagebox.showerror(APP_NAME, "未找到 FFmpeg，请先运行 setup.bat。")
            return
        if self.output_mode.get() == "custom" and not self.custom_dir.get().strip():
            messagebox.showinfo(APP_NAME, "请选择自定义输出目录。")
            return
        self.running = True
        self.cancel_current.clear()
        self.stop_after_current.clear()
        self.start_btn.configure(state="disabled")
        self.skip_btn.configure(state="normal")
        self.stop_btn.configure(state="normal")
        settings = {
            "encoder": self.encoder.get(), "ratio": int(float(self.ratio.get())),
            "output_mode": self.output_mode.get(), "custom_dir": self.custom_dir.get().strip(),
            "parallel": self.parallel_enabled.get() and self.encoder.get() == "自动",
            "cpu_workers": max(1, min(4, int(self.cpu_workers.get())))
        }
        target = self._process_parallel if settings["parallel"] else self._process_queue
        self.worker = threading.Thread(target=target, args=(pending, settings), daemon=True)
        self.worker.start()

    def _process_queue(self, pending, settings):
        completed = 0
        for item in pending:
            if self.stop_after_current.is_set():
                break
            self.cancel_current.clear()
            item.status, item.progress, item.error = "准备中", 0, ""
            self.events.put(("update", item))
            encoders = [settings["encoder"]]
            if settings["encoder"] == "自动":
                encoders = list(self.service.available) or ["CPU（libx264）"]
            # 同一种编码器先尝试新版，再尝试兼容版；CPU 只需尝试一个构建。
            plans = []
            for encoder_name in encoders:
                for build in self.service.builds:
                    if (settings["encoder"] != "自动" or encoder_name in {"CPU（libx264）", "NVENC（NVIDIA）"}
                            or encoder_name in build["available"]):
                        plans.append((encoder_name, build))
                        if encoder_name == "CPU（libx264）":
                            break
            success = False
            if not plans:
                item.status = "失败"
                item.error = "所选编码器在两个 FFmpeg 构建中均不可用"
            for idx, (encoder, build) in enumerate(plans):
                try:
                    self._compress_one(item, encoder, settings, build)
                    success = True
                    break
                except Exception as exc:
                    if self.cancel_current.is_set() or self.stop_after_current.is_set():
                        item.status = "已取消"
                        break
                    item.error = str(exc)
                    if idx == len(plans) - 1:
                        item.status = "失败"
                    else:
                        item.status = "当前方案失败，尝试下一 FFmpeg／编码器"
                        self.events.put(("update", item))
            if success:
                completed += 1
            self.events.put(("update", item))
            self.events.put(("queue_progress", completed, len(pending)))
        self.events.put(("finished",))

    def _process_parallel(self, pending, settings):
        """中央动态队列：编码器空闲后领取下一任务，不再为慢速 CPU 预分配 backlog。"""
        lanes = []
        for encoder_name in ("NVENC（NVIDIA）", "QSV（Intel 核显）"):
            if encoder_name == "NVENC（NVIDIA）":
                builds = list(self.service.builds)
            else:
                builds = [b for b in self.service.builds if encoder_name in b["available"]]
            if builds:
                lanes.append({"encoder": encoder_name, "builds": builds})
        if self.service.builds:
            for _ in range(settings["cpu_workers"]):
                lanes.append({"encoder": "CPU（libx264）", "builds": [self.service.builds[0]]})
        if not lanes:
            self.events.put(("finished",))
            return
        work_queue = queue.Queue()
        for item in pending:
            item.status, item.progress, item.error = "等待空闲编码器", 0, ""
            self.events.put(("update", item))
            work_queue.put(item)
        remaining = len(pending)
        remaining_lock = threading.Lock()

        def terminal(item):
            nonlocal remaining
            self.events.put(("update", item))
            with remaining_lock:
                remaining -= 1
                current = remaining
            self.events.put(("queue_progress", len(pending) - current, len(pending)))
            if current == 0:
                self.events.put(("finished",))

        def try_builds(item, encoder_name, builds):
            last_error = None
            for build in builds:
                try:
                    self._compress_one(item, encoder_name, settings, build)
                    return
                except Exception as exc:
                    last_error = exc
                    if self.cancel_current.is_set() or self.stop_after_current.is_set():
                        raise
                    item.status = f'FFmpeg {build["version"]} 失败，尝试兼容构建'
                    self.events.put(("update", item))
            if last_error:
                raise last_error

        def worker(preferred_lane):
            while True:
                item = work_queue.get()
                try:
                    if self.stop_after_current.is_set():
                        item.status = "已取消"
                        terminal(item)
                        continue
                    try:
                        try_builds(item, preferred_lane["encoder"], preferred_lane["builds"])
                        terminal(item)
                    except Exception as exc:
                        item.error = str(exc)
                        if self.cancel_current.is_set() or self.stop_after_current.is_set():
                            item.status = "已取消"
                            terminal(item)
                            continue
                        # 正式编码失败后，当前工作线程依次尝试其他编码器，不再产生慢速通道积压。
                        fallback_error = None
                        for lane in lanes:
                            if lane["encoder"] == preferred_lane["encoder"]:
                                continue
                            try:
                                item.status = f'编码失败，转交 {lane["encoder"].split("（")[0]}'
                                self.events.put(("update", item))
                                try_builds(item, lane["encoder"], lane["builds"])
                                fallback_error = None
                                break
                            except Exception as fallback_exc:
                                fallback_error = fallback_exc
                                if self.cancel_current.is_set() or self.stop_after_current.is_set():
                                    break
                        if fallback_error:
                            item.error = str(fallback_error)
                            item.status = "失败"
                        elif item.status != "完成" and item.status != "已取消":
                            item.status = "失败"
                        terminal(item)
                    finally:
                        if self.cancel_current.is_set():
                            with self.process_lock:
                                if not self.current_processes:
                                    self.cancel_current.clear()
                finally:
                    work_queue.task_done()

        for lane in lanes:
            threading.Thread(target=worker, args=(lane,), daemon=True).start()

    def _compress_one(self, item: VideoItem, encoder: str, settings: dict, build: dict):
        self.service.select_build(build)
        directory = Path(settings["custom_dir"]) if settings["output_mode"] == "custom" else item.path.parent / "output"
        directory.mkdir(parents=True, exist_ok=True)
        with self.output_lock:
            output = unique_path(directory / f"{safe_stem(item.path.name)}_compressed.mp4")
            temp = output.with_name(output.stem + ".partial" + output.suffix)
            # 立即创建占位文件，防止不同线程为同名源文件选择同一输出名。
            output.touch(exist_ok=False)
            temp.touch(exist_ok=False)
        item.output, item.encoder = output, f'{encoder} / FFmpeg {build["version"]}'
        item.status = f"压缩中（{encoder.split('（')[0]} / {build['version']}）"
        self.events.put(("update", item))
        cpu_threads = 0
        if encoder == "CPU（libx264）" and settings.get("parallel"):
            cpu_threads = max(2, (os.cpu_count() or 8) // (settings.get("cpu_workers", 1) + 1))
        cmd = self.service.build_command(item, temp, settings["ratio"], encoder, cpu_threads)
        try:
            process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                       encoding="utf-8", errors="replace", bufsize=1,
                                       creationflags=CREATE_NO_WINDOW)
        except Exception:
            temp.unlink(missing_ok=True)
            output.unlink(missing_ok=True)
            raise
        with self.process_lock:
            self.current_processes.add(process)
        errors = []
        def read_errors():
            errors.append(process.stderr.read())
        err_thread = threading.Thread(target=read_errors, daemon=True)
        err_thread.start()
        last_event = 0.0
        for line in process.stdout:
            if line.startswith("out_time_ms="):
                try:
                    seconds = int(line.split("=", 1)[1]) / 1_000_000
                    if item.info.duration > 0:
                        item.progress = min(99.9, seconds / item.info.duration * 100)
                        now = time.monotonic()
                        if now - last_event > 0.15:
                            self.events.put(("update", item))
                            last_event = now
                except ValueError:
                    pass
        returncode = process.wait()
        err_thread.join(timeout=2)
        with self.process_lock:
            self.current_processes.discard(process)
        if self.cancel_current.is_set() or self.stop_after_current.is_set():
            temp.unlink(missing_ok=True)
            output.unlink(missing_ok=True)
            item.status = "已取消"
            return
        if returncode != 0 or not temp.exists():
            temp.unlink(missing_ok=True)
            output.unlink(missing_ok=True)
            log = "\n".join(errors).strip()
            raise RuntimeError(log[-1200:] or f"FFmpeg 退出码：{returncode}")
        temp.replace(output)
        item.progress = 100
        item.status = "完成"
        item.ratio_text = f"{human_size(output.stat().st_size)}（{output.stat().st_size / item.info.size * 100:.1f}%）"

    def skip_current(self):
        self.cancel_current.set()
        self._terminate_current()

    def stop_queue(self):
        self.stop_after_current.set()
        self.cancel_current.set()
        self._terminate_current()

    def _terminate_current(self):
        with self.process_lock:
            processes = list(self.current_processes)
        for process in processes:
            if process.poll() is None:
                try:
                    process.kill()
                except OSError:
                    pass

    def _poll_events(self):
        try:
            while True:
                event = self.events.get_nowait()
                kind = event[0]
                if kind == "engine":
                    available, error = event[1], event[2]
                    values = ["自动", *available]
                    self.encoder_box.configure(values=values)
                    self.hardware_text.set(f"FFmpeg {self.service.version}｜可用：" + "、".join(available))
                    if error:
                        messagebox.showwarning("硬件编码兼容性提示", error)
                elif kind == "probe":
                    _, item, info, error = event
                    if info:
                        item.info = info
                    else:
                        item.status = "读取失败"
                        item.error = error
                    self._update_row(item)
                elif kind == "update":
                    self._update_row(event[1])
                elif kind == "queue_progress":
                    self.total_progress["value"] = event[1] / event[2] * 100
                    self.summary.set(f"队列进度：{event[1]} / {event[2]}")
                elif kind == "finished":
                    self.running = False
                    self.start_btn.configure(state="normal")
                    self.skip_btn.configure(state="disabled")
                    self.stop_btn.configure(state="disabled")
                    done = sum(x.status == "完成" for x in self.items)
                    failed = sum(x.status == "失败" for x in self.items)
                    self.summary.set(f"已结束：完成 {done}，失败 {failed}")
                    failures = [x for x in self.items if x.status == "失败" and x.error]
                    if failures:
                        messagebox.showwarning(APP_NAME, f"有 {len(failures)} 个文件失败。首个错误：\n{failures[0].error[-800:]}")
        except queue.Empty:
            pass
        self.after(100, self._poll_events)

    def on_close(self):
        if self.running and not messagebox.askyesno(APP_NAME, "压缩仍在进行，确定停止并退出吗？"):
            return
        self.stop_after_current.set()
        self.cancel_current.set()
        self._terminate_current()
        self.destroy()


if __name__ == "__main__":
    app = VideoCompressorApp()
    app.mainloop()
