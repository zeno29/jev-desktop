"""The voice caption beside the orb, in the console's Live-bubble style (design handoff 3b):
dark glass, a stage-coloured border and glow, a mono uppercase label and white text.

Like the orb it's a native layered window drawn with Pillow, but fully click-through
(WS_EX_TRANSPARENT) and never activated, so it can't steal focus or block the desktop. It sits
to the left of the orb (or the right, when the orb is on the left half) and fades in and out.
"""

from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes
from pathlib import Path
from typing import Callable

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps

from .orb import (AC_SRC_ALPHA, BITMAPINFO, BITMAPINFOHEADER, BLENDFUNCTION, SPI_GETWORKAREA,
                  SW_SHOWNOACTIVATE, ULW_ALPHA, WM_CLOSE, WM_DESTROY, WM_TIMER, WNDCLASSEXW, WNDPROC,
                  WS_EX_LAYERED, WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW, WS_EX_TOPMOST, WS_POPUP, gdi32,
                  kernel32, stage_rgb, user32)

WS_EX_TRANSPARENT = 0x20
WM_APP_SHOW, WM_APP_HIDE = 0x8002, 0x8003
SW_HIDE = 0
TIMER_FADE, TIMER_HIDE = 1, 2
FADE_MS, FADE_STEPS = 16, 10
FONTS = Path(__file__).parent / "web" / "fonts"
SANS = [FONTS / "Geist.ttf", Path(r"C:\Windows\Fonts\segoeui.ttf")]
MONO = [FONTS / "GeistMono.ttf", Path(r"C:\Windows\Fonts\CascadiaMono.ttf"), Path(r"C:\Windows\Fonts\consola.ttf")]
# logical px, from the design's Live bubble
MAX_TEXT_W, PAD_X, PAD_Y, LABEL_GAP, RADIUS, SMALL_RADIUS = 330, 16, 12, 5, 18, 6
BODY_SIZE, LABEL_SIZE, LABEL_TRACKING = 15.5, 10, 0.2
GLOW = 30          # room around the bubble for its coloured glow
GAP, MAX_LINES = 10, 4
GLASS = (5, 8, 12)
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wintypes.HDC]


def _font(paths: list[Path], size: float, weight: int) -> ImageFont.FreeTypeFont:
    for path in paths:
        try:
            font = ImageFont.truetype(str(path), round(size))
        except OSError:
            continue
        try:
            font.set_variation_by_axes([weight])  # Geist is a variable font
        except (OSError, ValueError):
            pass
        return font
    return ImageFont.load_default(round(size))


def wrap(text: str, font: ImageFont.FreeTypeFont, width: int, max_lines: int) -> list[str]:
    lines: list[str] = []
    for word in text.split():
        if lines and font.getlength(lines[-1] + " " + word) <= width:
            lines[-1] += " " + word
        else:
            lines.append(word)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        while lines[-1] and font.getlength(lines[-1] + "…") > width:
            lines[-1] = lines[-1][:-1]
        lines[-1] = lines[-1].rstrip(" ,.;:") + "…"
    return lines or [""]


class Bubble:
    def __init__(self, anchor: Callable[[], tuple[int, int, int]]):
        """anchor() -> the orb's (x, y, size) in physical pixels."""
        self.anchor = anchor
        self.scale = s = user32.GetDpiForSystem() / 96
        self.body = _font(SANS, BODY_SIZE * s, 500)
        self.label = _font(MONO, LABEL_SIZE * s, 500)
        self.hwnd = None
        self._pending: tuple[str, str, str] | None = None
        self._hide_ms = 0
        self._lock = threading.Lock()
        self._alpha, self._target = 0, 0
        self._image: Image.Image | None = None
        self._pos = (0, 0)
        self._ready = threading.Event()
        self._proc = WNDPROC(self._wndproc)

    # ------------------------------------------------------------------ public (any thread)

    def start(self) -> None:
        threading.Thread(target=self._run, name="jev-bubble", daemon=True).start()
        self._ready.wait(5)

    def show(self, text: str, label: str = "Responding", stage: str = "speaking") -> None:
        """text: the body; label: the small uppercase status; stage: which colour (orb states)."""
        text = " ".join(text.split())
        if not text or not self.hwnd:
            return
        with self._lock:
            self._pending = (text, label, stage)
        user32.PostMessageW(self.hwnd, WM_APP_SHOW, 0, 0)

    def hide(self, delay: float = 0.0) -> None:
        if self.hwnd:
            self._hide_ms = round(delay * 1000)
            user32.PostMessageW(self.hwnd, WM_APP_HIDE, 0, 0)

    def stop(self) -> None:
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)

    # ------------------------------------------------------------------ window thread

    def _run(self) -> None:
        hinst = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSEXW(cbSize=ctypes.sizeof(WNDCLASSEXW), lpfnWndProc=self._proc, hInstance=hinst,
                         lpszClassName="JevCaptionBubble")
        user32.RegisterClassExW(ctypes.byref(wc))
        ex = WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_TOPMOST
        self.hwnd = user32.CreateWindowExW(ex, "JevCaptionBubble", "Jev Caption", WS_POPUP,
                                           0, 0, 1, 1, None, None, hinst, None)
        self.screen_dc = user32.GetDC(None)
        self._ready.set()
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _tracked(self, draw: ImageDraw.ImageDraw, xy: tuple[float, float], text: str, fill) -> None:
        """Letter-spaced mono label (the design's letter-spacing: .2em)."""
        x, y = xy
        step = LABEL_TRACKING * LABEL_SIZE * self.scale
        for ch in text:
            draw.text((x, y), ch, font=self.label, fill=fill)
            x += self.label.getlength(ch) + step

    def _render(self, text: str, label: str, stage: str) -> Image.Image:
        s = self.scale
        c = stage_rgb(stage)
        label = label.upper()
        pad_x, pad_y, glow = round(PAD_X * s), round(PAD_Y * s), round(GLOW * s)
        lines = wrap(text, self.body, round(MAX_TEXT_W * s), MAX_LINES)
        asc, desc = self.body.getmetrics()
        line_h = round(BODY_SIZE * 1.4 * s)
        l_asc, l_desc = self.label.getmetrics()
        label_w = sum(self.label.getlength(ch) for ch in label) + LABEL_TRACKING * LABEL_SIZE * s * (len(label) - 1)
        inner_w = max(max(self.body.getlength(ln) for ln in lines), label_w)
        bw = round(inner_w) + 2 * pad_x
        bh = pad_y + (l_asc + l_desc) + round(LABEL_GAP * s) + line_h * len(lines) + pad_y
        W, H = bw + 2 * glow, bh + 2 * glow
        box = (glow, glow, glow + bw - 1, glow + bh - 1)

        # glow: 0 0 30px C@.15 around the bubble
        im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        halo = Image.new("L", (W, H), 0)
        ImageDraw.Draw(halo).rounded_rectangle(box, radius=round(RADIUS * s), fill=255)
        halo = halo.filter(ImageFilter.GaussianBlur(glow / 2.2)).point(lambda v: round(v * 0.15))
        im.paste(Image.new("RGBA", (W, H), c + (255,)), (0, 0), halo)

        # the bubble (3x supersampled for smooth corners): glass, ambient glow, border
        ss = 3
        panel = Image.new("RGBA", (bw * ss, bh * ss), (0, 0, 0, 0))
        mask = Image.new("L", (bw * ss, bh * ss), 0)
        md = ImageDraw.Draw(mask)
        r, rs = RADIUS * s * ss, SMALL_RADIUS * s * ss
        md.rounded_rectangle((0, 0, bw * ss - 1, bh * ss - 1), radius=round(r), fill=255)
        md.rectangle((0, bh * ss - r, r, bh * ss), fill=0)              # the bottom-left corner is
        md.rounded_rectangle((0, bh * ss - 2 * r, 2 * r, bh * ss - 1),  # tighter: 18 18 18 6
                             radius=round(rs), fill=255)
        glass = Image.new("RGBA", panel.size, GLASS + (round(255 * 0.9),))
        ambient = Image.new("L", panel.size, 0)   # 260x160 ellipse at (-40,-80), C@.3 fading out by 65%
        ad = ImageDraw.Draw(ambient)
        ex0, ey0, ew, eh = -40 * s * ss, -80 * s * ss, 260 * s * ss, 160 * s * ss
        for k in range(24):
            f = 1 - k / 24
            ad.ellipse((ex0 + ew / 2 * (1 - f * 0.65 * 1.54), ey0 + eh / 2 * (1 - f * 0.65 * 1.54),
                        ex0 + ew / 2 * (1 + f * 0.65 * 1.54), ey0 + eh / 2 * (1 + f * 0.65 * 1.54)),
                       fill=round(255 * 0.3 * 0.6 * (1 - f)))
        ambient = ambient.filter(ImageFilter.GaussianBlur(8 * s * ss))
        glass.alpha_composite(Image.merge("RGBA", (*(Image.new("L", panel.size, v) for v in c), ambient)))
        panel.paste(glass, (0, 0), mask)
        line = max(ss, round(s * ss))  # a 1 px border that follows the real outline (both radii);
        pad = line + 1                 # padded so the erosion also bites at the image's own edges
        inner = ImageOps.expand(mask, border=pad, fill=0).filter(ImageFilter.MinFilter(2 * line + 1))
        border = ImageChops.subtract(mask, inner.crop((pad, pad, pad + mask.width, pad + mask.height)))
        panel.paste(Image.new("RGBA", panel.size, c + (round(255 * 0.5),)), (0, 0), border)
        im.alpha_composite(panel.resize((bw, bh), Image.LANCZOS), (glow, glow))

        d = ImageDraw.Draw(im)
        x0, y0 = glow + pad_x, glow + pad_y
        self._tracked(d, (x0, y0), label, c + (255,))
        ty = y0 + l_asc + l_desc + round(LABEL_GAP * s) + (line_h - asc - desc) // 2
        for i, ln in enumerate(lines):
            d.text((x0, ty + i * line_h), ln, font=self.body, fill=(255, 255, 255, 255))
        return im

    def _place(self, w: int, h: int) -> tuple[int, int]:
        ox, oy, osz = self.anchor()
        work = wintypes.RECT()
        user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(work), 0)
        glow, gap = round(GLOW * self.scale), round(GAP * self.scale)
        left_side = ox + osz // 2 > (work.left + work.right) // 2  # the side with room (left, normally)
        x = ox - gap - w + glow if left_side else ox + osz + gap - glow
        y = oy + osz // 2 - h // 2
        return (max(work.left - glow, min(x, work.right - w + glow)),
                max(work.top - glow, min(y, work.bottom - h + glow)))

    def _blit(self) -> None:
        im = self._image
        if im is None:
            return
        w, h = im.size
        mem_dc = gdi32.CreateCompatibleDC(self.screen_dc)
        bmi = BITMAPINFO()
        bmi.bmiHeader = BITMAPINFOHEADER(biSize=ctypes.sizeof(BITMAPINFOHEADER), biWidth=w, biHeight=-h,
                                         biPlanes=1, biBitCount=32, biCompression=0)
        bits = ctypes.c_void_p()
        dib = gdi32.CreateDIBSection(mem_dc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
        old = gdi32.SelectObject(mem_dc, dib)
        data = im.tobytes("raw", "BGRa")  # premultiplied BGRA
        ctypes.memmove(bits, data, len(data))
        blend = BLENDFUNCTION(0, 0, self._alpha, AC_SRC_ALPHA)
        user32.UpdateLayeredWindow(self.hwnd, self.screen_dc, ctypes.byref(wintypes.POINT(*self._pos)),
                                   ctypes.byref(wintypes.SIZE(w, h)), mem_dc, ctypes.byref(wintypes.POINT(0, 0)),
                                   0, ctypes.byref(blend), ULW_ALPHA)
        gdi32.SelectObject(mem_dc, old)
        gdi32.DeleteObject(dib)
        gdi32.DeleteDC(mem_dc)

    def _fade_to(self, target: int) -> None:
        self._target = target
        user32.SetTimer(self.hwnd, TIMER_FADE, FADE_MS, None)

    def _wndproc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_APP_SHOW:
                with self._lock:
                    pending, self._pending = self._pending, None
                if pending:
                    user32.KillTimer(hwnd, TIMER_HIDE)
                    self._image = self._render(*pending)
                    self._pos = self._place(*self._image.size)
                    self._blit()
                    user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
                    self._fade_to(255)
                return 0
            if msg == WM_APP_HIDE:
                if self._hide_ms > 0:
                    user32.SetTimer(hwnd, TIMER_HIDE, self._hide_ms, None)
                else:
                    self._fade_to(0)
                return 0
            if msg == WM_TIMER:
                if wparam == TIMER_HIDE:
                    user32.KillTimer(hwnd, TIMER_HIDE)
                    self._fade_to(0)
                elif wparam == TIMER_FADE:
                    step = 255 // FADE_STEPS + 1
                    self._alpha = (min(self._target, self._alpha + step) if self._target > self._alpha
                                   else max(self._target, self._alpha - step))
                    self._blit()
                    if self._alpha == self._target:
                        user32.KillTimer(hwnd, TIMER_FADE)
                        if self._alpha == 0:
                            user32.ShowWindow(hwnd, SW_HIDE)
                return 0
            if msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
        except Exception:
            pass
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)
