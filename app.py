import os
import sys
import json
import time
import zipfile
import platform
import subprocess
import threading
from datetime import datetime, timedelta
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import customtkinter as ctk
from tkinter import filedialog, messagebox
import tkinter as tk
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageOps, ImageEnhance
import piexif

# ドラッグ＆ドロップ対応
try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    DND_AVAILABLE = True
except ImportError:
    DND_AVAILABLE = False

ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")

# ==========================================
# OS判別 & .exe / .app (onedir・読み取り専用DMG対応) パス解決
# ==========================================
IS_WINDOWS = (platform.system() == "Windows")
IS_MAC = (platform.system() == "Darwin")


def _resolve_storage_dirs():
    """
    実行ファイル横のディレクトリを特定し、書き込み不可環境（読み取り専用DMG内や保護フォルダ）で
    起動された場合はOS標準のユーザーデータ領域へ安全にフォールバックする。
    """
    if getattr(sys, "frozen", False):
        exe_path = Path(sys.executable).resolve()
        bundle_parent = exe_path.parent
        if IS_MAC:
            for parent in exe_path.parents:
                if parent.suffix == ".app":
                    bundle_parent = parent.parent
                    break
    else:
        bundle_parent = Path(__file__).resolve().parent

    bundled_fonts = bundle_parent / "fonts"

    # 実行ファイル横に書き込み可能かテスト
    writable_base = None
    probe_file = bundle_parent / ".write_probe_tmp"
    try:
        if os.access(str(bundle_parent), os.W_OK):
            with open(probe_file, "w", encoding="utf-8") as f:
                f.write("")
            probe_file.unlink(missing_ok=True)
            (bundle_parent / "fonts").mkdir(exist_ok=True)
            writable_base = bundle_parent
    except OSError:
        try:
            probe_file.unlink(missing_ok=True)
        except OSError:
            pass

    # 読み取り専用環境（DMG直起動や/Applications配下など）の場合はユーザー領域を使用
    if writable_base is None:
        if IS_MAC:
            writable_base = Path.home() / "Library" / "Application Support" / "InstantDeliveryStudio"
        elif IS_WINDOWS:
            local_app = os.environ.get("LOCALAPPDATA")
            base_root = Path(local_app) if local_app else (Path.home() / "AppData" / "Local")
            writable_base = base_root / "InstantDeliveryStudio"
        else:
            writable_base = Path.home() / ".config" / "InstantDeliveryStudio"

        try:
            (writable_base / "fonts").mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    return bundle_parent, bundled_fonts, writable_base / "fonts", writable_base / "autosave_config.json"


BASE_APP_DIR, BUNDLED_FONTS_DIR, LOCAL_FONTS_DIR, AUTOSAVE_FILE = _resolve_storage_dirs()

_SYSTEM_FONTS_CACHE = None

# ==========================================
# Sleek Minimal カラーパレット & フォント定義
# ==========================================
FONT_FAMILY = "Yu Gothic UI" if IS_WINDOWS else ("Hiragino Sans" if IS_MAC else "Sans")
COLOR_BG = "#0e0f12"          # アプリ最背面
COLOR_SURFACE = "#16181d"     # パネル背景
COLOR_ELEVATED = "#1e2028"    # カード・入力セクション背景
COLOR_ACTIVE_ROW = "#252a3a"  # 選択中の行背景
COLOR_BORDER = "#282b36"      # 1px ボーダー
COLOR_INPUT = "#262934"       # プルダウン・入力欄背景
COLOR_INPUT_HOVER = "#323644" # ホバー時
COLOR_ACCENT = "#3b82f6"      # 選択アクセント
COLOR_PRIMARY_BTN = "#2563eb" # メインアクションボタン
COLOR_PRIMARY_HOVER = "#1d4ed8"
COLOR_TEXT_MAIN = "#f4f4f5"   # メイン文字
COLOR_TEXT_SUB = "#9ca3af"    # ラベル・補足文字
COLOR_TEXT_MUTED = "#6b7280"  # 非活性文字


def ui_font(size: int = 12, weight: str = "normal"):
    return ctk.CTkFont(family=FONT_FAMILY, size=size, weight=weight)


def open_folder(path_str: str):
    """Windows / Mac / Linux すべてで指定フォルダを開くクロスプラットフォーム関数"""
    try:
        target = Path(path_str)
        if not target.exists():
            target.mkdir(parents=True, exist_ok=True)
        if IS_WINDOWS:
            os.startfile(str(target))
        elif IS_MAC:
            subprocess.run(["open", str(target)], check=False)
        else:
            subprocess.run(["xdg-open", str(target)], check=False)
    except Exception:
        pass


# ==========================================
# PC内フォントスキャン & 専用フォントピッカー
# ==========================================

def scan_pc_fonts(force_refresh: bool = False):
    global _SYSTEM_FONTS_CACHE
    if _SYSTEM_FONTS_CACHE is not None and not force_refresh:
        return _SYSTEM_FONTS_CACHE

    font_dirs = []
    for local_d in (LOCAL_FONTS_DIR, BUNDLED_FONTS_DIR):
        if local_d.exists() and local_d not in font_dirs:
            font_dirs.append(local_d)

    if IS_WINDOWS:
        local_appdata = os.environ.get("LOCALAPPDATA", "")
        if local_appdata:
            user_font_dir = Path(local_appdata) / "Microsoft" / "Windows" / "Fonts"
            if user_font_dir.exists():
                font_dirs.append(user_font_dir)
        win_font_dir = Path("C:/Windows/Fonts")
        if win_font_dir.exists():
            font_dirs.append(win_font_dir)
    elif IS_MAC:
        for mac_p in [
            Path.home() / "Library" / "Fonts",
            Path("/Library/Fonts"),
            Path("/System/Library/Fonts"),
            Path("/System/Library/Fonts/Supplemental"),
        ]:
            if mac_p.exists():
                font_dirs.append(mac_p)
    else:
        for lin_p in [Path.home() / ".fonts", Path("/usr/share/fonts")]:
            if lin_p.exists():
                font_dirs.append(lin_p)

    valid_exts = {".ttf", ".otf", ".ttc"}
    results = []
    seen_paths = set()

    for d in font_dirs:
        try:
            for f in d.iterdir():
                if f.is_file() and f.suffix.lower() in valid_exts:
                    p_str = str(f)
                    if p_str in seen_paths:
                        continue
                    seen_paths.add(p_str)

                    display_name = f.name
                    try:
                        font_obj = ImageFont.truetype(p_str, 16)
                        family, style = font_obj.getname()
                        if family:
                            is_local = (d in (LOCAL_FONTS_DIR, BUNDLED_FONTS_DIR))
                            tag = " [Local]" if is_local else ""
                            display_name = f"{family} ({style})  -  {f.name}{tag}"
                    except Exception:
                        pass
                    results.append((display_name, p_str))
        except Exception:
            continue

    results.sort(key=lambda x: x[0].lower())
    _SYSTEM_FONTS_CACHE = results
    return results


class FontPickerDialog(ctk.CTkToplevel):
    def __init__(self, master, target_entry: ctk.CTkEntry, on_change_callback=None):
        super().__init__(master)
        self.title("Font Selector")
        self.geometry("680x520")
        self.configure(fg_color=COLOR_BG)
        self.attributes("-topmost", True)
        self.target_entry = target_entry
        self.on_change_callback = on_change_callback

        self.all_fonts = scan_pc_fonts()
        self.filtered_fonts = list(self.all_fonts)
        self.selected_path = ""

        top_row = ctk.CTkFrame(self, fg_color=COLOR_SURFACE, corner_radius=6, border_width=1, border_color=COLOR_BORDER)
        top_row.pack(fill="x", padx=12, pady=(12, 6))

        ctk.CTkLabel(top_row, text="検索:", font=ui_font(12), text_color=COLOR_TEXT_SUB).pack(side="left", padx=(12, 6))
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self._filter_list())
        self.search_entry = ctk.CTkEntry(
            top_row, textvariable=self.search_var, width=220, height=26, corner_radius=4,
            fg_color=COLOR_INPUT, border_width=0, font=ui_font(12), placeholder_text="フォント名を入力..."
        )
        self.search_entry.pack(side="left", padx=4, pady=8)

        ctk.CTkButton(
            top_row, text="専用fontsフォルダ", width=130, height=26, corner_radius=4,
            fg_color=COLOR_INPUT, hover_color=COLOR_INPUT_HOVER, font=ui_font(11),
            command=lambda: open_folder(str(LOCAL_FONTS_DIR))
        ).pack(side="right", padx=8)

        ctk.CTkButton(
            top_row, text="更新", width=64, height=26, corner_radius=4,
            fg_color=COLOR_INPUT, hover_color=COLOR_INPUT_HOVER, font=ui_font(11), command=self._rescan
        ).pack(side="right", padx=2)

        prev_frame = ctk.CTkFrame(self, fg_color=COLOR_SURFACE, corner_radius=6, border_width=1, border_color=COLOR_BORDER)
        prev_frame.pack(fill="x", padx=12, pady=4)
        self.preview_img_label = ctk.CTkLabel(
            prev_frame, text="リストからフォントを選択するとプレビューが表示されます",
            height=80, font=ui_font(12), text_color=COLOR_TEXT_MUTED
        )
        self.preview_img_label.pack(fill="x", padx=8, pady=6)

        list_frame = ctk.CTkFrame(self, fg_color=COLOR_SURFACE, corner_radius=6, border_width=1, border_color=COLOR_BORDER)
        list_frame.pack(fill="both", expand=True, padx=12, pady=6)

        scrollbar = tk.Scrollbar(list_frame)
        scrollbar.pack(side="right", fill="y")

        self.listbox = tk.Listbox(
            list_frame, yscrollcommand=scrollbar.set,
            bg=COLOR_SURFACE, fg=COLOR_TEXT_MAIN, selectbackground=COLOR_ACCENT, selectforeground="#ffffff",
            font=(FONT_FAMILY, 10), borderwidth=0, highlightthickness=0
        )
        self.listbox.pack(side="left", fill="both", expand=True, padx=6, pady=6)
        scrollbar.config(command=self.listbox.yview)

        self.listbox.bind("<<ListboxSelect>>", self._on_select)
        self.listbox.bind("<Double-Button-1>", lambda e: self._confirm())

        btn_row = ctk.CTkFrame(self, fg_color="transparent")
        btn_row.pack(fill="x", padx=12, pady=(2, 12))

        ctk.CTkButton(
            btn_row, text="ファイルを直接指定 (.ttf / .otf)...", width=210, height=30, corner_radius=4,
            fg_color=COLOR_INPUT, hover_color=COLOR_INPUT_HOVER, font=ui_font(12), command=self._browse_direct_file
        ).pack(side="left")

        ctk.CTkButton(
            btn_row, text="適用する", width=140, height=30, corner_radius=4,
            fg_color=COLOR_PRIMARY_BTN, hover_color=COLOR_PRIMARY_HOVER,
            font=ui_font(12, "bold"), command=self._confirm
        ).pack(side="right")

        self._populate_listbox()
        self.search_entry.focus_set()

    def _populate_listbox(self):
        self.listbox.delete(0, "end")
        for disp, _ in self.filtered_fonts:
            self.listbox.insert("end", disp)

    def _filter_list(self):
        q = self.search_var.get().strip().lower()
        self.filtered_fonts = [item for item in self.all_fonts if q in item[0].lower()] if q else list(self.all_fonts)
        self._populate_listbox()

    def _rescan(self):
        self.all_fonts = scan_pc_fonts(force_refresh=True)
        self._filter_list()

    def _on_select(self, event):
        sel = self.listbox.curselection()
        if not sel:
            return
        _, path = self.filtered_fonts[sel[0]]
        self.selected_path = path
        self._render_font_preview(path)

    def _render_font_preview(self, font_path: str):
        w, h = 630, 74
        img = Image.new("RGB", (w, h), (22, 24, 29))
        draw = ImageDraw.Draw(img)
        try:
            f_large = ImageFont.truetype(font_path, 25)
            f_small = ImageFont.truetype(font_path, 14)
            draw.text((w // 2, 24), "Photo by POGO   |   2026.11.05", font=f_large, fill=(244, 244, 245), anchor="mm")
            draw.text((w // 2, 54), "LEICA M6  |  Summicron 35mm f/2  |  Kodak Tri-X 400", font=f_small, fill=(156, 163, 175), anchor="mm")
        except Exception:
            draw.text((w // 2, h // 2), "プレビュー非対応のフォントです", fill=(220, 100, 100), anchor="mm")

        ctk_img = ctk.CTkImage(light_image=img, dark_image=img, size=(w, h))
        self.preview_img_label.configure(text="", image=ctk_img)
        self.preview_img_label.image = ctk_img

    def _browse_direct_file(self):
        p = filedialog.askopenfilename(filetypes=[("Font Files", "*.ttf;*.otf;*.ttc"), ("All", "*.*")])
        if p:
            self.selected_path = p
            self._confirm()

    def _confirm(self):
        if not self.selected_path:
            return
        self.target_entry.delete(0, "end")
        self.target_entry.insert(0, self.selected_path)
        if self.on_change_callback:
            self.on_change_callback()
        self.destroy()


# ==========================================
# スリム・スライダー ⇔ 数値入力 連動ウィジェット
# ==========================================

class SliderEntryWidget(ctk.CTkFrame):
    def __init__(self, master, label_text: str, min_val: float, max_val: float, default_val: float, step: float, on_change_cb, label_width: int = 82):
        super().__init__(master, fg_color="transparent")
        self.min_val = min_val
        self.max_val = max_val
        self.step = step
        self.on_change_cb = on_change_cb
        self.is_int = (step >= 1.0 and float(step).is_integer())

        ctk.CTkLabel(
            self, text=label_text, width=label_width, anchor="w",
            font=ui_font(11), text_color=COLOR_TEXT_SUB
        ).pack(side="left")

        steps_count = max(1, int(round((max_val - min_val) / step)))
        self.slider = ctk.CTkSlider(
            self, from_=min_val, to=max_val, number_of_steps=steps_count,
            width=100, height=14, button_length=10,
            fg_color=COLOR_INPUT, progress_color=COLOR_ACCENT,
            button_color=COLOR_TEXT_MAIN, button_hover_color=COLOR_ACCENT,
            command=self._on_slider_move
        )
        self.slider.pack(side="left", fill="x", expand=True, padx=6)

        self.entry = ctk.CTkEntry(
            self, width=46, height=24, corner_radius=4, justify="right",
            fg_color=COLOR_INPUT, border_width=1, border_color=COLOR_BORDER, font=ui_font(11)
        )
        self.entry.pack(side="left")

        self.set(default_val, trigger=False)

        self.entry.bind("<KeyRelease>", self._on_entry_key)
        self.entry.bind("<FocusOut>", self._on_entry_focus_out)

    def _format(self, val: float) -> str:
        if self.is_int:
            return str(int(round(val)))
        return f"{val:.1f}".rstrip("0").rstrip(".") if f"{val:.1f}".endswith(".0") else f"{val:.1f}"

    def _on_slider_move(self, val):
        self.entry.delete(0, "end")
        self.entry.insert(0, self._format(val))
        if self.on_change_cb:
            self.on_change_cb()

    def _on_entry_key(self, event):
        txt = self.entry.get().strip()
        if txt in ("", "-", ".", "-."):
            return
        try:
            val = float(txt)
            clamped = max(self.min_val, min(self.max_val, val))
            self.slider.set(clamped)
            if self.on_change_cb:
                self.on_change_cb()
        except ValueError:
            pass

    def _on_entry_focus_out(self, event):
        self.set(self.get(), trigger=False)

    def get(self) -> float:
        try:
            return float(self.entry.get().strip())
        except ValueError:
            return float(self.slider.get())

    def set(self, val: float, trigger: bool = False):
        try:
            v = float(val)
        except ValueError:
            v = self.min_val
        clamped = max(self.min_val, min(self.max_val, v))
        self.slider.set(clamped)
        self.entry.delete(0, "end")
        self.entry.insert(0, self._format(v))
        if trigger and self.on_change_cb:
            self.on_change_cb()


# ==========================================
# 画像処理・新ブラーエンジン・EXIF処理
# ==========================================

def load_font(font_path: str, size: int):
    candidates = []
    if font_path:
        if os.path.exists(font_path):
            candidates.append(font_path)
        else:
            fname = Path(font_path).name
            for font_dir in (LOCAL_FONTS_DIR, BUNDLED_FONTS_DIR):
                cand = font_dir / fname
                if cand.exists():
                    candidates.append(str(cand))

    candidates.extend([
        # Windows標準フォント
        "C:/Windows/Fonts/segoeui.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/YuGothM.ttc",
        "C:/Windows/Fonts/meiryo.ttc",
        # Mac標準フォント
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
    ])
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, max(10, int(size)))
            except Exception:
                continue
    return ImageFont.load_default()


def create_sample_dummy_image(is_portrait: bool = False):
    w, h = (800, 1200) if is_portrait else (1200, 800)
    img = Image.new("RGB", (w, h), (20, 22, 34))
    draw = ImageDraw.Draw(img)

    for i in range(0, h, 4):
        r = int(20 + (i / h) * 40)
        g = int(24 + (i / h) * 22)
        b = int(40 + (1.0 - i / h) * 60)
        draw.line([(0, i), (w, i + 3)], fill=(r, g, b), width=4)

    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    odraw = ImageDraw.Draw(overlay)
    odraw.ellipse([int(w * 0.1), int(h * 0.08), int(w * 0.7), int(h * 0.75)], fill=(59, 130, 246, 75))
    odraw.ellipse([int(w * 0.35), int(h * 0.2), int(w * 0.92), int(h * 0.88)], fill=(236, 72, 153, 65))
    overlay = overlay.filter(ImageFilter.GaussianBlur(50))
    img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")

    draw = ImageDraw.Draw(img)
    f_title = load_font("", int(min(w, h) * 0.042))
    f_sub = load_font("", int(min(w, h) * 0.024))
    label = "PORTRAIT SAMPLE (2:3)" if is_portrait else "LANDSCAPE SAMPLE (3:2)"
    draw.text((w // 2, h // 2 - 16), label, font=f_title, fill=(240, 244, 255), anchor="mm")
    draw.text((w // 2, h // 2 + 22), "Drop folder or select images to preview", font=f_sub, fill=(160, 170, 190), anchor="mm")

    sample_meta = {
        "datetime": datetime.now(), "subsec": "000",
        "camera": "OLYMPUS E-M1X", "lens": "M.ZUIKO DIGITAL ED 12-40mm F2.8 PRO",
        "focal": "24mm", "fnumber": "f/2.8", "shutter": "1/80s", "iso": "ISO 3200",
        "date_str": datetime.now().strftime("%Y.%m.%d"), "raw_exif": None
    }
    return img, sample_meta


def _safe_decode_exif_str(raw_val, default: str = "") -> str:
    if isinstance(raw_val, bytes):
        return raw_val.decode("utf-8", "ignore").strip("\x00 ")
    if isinstance(raw_val, str):
        return raw_val.strip("\x00 ")
    return default


def extract_exif_metadata(img_path: str):
    meta = {
        "datetime": None, "subsec": "000", "camera": "", "lens": "",
        "focal": "", "fnumber": "", "shutter": "", "iso": "", "date_str": "", "raw_exif": None
    }
    try:
        exif_dict = piexif.load(img_path)
        meta["raw_exif"] = exif_dict
        zeroth = exif_dict.get("0th", {})
        exif = exif_dict.get("Exif", {})

        make = _safe_decode_exif_str(zeroth.get(piexif.ImageIFD.Make))
        model = _safe_decode_exif_str(zeroth.get(piexif.ImageIFD.Model))
        if model:
            if make and not model.upper().startswith(make.split()[0].upper()):
                meta["camera"] = f"{make.split()[0]} {model}"
            else:
                meta["camera"] = model

        meta["lens"] = _safe_decode_exif_str(exif.get(piexif.ExifIFD.LensModel))

        dt_raw = exif.get(piexif.ExifIFD.DateTimeOriginal) or zeroth.get(piexif.ImageIFD.DateTime)
        dt_str = _safe_decode_exif_str(dt_raw)
        if dt_str:
            try:
                dt_obj = datetime.strptime(dt_str, "%Y:%m:%d %H:%M:%S")
                meta["datetime"] = dt_obj
                meta["date_str"] = dt_obj.strftime("%Y.%m.%d")
            except ValueError:
                pass

        subsec = _safe_decode_exif_str(exif.get(piexif.ExifIFD.SubSecTimeOriginal), "000")
        meta["subsec"] = subsec if subsec.isdigit() else "000"

        fl = exif.get(piexif.ExifIFD.FocalLength)
        if isinstance(fl, tuple) and len(fl) == 2 and fl[1] != 0:
            val = fl[0] / fl[1]
            meta["focal"] = f"{int(val)}mm" if val.is_integer() else f"{val:.1f}mm"

        fn = exif.get(piexif.ExifIFD.FNumber)
        if isinstance(fn, tuple) and len(fn) == 2 and fn[1] != 0:
            val = fn[0] / fn[1]
            meta["fnumber"] = f"f/{val:.1f}".rstrip("0").rstrip(".") if val >= 10 else f"f/{val:.1f}"

        exp = exif.get(piexif.ExifIFD.ExposureTime)
        if isinstance(exp, tuple) and len(exp) == 2 and exp[1] != 0 and exp[0] != 0:
            val = exp[0] / exp[1]
            meta["shutter"] = f"1/{int(round(1.0 / val))}s" if val < 1.0 else f"{val:.1f}".rstrip("0").rstrip(".") + "s"

        iso = exif.get(piexif.ExifIFD.ISOSpeedRatings)
        if iso is not None:
            meta["iso"] = f"ISO {iso}"
    except Exception:
        pass

    if meta["datetime"] is None:
        try:
            mtime = os.path.getmtime(img_path)
            meta["datetime"] = datetime.fromtimestamp(mtime)
        except OSError:
            meta["datetime"] = datetime.now()
        meta["date_str"] = meta["datetime"].strftime("%Y.%m.%d")

    return meta


def create_text_watermark(text: str, font_path: str, target_px_height: int, tracking_pct: float, invert_black: bool):
    font = load_font(font_path, target_px_height)
    tracking_px = int(target_px_height * (tracking_pct / 100.0))
    fill_color = (25, 25, 25, 255) if invert_black else (255, 255, 255, 255)

    dummy = Image.new("RGBA", (10, 10))
    d_draw = ImageDraw.Draw(dummy)
    char_widths = []
    max_h = target_px_height
    for ch in text:
        bbox = d_draw.textbbox((0, 0), ch, font=font)
        w = bbox[2] - bbox[0]
        h = bbox[3] - bbox[1]
        char_widths.append(w)
        if h > max_h:
            max_h = h

    total_w = sum(char_widths) + max(0, len(text) - 1) * tracking_px + int(target_px_height * 0.5)
    total_h = int(max_h * 1.6)

    wm_img = Image.new("RGBA", (max(1, total_w), max(1, total_h)), (0, 0, 0, 0))
    draw = ImageDraw.Draw(wm_img)

    x_cursor = int(target_px_height * 0.2)
    y_pos = int(max_h * 0.2)
    for ch, w in zip(text, char_widths):
        draw.text((x_cursor, y_pos), ch, font=font, fill=fill_color)
        x_cursor += w + tracking_px

    bbox = wm_img.getbbox()
    if bbox:
        wm_img = wm_img.crop(bbox)
    return wm_img


def apply_drop_shadow(wm_rgba: Image.Image, strength: str):
    if strength == "なし":
        return wm_rgba, 0
    params = {
        "弱": (0.04, 0.40, 1),
        "中": (0.07, 0.62, 2),
        "強": (0.11, 0.85, 3),
    }.get(strength, (0.05, 0.5, 2))

    blur_ratio, opacity, offset_mul = params
    w, h = wm_rgba.size
    radius = max(2, int(min(w, h) * blur_ratio))
    pad = radius * 2 + 4

    canvas = Image.new("RGBA", (w + pad * 2, h + pad * 2), (0, 0, 0, 0))
    alpha = wm_rgba.split()[3]
    shadow_alpha = alpha.point(lambda p: int(p * opacity))
    shadow = Image.new("RGBA", (w, h), (0, 0, 0, 255))
    shadow.putalpha(shadow_alpha)

    offset = max(1, offset_mul)
    canvas.paste(shadow, (pad + offset, pad + offset))
    canvas = canvas.filter(ImageFilter.GaussianBlur(radius))
    canvas.paste(wm_rgba, (pad, pad), wm_rgba)
    return canvas, pad


def paste_watermarks_on_surface(surface_img: Image.Image, wm_configs: list, reference_long_edge: int, invert_for_light_bg: bool):
    out = surface_img.convert("RGBA")
    surf_w, surf_h = out.size

    for cfg in wm_configs:
        if not cfg.get("enabled", False):
            continue

        mode = cfg.get("mode", "PNG")
        size_pct = float(cfg.get("size_pct", 12.0))
        margin_pct = float(cfg.get("margin_pct", 2.0))
        opacity_pct = float(cfg.get("opacity_pct", 90.0))
        shadow_mode = "なし" if invert_for_light_bg else cfg.get("shadow", "弱")
        pos = cfg.get("position", "右下")

        wm_rgba = None
        if mode == "PNG":
            png_path = cfg.get("path", "")
            if not png_path or not os.path.exists(png_path):
                continue
            try:
                wm_rgba = Image.open(png_path).convert("RGBA")
                bbox = wm_rgba.getbbox()
                if bbox:
                    wm_rgba = wm_rgba.crop(bbox)
                if invert_for_light_bg:
                    r, g, b, a = wm_rgba.split()
                    dark = Image.new("L", wm_rgba.size, 30)
                    wm_rgba = Image.merge("RGBA", (dark, dark, dark, a))
                target_w = max(16, int(reference_long_edge * (size_pct / 100.0)))
                ratio = target_w / wm_rgba.width
                target_h = max(1, int(wm_rgba.height * ratio))
                wm_rgba = wm_rgba.resize((target_w, target_h), Image.Resampling.LANCZOS)
            except Exception:
                continue
        else:
            txt = cfg.get("text", "").strip()
            if not txt:
                continue
            target_h = max(12, int(reference_long_edge * (size_pct / 100.0) * 0.25))
            wm_rgba = create_text_watermark(
                txt, cfg.get("font_path", ""), target_h,
                float(cfg.get("tracking", 8.0)), invert_for_light_bg
            )

        if wm_rgba is None:
            continue

        if opacity_pct < 100.0:
            r, g, b, a = wm_rgba.split()
            a = a.point(lambda p: int(p * (opacity_pct / 100.0)))
            wm_rgba.putalpha(a)

        core_w, core_h = wm_rgba.size
        wm_rgba, pad = apply_drop_shadow(wm_rgba, shadow_mode)

        margin_px = int(min(surf_w, surf_h) * (margin_pct / 100.0))

        if pos == "右下":
            x = surf_w - core_w - margin_px - pad
            y = surf_h - core_h - margin_px - pad
        elif pos == "左下":
            x = margin_px - pad
            y = surf_h - core_h - margin_px - pad
        elif pos == "右上":
            x = surf_w - core_w - margin_px - pad
            y = margin_px - pad
        else:
            x = margin_px - pad
            y = margin_px - pad

        layer = Image.new("RGBA", (surf_w, surf_h), (0, 0, 0, 0))
        layer.paste(wm_rgba, (int(x), int(y)))
        out = Image.alpha_composite(out, layer)

    return out.convert("RGB")


def generate_blurred_background(base_img: Image.Image, canvas_w: int, canvas_h: int, frame_style: str, blur_strength_pct: float, blur_bright_offset: float):
    w, h = base_img.size
    crop_margin_x = int(w * 0.10)
    crop_margin_y = int(h * 0.10)
    center_cropped = base_img.crop((crop_margin_x, crop_margin_y, w - crop_margin_x, h - crop_margin_y))

    bg = ImageOps.fit(center_cropped, (canvas_w, canvas_h), method=Image.Resampling.BILINEAR)

    target_long = max(canvas_w, canvas_h)
    blur_r = max(8, int(target_long * 0.045 * (blur_strength_pct / 100.0)))
    bg = bg.filter(ImageFilter.GaussianBlur(blur_r))

    if frame_style == "ブラー(明・すりガラス)":
        bg = ImageEnhance.Color(bg).enhance(1.20)
        white_layer = Image.new("RGB", (canvas_w, canvas_h), (255, 255, 255))
        bg = Image.blend(bg, white_layer, 0.28)
    elif frame_style == "ブラー(標準・そのまま)":
        bg = ImageEnhance.Color(bg).enhance(1.10)
    elif frame_style == "ブラー(暗・シネマ風)":
        dark_layer = Image.new("RGB", (canvas_w, canvas_h), (8, 8, 12))
        bg = Image.blend(bg, dark_layer, 0.32)

    if blur_bright_offset > 0:
        ratio = min(0.75, (blur_bright_offset / 100.0) * 1.1)
        white_layer = Image.new("RGB", (canvas_w, canvas_h), (255, 255, 255))
        bg = Image.blend(bg, white_layer, ratio)
    elif blur_bright_offset < 0:
        ratio = min(0.75, (abs(blur_bright_offset) / 100.0) * 1.1)
        dark_layer = Image.new("RGB", (canvas_w, canvas_h), (0, 0, 0))
        bg = Image.blend(bg, dark_layer, ratio)

    return bg


def render_single_pattern(base_img: Image.Image, meta: dict, pattern: dict, wm_configs: list, caption_cfg: dict, preview_max_edge: int = None):
    orig_w, orig_h = base_img.size
    aspect_str = pattern.get("aspect", "元比率")
    frame_style = pattern.get("frame_style", "枠なし")
    pad_preset = pattern.get("padding", "なし(0%)")
    wm_place = pattern.get("wm_place", "写真の内側")
    exif_print = pattern.get("exif_print", "印字なし")
    res_str = pattern.get("resolution", "オリジナル")
    blur_bright = float(pattern.get("blur_bright", 0.0))
    blur_strength = float(pattern.get("blur_strength", 100.0))

    if frame_style == "背景ブラー":
        frame_style = "ブラー(標準・そのまま)"

    orig_long = max(orig_w, orig_h)
    if preview_max_edge:
        target_long = preview_max_edge
    elif res_str == "オリジナル":
        target_long = orig_long
    else:
        try:
            target_long = int(res_str.replace("px", "").strip())
        except ValueError:
            target_long = orig_long

    if aspect_str == "1:1":
        canvas_w, canvas_h = target_long, target_long
    elif aspect_str == "4:5":
        canvas_w, canvas_h = int(target_long * 0.8), target_long
    elif aspect_str == "9:16":
        canvas_w, canvas_h = int(target_long * 9 / 16), target_long
    else:
        scale = target_long / orig_long
        canvas_w, canvas_h = max(1, int(orig_w * scale)), max(1, int(orig_h * scale))

    pad_map = {"なし(0%)": 0.0, "小(4%)": 0.04, "中(7.5%)": 0.075, "大(12.5%)": 0.125}
    pad_ratio = pad_map.get(pad_preset, 0.0)

    if frame_style == "枠なし" and aspect_str == "元比率" and pad_ratio == 0.0 and exif_print == "印字なし":
        photo_w, photo_h = canvas_w, canvas_h
        img_resized = base_img.resize((photo_w, photo_h), Image.Resampling.LANCZOS)
        if wm_place != "WMなし":
            img_resized = paste_watermarks_on_surface(img_resized, wm_configs, max(photo_w, photo_h), False)
        return img_resized

    bottom_extra_pct = float(caption_cfg.get("bottom_space_pct", 5.0)) / 100.0
    bottom_extra_ratio = bottom_extra_pct if exif_print != "印字なし" else 0.0
    avail_w = max(10, int(canvas_w * (1.0 - pad_ratio * 2)))
    avail_h = max(10, int(canvas_h * (1.0 - pad_ratio * 2 - bottom_extra_ratio)))

    scale_inner = min(avail_w / orig_w, avail_h / orig_h)
    photo_w = max(1, int(orig_w * scale_inner))
    photo_h = max(1, int(orig_h * scale_inner))

    photo_img = base_img.resize((photo_w, photo_h), Image.Resampling.LANCZOS)

    if wm_place == "写真の内側":
        photo_img = paste_watermarks_on_surface(photo_img, wm_configs, max(photo_w, photo_h), False)

    if frame_style == "黒枠":
        canvas = Image.new("RGB", (canvas_w, canvas_h), (14, 14, 16))
    elif frame_style.startswith("ブラー"):
        canvas = generate_blurred_background(base_img, canvas_w, canvas_h, frame_style, blur_strength, blur_bright)
    else:
        canvas = Image.new("RGB", (canvas_w, canvas_h), (255, 255, 255))

    paste_x = (canvas_w - photo_w) // 2
    if exif_print != "印字なし":
        paste_y = int((canvas_h - canvas_h * bottom_extra_ratio - photo_h) // 2)
    else:
        paste_y = (canvas_h - photo_h) // 2

    canvas.paste(photo_img, (paste_x, paste_y))

    is_light_bg = (
        frame_style in ["白枠", "枠なし"]
        or (frame_style == "ブラー(明・すりガラス)" and blur_bright >= -10)
        or blur_bright >= 25
    )

    if wm_place == "枠の外側":
        canvas = paste_watermarks_on_surface(canvas, wm_configs, max(canvas_w, canvas_h), is_light_bg)

    if exif_print != "印字なし":
        draw = ImageDraw.Draw(canvas)
        main_color = (28, 28, 30) if is_light_bg else (240, 240, 245)
        sub_color = (95, 95, 105) if is_light_bg else (185, 185, 195)

        cam_name = meta.get("camera", "")
        lens_name = meta.get("lens", "")
        for rule in caption_cfg.get("replace_rules", "").splitlines():
            if "=" in rule:
                k, v = rule.split("=", 1)
                cam_name = cam_name.replace(k.strip(), v.strip())
                lens_name = lens_name.replace(k.strip(), v.strip())

        line1_parts = []
        if caption_cfg.get("show_camera", True) and cam_name:
            line1_parts.append(cam_name)
        if caption_cfg.get("show_lens", True) and lens_name:
            line1_parts.append(lens_name)
        custom_txt = caption_cfg.get("custom_text", "").strip()
        if custom_txt:
            line1_parts.append(custom_txt)

        line2_parts = []
        if caption_cfg.get("show_focal", False) and meta.get("focal"):
            line2_parts.append(meta["focal"])
        if caption_cfg.get("show_fnumber", False) and meta.get("fnumber"):
            line2_parts.append(meta["fnumber"])
        if caption_cfg.get("show_shutter", False) and meta.get("shutter"):
            line2_parts.append(meta["shutter"])
        if caption_cfg.get("show_iso", False) and meta.get("iso"):
            line2_parts.append(meta["iso"])
        if caption_cfg.get("show_date", False) and meta.get("date_str"):
            line2_parts.append(meta["date_str"])

        line1_str = "  |  ".join(line1_parts)
        line2_str = "   ".join(line2_parts)

        font_scale = float(caption_cfg.get("font_scale_pct", 100.0)) / 100.0
        bottom_space_top = paste_y + photo_h
        bottom_space_h = max(16, canvas_h - bottom_space_top)
        font_size_main = max(10, int(target_long * 0.0155 * font_scale))
        font_size_sub = max(8, int(target_long * 0.0125 * font_scale))
        f_main = load_font(caption_cfg.get("font_path", ""), font_size_main)
        f_sub = load_font(caption_cfg.get("font_path", ""), font_size_sub)

        if exif_print == "センター2段":
            if line1_str and line2_str:
                y1 = bottom_space_top + int(bottom_space_h * 0.33)
                y2 = y1 + int(font_size_main * 1.45)
                draw.text((canvas_w // 2, y1), line1_str, font=f_main, fill=main_color, anchor="mm")
                draw.text((canvas_w // 2, y2), line2_str, font=f_sub, fill=sub_color, anchor="mm")
            elif line1_str or line2_str:
                txt = line1_str or line2_str
                ym = bottom_space_top + bottom_space_h // 2
                draw.text((canvas_w // 2, ym), txt, font=f_main, fill=main_color, anchor="mm")
        else:
            ym = bottom_space_top + bottom_space_h // 2
            if line1_str:
                draw.text((paste_x, ym), line1_str, font=f_main, fill=main_color, anchor="lm")
            if line2_str:
                draw.text((paste_x + photo_w, ym), line2_str, font=f_sub, fill=sub_color, anchor="rm")

    return canvas


def place_on_fixed_stage(img: Image.Image, stage_w: int, stage_h: int, bg_rgb=(20, 22, 27)) -> Image.Image:
    working = img.copy()
    working.thumbnail((stage_w, stage_h), Image.Resampling.LANCZOS)
    stage = Image.new("RGB", (stage_w, stage_h), bg_rgb)
    ox = (stage_w - working.width) // 2
    oy = (stage_h - working.height) // 2
    stage.paste(working, (ox, oy))
    return stage


def make_simulated_opposite_orientation(img: Image.Image) -> Image.Image:
    w, h = img.size
    if w >= h:
        target_w = max(10, int(h * (2.0 / 3.0)))
        left = max(0, (w - target_w) // 2)
        return img.crop((left, 0, left + target_w, h))
    else:
        target_h = max(10, int(w * (2.0 / 3.0)))
        top = max(0, (h - target_h) // 2)
        return img.crop((0, top, w, top + target_h))


def build_filtered_exif_bytes(meta: dict, exif_mode: str, corrected_dt: datetime, copyright_str: str):
    """全モードを例外保護し、不正タグ混入時でも書き出しスレッドを止めない安全なEXIF再構築"""
    if exif_mode == "④全削除":
        return None

    try:
        dt_str = corrected_dt.strftime("%Y:%m:%d %H:%M:%S").encode("utf-8")
        subsec_bytes = str(meta.get("subsec", "000")).encode("utf-8")
        raw = meta.get("raw_exif")

        if exif_mode == "①納品最適(日時のみ)" or not raw:
            zeroth = {piexif.ImageIFD.Orientation: 1, piexif.ImageIFD.DateTime: dt_str}
            exif_ifd = {
                piexif.ExifIFD.DateTimeOriginal: dt_str,
                piexif.ExifIFD.DateTimeDigitized: dt_str,
                piexif.ExifIFD.SubSecTimeOriginal: subsec_bytes,
            }
            if copyright_str:
                c_bytes = copyright_str.encode("utf-8")
                zeroth[piexif.ImageIFD.Copyright] = c_bytes
                zeroth[piexif.ImageIFD.Artist] = c_bytes
            return piexif.dump({"0th": zeroth, "Exif": exif_ifd, "GPS": {}, "1st": {}, "thumbnail": None})

        if exif_mode == "②カメラ維持":
            orig_0th = raw.get("0th", {})
            orig_exif = raw.get("Exif", {})
            zeroth = {piexif.ImageIFD.Orientation: 1, piexif.ImageIFD.DateTime: dt_str}
            for tag in [piexif.ImageIFD.Make, piexif.ImageIFD.Model]:
                if tag in orig_0th:
                    zeroth[tag] = orig_0th[tag]
            if copyright_str:
                c_bytes = copyright_str.encode("utf-8")
                zeroth[piexif.ImageIFD.Copyright] = c_bytes
                zeroth[piexif.ImageIFD.Artist] = c_bytes

            exif_ifd = {
                piexif.ExifIFD.DateTimeOriginal: dt_str,
                piexif.ExifIFD.DateTimeDigitized: dt_str,
                piexif.ExifIFD.SubSecTimeOriginal: subsec_bytes,
            }
            for tag in [
                piexif.ExifIFD.ExposureTime, piexif.ExifIFD.FNumber,
                piexif.ExifIFD.ISOSpeedRatings, piexif.ExifIFD.FocalLength, piexif.ExifIFD.LensModel
            ]:
                if tag in orig_exif:
                    exif_ifd[tag] = orig_exif[tag]
            return piexif.dump({"0th": zeroth, "Exif": exif_ifd, "GPS": {}, "1st": {}, "thumbnail": None})

        # ③完全維持
        copied = dict(raw)
        copied["0th"] = dict(raw.get("0th", {}))
        copied["Exif"] = dict(raw.get("Exif", {}))
        copied["0th"][piexif.ImageIFD.Orientation] = 1
        copied["0th"][piexif.ImageIFD.DateTime] = dt_str
        copied["Exif"][piexif.ExifIFD.DateTimeOriginal] = dt_str
        if copyright_str:
            copied["0th"][piexif.ImageIFD.Copyright] = copyright_str.encode("utf-8")
        return piexif.dump(copied)
    except Exception:
        return None


# ==========================================
# GUI コンポーネント: スリム・パターン行アイテム
# ==========================================

BaseWindowClass = ctk.CTk
if DND_AVAILABLE:
    class DnDCTk(ctk.CTk, TkinterDnD.DnDWrapper):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.TkdndVersion = TkinterDnD._require(self)
    BaseWindowClass = DnDCTk


class PatternListItem(ctk.CTkFrame):
    def __init__(self, master, data: dict, on_select_cb, on_delete_cb):
        super().__init__(master, fg_color=COLOR_ELEVATED, corner_radius=5, border_width=1, border_color=COLOR_BORDER, height=34)
        self.pack_propagate(False)

        self.data = dict(data)
        self.data.setdefault("blur_bright", 0.0)
        self.data.setdefault("blur_strength", 100.0)
        self.on_select_cb = on_select_cb
        self.on_delete_cb = on_delete_cb

        self.enabled_var = ctk.BooleanVar(value=self.data.get("enabled", True))
        self.chk = ctk.CTkCheckBox(
            self, text="", variable=self.enabled_var, width=18, height=18,
            checkbox_width=16, checkbox_height=16, corner_radius=4,
            border_width=2, fg_color=COLOR_ACCENT, command=self._on_check_toggle
        )
        self.chk.pack(side="left", padx=(10, 6))

        self.folder_lbl = ctk.CTkLabel(
            self, text="", width=148, anchor="w",
            font=ui_font(12, "bold"), text_color=COLOR_TEXT_MAIN
        )
        self.folder_lbl.pack(side="left", padx=(0, 6))

        self.del_btn = ctk.CTkButton(
            self, text="×", width=24, height=22, corner_radius=4,
            fg_color="transparent", hover_color="#7f1d1d", text_color=COLOR_TEXT_MUTED,
            font=ui_font(13, "bold"), command=lambda: self.on_delete_cb(self)
        )
        self.del_btn.pack(side="right", padx=6)

        self.summary_lbl = ctk.CTkLabel(
            self, text="", anchor="w",
            font=ui_font(11), text_color=COLOR_TEXT_SUB
        )
        self.summary_lbl.pack(side="left", fill="x", expand=True, padx=4)

        for widget in (self, self.folder_lbl, self.summary_lbl):
            widget.bind("<Button-1>", lambda e: self.on_select_cb(self))

        self.refresh_labels()

    def _on_check_toggle(self):
        self.data["enabled"] = self.enabled_var.get()
        self.on_select_cb(self)

    def refresh_labels(self):
        d = self.data
        self.folder_lbl.configure(text=d.get("folder", "Output"))

        frame_short = {
            "枠なし": "枠なし",
            "白枠": "白枠",
            "黒枠": "黒枠",
            "ブラー(明・すりガラス)": "ブラー(明)",
            "ブラー(標準・そのまま)": "ブラー(標準)",
            "ブラー(暗・シネマ風)": "ブラー(暗)",
        }.get(d.get("frame_style", "枠なし"), d.get("frame_style", "枠なし"))

        pad_short = d.get("padding", "なし").split("(")[0]
        wm_short = {"WMなし": "WM:なし", "写真の内側": "WM:内側", "枠の外側": "WM:枠外"}.get(d.get("wm_place", ""), "WM:なし")
        exif_short = "" if d.get("exif_print", "印字なし") == "印字なし" else " · EXIF文字"

        summary = f"{d.get('aspect', '元比率')}  ·  {frame_short} (余白:{pad_short})  ·  {wm_short}{exif_short}  ·  {d.get('resolution', 'オリジナル')}"
        self.summary_lbl.configure(text=summary)

    def set_selected(self, selected: bool):
        if selected:
            self.configure(fg_color=COLOR_ACTIVE_ROW, border_color=COLOR_ACCENT)
        else:
            self.configure(fg_color=COLOR_ELEVATED, border_color=COLOR_BORDER)


# ==========================================
# メインウィンドウ (v4.2 Thread-Safe & Read-Only Resilient Edition)
# ==========================================

class PhotoDeliveryApp(BaseWindowClass):
    def __init__(self):
        super().__init__()
        self.title("Instant Delivery & SNS Studio  —  v4.2")
        self.geometry("1440x880")
        self.minsize(1240, 760)
        self.configure(fg_color=COLOR_BG)

        self.image_files = []
        self.pattern_items = []
        self.selected_item = None
        self._updating_inspector = False

        self.wm_slot_widgets = []
        self.wm_slot_frames = []
        self.camera_offset_widgets = {}

        self.preview_debounce_id = None
        self.single_preview_index = 0
        self.sample_landscape = create_sample_dummy_image(is_portrait=False)
        self.sample_portrait = create_sample_dummy_image(is_portrait=True)
        self._cached_dual_sources = None
        self._cached_single_source = None

        self._build_ui()

        if not self._restore_autosave_if_exists():
            self._load_default_patterns()

        self.protocol("WM_DELETE_WINDOW", self._on_close_window)
        self.schedule_preview_update()

    def _build_ui(self):
        top_bar = ctk.CTkFrame(self, height=40, fg_color=COLOR_SURFACE, corner_radius=0)
        top_bar.pack(fill="x", padx=0, pady=(0, 8))
        top_bar.pack_propagate(False)

        ctk.CTkLabel(
            top_bar, text="INSTANT DELIVERY & SNS STUDIO",
            font=ui_font(13, "bold"), text_color=COLOR_TEXT_MAIN
        ).pack(side="left", padx=16)

        ctk.CTkLabel(
            top_bar, text="v4.2", font=ui_font(11), text_color=COLOR_TEXT_MUTED
        ).pack(side="left")

        self.pro_mode_var = ctk.BooleanVar(value=True)
        ctk.CTkSwitch(
            top_bar, text="詳細パネル", variable=self.pro_mode_var,
            font=ui_font(11), width=40, height=20, progress_color=COLOR_ACCENT,
            command=self._toggle_ui_mode
        ).pack(side="right", padx=16)

        for label, cmd in [
            ("プリセット保存", self.save_preset),
            ("プリセット読込", self.load_preset),
            ("専用fontsフォルダ", lambda: open_folder(str(LOCAL_FONTS_DIR)))
        ]:
            ctk.CTkButton(
                top_bar, text=label, width=108, height=26, corner_radius=4,
                fg_color=COLOR_INPUT, hover_color=COLOR_INPUT_HOVER,
                font=ui_font(11), command=cmd
            ).pack(side="right", padx=3)

        main_body = ctk.CTkFrame(self, fg_color="transparent")
        main_body.pack(fill="both", expand=True, padx=12, pady=(0, 10))

        self.left_pane = ctk.CTkFrame(main_body, width=580, fg_color="transparent")
        self.left_pane.pack(side="left", fill="y", expand=False, padx=(0, 10))
        self.left_pane.pack_propagate(False)

        self.right_pane = ctk.CTkFrame(
            main_body, fg_color=COLOR_SURFACE, corner_radius=6, border_width=1, border_color=COLOR_BORDER
        )
        self.right_pane.pack(side="right", fill="both", expand=True)
        self.right_pane.pack_propagate(False)

        self._build_left_pane()
        self._build_right_preview_pane()

    def _make_dropdown(self, parent, values, width=115, command=None):
        return ctk.CTkOptionMenu(
            parent, values=values, width=width, height=26, corner_radius=4,
            dynamic_resizing=False, fg_color=COLOR_INPUT,
            button_color=COLOR_INPUT, button_hover_color=COLOR_INPUT_HOVER,
            dropdown_fg_color=COLOR_ELEVATED, font=ui_font(11), dropdown_font=ui_font(11),
            command=command
        )

    def _build_left_pane(self):
        sec1 = ctk.CTkFrame(self.left_pane, fg_color=COLOR_SURFACE, corner_radius=6, border_width=1, border_color=COLOR_BORDER)
        sec1.pack(fill="x", pady=(0, 8))

        r_dnd = ctk.CTkFrame(sec1, fg_color="transparent")
        r_dnd.pack(fill="x", padx=10, pady=(8, 6))

        dnd_text = "フォルダまたは画像をここにドロップ" if DND_AVAILABLE else "右のボタンから写真を選択"
        self.drop_label = ctk.CTkLabel(
            r_dnd, text=dnd_text, height=32, fg_color=COLOR_BG, corner_radius=4,
            font=ui_font(11, "bold"), text_color=COLOR_TEXT_SUB
        )
        self.drop_label.pack(side="left", fill="x", expand=True, padx=(0, 6))

        if DND_AVAILABLE:
            self.drop_label.drop_target_register(DND_FILES)
            self.drop_label.dnd_bind("<<Drop>>", self._on_drop_files)

        ctk.CTkButton(
            r_dnd, text="フォルダ選択", width=92, height=30, corner_radius=4,
            fg_color=COLOR_PRIMARY_BTN, hover_color=COLOR_PRIMARY_HOVER, font=ui_font(11, "bold"),
            command=self.select_folder
        ).pack(side="left", padx=2)
        ctk.CTkButton(
            r_dnd, text="画像選択", width=74, height=30, corner_radius=4,
            fg_color=COLOR_INPUT, hover_color=COLOR_INPUT_HOVER, font=ui_font(11),
            command=self.select_files
        ).pack(side="left", padx=(2, 0))

        r_opt = ctk.CTkFrame(sec1, fg_color="transparent")
        r_opt.pack(fill="x", padx=10, pady=(0, 8))

        ctk.CTkLabel(r_opt, text="イベント名", font=ui_font(11), text_color=COLOR_TEXT_SUB).pack(side="left", padx=(0, 4))
        self.event_name_entry = ctk.CTkEntry(
            r_opt, width=140, height=26, corner_radius=4,
            fg_color=COLOR_INPUT, border_width=1, border_color=COLOR_BORDER, font=ui_font(11)
        )
        self.event_name_entry.insert(0, f"{datetime.now().strftime('%Y%m%d')}_Event")
        self.event_name_entry.pack(side="left", padx=(0, 10))

        ctk.CTkLabel(r_opt, text="順序", font=ui_font(11), text_color=COLOR_TEXT_SUB).pack(side="left", padx=(0, 4))
        self.sort_mode_cb = self._make_dropdown(
            r_opt, ["EXIF撮影日時順(ズレ補正対応)", "元のファイル名順", "リネームしない(元名維持)"], width=175
        )
        self.sort_mode_cb.pack(side="left", padx=(0, 10))

        self.zip_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            r_opt, text="ZIP化", variable=self.zip_var, width=58,
            checkbox_width=15, checkbox_height=15, corner_radius=3, font=ui_font(11)
        ).pack(side="left", padx=2)

        self.sync_time_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            r_opt, text="時刻同期", variable=self.sync_time_var, width=68,
            checkbox_width=15, checkbox_height=15, corner_radius=3, font=ui_font(11)
        ).pack(side="left", padx=2)

        sec2 = ctk.CTkFrame(self.left_pane, fg_color=COLOR_SURFACE, corner_radius=6, border_width=1, border_color=COLOR_BORDER)
        sec2.pack(fill="x", pady=(0, 8))

        l_head = ctk.CTkFrame(sec2, fg_color="transparent")
        l_head.pack(fill="x", padx=10, pady=(7, 3))
        ctk.CTkLabel(
            l_head, text="同時書き出しパターン", font=ui_font(12, "bold"), text_color=COLOR_TEXT_MAIN
        ).pack(side="left")
        ctk.CTkLabel(
            l_head, text="（行をクリックして編集）", font=ui_font(11), text_color=COLOR_TEXT_MUTED
        ).pack(side="left", padx=4)

        self.add_pat_btn = ctk.CTkButton(
            l_head, text="+ 追加", width=68, height=22, corner_radius=4,
            fg_color=COLOR_INPUT, hover_color=COLOR_ACCENT, font=ui_font(11, "bold"),
            command=self.add_empty_pattern
        )
        self.add_pat_btn.pack(side="right")

        self.list_scroll = ctk.CTkScrollableFrame(sec2, height=120, fg_color="transparent")
        self.list_scroll.pack(fill="x", padx=4, pady=(0, 4))

        self.inspector_tabs = ctk.CTkTabview(
            self.left_pane, fg_color=COLOR_SURFACE, corner_radius=6, border_width=1, border_color=COLOR_BORDER,
            segmented_button_fg_color=COLOR_BG,
            segmented_button_selected_color=COLOR_ACCENT,
            segmented_button_unselected_color=COLOR_ELEVATED
        )
        self.inspector_tabs.pack(fill="both", expand=True)

        tab_pat = self.inspector_tabs.add("パターン設定")
        tab_wm = self.inspector_tabs.add("ウォーターマーク")
        tab_cap = self.inspector_tabs.add("枠外EXIF印字")
        tab_cam = self.inspector_tabs.add("時刻補正・画質")

        self._build_pattern_inspector_tab(tab_pat)
        self._build_wm_tab(tab_wm)
        self._build_caption_tab(tab_cap)
        self._build_camera_tab(tab_cam)

    def _build_pattern_inspector_tab(self, parent):
        head_row = ctk.CTkFrame(parent, fg_color="transparent")
        head_row.pack(fill="x", padx=8, pady=(2, 6))

        ctk.CTkLabel(head_row, text="編集中:", font=ui_font(11), text_color=COLOR_TEXT_MUTED).pack(side="left")
        self.editing_title_lbl = ctk.CTkLabel(
            head_row, text="-", font=ui_font(12, "bold"), text_color=COLOR_ACCENT
        )
        self.editing_title_lbl.pack(side="left", padx=6)

        grid_card = ctk.CTkFrame(parent, fg_color=COLOR_ELEVATED, corner_radius=6, border_width=1, border_color=COLOR_BORDER)
        grid_card.pack(fill="x", padx=6, pady=2)

        for col in range(3):
            grid_card.grid_columnconfigure(col, weight=1, uniform="insp_col")

        def make_cell(r, c, label_txt):
            cell = ctk.CTkFrame(grid_card, fg_color="transparent")
            cell.grid(row=r, column=c, sticky="ew", padx=8, pady=6)
            ctk.CTkLabel(cell, text=label_txt, font=ui_font(11), text_color=COLOR_TEXT_SUB, anchor="w").pack(fill="x", pady=(0, 2))
            return cell

        c00 = make_cell(0, 0, "出力フォルダ名")
        self.insp_folder_entry = ctk.CTkEntry(
            c00, height=26, corner_radius=4, fg_color=COLOR_INPUT, border_width=1, border_color=COLOR_BORDER, font=ui_font(11)
        )
        self.insp_folder_entry.pack(fill="x")
        self.insp_folder_entry.bind("<KeyRelease>", lambda e: self._on_inspector_changed())

        c01 = make_cell(0, 1, "アスペクト比")
        self.insp_aspect_cb = self._make_dropdown(c01, ["元比率", "1:1", "4:5", "9:16"], command=lambda _: self._on_inspector_changed())
        self.insp_aspect_cb.pack(fill="x")

        c02 = make_cell(0, 2, "出力解像度 (長辺)")
        self.insp_res_cb = self._make_dropdown(
            c02, ["オリジナル", "4096px", "3000px", "2160px", "1440px", "1080px"], command=lambda _: self._on_inspector_changed()
        )
        self.insp_res_cb.pack(fill="x")

        c10 = make_cell(1, 0, "枠・背景スタイル")
        self.insp_frame_cb = self._make_dropdown(
            c10, ["枠なし", "白枠", "黒枠", "ブラー(明・すりガラス)", "ブラー(標準・そのまま)", "ブラー(暗・シネマ風)"],
            command=lambda _: self._on_inspector_changed()
        )
        self.insp_frame_cb.pack(fill="x")

        c11 = make_cell(1, 1, "四方余白 (マット幅)")
        self.insp_pad_cb = self._make_dropdown(
            c11, ["なし(0%)", "小(4%)", "中(7.5%)", "大(12.5%)"], command=lambda _: self._on_inspector_changed()
        )
        self.insp_pad_cb.pack(fill="x")

        c12 = make_cell(1, 2, "ロゴ(WM)の配置")
        self.insp_wm_cb = self._make_dropdown(
            c12, ["WMなし", "写真の内側", "枠の外側"], command=lambda _: self._on_inspector_changed()
        )
        self.insp_wm_cb.pack(fill="x")

        c20 = make_cell(2, 0, "枠外EXIF文字レイアウト")
        self.insp_exif_print_cb = self._make_dropdown(
            c20, ["印字なし", "センター2段", "左右スプリット"], command=lambda _: self._on_inspector_changed()
        )
        self.insp_exif_print_cb.pack(fill="x")

        c21 = make_cell(2, 1, "EXIFメタデータ保存")
        self.insp_exif_mode_cb = self._make_dropdown(
            c21, ["①納品最適(日時のみ)", "②カメラ維持", "③完全維持", "④全削除"], command=lambda _: self._on_inspector_changed()
        )
        self.insp_exif_mode_cb.pack(fill="x")

        blur_card = ctk.CTkFrame(parent, fg_color=COLOR_ELEVATED, corner_radius=6, border_width=1, border_color=COLOR_BORDER)
        blur_card.pack(fill="x", padx=6, pady=(8, 2))

        ctk.CTkLabel(
            blur_card, text="背景ブラー微調整 （枠・背景で『ブラー』選択時に有効）",
            font=ui_font(11, "bold"), text_color=COLOR_TEXT_SUB
        ).pack(anchor="w", padx=10, pady=(6, 2))

        b_row = ctk.CTkFrame(blur_card, fg_color="transparent")
        b_row.pack(fill="x", padx=10, pady=(2, 8))

        self.insp_blur_bright_slider = SliderEntryWidget(
            b_row, "明るさ(%)", min_val=-50.0, max_val=50.0, default_val=0.0,
            step=2.0, on_change_cb=self._on_inspector_changed, label_width=64
        )
        self.insp_blur_bright_slider.pack(side="left", fill="x", expand=True, padx=(0, 12))

        self.insp_blur_strength_slider = SliderEntryWidget(
            b_row, "ぼかし量(%)", min_val=30.0, max_val=200.0, default_val=100.0,
            step=5.0, on_change_cb=self._on_inspector_changed, label_width=70
        )
        self.insp_blur_strength_slider.pack(side="left", fill="x", expand=True)

    def _build_wm_tab(self, parent):
        slot_bar = ctk.CTkFrame(parent, fg_color="transparent")
        slot_bar.pack(fill="x", padx=6, pady=(2, 6))

        ctk.CTkLabel(slot_bar, text="編集スロット", font=ui_font(11), text_color=COLOR_TEXT_SUB).pack(side="left", padx=(2, 8))

        self.slot_selector = ctk.CTkSegmentedButton(
            slot_bar, values=["Slot 1", "Slot 2", "Slot 3", "Slot 4"],
            command=self._switch_wm_slot_view, width=260, height=26, font=ui_font(11, "bold"),
            fg_color=COLOR_BG, selected_color=COLOR_ACCENT, unselected_color=COLOR_ELEVATED
        )
        self.slot_selector.set("Slot 1")
        self.slot_selector.pack(side="left")

        self.wm_panel_container = ctk.CTkFrame(parent, fg_color=COLOR_ELEVATED, corner_radius=6, border_width=1, border_color=COLOR_BORDER)
        self.wm_panel_container.pack(fill="both", expand=True, padx=6, pady=2)

        defaults = [
            {"enabled": True, "mode": "テキスト(フォント)", "text": "Photo by POGO", "pos": "右下", "size": 12.0, "margin": 2.0, "opacity": 90.0, "tracking": 8.0, "shadow": "弱"},
            {"enabled": False, "mode": "PNG", "text": "", "pos": "左下", "size": 10.0, "margin": 2.5, "opacity": 90.0, "tracking": 8.0, "shadow": "弱"},
            {"enabled": False, "mode": "PNG", "text": "", "pos": "右上", "size": 10.0, "margin": 2.5, "opacity": 90.0, "tracking": 8.0, "shadow": "なし"},
            {"enabled": False, "mode": "PNG", "text": "", "pos": "左上", "size": 10.0, "margin": 2.5, "opacity": 90.0, "tracking": 8.0, "shadow": "なし"},
        ]

        for idx, d in enumerate(defaults):
            slot_frame = ctk.CTkFrame(self.wm_panel_container, fg_color="transparent")
            self.wm_slot_frames.append(slot_frame)

            r1 = ctk.CTkFrame(slot_frame, fg_color="transparent")
            r1.pack(fill="x", padx=10, pady=(10, 4))

            en_var = ctk.BooleanVar(value=d["enabled"])
            ctk.CTkCheckBox(
                r1, text=f"Slot {idx+1} を有効化", variable=en_var,
                checkbox_width=16, checkbox_height=16, corner_radius=4,
                font=ui_font(12, "bold"), command=self.schedule_preview_update
            ).pack(side="left", padx=(0, 12))

            ctk.CTkLabel(r1, text="方式", font=ui_font(11), text_color=COLOR_TEXT_SUB).pack(side="left", padx=(4, 4))
            mode_cb = self._make_dropdown(r1, ["PNG", "テキスト(フォント)"], width=125)
            mode_cb.set(d["mode"])
            mode_cb.pack(side="left", padx=(0, 10))

            ctk.CTkLabel(r1, text="位置", font=ui_font(11), text_color=COLOR_TEXT_SUB).pack(side="left", padx=(4, 4))
            pos_cb = self._make_dropdown(r1, ["右下", "左下", "右上", "左上"], width=72, command=lambda _: self.schedule_preview_update())
            pos_cb.set(d["pos"])
            pos_cb.pack(side="left", padx=(0, 10))

            ctk.CTkLabel(r1, text="影", font=ui_font(11), text_color=COLOR_TEXT_SUB).pack(side="left", padx=(4, 4))
            sh_cb = self._make_dropdown(r1, ["なし", "弱", "中", "強"], width=68, command=lambda _: self.schedule_preview_update())
            sh_cb.set(d["shadow"])
            sh_cb.pack(side="left")

            r2 = ctk.CTkFrame(slot_frame, fg_color="transparent")
            r2.pack(fill="x", padx=10, pady=6)

            path_entry = ctk.CTkEntry(
                r2, width=180, height=26, corner_radius=4, fg_color=COLOR_INPUT,
                border_width=1, border_color=COLOR_BORDER, font=ui_font(11), placeholder_text="フォント / PNGパス"
            )
            pick_btn = ctk.CTkButton(
                r2, text="フォント選択", width=96, height=26, corner_radius=4,
                fg_color=COLOR_INPUT, hover_color=COLOR_ACCENT, font=ui_font(11)
            )

            def on_mode_change(choice, btn=pick_btn):
                btn.configure(text="PNG参照" if choice == "PNG" else "フォント選択")
                self.schedule_preview_update()

            mode_cb.configure(command=on_mode_change)
            on_mode_change(d["mode"], pick_btn)

            def handle_pick(entry=path_entry, m_cb=mode_cb):
                if m_cb.get() == "PNG":
                    p = filedialog.askopenfilename(filetypes=[("PNG Image", "*.png"), ("All", "*.*")])
                    if p:
                        entry.delete(0, "end")
                        entry.insert(0, p)
                        self.schedule_preview_update()
                else:
                    FontPickerDialog(self, entry, on_change_callback=self.schedule_preview_update)

            pick_btn.configure(command=handle_pick)
            pick_btn.pack(side="left", padx=(0, 6))
            path_entry.pack(side="left", padx=(0, 10))
            path_entry.bind("<KeyRelease>", lambda e: self.schedule_preview_update())

            ctk.CTkLabel(r2, text="文字", font=ui_font(11), text_color=COLOR_TEXT_SUB).pack(side="left", padx=(2, 4))
            txt_entry = ctk.CTkEntry(
                r2, height=26, corner_radius=4, fg_color=COLOR_INPUT,
                border_width=1, border_color=COLOR_BORDER, font=ui_font(11), placeholder_text="Photo by ..."
            )
            txt_entry.insert(0, d["text"])
            txt_entry.pack(side="left", fill="x", expand=True)
            txt_entry.bind("<KeyRelease>", lambda e: self.schedule_preview_update())

            g_row1 = ctk.CTkFrame(slot_frame, fg_color="transparent")
            g_row1.pack(fill="x", padx=10, pady=(8, 4))

            size_slider = SliderEntryWidget(
                g_row1, "サイズ(%)", min_val=1.0, max_val=40.0, default_val=float(d["size"]),
                step=0.5, on_change_cb=self.schedule_preview_update, label_width=68
            )
            size_slider.pack(side="left", fill="x", expand=True, padx=(0, 12))

            margin_slider = SliderEntryWidget(
                g_row1, "端余白(%)", min_val=-5.0, max_val=20.0, default_val=float(d["margin"]),
                step=0.5, on_change_cb=self.schedule_preview_update, label_width=68
            )
            margin_slider.pack(side="left", fill="x", expand=True)

            g_row2 = ctk.CTkFrame(slot_frame, fg_color="transparent")
            g_row2.pack(fill="x", padx=10, pady=(4, 8))

            opacity_slider = SliderEntryWidget(
                g_row2, "不透明度(%)", min_val=10.0, max_val=100.0, default_val=float(d["opacity"]),
                step=1.0, on_change_cb=self.schedule_preview_update, label_width=68
            )
            opacity_slider.pack(side="left", fill="x", expand=True, padx=(0, 12))

            tracking_slider = SliderEntryWidget(
                g_row2, "文字間隔(%)", min_val=-5.0, max_val=50.0, default_val=float(d["tracking"]),
                step=1.0, on_change_cb=self.schedule_preview_update, label_width=68
            )
            tracking_slider.pack(side="left", fill="x", expand=True)

            self.wm_slot_widgets.append({
                "enabled": en_var, "mode": mode_cb, "path": path_entry, "text": txt_entry,
                "pos": pos_cb, "size": size_slider, "margin": margin_slider,
                "opacity": opacity_slider, "tracking": tracking_slider,
                "shadow": sh_cb, "pick_btn": pick_btn
            })

        self._switch_wm_slot_view("Slot 1")

    def _switch_wm_slot_view(self, selected_label: str):
        idx = int(selected_label.replace("Slot", "").strip()) - 1
        for i, frame in enumerate(self.wm_slot_frames):
            if i == idx:
                frame.pack(fill="both", expand=True)
            else:
                frame.pack_forget()

    def _build_caption_tab(self, parent):
        box = ctk.CTkFrame(parent, fg_color=COLOR_ELEVATED, corner_radius=6, border_width=1, border_color=COLOR_BORDER)
        box.pack(fill="both", expand=True, padx=6, pady=2)

        row1 = ctk.CTkFrame(box, fg_color="transparent")
        row1.pack(fill="x", padx=10, pady=(10, 6))

        ctk.CTkLabel(row1, text="印字項目", font=ui_font(11, "bold"), text_color=COLOR_TEXT_SUB).pack(side="left", padx=(0, 8))
        self.cap_cam_var = ctk.BooleanVar(value=True)
        self.cap_lens_var = ctk.BooleanVar(value=True)
        self.cap_focal_var = ctk.BooleanVar(value=False)
        self.cap_fnum_var = ctk.BooleanVar(value=False)
        self.cap_ss_var = ctk.BooleanVar(value=False)
        self.cap_iso_var = ctk.BooleanVar(value=False)
        self.cap_date_var = ctk.BooleanVar(value=False)

        for label, var in [
            ("カメラ", self.cap_cam_var), ("レンズ", self.cap_lens_var),
            ("焦点距離", self.cap_focal_var), ("F値", self.cap_fnum_var),
            ("SS", self.cap_ss_var), ("ISO", self.cap_iso_var), ("日付", self.cap_date_var)
        ]:
            ctk.CTkCheckBox(
                row1, text=label, variable=var, width=56,
                checkbox_width=15, checkbox_height=15, corner_radius=3, font=ui_font(11),
                command=self.schedule_preview_update
            ).pack(side="left", padx=3)

        row2 = ctk.CTkFrame(box, fg_color="transparent")
        row2.pack(fill="x", padx=10, pady=6)

        ctk.CTkLabel(row2, text="自由追記", font=ui_font(11), text_color=COLOR_TEXT_SUB).pack(side="left", padx=(0, 4))
        self.cap_custom_entry = ctk.CTkEntry(
            row2, width=155, height=26, corner_radius=4, fg_color=COLOR_INPUT,
            border_width=1, border_color=COLOR_BORDER, font=ui_font(11), placeholder_text="例: Kodak Tri-X 400"
        )
        self.cap_custom_entry.pack(side="left", padx=(0, 10))
        self.cap_custom_entry.bind("<KeyRelease>", lambda e: self.schedule_preview_update())

        ctk.CTkLabel(row2, text="書体", font=ui_font(11), text_color=COLOR_TEXT_SUB).pack(side="left", padx=(0, 4))
        self.cap_font_entry = ctk.CTkEntry(
            row2, height=26, corner_radius=4, fg_color=COLOR_INPUT,
            border_width=1, border_color=COLOR_BORDER, font=ui_font(11), placeholder_text="システム標準欧文"
        )
        self.cap_font_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.cap_font_entry.bind("<KeyRelease>", lambda e: self.schedule_preview_update())

        ctk.CTkButton(
            row2, text="フォント選択", width=96, height=26, corner_radius=4,
            fg_color=COLOR_INPUT, hover_color=COLOR_ACCENT, font=ui_font(11),
            command=lambda: FontPickerDialog(self, self.cap_font_entry, on_change_callback=self.schedule_preview_update)
        ).pack(side="left")

        row_sliders = ctk.CTkFrame(box, fg_color="transparent")
        row_sliders.pack(fill="x", padx=10, pady=6)

        self.cap_font_scale_slider = SliderEntryWidget(
            row_sliders, "文字倍率(%)", min_val=50.0, max_val=200.0, default_val=100.0,
            step=5.0, on_change_cb=self.schedule_preview_update, label_width=72
        )
        self.cap_font_scale_slider.pack(side="left", fill="x", expand=True, padx=(0, 12))

        self.cap_bottom_space_slider = SliderEntryWidget(
            row_sliders, "下余白(%)", min_val=2.0, max_val=12.0, default_val=5.0,
            step=0.5, on_change_cb=self.schedule_preview_update, label_width=64
        )
        self.cap_bottom_space_slider.pack(side="left", fill="x", expand=True)

        row3 = ctk.CTkFrame(box, fg_color="transparent")
        row3.pack(fill="x", padx=10, pady=(4, 10))
        ctk.CTkLabel(row3, text="表記置換", font=ui_font(11), text_color=COLOR_TEXT_SUB).pack(side="left", padx=(0, 6))
        self.cap_replace_entry = ctk.CTkEntry(
            row3, height=26, corner_radius=4, fg_color=COLOR_INPUT, border_width=1, border_color=COLOR_BORDER, font=ui_font(11)
        )
        self.cap_replace_entry.insert(0, "OLYMPUS IMAGING CORP.=OLYMPUS")
        self.cap_replace_entry.pack(side="left", fill="x", expand=True)
        self.cap_replace_entry.bind("<KeyRelease>", lambda e: self.schedule_preview_update())

    def _build_camera_tab(self, parent):
        left = ctk.CTkFrame(parent, fg_color=COLOR_ELEVATED, corner_radius=6, border_width=1, border_color=COLOR_BORDER)
        left.pack(side="left", fill="both", expand=True, padx=(4, 4), pady=2)
        ctk.CTkLabel(left, text="カメラ別 時刻オフセット (分 / 秒)", font=ui_font(11, "bold"), text_color=COLOR_TEXT_SUB).pack(anchor="w", padx=10, pady=6)
        self.cam_list_frame = ctk.CTkScrollableFrame(left, height=130, fg_color="transparent")
        self.cam_list_frame.pack(fill="both", expand=True, padx=4, pady=2)
        ctk.CTkLabel(self.cam_list_frame, text="写真読込時にカメラ機種を自動一覧化します", font=ui_font(11), text_color=COLOR_TEXT_MUTED).pack(anchor="w", padx=6, pady=6)

        right = ctk.CTkFrame(parent, width=210, fg_color=COLOR_ELEVATED, corner_radius=6, border_width=1, border_color=COLOR_BORDER)
        right.pack(side="right", fill="y", padx=(4, 4), pady=2)
        right.pack_propagate(False)
        ctk.CTkLabel(right, text="画質・メタデータ", font=ui_font(11, "bold"), text_color=COLOR_TEXT_SUB).pack(anchor="w", padx=10, pady=6)

        q_row = ctk.CTkFrame(right, fg_color="transparent")
        q_row.pack(fill="x", padx=10, pady=4)
        ctk.CTkLabel(q_row, text="JPEG品質", font=ui_font(11), text_color=COLOR_TEXT_SUB).pack(side="left")
        self.jpeg_q_entry = ctk.CTkEntry(q_row, width=50, height=26, corner_radius=4, fg_color=COLOR_INPUT, border_width=1, border_color=COLOR_BORDER, font=ui_font(11))
        self.jpeg_q_entry.insert(0, "95")
        self.jpeg_q_entry.pack(side="right")

        c_row = ctk.CTkFrame(right, fg_color="transparent")
        c_row.pack(fill="x", padx=10, pady=4)
        ctk.CTkLabel(c_row, text="Copyright", font=ui_font(11), text_color=COLOR_TEXT_SUB).pack(anchor="w", pady=(2, 2))
        self.copyright_entry = ctk.CTkEntry(c_row, height=26, corner_radius=4, fg_color=COLOR_INPUT, border_width=1, border_color=COLOR_BORDER, font=ui_font(11))
        self.copyright_entry.insert(0, "Photo by POGO")
        self.copyright_entry.pack(fill="x")

    def _build_right_preview_pane(self):
        p_head = ctk.CTkFrame(self.right_pane, height=40, fg_color="transparent")
        p_head.pack(fill="x", padx=12, pady=(8, 4))
        p_head.pack_propagate(False)

        ctk.CTkLabel(
            p_head, text="PREVIEW", font=ui_font(12, "bold"), text_color=COLOR_TEXT_SUB
        ).pack(side="left")

        self.active_pattern_label = ctk.CTkLabel(
            p_head, text="", width=220, anchor="w", font=ui_font(12, "bold"), text_color=COLOR_TEXT_MAIN
        )
        self.active_pattern_label.pack(side="left", padx=10)

        self.preview_layout_mode = ctk.CTkSegmentedButton(
            p_head, values=["全パターン比較", "縦横向き比較", "1枚拡大"], width=270, height=26,
            font=ui_font(11, "bold"), fg_color=COLOR_BG, selected_color=COLOR_ACCENT,
            unselected_color=COLOR_ELEVATED, command=lambda _: self.schedule_preview_update()
        )
        self.preview_layout_mode.set("縦横向き比較")
        self.preview_layout_mode.pack(side="right")

        self.preview_stage = ctk.CTkFrame(self.right_pane, fg_color=COLOR_BG, corner_radius=6, border_width=1, border_color=COLOR_BORDER)
        self.preview_stage.pack(fill="both", expand=True, padx=12, pady=4)
        self.preview_stage.pack_propagate(False)

        self.grid_stage = ctk.CTkFrame(self.preview_stage, fg_color="transparent")
        self.grid_stage.grid_columnconfigure(0, weight=1)
        self.grid_stage.grid_columnconfigure(1, weight=1)
        self.grid_stage.grid_rowconfigure(0, weight=1)
        self.grid_stage.grid_rowconfigure(1, weight=1)

        self.grid_cells = []
        for i in range(4):
            r_idx, c_idx = divmod(i, 2)
            cell = ctk.CTkFrame(self.grid_stage, fg_color=COLOR_SURFACE, corner_radius=5, border_width=1, border_color=COLOR_BORDER)
            cell.grid(row=r_idx, column=c_idx, sticky="nsew", padx=4, pady=4)
            cell.pack_propagate(False)

            title_l = ctk.CTkLabel(cell, text="", font=ui_font(11, "bold"), text_color=COLOR_TEXT_SUB)
            title_l.pack(pady=(4, 2))
            img_l = ctk.CTkLabel(cell, text="")
            img_l.pack(expand=True, pady=2)

            self.grid_cells.append({"frame": cell, "title": title_l, "img": img_l})

        self.dual_stage = ctk.CTkFrame(self.preview_stage, fg_color="transparent")

        self.land_card = ctk.CTkFrame(self.dual_stage, fg_color=COLOR_SURFACE, corner_radius=5)
        self.land_card.pack(fill="both", expand=True, padx=4, pady=(4, 2))
        self.land_card.pack_propagate(False)
        self.land_title = ctk.CTkLabel(self.land_card, text="LANDSCAPE", width=560, anchor="center", font=ui_font(11), text_color=COLOR_TEXT_SUB)
        self.land_title.pack(pady=(4, 0))
        self.land_img_lbl = ctk.CTkLabel(self.land_card, text="")
        self.land_img_lbl.pack(expand=True, pady=2)

        self.port_card = ctk.CTkFrame(self.dual_stage, fg_color=COLOR_SURFACE, corner_radius=5)
        self.port_card.pack(fill="both", expand=True, padx=4, pady=(2, 4))
        self.port_card.pack_propagate(False)
        self.port_title = ctk.CTkLabel(self.port_card, text="PORTRAIT", width=560, anchor="center", font=ui_font(11), text_color=COLOR_TEXT_SUB)
        self.port_title.pack(pady=(4, 0))
        self.port_img_lbl = ctk.CTkLabel(self.port_card, text="")
        self.port_img_lbl.pack(expand=True, pady=2)

        self.single_stage = ctk.CTkFrame(self.preview_stage, fg_color="transparent")
        self.single_title = ctk.CTkLabel(self.single_stage, text="", width=600, anchor="center", font=ui_font(11, "bold"), text_color=COLOR_TEXT_SUB)
        self.single_title.pack(pady=(6, 2))
        self.single_img_lbl = ctk.CTkLabel(self.single_stage, text="")
        self.single_img_lbl.pack(fill="both", expand=True, pady=4)

        footer_bar = ctk.CTkFrame(self.right_pane, height=52, fg_color="transparent")
        footer_bar.pack(fill="x", padx=12, pady=(4, 10))
        footer_bar.pack_propagate(False)

        nav_box = ctk.CTkFrame(footer_bar, fg_color=COLOR_BG, corner_radius=5, border_width=1, border_color=COLOR_BORDER)
        nav_box.pack(side="left", fill="y", pady=4)

        ctk.CTkButton(
            nav_box, text="‹ 前へ", width=60, height=26, corner_radius=4,
            fg_color="transparent", hover_color=COLOR_INPUT, font=ui_font(11), command=self._prev_photo
        ).pack(side="left", padx=4, pady=4)

        self.photo_counter_lbl = ctk.CTkLabel(nav_box, text="Sample", width=155, font=ui_font(11), text_color=COLOR_TEXT_SUB)
        self.photo_counter_lbl.pack(side="left", padx=4)

        ctk.CTkButton(
            nav_box, text="次へ ›", width=60, height=26, corner_radius=4,
            fg_color="transparent", hover_color=COLOR_INPUT, font=ui_font(11), command=self._next_photo
        ).pack(side="left", padx=4, pady=4)

        prog_box = ctk.CTkFrame(footer_bar, fg_color="transparent")
        prog_box.pack(side="left", fill="x", expand=True, padx=14, pady=6)
        self.status_label = ctk.CTkLabel(prog_box, text="Ready (Auto-save enabled)", anchor="w", font=ui_font(11), text_color=COLOR_TEXT_SUB)
        self.status_label.pack(fill="x")
        self.progress_bar = ctk.CTkProgressBar(prog_box, height=6, corner_radius=3, fg_color=COLOR_BG, progress_color=COLOR_ACCENT)
        self.progress_bar.pack(fill="x", pady=(4, 0))
        self.progress_bar.set(0)

        self.run_btn = ctk.CTkButton(
            footer_bar, text="全パターン一括書き出し", width=190, height=38, corner_radius=5,
            fg_color=COLOR_PRIMARY_BTN, hover_color=COLOR_PRIMARY_HOVER,
            font=ui_font(13, "bold"), command=self.start_batch_export
        )
        self.run_btn.pack(side="right", pady=4)

    # --- パターンリスト ⇔ インスペクター連動 ---
    def _toggle_ui_mode(self):
        if self.pro_mode_var.get():
            self.inspector_tabs.pack(fill="both", expand=True)
            self.add_pat_btn.pack(side="right")
        else:
            self.inspector_tabs.pack_forget()
            self.add_pat_btn.pack_forget()

    def _load_default_patterns(self):
        defaults = [
            {"enabled": True, "folder": "01_Original_NoWM", "aspect": "元比率", "frame_style": "枠なし", "padding": "なし(0%)", "wm_place": "WMなし", "exif_print": "印字なし", "resolution": "オリジナル", "exif_mode": "③完全維持", "blur_bright": 0.0, "blur_strength": 100.0},
            {"enabled": True, "folder": "02_Original_WM", "aspect": "元比率", "frame_style": "枠なし", "padding": "なし(0%)", "wm_place": "写真の内側", "exif_print": "印字なし", "resolution": "3000px", "exif_mode": "①納品最適(日時のみ)", "blur_bright": 0.0, "blur_strength": 100.0},
            {"enabled": True, "folder": "03_SNS_1x1_Blur", "aspect": "1:1", "frame_style": "ブラー(明・すりガラス)", "padding": "中(7.5%)", "wm_place": "写真の内側", "exif_print": "センター2段", "resolution": "2160px", "exif_mode": "①納品最適(日時のみ)", "blur_bright": 0.0, "blur_strength": 100.0},
        ]
        for d in defaults:
            self.add_pattern_item(d, select_now=False)
        if self.pattern_items:
            self.select_pattern_item(self.pattern_items[-1])

    def add_pattern_item(self, data: dict, select_now: bool = True):
        item = PatternListItem(self.list_scroll, data, on_select_cb=self.select_pattern_item, on_delete_cb=self.remove_pattern_item)
        item.pack(fill="x", padx=2, pady=2)
        self.pattern_items.append(item)
        if select_now:
            self.select_pattern_item(item)

    def add_empty_pattern(self):
        idx = len(self.pattern_items) + 1
        self.add_pattern_item({
            "enabled": True, "folder": f"0{idx}_SNS_Custom", "aspect": "4:5",
            "frame_style": "白枠", "padding": "中(7.5%)", "wm_place": "写真の内側",
            "exif_print": "センター2段", "resolution": "2160px", "exif_mode": "①納品最適(日時のみ)",
            "blur_bright": 0.0, "blur_strength": 100.0
        })

    def remove_pattern_item(self, item_widget):
        if len(self.pattern_items) <= 1:
            messagebox.showwarning("確認", "最低1つの出力パターンが必要です。")
            return
        self.pattern_items.remove(item_widget)
        item_widget.destroy()
        self.select_pattern_item(self.pattern_items[-1])

    def select_pattern_item(self, item_widget):
        self.selected_item = item_widget
        for it in self.pattern_items:
            it.set_selected(it == item_widget)

        self._updating_inspector = True
        d = item_widget.data
        if d.get("frame_style") == "背景ブラー":
            d["frame_style"] = "ブラー(標準・そのまま)"

        self.editing_title_lbl.configure(text=d.get("folder", ""))
        self.active_pattern_label.configure(text=f"—  {d.get('folder', '')}")

        self.insp_folder_entry.delete(0, "end")
        self.insp_folder_entry.insert(0, d.get("folder", "Output"))
        self.insp_aspect_cb.set(d.get("aspect", "元比率"))
        self.insp_frame_cb.set(d.get("frame_style", "枠なし"))
        self.insp_pad_cb.set(d.get("padding", "なし(0%)"))
        self.insp_wm_cb.set(d.get("wm_place", "写真の内側"))
        self.insp_exif_print_cb.set(d.get("exif_print", "印字なし"))
        self.insp_res_cb.set(d.get("resolution", "オリジナル"))
        self.insp_exif_mode_cb.set(d.get("exif_mode", "①納品最適(日時のみ)"))
        self.insp_blur_bright_slider.set(d.get("blur_bright", 0.0), trigger=False)
        self.insp_blur_strength_slider.set(d.get("blur_strength", 100.0), trigger=False)
        self._updating_inspector = False

        self.schedule_preview_update()

    def _on_inspector_changed(self):
        if self._updating_inspector or not self.selected_item:
            return
        d = self.selected_item.data
        d["folder"] = self.insp_folder_entry.get().strip() or "Output"
        d["aspect"] = self.insp_aspect_cb.get()
        d["frame_style"] = self.insp_frame_cb.get()
        d["padding"] = self.insp_pad_cb.get()
        d["wm_place"] = self.insp_wm_cb.get()
        d["exif_print"] = self.insp_exif_print_cb.get()
        d["resolution"] = self.insp_res_cb.get()
        d["exif_mode"] = self.insp_exif_mode_cb.get()
        d["blur_bright"] = self.insp_blur_bright_slider.get()
        d["blur_strength"] = self.insp_blur_strength_slider.get()

        self.editing_title_lbl.configure(text=d["folder"])
        self.active_pattern_label.configure(text=f"—  {d['folder']}")
        self.selected_item.refresh_labels()
        self.schedule_preview_update()

    # --- 3モード対応 リアルタイムプレビュー描画 ---
    def schedule_preview_update(self):
        if self.preview_debounce_id is not None:
            self.after_cancel(self.preview_debounce_id)
        self.preview_debounce_id = self.after(45, self._render_live_preview_now)

    def _prev_photo(self):
        if self.image_files:
            self.single_preview_index = (self.single_preview_index - 1) % len(self.image_files)
            self._cached_single_source = None
            self._cached_dual_sources = None
            self.schedule_preview_update()

    def _next_photo(self):
        if self.image_files:
            self.single_preview_index = (self.single_preview_index + 1) % len(self.image_files)
            self._cached_single_source = None
            self._cached_dual_sources = None
            self.schedule_preview_update()

    def _get_current_photo_source(self):
        if not self.image_files:
            im, meta = self.sample_landscape
            return im, meta, "Sample"

        self.single_preview_index = max(0, min(self.single_preview_index, len(self.image_files) - 1))
        if self._cached_single_source and self._cached_single_source[0] == self.single_preview_index:
            _, im, meta, name = self._cached_single_source
            return im, meta, name

        p = self.image_files[self.single_preview_index]
        with Image.open(p) as raw_im:
            base_im = ImageOps.exif_transpose(raw_im).convert("RGB")
            base_im.thumbnail((1100, 1100), Image.Resampling.LANCZOS)
        meta = extract_exif_metadata(p)
        name = Path(p).name
        self._cached_single_source = (self.single_preview_index, base_im, meta, name)
        return base_im, meta, name

    def _get_dual_orientation_sources(self):
        if not self.image_files:
            l_im, l_meta = self.sample_landscape
            p_im, p_meta = self.sample_portrait
            return l_im, l_meta, "Landscape Sample", p_im, p_meta, "Portrait Sample"

        cur_im, cur_meta, cur_name = self._get_current_photo_source()
        if cur_im.width >= cur_im.height:
            land_im, land_meta, land_name = cur_im, cur_meta, cur_name
            port_im, port_meta, port_name = None, None, None
            for p in self.image_files:
                try:
                    with Image.open(p) as im:
                        im_t = ImageOps.exif_transpose(im)
                        if im_t.height > im_t.width:
                            port_im = im_t.convert("RGB")
                            port_im.thumbnail((1000, 1000), Image.Resampling.LANCZOS)
                            port_meta = extract_exif_metadata(p)
                            port_name = Path(p).name
                            break
                except Exception:
                    continue
            if port_im is None:
                port_im = make_simulated_opposite_orientation(land_im)
                port_meta = land_meta
                port_name = f"{land_name} (2:3 Crop)"
        else:
            port_im, port_meta, port_name = cur_im, cur_meta, cur_name
            land_im, land_meta, land_name = None, None, None
            for p in self.image_files:
                try:
                    with Image.open(p) as im:
                        im_t = ImageOps.exif_transpose(im)
                        if im_t.width >= im_t.height:
                            land_im = im_t.convert("RGB")
                            land_im.thumbnail((1000, 1000), Image.Resampling.LANCZOS)
                            land_meta = extract_exif_metadata(p)
                            land_name = Path(p).name
                            break
                except Exception:
                    continue
            if land_im is None:
                land_im = make_simulated_opposite_orientation(port_im)
                land_meta = port_meta
                land_name = f"{port_name} (3:2 Crop)"

        return land_im, land_meta, land_name, port_im, port_meta, port_name

    def _render_live_preview_now(self):
        self.preview_debounce_id = None
        if not self.selected_item:
            return

        wm_cfgs = self._collect_wm_configs()
        cap_cfg = self._collect_caption_config()
        mode = self.preview_layout_mode.get()

        base_im, meta, name = self._get_current_photo_source()
        if self.image_files:
            self.photo_counter_lbl.configure(text=f"{name}  ({self.single_preview_index + 1}/{len(self.image_files)})")
        else:
            self.photo_counter_lbl.configure(text="Sample Preview")

        self.grid_stage.pack_forget()
        self.dual_stage.pack_forget()
        self.single_stage.pack_forget()

        if mode == "全パターン比較":
            self.grid_stage.pack(fill="both", expand=True, padx=4, pady=4)
            for idx, cell in enumerate(self.grid_cells):
                if idx < len(self.pattern_items):
                    pat_item = self.pattern_items[idx]
                    pat_data = pat_item.data
                    is_sel = (pat_item == self.selected_item)

                    cell["frame"].configure(
                        border_color=COLOR_ACCENT if is_sel else COLOR_BORDER,
                        fg_color=COLOR_ACTIVE_ROW if is_sel else COLOR_SURFACE
                    )
                    rendered = render_single_pattern(base_im, meta, pat_data, wm_cfgs, cap_cfg, preview_max_edge=560)
                    stage_img = place_on_fixed_stage(rendered, 350, 275, bg_rgb=(37, 42, 58) if is_sel else (22, 24, 29))

                    state_dot = "• " if pat_data.get("enabled", True) else "(OFF) "
                    cell["title"].configure(
                        text=f"{state_dot}{pat_data['folder']}   [{pat_data['aspect']} / {pat_data['frame_style']}]",
                        text_color=COLOR_TEXT_MAIN if is_sel else COLOR_TEXT_SUB
                    )
                    ctk_im = ctk.CTkImage(light_image=stage_img, dark_image=stage_img, size=(350, 275))
                    cell["img"].configure(image=ctk_im, text="")
                    cell["img"].image = ctk_im

                    for w in (cell["frame"], cell["title"], cell["img"]):
                        w.bind("<Button-1>", lambda e, it=pat_item: self.select_pattern_item(it))
                else:
                    cell["frame"].configure(border_color=COLOR_BG, fg_color=COLOR_BG)
                    cell["title"].configure(text="", text_color=COLOR_TEXT_MUTED)
                    cell["img"].configure(image=None, text="")

        elif mode == "縦横向き比較":
            self.dual_stage.pack(fill="both", expand=True, padx=4, pady=4)
            pattern = self.selected_item.data
            land_im, land_meta, land_name, port_im, port_meta, port_name = self._get_dual_orientation_sources()

            r_land = render_single_pattern(land_im, land_meta, pattern, wm_cfgs, cap_cfg, preview_max_edge=740)
            r_port = render_single_pattern(port_im, port_meta, pattern, wm_cfgs, cap_cfg, preview_max_edge=740)

            stage_land = place_on_fixed_stage(r_land, 580, 295)
            stage_port = place_on_fixed_stage(r_port, 580, 295)

            self.land_title.configure(text=f"横位置: {land_name}   ({pattern['aspect']} · {pattern['frame_style']})")
            ctk_l = ctk.CTkImage(light_image=stage_land, dark_image=stage_land, size=(580, 295))
            self.land_img_lbl.configure(image=ctk_l)
            self.land_img_lbl.image = ctk_l

            self.port_title.configure(text=f"縦位置: {port_name}   ({pattern['aspect']} · {pattern['frame_style']})")
            ctk_p = ctk.CTkImage(light_image=stage_port, dark_image=stage_port, size=(580, 295))
            self.port_img_lbl.configure(image=ctk_p)
            self.port_img_lbl.image = ctk_p

        else:  # 1枚拡大
            self.single_stage.pack(fill="both", expand=True, padx=4, pady=4)
            pattern = self.selected_item.data
            rendered = render_single_pattern(base_im, meta, pattern, wm_cfgs, cap_cfg, preview_max_edge=1000)
            stage_single = place_on_fixed_stage(rendered, 680, 580)

            self.single_title.configure(text=f"{name}   —   {pattern['folder']}  ({pattern['aspect']} · {pattern['frame_style']})")
            ctk_s = ctk.CTkImage(light_image=stage_single, dark_image=stage_single, size=(680, 580))
            self.single_img_lbl.configure(image=ctk_s)
            self.single_img_lbl.image = ctk_s

    # --- ファイル読込とカメラ機種スキャン ---
    def _on_drop_files(self, event):
        raw_paths = self.tk.splitlist(event.data)
        self._register_input_paths(raw_paths)

    def select_folder(self):
        folder = filedialog.askdirectory(title="書き出し元フォルダを選択")
        if folder:
            self._register_input_paths([folder])

    def select_files(self):
        files = filedialog.askopenfilenames(filetypes=[("JPEG/PNG Images", "*.jpg;*.jpeg;*.png")])
        if files:
            self._register_input_paths(files)

    def _register_input_paths(self, paths):
        valid_exts = {".jpg", ".jpeg", ".png"}
        collected = []
        for p in paths:
            path_obj = Path(p)
            if path_obj.is_dir():
                for child in sorted(path_obj.iterdir()):
                    if child.is_file() and child.suffix.lower() in valid_exts:
                        collected.append(str(child))
            elif path_obj.is_file() and path_obj.suffix.lower() in valid_exts:
                collected.append(str(path_obj))

        if not collected:
            messagebox.showwarning("画像なし", "対応する画像ファイル(.jpg / .png)が見つかりませんでした。")
            return

        self.image_files = collected
        self.single_preview_index = 0
        self._cached_dual_sources = None
        self._cached_single_source = None

        parent_name = Path(collected[0]).parent.name
        self.drop_label.configure(text=f"{len(collected)} 枚読込済  ({parent_name})", text_color="#4ade80")
        self.status_label.configure(text=f"読込完了: {len(collected)}枚")
        self._scan_cameras_in_files()
        self.schedule_preview_update()

    def _scan_cameras_in_files(self):
        for w in self.cam_list_frame.winfo_children():
            w.destroy()
        self.camera_offset_widgets.clear()

        counts = {}
        for img_p in self.image_files:
            meta = extract_exif_metadata(img_p)
            cam = meta["camera"] or "EXIFカメラ名なし"
            counts[cam] = counts.get(cam, 0) + 1

        for cam, cnt in counts.items():
            r = ctk.CTkFrame(self.cam_list_frame, fg_color="transparent")
            r.pack(fill="x", padx=4, pady=2)
            ctk.CTkLabel(r, text=f"{cam} ({cnt})", width=175, anchor="w", font=ui_font(11)).pack(side="left", padx=2)
            min_e = ctk.CTkEntry(r, width=42, height=24, corner_radius=4, fg_color=COLOR_INPUT, border_width=1, border_color=COLOR_BORDER, font=ui_font(11))
            min_e.insert(0, "0")
            min_e.pack(side="left", padx=2)
            ctk.CTkLabel(r, text="分", font=ui_font(11), text_color=COLOR_TEXT_SUB).pack(side="left", padx=(1, 4))
            sec_e = ctk.CTkEntry(r, width=42, height=24, corner_radius=4, fg_color=COLOR_INPUT, border_width=1, border_color=COLOR_BORDER, font=ui_font(11))
            sec_e.insert(0, "0")
            sec_e.pack(side="left", padx=2)
            ctk.CTkLabel(r, text="秒", font=ui_font(11), text_color=COLOR_TEXT_SUB).pack(side="left", padx=1)
            self.camera_offset_widgets[cam] = (min_e, sec_e)

    def _collect_wm_configs(self):
        configs = []
        for w in self.wm_slot_widgets:
            mode_val = "PNG" if w["mode"].get() == "PNG" else "TEXT"
            configs.append({
                "enabled": w["enabled"].get(),
                "mode": mode_val,
                "path": w["path"].get().strip(),
                "font_path": w["path"].get().strip(),
                "text": w["text"].get(),
                "position": w["pos"].get(),
                "size_pct": w["size"].get(),
                "margin_pct": w["margin"].get(),
                "opacity_pct": w["opacity"].get(),
                "tracking": w["tracking"].get(),
                "shadow": w["shadow"].get()
            })
        return configs

    def _collect_caption_config(self):
        rules = self.cap_replace_entry.get().replace(",", "\n")
        return {
            "show_camera": self.cap_cam_var.get(),
            "show_lens": self.cap_lens_var.get(),
            "show_focal": self.cap_focal_var.get(),
            "show_fnumber": self.cap_fnum_var.get(),
            "show_shutter": self.cap_ss_var.get(),
            "show_iso": self.cap_iso_var.get(),
            "show_date": self.cap_date_var.get(),
            "custom_text": self.cap_custom_entry.get(),
            "font_path": self.cap_font_entry.get().strip(),
            "font_scale_pct": self.cap_font_scale_slider.get(),
            "bottom_space_pct": self.cap_bottom_space_slider.get(),
            "replace_rules": rules
        }

    # --- スレッドセーフ & 例外復帰保証付き一括並列書き出し ---
    def _set_export_progress_ui(self, prog: float, text: str):
        self.progress_bar.set(prog)
        self.status_label.configure(text=text)

    def start_batch_export(self):
        if not self.image_files:
            messagebox.showwarning("未選択", "先に写真フォルダまたは画像ファイルを選択してください。")
            return

        active_patterns = [dict(it.data) for it in self.pattern_items if it.data.get("enabled", True)]
        if not active_patterns:
            messagebox.showwarning("パターン未選択", "少なくとも1つの出力パターンにチェックを入れてください。")
            return

        out_root = filedialog.askdirectory(title="書き出し先の親フォルダを選択してください")
        if not out_root:
            return

        self._save_state_to_file(AUTOSAVE_FILE)

        self.run_btn.configure(state="disabled", text="書き出し中...")
        self.progress_bar.set(0)
        wm_cfgs = self._collect_wm_configs()
        cap_cfg = self._collect_caption_config()
        sort_mode = self.sort_mode_cb.get()
        event_base = self.event_name_entry.get().strip() or "Event"
        make_zip = self.zip_var.get()
        sync_time = self.sync_time_var.get()
        try:
            jpeg_q = max(1, min(100, int(self.jpeg_q_entry.get() or 95)))
        except ValueError:
            jpeg_q = 95
        copyright_str = self.copyright_entry.get().strip()
        files_snapshot = list(self.image_files)

        cam_offsets = {}
        for cam, (m_e, s_e) in self.camera_offset_widgets.items():
            try:
                total_sec = int(m_e.get() or 0) * 60 + int(s_e.get() or 0)
            except ValueError:
                total_sec = 0
            cam_offsets[cam] = total_sec

        def worker():
            start_t = time.time()
            delivery_root = Path(out_root) / f"{event_base}_Delivery"
            try:
                items = []
                for p in files_snapshot:
                    meta = extract_exif_metadata(p)
                    cam = meta["camera"] or "EXIFカメラ名なし"
                    offset_sec = cam_offsets.get(cam, 0)
                    corr_dt = meta["datetime"] + timedelta(seconds=offset_sec)
                    items.append({"path": p, "meta": meta, "corrected_dt": corr_dt, "name": Path(p).stem})

                if sort_mode.startswith("EXIF撮影日時順"):
                    items.sort(key=lambda x: (x["corrected_dt"], x["meta"]["subsec"], x["name"]))
                else:
                    items.sort(key=lambda x: x["name"])

                delivery_root.mkdir(parents=True, exist_ok=True)
                for pat in active_patterns:
                    (delivery_root / pat["folder"]).mkdir(parents=True, exist_ok=True)

                total_count = len(items)
                digits = max(3, len(str(total_count)))

                def process_one(idx_and_item):
                    idx, item = idx_and_item
                    p = item["path"]
                    meta = item["meta"]
                    corr_dt = item["corrected_dt"]

                    if sort_mode.startswith("リネームしない"):
                        base_filename = f"{item['name']}.jpg"
                    else:
                        base_filename = f"{event_base}_{str(idx + 1).zfill(digits)}.jpg"

                    with Image.open(p) as raw_im:
                        icc = raw_im.info.get("icc_profile")
                        base_im = ImageOps.exif_transpose(raw_im).convert("RGB")

                    for pat in active_patterns:
                        out_img = render_single_pattern(base_im, meta, pat, wm_cfgs, cap_cfg)
                        out_path = delivery_root / pat["folder"] / base_filename
                        exif_bytes = build_filtered_exif_bytes(meta, pat["exif_mode"], corr_dt, copyright_str)

                        save_kwargs = {"quality": jpeg_q, "subsampling": 0}
                        if icc:
                            save_kwargs["icc_profile"] = icc
                        if exif_bytes:
                            save_kwargs["exif"] = exif_bytes

                        out_img.save(str(out_path), "JPEG", **save_kwargs)

                        if sync_time:
                            try:
                                ts = corr_dt.timestamp()
                                os.utime(str(out_path), (ts, ts))
                            except OSError:
                                pass

                    return idx + 1

                completed = 0
                with ThreadPoolExecutor(max_workers=min(8, (os.cpu_count() or 4))) as ex:
                    for _ in ex.map(process_one, enumerate(items)):
                        completed += 1
                        prog = completed / total_count
                        msg = f"Processing: {completed} / {total_count}"
                        self.after(0, self._set_export_progress_ui, prog, msg)

                if make_zip:
                    self.after(0, lambda: self.status_label.configure(text="Creating ZIP archives..."))
                    for pat in active_patterns:
                        folder_path = delivery_root / pat["folder"]
                        zip_path = delivery_root / f"{pat['folder']}.zip"
                        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                            for f in sorted(folder_path.iterdir()):
                                zf.write(f, arcname=f.name)

                elapsed = time.time() - start_t
                done_msg = f"完了 ({total_count}枚 × {len(active_patterns)}パターン / {elapsed:.1f}s)"
                target_dir_str = str(delivery_root)
                self.after(0, lambda m=done_msg, d=target_dir_str: (
                    self._set_export_progress_ui(1.0, m),
                    open_folder(d)
                ))
            except Exception as exc:
                err_msg = str(exc)
                self.after(0, lambda m=err_msg: (
                    self.status_label.configure(text=f"エラー中断: {m}"),
                    messagebox.showerror("書き出しエラー", f"処理中にエラーが発生しました:\n{m}")
                ))
            finally:
                self.after(0, lambda: self.run_btn.configure(state="normal", text="全パターン一括書き出し"))

        threading.Thread(target=worker, daemon=True).start()

    # ==========================================
    # プリセット保存・読込 & 自動オートセーブ機能
    # ==========================================

    def _build_state_dict(self) -> dict:
        sel_idx = 0
        if self.selected_item in self.pattern_items:
            sel_idx = self.pattern_items.index(self.selected_item)
        return {
            "event_name": self.event_name_entry.get(),
            "sort_mode": self.sort_mode_cb.get(),
            "zip": self.zip_var.get(),
            "sync_time": self.sync_time_var.get(),
            "jpeg_q": self.jpeg_q_entry.get(),
            "copyright": self.copyright_entry.get(),
            "pro_mode": self.pro_mode_var.get(),
            "preview_mode": self.preview_layout_mode.get(),
            "selected_pattern_index": sel_idx,
            "wm_configs": self._collect_wm_configs(),
            "caption_config": self._collect_caption_config(),
            "patterns": [it.data for it in self.pattern_items]
        }

    def _save_state_to_file(self, filepath: Path):
        try:
            filepath.parent.mkdir(parents=True, exist_ok=True)
            data = self._build_state_dict()
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _apply_state_dict(self, data: dict):
        if "event_name" in data:
            self.event_name_entry.delete(0, "end")
            self.event_name_entry.insert(0, data["event_name"])
        if "sort_mode" in data:
            self.sort_mode_cb.set(data["sort_mode"])
        if "zip" in data:
            self.zip_var.set(bool(data["zip"]))
        if "sync_time" in data:
            self.sync_time_var.set(bool(data["sync_time"]))
        if "jpeg_q" in data:
            self.jpeg_q_entry.delete(0, "end")
            self.jpeg_q_entry.insert(0, str(data["jpeg_q"]))
        if "copyright" in data:
            self.copyright_entry.delete(0, "end")
            self.copyright_entry.insert(0, data["copyright"])
        if "preview_mode" in data and data["preview_mode"] in ["全パターン比較", "縦横向き比較", "1枚拡大"]:
            self.preview_layout_mode.set(data["preview_mode"])
        if "pro_mode" in data:
            self.pro_mode_var.set(bool(data["pro_mode"]))
            self._toggle_ui_mode()

        for w, saved_wm in zip(self.wm_slot_widgets, data.get("wm_configs", [])):
            w["enabled"].set(saved_wm.get("enabled", False))
            m_str = "PNG" if saved_wm.get("mode") == "PNG" else "テキスト(フォント)"
            w["mode"].set(m_str)
            w["pick_btn"].configure(text="PNG参照" if m_str == "PNG" else "フォント選択")
            w["path"].delete(0, "end")
            w["path"].insert(0, saved_wm.get("path", ""))
            w["text"].delete(0, "end")
            w["text"].insert(0, saved_wm.get("text", ""))
            w["pos"].set(saved_wm.get("position", "右下"))
            w["size"].set(saved_wm.get("size_pct", 12.0))
            w["margin"].set(saved_wm.get("margin_pct", 2.0))
            w["opacity"].set(saved_wm.get("opacity_pct", 90.0))
            w["tracking"].set(saved_wm.get("tracking", 8.0))
            w["shadow"].set(saved_wm.get("shadow", "弱"))

        cap_saved = data.get("caption_config", {})
        if cap_saved:
            self.cap_cam_var.set(cap_saved.get("show_camera", True))
            self.cap_lens_var.set(cap_saved.get("show_lens", True))
            self.cap_focal_var.set(cap_saved.get("show_focal", False))
            self.cap_fnum_var.set(cap_saved.get("show_fnumber", False))
            self.cap_ss_var.set(cap_saved.get("show_shutter", False))
            self.cap_iso_var.set(cap_saved.get("show_iso", False))
            self.cap_date_var.set(cap_saved.get("show_date", False))

            self.cap_custom_entry.delete(0, "end")
            self.cap_custom_entry.insert(0, cap_saved.get("custom_text", ""))

            self.cap_font_entry.delete(0, "end")
            self.cap_font_entry.insert(0, cap_saved.get("font_path", ""))

            self.cap_font_scale_slider.set(cap_saved.get("font_scale_pct", 100.0))
            self.cap_bottom_space_slider.set(cap_saved.get("bottom_space_pct", 5.0))

            if "replace_rules" in cap_saved:
                self.cap_replace_entry.delete(0, "end")
                self.cap_replace_entry.insert(0, cap_saved["replace_rules"].replace("\n", ", "))

        patterns = data.get("patterns", [])
        if patterns:
            for it in list(self.pattern_items):
                it.destroy()
            self.pattern_items.clear()
            for pat in patterns:
                self.add_pattern_item(pat, select_now=False)

            sel_idx = int(data.get("selected_pattern_index", len(self.pattern_items) - 1))
            sel_idx = max(0, min(sel_idx, len(self.pattern_items) - 1))
            self.select_pattern_item(self.pattern_items[sel_idx])

    def _restore_autosave_if_exists(self) -> bool:
        if not AUTOSAVE_FILE.exists():
            return False
        try:
            with open(AUTOSAVE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._apply_state_dict(data)
            self.status_label.configure(text="前回の設定を自動復元しました")
            return True
        except Exception:
            return False

    def _on_close_window(self):
        self._save_state_to_file(AUTOSAVE_FILE)
        self.destroy()

    def save_preset(self):
        p = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("JSON Preset", "*.json")])
        if not p:
            return
        self._save_state_to_file(Path(p))
        messagebox.showinfo("保存完了", "プリセットを保存しました。")

    def load_preset(self):
        p = filedialog.askopenfilename(filetypes=[("JSON Preset", "*.json")])
        if not p:
            return
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        self._apply_state_dict(data)
        self.schedule_preview_update()
        messagebox.showinfo("読込完了", "プリセットを読み込みました。")


if __name__ == "__main__":
    app = PhotoDeliveryApp()
    app.mainloop()
