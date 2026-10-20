"""
Focus Lock — таймер фокуса с принудительным отдыхом.

Цикл:  30 мин работы -> 5 мин блокировки -> 30 мин работы -> главный экран.

Во время фокуса в правом нижнем углу виден мини-таймер (поверх всех окон,
клик по нему открывает главное окно).

Во время отдыха все открытые окна сворачиваются, а поверх экрана
(на всех мониторах) показывается блокирующий экран с таймером.

Запуск:      python focus_lock.py
Для теста:   python focus_lock.py --focus 0.2 --rest 0.2   (значения в минутах)

Аварийный выход из блокировки: Ctrl + Shift + F12
"""

import argparse
import math
import os
import sys
import time
import tkinter as tk

IS_WIN = sys.platform.startswith("win")

if IS_WIN:
    import ctypes
    from ctypes import wintypes

    try:
        import winsound
    except ImportError:  # pragma: no cover
        winsound = None
else:
    winsound = None

# ───────────────────────────── Дизайн ─────────────────────────────
BG = "#0E1016"
CARD = "#161A23"
TRACK = "#222735"
TEXT = "#EEF0F6"
MUTED = "#8B93A7"
ACCENT = "#7C5CFF"
ACCENT_HOVER = "#947AFF"
REST = "#3DDBB0"
OV_BG = "#080A10"
FONT = "Segoe UI"


# ─────────────────────── Работа с окнами Windows ───────────────────────
SKIP_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}


def virtual_screen(root):
    """Прямоугольник, покрывающий все мониторы: x, y, w, h."""
    if IS_WIN:
        u = ctypes.windll.user32
        return u.GetSystemMetrics(76), u.GetSystemMetrics(77), u.GetSystemMetrics(78), u.GetSystemMetrics(79)
    return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight()


def minimize_others(already):
    """Сворачивает все чужие видимые окна. Возвращает список свёрнутых hwnd."""
    if not IS_WIN:
        return []
    user32 = ctypes.windll.user32
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]

    own_pid = os.getpid()
    done = []

    def cloaked(hwnd):
        try:
            val = wintypes.DWORD(0)
            ctypes.windll.dwmapi.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(val), ctypes.sizeof(val))
            return val.value != 0
        except Exception:
            return False

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, _lparam):
        try:
            if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
                return True
            if hwnd in already:
                return True
            pid = wintypes.DWORD(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == own_pid:
                return True
            if user32.GetWindowTextLengthW(hwnd) == 0:
                return True
            if user32.GetWindowLongW(hwnd, -20) & 0x80:  # WS_EX_TOOLWINDOW
                return True
            buf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, buf, 256)
            if buf.value in SKIP_CLASSES or cloaked(hwnd):
                return True
            user32.ShowWindow(hwnd, 6)  # SW_MINIMIZE
            done.append(hwnd)
        except Exception:
            pass
        return True

    user32.EnumWindows(callback, 0)
    return done


def restore_windows(hwnds):
    if not IS_WIN:
        return
    user32 = ctypes.windll.user32
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    for hwnd in reversed(hwnds):
        try:
            if user32.IsWindow(hwnd):
                user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        except Exception:
            pass


def work_area(root):
    """Рабочая область основного монитора (без панели задач): x, y, w, h."""
    if IS_WIN:
        try:
            rect = wintypes.RECT()
            if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):  # SPI_GETWORKAREA
                return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top
        except Exception:
            pass
    return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight()


def no_activate(win):
    """Окно не забирает фокус у приложений и не показывается на панели задач (Windows)."""
    if not IS_WIN:
        return
    try:
        user32 = ctypes.windll.user32
        user32.GetParent.argtypes = [wintypes.HWND]
        user32.GetParent.restype = wintypes.HWND
        user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
        win.update_idletasks()
        hwnd = user32.GetParent(win.winfo_id()) or win.winfo_id()
        style = user32.GetWindowLongW(hwnd, -20)  # GWL_EXSTYLE
        user32.SetWindowLongW(hwnd, -20, style | 0x08000000 | 0x00000080)  # NOACTIVATE | TOOLWINDOW
    except Exception:
        pass


# ───────────────────────────── Рисование ─────────────────────────────
def rounded(canvas, x1, y1, x2, y2, r, **kw):
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
           x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return canvas.create_polygon(pts, smooth=True, **kw)


def fmt(seconds):
    s = max(0, math.ceil(seconds))
    return f"{s // 60:02d}:{s % 60:02d}"


# ───────────────────────────── Приложение ─────────────────────────────
class App(tk.Tk):
    W, H = 420, 640

    def __init__(self, focus_min, rest_min):
        super().__init__()
        self.focus_s = max(1, int(focus_min * 60))
        self.rest_s = max(1, int(rest_min * 60))
        self.phases = [("focus", 1), ("rest", 0), ("focus", 2)]

        self.title("Focus Lock")
        self.configure(bg=BG)
        self.resizable(False, False)
        x = (self.winfo_screenwidth() - self.W) // 2
        y = (self.winfo_screenheight() - self.H) // 2
        self.geometry(f"{self.W}x{self.H}+{x}+{y}")

        self.canvas = tk.Canvas(self, width=self.W, height=self.H, bg=BG, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)

        self.overlay = None
        self.ov_canvas = None
        self.widget = None
        self.wg_canvas = None
        self.wg_arc = None
        self.wg_time = None
        self.wg_n = 0
        self.minimized = []
        self.after_tick = None
        self.after_keep = None
        self.idx = -1
        self.deadline = 0.0
        self.total = 1
        self.kind = None
        self.message = ""

        self.protocol("WM_DELETE_WINDOW", self.quit_app)
        self.show_home()

    # ───────── вспомогательное ─────────
    def button(self, cx, cy, w, h, text, command, fill, hover, fg="#FFFFFF", outline=""):
        tag = f"btn_{text}"
        shape = rounded(self.canvas, cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2, h / 2,
                        fill=fill, outline=outline, width=2 if outline else 0, tags=(tag,))
        self.canvas.create_text(cx, cy, text=text, fill=fg, font=(FONT, 13, "bold"), tags=(tag,))

        def enter(_e):
            self.canvas.itemconfigure(shape, fill=hover)
            self.canvas.configure(cursor="hand2")

        def leave(_e):
            self.canvas.itemconfigure(shape, fill=fill)
            self.canvas.configure(cursor="")

        self.canvas.tag_bind(tag, "<Enter>", enter)
        self.canvas.tag_bind(tag, "<Leave>", leave)
        self.canvas.tag_bind(tag, "<Button-1>", lambda _e: command())

    def cancel_timers(self):
        for name in ("after_tick", "after_keep"):
            aid = getattr(self, name)
            if aid:
                try:
                    self.after_cancel(aid)
                except Exception:
                    pass
                setattr(self, name, None)

    def draw_ring(self, canvas, cx, cy, r, color, width=14):
        canvas.create_oval(cx - r, cy - r, cx + r, cy + r, outline=TRACK, width=width)
        return canvas.create_arc(cx - r, cy - r, cx + r, cy + r, start=90, extent=-359.9,
                                 style="arc", outline=color, width=width)

    # ───────── экраны ─────────
    def show_home(self):
        self.cancel_timers()
        self.close_widget()
        self.close_overlay()
        self.kind = None
        c = self.canvas
        c.delete("all")
        c.configure(bg=BG)

        # декоративное свечение
        for i, col in enumerate(("#14172A", "#171A30", "#1B1F3A")):
            r = 150 - i * 30
            c.create_oval(self.W / 2 - r, 170 - r, self.W / 2 + r, 170 + r, fill=col, outline="")
        c.create_oval(self.W / 2 - 62, 170 - 62, self.W / 2 + 62, 170 + 62, outline=ACCENT, width=6)
        c.create_oval(self.W / 2 - 14, 170 - 14, self.W / 2 + 14, 170 + 14, fill=ACCENT, outline="")

        c.create_text(self.W / 2, 305, text="FOCUS LOCK", fill=TEXT, font=(FONT, 28, "bold"))
        c.create_text(self.W / 2, 340, text="Работай глубоко. Отдыхай по расписанию.",
                      fill=MUTED, font=(FONT, 11))

        # карточка с планом сессии
        rounded(c, 50, 375, self.W - 50, 455, 18, fill=CARD, outline="")
        f, r = self.focus_s // 60, self.rest_s // 60
        f_txt = f"{f} мин" if self.focus_s % 60 == 0 else f"{self.focus_s} сек"
        r_txt = f"{r} мин" if self.rest_s % 60 == 0 else f"{self.rest_s} сек"
        c.create_text(self.W / 2, 400, text=f"{f_txt}  ›  {r_txt} отдых  ›  {f_txt}",
                      fill=TEXT, font=(FONT, 13, "bold"))
        c.create_text(self.W / 2, 428, text="работа · блокировка · работа",
                      fill=MUTED, font=(FONT, 10))

        if self.message:
            c.create_text(self.W / 2, 482, text=self.message, fill=REST, font=(FONT, 11, "bold"))

        self.button(self.W / 2, 530, 260, 54, "Начать работу", self.start_session, ACCENT, ACCENT_HOVER)
        self.button(self.W / 2, 592, 260, 46, "Выйти", self.quit_app, BG, CARD, fg=MUTED, outline=TRACK)

    def show_focus(self, round_no):
        c = self.canvas
        c.delete("all")
        c.configure(bg=BG)
        c.create_text(self.W / 2, 70, text=f"ФОКУС  ·  КРУГ {round_no} ИЗ 2",
                      fill=MUTED, font=(FONT, 11, "bold"))
        cx, cy = self.W / 2, 270
        self.ring_arc = self.draw_ring(c, cx, cy, 130, ACCENT)
        self.time_text = c.create_text(cx, cy - 6, text="00:00", fill=TEXT, font=(FONT, 48, "bold"))
        c.create_text(cx, cy + 46, text="до перерыва" if round_no == 1 else "до конца сессии",
                      fill=MUTED, font=(FONT, 11))

        # индикатор кругов
        for i in range(2):
            col = ACCENT if i < round_no else TRACK
            x = self.W / 2 - 14 + i * 28
            c.create_oval(x - 6, 470 - 6, x + 6, 470 + 6, fill=col, outline="")

        self.button(self.W / 2, 560, 240, 48, "Остановить", self.show_home, CARD, TRACK, fg=TEXT, outline=TRACK)

        self.open_widget(round_no)
        self.iconify()  # работаем дальше, таймер виден в углу

    # ───────── мини-таймер в углу экрана ─────────
    WG_W, WG_H, WG_MARGIN = 210, 76, 18

    def open_widget(self, round_no):
        self.close_widget()
        w = tk.Toplevel(self)
        w.overrideredirect(True)
        w.configure(bg=BG)
        try:
            w.attributes("-topmost", True)
            w.attributes("-alpha", 0.95)
        except tk.TclError:
            pass
        ax, ay, aw, ah = work_area(self)
        x = ax + aw - self.WG_W - self.WG_MARGIN
        y = ay + ah - self.WG_H - self.WG_MARGIN
        w.geometry(f"{self.WG_W}x{self.WG_H}+{x}+{y}")

        cv = tk.Canvas(w, width=self.WG_W, height=self.WG_H, bg=BG, highlightthickness=0, cursor="hand2")
        cv.pack(fill="both", expand=True)
        rounded(cv, 2, 2, self.WG_W - 2, self.WG_H - 2, 18, fill=CARD, outline=TRACK, width=2)
        self.wg_arc = self.draw_ring(cv, 38, self.WG_H / 2, 20, ACCENT, width=5)
        self.wg_time = cv.create_text(124, 32, text=fmt(self.total), fill=TEXT, font=(FONT, 24, "bold"))
        cv.create_text(124, 57, text=f"фокус · круг {round_no} из 2", fill=MUTED, font=(FONT, 9))
        cv.bind("<Button-1>", lambda _e: self.show_main())
        no_activate(w)
        self.widget, self.wg_canvas, self.wg_n = w, cv, 0

    def close_widget(self):
        if self.widget is not None:
            try:
                self.widget.destroy()
            except tk.TclError:
                pass
        self.widget = self.wg_canvas = None

    def update_widget(self, remaining, extent):
        if self.wg_canvas is None:
            return
        try:
            self.wg_canvas.itemconfigure(self.wg_arc, extent=extent)
            self.wg_canvas.itemconfigure(self.wg_time, text=fmt(remaining))
            self.wg_n += 1
            if self.wg_n % 5 == 0:  # раз в секунду возвращаем поверх остальных окон
                self.widget.attributes("-topmost", True)
                self.widget.lift()
        except tk.TclError:
            pass

    def show_main(self):
        self.deiconify()
        self.lift()

    # ───────── логика сессии ─────────
    def start_session(self):
        self.message = ""
        self.idx = -1
        self.next_phase()

    def next_phase(self):
        if self.kind == "rest":
            self.end_rest()
        self.idx += 1
        if self.idx >= len(self.phases):
            self.finish()
            return
        self.kind, num = self.phases[self.idx]
        self.total = self.focus_s if self.kind == "focus" else self.rest_s
        self.deadline = time.monotonic() + self.total
        if self.kind == "focus":
            self.show_focus(num)
        else:
            self.begin_rest()
        self.tick()

    def tick(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0.05:
            self.next_phase()
            return
        frac = remaining / self.total
        extent = -max(0.1, 359.9 * frac)
        if self.kind == "focus":
            self.canvas.itemconfigure(self.ring_arc, extent=extent)
            self.canvas.itemconfigure(self.time_text, text=fmt(remaining))
            self.update_widget(remaining, extent)
        elif self.kind == "rest" and self.ov_canvas is not None:
            self.ov_canvas.itemconfigure(self.ov_arc, extent=extent)
            self.ov_canvas.itemconfigure(self.ov_time, text=fmt(remaining))
        self.after_tick = self.after(200, self.tick)

    def finish(self):
        self.cancel_timers()
        self.beep()
        self.message = "Сессия завершена — отличная работа!"
        self.deiconify()
        self.show_home()
        self.lift()
        self.focus_force()

    # ───────── блокировка (отдых) ─────────
    def begin_rest(self):
        self.close_widget()
        self.beep()
        self.minimized = minimize_others(set())
        vx, vy, vw, vh = virtual_screen(self)

        ov = tk.Toplevel(self)
        ov.overrideredirect(True)
        ov.configure(bg=OV_BG, cursor="arrow")
        ov.geometry(f"{vw}x{vh}{vx:+d}{vy:+d}")
        ov.attributes("-topmost", True)
        ov.protocol("WM_DELETE_WINDOW", lambda: None)
        ov.bind("<Alt-F4>", lambda _e: "break")
        ov.bind("<Control-Shift-F12>", lambda _e: self.emergency_exit())
        self.overlay = ov

        cv = tk.Canvas(ov, width=vw, height=vh, bg=OV_BG, highlightthickness=0)
        cv.pack(fill="both", expand=True)
        self.ov_canvas = cv

        # центр основного монитора в координатах оверлея
        cx = -vx + self.winfo_screenwidth() / 2
        cy = -vy + self.winfo_screenheight() / 2
        for i, col in enumerate(("#0B1114", "#0D1519", "#101C20")):
            r = 300 - i * 50
            cv.create_oval(cx - r, cy - r, cx + r, cy + r, fill=col, outline="")
        cv.create_text(cx, cy - 250, text="ВРЕМЯ ОТДЫХА", fill=REST, font=(FONT, 16, "bold"))
        self.ov_arc = self.draw_ring(cv, cx, cy, 170, REST, width=16)
        self.ov_time = cv.create_text(cx, cy - 8, text=fmt(self.rest_s), fill=TEXT, font=(FONT, 76, "bold"))
        cv.create_text(cx, cy + 70, text="приложения заблокированы", fill=MUTED, font=(FONT, 13))
        cv.create_text(cx, cy + 250, text="Отведи взгляд от экрана  ·  разомни шею и плечи  ·  выпей воды",
                       fill=MUTED, font=(FONT, 14))

        ov.update_idletasks()
        self.keep_on_top()

    def keep_on_top(self):
        if self.overlay is None:
            return
        try:
            self.overlay.attributes("-topmost", True)
            self.overlay.lift()
            self.overlay.focus_force()
            try:
                self.overlay.grab_set()
            except tk.TclError:
                pass
            # новые окна, появившиеся во время отдыха, сворачиваем снова
            self.minimized += minimize_others(set(self.minimized))
        except tk.TclError:
            return
        self.after_keep = self.after(700, self.keep_on_top)

    def close_overlay(self):
        if self.overlay is not None:
            try:
                self.overlay.grab_release()
            except tk.TclError:
                pass
            try:
                self.overlay.destroy()
            except tk.TclError:
                pass
        self.overlay = None
        self.ov_canvas = None
        if self.after_keep:
            try:
                self.after_cancel(self.after_keep)
            except Exception:
                pass
            self.after_keep = None

    def end_rest(self):
        self.close_overlay()
        restore_windows(self.minimized)
        self.minimized = []
        self.deiconify()
        self.lift()

    def emergency_exit(self):
        self.cancel_timers()
        self.end_rest()
        self.message = "Блокировка прервана вручную"
        self.show_home()

    # ───────── прочее ─────────
    def beep(self):
        try:
            if winsound:
                winsound.MessageBeep(winsound.MB_ICONASTERISK)
            else:
                self.bell()
        except Exception:
            pass

    def quit_app(self):
        self.cancel_timers()
        self.close_widget()
        if self.overlay is not None:
            self.end_rest()
        self.destroy()


def main():
    parser = argparse.ArgumentParser(description="Focus Lock")
    parser.add_argument("--focus", type=float, default=30, help="минут работы (по умолчанию 30)")
    parser.add_argument("--rest", type=float, default=5, help="минут отдыха (по умолчанию 5)")
    args = parser.parse_args()

    if IS_WIN:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

    App(args.focus, args.rest).mainloop()


if __name__ == "__main__":
    main()
