"""The smoke-orb desktop widget (design handoff "Stream", option 3b).

A native Win32 *layered* window with per-pixel alpha: truly transparent over any wallpaper,
anti-aliased, and click-through outside the dark disc. A ring of translucent silk strands drifts
slowly while idle (~12 fps) and swirls, swells and takes on the stage colour while Jev listens,
works or speaks (25 fps), driven by the real microphone / voice level when there is one.

Left-click: open the typing console. Drag: move it (position is remembered).
Right-click: menu (open console / quit).
"""

from __future__ import annotations

import ctypes
import json
import math
import random
import threading
import time
from ctypes import wintypes
from pathlib import Path
from typing import Callable

import numpy as np

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR), ("hIconSm", wintypes.HICON)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 1)]


class TRACKMOUSEEVENT(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("hwndTrack", wintypes.HWND), ("dwHoverTime", wintypes.DWORD)]


user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
user32.CreateWindowExW.restype = wintypes.HWND
user32.UpdateLayeredWindow.argtypes = [wintypes.HWND, wintypes.HDC, ctypes.POINTER(wintypes.POINT),
                                       ctypes.POINTER(wintypes.SIZE), wintypes.HDC, ctypes.POINTER(wintypes.POINT),
                                       wintypes.COLORREF, ctypes.POINTER(BLENDFUNCTION), wintypes.DWORD]
user32.GetDC.restype = wintypes.HDC
user32.GetDC.argtypes = [wintypes.HWND]
user32.LoadCursorW.restype = wintypes.HANDLE
user32.LoadCursorW.argtypes = [wintypes.HINSTANCE, wintypes.LPVOID]
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, wintypes.UINT]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, wintypes.LPVOID]
user32.KillTimer.argtypes = [wintypes.HWND, ctypes.c_size_t]
user32.CreatePopupMenu.restype = wintypes.HMENU
user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
user32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                  wintypes.HWND, wintypes.LPVOID]
user32.DestroyMenu.argtypes = [wintypes.HMENU]
user32.SystemParametersInfoW.argtypes = [wintypes.UINT, wintypes.UINT, wintypes.LPVOID, wintypes.UINT]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.POINTER(BITMAPINFO), wintypes.UINT,
                                   ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD]
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]

WS_POPUP = 0x80000000
WS_EX_LAYERED, WS_EX_TOOLWINDOW, WS_EX_TOPMOST, WS_EX_NOACTIVATE = 0x80000, 0x80, 0x8, 0x8000000
WM_DESTROY, WM_CLOSE, WM_TIMER, WM_MOUSEMOVE = 0x0002, 0x0010, 0x0113, 0x0200
WM_LBUTTONDOWN, WM_LBUTTONUP, WM_RBUTTONUP, WM_MOUSELEAVE, WM_NULL = 0x0201, 0x0202, 0x0205, 0x02A3, 0x0000
WM_APP_REDRAW = 0x8001
SWP_NOSIZE, SWP_NOZORDER, SWP_NOACTIVATE = 0x1, 0x4, 0x10
SW_SHOWNOACTIVATE = 4
ULW_ALPHA, AC_SRC_ALPHA = 0x2, 0x1
TPM_RETURNCMD, TPM_RIGHTBUTTON = 0x100, 0x2
SPI_GETWORKAREA = 0x30
IDC_HAND = 32649
TME_LEAVE = 0x2
FRAME_MS = 40       # 25 fps while anything is happening
IDLE_FRAME_MS = 83  # ~12 fps for the slow idle drift
DRAG_THRESHOLD = 5
MENU_OPEN, MENU_QUIT = 1, 2

# state -> (stage colour, strand speed). Error holds still.
STATES = {
    "idle":         ((84, 220, 255), 0.12),   # #54dcff: a slow drift, never fully still
    "listening":    ((150, 240, 255), 1.9),   # #96f0ff
    "transcribing": ((127, 233, 255), 2.2),   # routing's colour
    "routing":      ((127, 233, 255), 2.2),   # #7fe9ff
    "action":       ((61, 255, 184), 1.7),    # #3dffb8
    "fast":         ((56, 232, 224), 1.5),    # #38e8e0
    "deep":         ((140, 157, 255), 2.4),   # #8c9dff
    "speaking":     ((105, 205, 255), 1.1),   # #69cdff
    "confirm":      ((255, 184, 77), 0.35),   # #ffb84d
    "error":        ((255, 93, 110), 0.0),    # #ff5d6e
}
BUSY = {"transcribing", "routing", "action", "fast", "deep"}


def stage_rgb(state: str) -> tuple[int, int, int]:
    return STATES.get(state, STATES["idle"])[0]


# ---------------------------------------------------------------------------------- drawing

class SmokeRenderer:
    """The smoke orb: a ring of drifting translucent silk strands (design handoff "Stream", 3b).

    A straight port of the reference's canvas drawSmoke(). The shape changes every frame, so
    nothing is pre-rendered; numpy evaluates every strand point at once and splats the strokes into
    an additive light buffer (the canvas's 'lighter' blending), which is then composited over the
    dark backdrop disc. At 240 px that's a few milliseconds a frame.
    """

    SUBSAMPLES = 4  # points per polyline segment (~0.75 px apart at 240 px), so thin strokes stay continuous

    def __init__(self, size: int):
        self.W = W = size
        small = W <= 120                      # ~60 logical px on a 200% display
        self.N = 36 if small else round(44 * 1.8)
        # the reference samples 170 points on its 380 px canvas; keep segments ~2-3 px long here
        self.P = 90 if small else min(170, max(120, round(W * 0.45)))
        self.line = max(0.7, W / 400)
        self.cx = self.cy = W / 2
        y, x = np.mgrid[0:W, 0:W].astype(np.float32) + np.float32(0.5)
        self.dist = np.hypot(x - np.float32(self.cx), y - np.float32(self.cy))
        self.backdrop = self._backdrop()
        f32 = np.float32
        i = np.arange(self.N, dtype=f32)[:, None]
        self.fi = i / f32(self.N - 1) * f32(1.7) - f32(1.05)
        self.wisp = (np.arange(self.N) % 6 == 0)[:, None]
        self.wisp_sign = np.where(np.arange(self.N) % 12 == 0, 1.0, -0.7)[:, None].astype(f32)
        self.nz3 = np.where(self.wisp, 0.35, 0.12).astype(f32)        # third noise octave's weight
        self.alpha0 = np.where(self.wisp, 0.08, 0.10).astype(f32)     # stroke alpha before level
        self.i = i
        self.th = (np.arange(self.P + 1, dtype=f32) / f32(self.P) * f32(math.tau))[None, :]
        self.cos_th, self.sin_th = np.cos(self.th), np.sin(self.th)
        self.t = (np.arange(self.SUBSAMPLES, dtype=f32) / f32(self.SUBSAMPLES))[None, None, :]

    def _backdrop(self) -> np.ndarray:
        """Premultiplied RGBA of the dark disc. The CSS gradient is `circle` (farthest-corner), so
        its percentages are of W/2·√2: .88 to 40%, .55 at 55%, .18 at 64%, 0 at 71% (≈ the edge).
        Alpha never drops below 2/255 inside the circle so the whole orb stays clickable."""
        ray = self.W / 2 * math.sqrt(2)
        a = np.interp(self.dist / ray, [0, .40, .55, .64, .71], [.88, .88, .55, .18, 0.0]).astype(np.float32)
        a = np.where(self.dist <= self.W / 2, np.maximum(a, 2 / 255), a)
        rgb = np.array([2, 6, 10], np.float32) / 255
        return np.dstack([rgb[0] * a, rgb[1] * a, rgb[2] * a, a])

    def _splat(self, x: np.ndarray, y: np.ndarray, w: np.ndarray) -> np.ndarray:
        """Additively deposit weights at sub-pixel positions (bilinear), flattened W*W buffer.
        All four corners go through a single bincount: this is the renderer's hot loop."""
        W = self.W
        ok = (x >= 0) & (x < W - 1) & (y >= 0) & (y < W - 1)
        x, y, w = x[ok], y[ok], w[ok]
        xi, yi = x.astype(np.int32), y.astype(np.int32)   # floor: coordinates are non-negative here
        fx, fy = x - xi, y - yi
        idx = yi * W + xi
        wx0, wx1 = w * (1 - fx), w * fx
        return np.bincount(np.concatenate((idx, idx + 1, idx + W, idx + W + 1)),
                           weights=np.concatenate((wx0 * (1 - fy), wx1 * (1 - fy), wx0 * fy, wx1 * fy)),
                           minlength=W * W).astype(np.float32)

    def _polyline(self, x: np.ndarray, y: np.ndarray, alpha: np.ndarray, width: float) -> np.ndarray:
        """Stroke closed polylines (rows of x/y points): each segment is subdivided and every
        sub-point carries alpha × width × its share of the segment length (a thin AA stroke)."""
        x1, y1 = x[:, :-1, None], y[:, :-1, None]
        x2, y2 = x[:, 1:, None], y[:, 1:, None]
        seg = np.hypot(x2 - x1, y2 - y1) / self.SUBSAMPLES
        px, py = x1 + (x2 - x1) * self.t, y1 + (y2 - y1) * self.t
        w = np.broadcast_to(alpha[:, :, None] * width * seg, px.shape)
        return self._splat(px.ravel(), py.ravel(), w.ravel())

    def frame(self, rgb: tuple[float, float, float], ph: float, lv: float) -> bytes:
        """One frame as premultiplied BGRA bytes (what UpdateLayeredWindow wants)."""
        f = np.float32
        W, th, i = self.W, self.th, self.i
        ph, lv = f(ph), f(lv)
        R = f(W * 0.33 * (1 + lv * 0.04))
        spread = f(W * (0.075 + lv * 0.06))
        e1 = (f(0.5) + f(0.5) * np.sin(th + ph * f(0.3) + f(1.2))) ** 3
        e2 = (f(0.5) + f(0.5) * np.sin(th * 2 - ph * f(0.45) + f(2) + f(math.sin(ph * 0.2)))) ** 6
        env = f(0.06) + f(0.7) * e1 + f(0.5) * e2
        nz = (np.sin(th * 2 + ph * f(0.7) + i * f(0.21)) * f(0.5)
              + np.sin(th * 3 - ph * f(1.1) + i * f(0.57)) * f(0.35)
              + np.sin(th * 4 + ph * f(1.6) + i * f(0.93)) * self.nz3)
        o = np.where(self.wisp, spread * env * (np.abs(nz) * f(1.6) + f(0.2)) * self.wisp_sign,
                     spread * env * (f(0.55) * self.fi + nz * f(0.9)))
        rr = R + o
        x, y = f(self.cx) + rr * self.cos_th, f(self.cy) + rr * self.sin_th
        alpha = np.broadcast_to(self.alpha0 + lv * f(0.05), (self.N, self.P))
        light = self._polyline(x, y, alpha, self.line).reshape(W, W)

        # halo: radial gradient R*0.82 -> R*1.3; transparent, stage colour at 30%, transparent
        t = (self.dist - R * f(0.82)) / (R * f(0.48))
        peak = f(0.08) + lv * f(0.14)
        light += np.clip(np.minimum(t / f(0.3), (f(1) - t) / f(0.7)), 0, 1) * peak

        # rim: a white hairline at R, drawn analytically (anti-aliased profile of width W/300)
        d = (self.dist - R) / f(0.6)
        rim = np.exp(-d * d) * f((0.10 + lv * 0.08) * (W / 300) / (0.6 * math.sqrt(math.pi)))

        out = np.empty((W, W, 4), f)            # B, G, R, A — premultiplied, like the canvas 'lighter'
        np.minimum(f(1), light + rim, out=out[..., 3])
        bg, keep = self.backdrop, f(1) - out[..., 3]   # the canvas sits over the backdrop disc
        for ch, c in ((0, rgb[2]), (1, rgb[1]), (2, rgb[0])):
            np.minimum(f(1), light * f(c / 255) + rim, out=out[..., ch])
            out[..., ch] += bg[..., 2 - ch] * keep
        out[..., 3] += bg[..., 3] * keep
        out *= f(255)
        out += f(0.5)
        return out.astype(np.uint8).tobytes()


# ---------------------------------------------------------------------------------- window

class OrbWidget:
    def __init__(self, size_logical: int, state_path: Path, on_click: Callable[[], None],
                 on_quit: Callable[[], None], on_top: bool = True,
                 level: Callable[[], float | None] | None = None):
        """level(): the real mic / voice loudness 0..1 while listening or speaking, else None."""
        self.scale = user32.GetDpiForSystem() / 96
        self.size = round(size_logical * self.scale)
        self.state_path = state_path
        self.on_click, self.on_quit, self.on_top = on_click, on_quit, on_top
        self.level_source = level
        self.hwnd = None
        self.state = "idle"
        self.hover = False
        self.ph = 20 * (time.time() % 1)           # animation phase (random start, like the reference)
        self.speed = STATES["idle"][1]
        self.lv = 0.12                             # eased activity level 0..1
        self.rgb = list(map(float, STATES["idle"][0]))  # eased stage colour
        self._interval = 0
        self._t_last = time.perf_counter()
        self._drag: tuple | None = None
        self._ready = threading.Event()
        self._proc = WNDPROC(self._wndproc)  # keep a reference: Windows calls back into it

    # ------------------------------------------------------------------ public (any thread)

    def start(self) -> None:
        threading.Thread(target=self._run, name="jev-orb", daemon=True).start()
        self._ready.wait(10)

    def set_state(self, state: str) -> None:
        self.state = state if state in STATES else "idle"
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_APP_REDRAW, 0, 0)

    def stop(self) -> None:
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)

    def rect(self) -> tuple[int, int, int]:
        """(x, y, size) in physical pixels."""
        r = wintypes.RECT()
        user32.GetWindowRect(self.hwnd, ctypes.byref(r))
        return r.left, r.top, self.size

    # ------------------------------------------------------------------ window thread

    def _default_position(self) -> tuple[int, int]:
        work = wintypes.RECT()
        user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(work), 0)
        margin = round(18 * self.scale)
        top = round(56 * self.scale)  # below title bars, so maximised windows' close buttons stay clickable
        pos = (work.right - self.size - margin, work.top + top)
        try:
            saved = json.loads(self.state_path.read_text(encoding="utf-8"))["orb"]
            vx, vy = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)  # virtual screen origin
            vw, vh = user32.GetSystemMetrics(78), user32.GetSystemMetrics(79)
            if vx <= saved[0] <= vx + vw - self.size // 2 and vy <= saved[1] <= vy + vh - self.size // 2:
                pos = (saved[0], saved[1])
        except Exception:
            pass
        return pos

    def _save_position(self) -> None:
        x, y, _ = self.rect()
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8")) if self.state_path.exists() else {}
        except Exception:
            data = {}
        data["orb"] = [x, y]
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(data), encoding="utf-8")

    def _run(self) -> None:
        self.renderer = SmokeRenderer(self.size)
        hinst = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSEXW(cbSize=ctypes.sizeof(WNDCLASSEXW), lpfnWndProc=self._proc, hInstance=hinst,
                         hCursor=user32.LoadCursorW(None, ctypes.c_void_p(IDC_HAND)),
                         lpszClassName="JevOrbWidget")
        user32.RegisterClassExW(ctypes.byref(wc))
        x, y = self._default_position()
        ex = WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | (WS_EX_TOPMOST if self.on_top else 0)
        self.hwnd = user32.CreateWindowExW(ex, "JevOrbWidget", "Jev Orb", WS_POPUP,
                                           x, y, self.size, self.size, None, None, hinst, None)
        self._init_surface()
        self._draw()
        user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE)
        self._schedule()
        self._ready.set()
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _init_surface(self) -> None:
        self.screen_dc = user32.GetDC(None)
        self.mem_dc = gdi32.CreateCompatibleDC(self.screen_dc)
        bmi = BITMAPINFO()
        bmi.bmiHeader = BITMAPINFOHEADER(biSize=ctypes.sizeof(BITMAPINFOHEADER), biWidth=self.size,
                                         biHeight=-self.size, biPlanes=1, biBitCount=32, biCompression=0)
        self.bits = ctypes.c_void_p()
        self.dib = gdi32.CreateDIBSection(self.mem_dc, ctypes.byref(bmi), 0, ctypes.byref(self.bits), None, 0)
        gdi32.SelectObject(self.mem_dc, self.dib)

    def _draw(self) -> None:
        data = self.renderer.frame(tuple(self.rgb), self.ph, self.lv)  # premultiplied BGRA
        ctypes.memmove(self.bits, data, len(data))
        blend = BLENDFUNCTION(0, 0, 255, AC_SRC_ALPHA)
        rect = wintypes.RECT()
        user32.GetWindowRect(self.hwnd, ctypes.byref(rect))
        user32.UpdateLayeredWindow(self.hwnd, self.screen_dc, ctypes.byref(wintypes.POINT(rect.left, rect.top)),
                                   ctypes.byref(wintypes.SIZE(self.size, self.size)), self.mem_dc,
                                   ctypes.byref(wintypes.POINT(0, 0)), 0, ctypes.byref(blend), ULW_ALPHA)

    def _schedule(self) -> None:
        """25 fps while anything is happening; ~12 fps for the idle drift (it never fully stops)."""
        calm = self.state in ("idle", "error") and abs(self.speed - STATES[self.state][1]) < 0.05
        interval = IDLE_FRAME_MS if calm else FRAME_MS
        if interval != self._interval:
            self._interval = interval
            user32.SetTimer(self.hwnd, 1, interval, None)

    def _target_level(self, t: float) -> float:
        """How 'loud' the orb looks: the real mic / voice level when there is one, otherwise the
        reference's stand-in motion for the state."""
        state = self.state
        if state in ("listening", "speaking") and self.level_source is not None:
            try:
                real = self.level_source()
            except Exception:
                real = None
            if real is not None:
                return min(1.0, 0.25 + 0.75 * real)
        if state == "listening":
            return 0.3 + 0.55 * abs(math.sin(t * 3.1) * math.sin(t * 7.3 + 1)) + random.random() * 0.15
        if state == "speaking":
            return 0.3 + 0.5 * abs(math.sin(t * 4.2) * math.sin(t * 2.3 + 0.5)) + random.random() * 0.1
        if state in BUSY:
            return 0.45 + 0.1 * math.sin(t * 9)
        return 0.12 + 0.05 * math.sin(t * 1.3)

    def _animate(self) -> None:
        now = time.perf_counter()
        dt, self._t_last = min(now - self._t_last, 0.1), now
        rgb, target = STATES[self.state]
        self.speed += (target - self.speed) * min(1.0, dt * 4)
        if self.state != "error":  # an error holds the smoke still
            self.ph += dt * (0.3 + self.speed * 0.6)
        lv_target = self._target_level(now) + (0.1 if self.hover else 0.0)
        self.lv += (min(1.0, lv_target) - self.lv) * 0.25
        k = min(1.0, dt / 0.35 * 3)  # the colour follows the stage over ~.35 s, like the console
        self.rgb = [c + (n - c) * k for c, n in zip(self.rgb, rgb)]
        self._draw()
        self._schedule()

    def _wndproc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_APP_REDRAW:
                self._schedule()   # a new state: back to full frame rate straight away
                return 0
            if msg == WM_TIMER:
                self._animate()
                return 0
            if msg == WM_LBUTTONDOWN:
                pt = wintypes.POINT(); user32.GetCursorPos(ctypes.byref(pt))
                x, y, _ = self.rect()
                self._drag = (pt.x, pt.y, x, y, False)
                user32.SetCapture(hwnd)
                return 0
            if msg == WM_MOUSEMOVE:
                if self._drag:
                    pt = wintypes.POINT(); user32.GetCursorPos(ctypes.byref(pt))
                    sx, sy, wx, wy, moved = self._drag
                    if moved or abs(pt.x - sx) + abs(pt.y - sy) > DRAG_THRESHOLD:
                        self._drag = (sx, sy, wx, wy, True)
                        user32.SetWindowPos(hwnd, None, wx + pt.x - sx, wy + pt.y - sy, 0, 0,
                                            SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
                elif not self.hover:
                    self.hover = True
                    tme = TRACKMOUSEEVENT(ctypes.sizeof(TRACKMOUSEEVENT), TME_LEAVE, hwnd, 0)
                    user32.TrackMouseEvent(ctypes.byref(tme))
                return 0
            if msg == WM_MOUSELEAVE:
                self.hover = False  # the next frame picks it up (the animation never stops)
                return 0
            if msg == WM_LBUTTONUP:
                user32.ReleaseCapture()
                drag, self._drag = self._drag, None
                if drag and drag[4]:
                    self._save_position()
                else:
                    threading.Thread(target=self.on_click, daemon=True).start()
                return 0
            if msg == WM_RBUTTONUP:
                self._menu(hwnd)
                return 0
            if msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
        except Exception:
            pass
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _menu(self, hwnd) -> None:
        menu = user32.CreatePopupMenu()
        user32.AppendMenuW(menu, 0, MENU_OPEN, "Open console")
        user32.AppendMenuW(menu, 0x800, 0, None)  # separator
        user32.AppendMenuW(menu, 0, MENU_QUIT, "Quit Jev")
        pt = wintypes.POINT(); user32.GetCursorPos(ctypes.byref(pt))
        user32.SetForegroundWindow(hwnd)
        cmd = user32.TrackPopupMenu(menu, TPM_RETURNCMD | TPM_RIGHTBUTTON, pt.x, pt.y, 0, hwnd, None)
        user32.PostMessageW(hwnd, WM_NULL, 0, 0)
        user32.DestroyMenu(menu)
        if cmd == MENU_OPEN:
            threading.Thread(target=self.on_click, daemon=True).start()
        elif cmd == MENU_QUIT:
            threading.Thread(target=self.on_quit, daemon=True).start()
