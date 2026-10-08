"""Optional Tk GUI for the CSS45 spectral sensor.

Run: python -m pycss45_gui   (pick "Demo" as device to try it without hardware)

Click Measure for a single-shot spectrum. Photopic/radiometric integrals and
the CIE 1976 colour are computed from the measured spectrum with luxpy, not
from the driver's get_photopic()/get_par(). The File writer tab appends each
measurement to a CSV file.
"""

from __future__ import annotations

import csv
import tkinter as tk
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, ttk
from typing import TypeVar

import numpy as np
from matplotlib.axes import Axes
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
from matplotlib.lines import Line2D

import luxpy as lx

from pycss45 import CSS45, Css45Error, IntegrationTime, Measurement, N_PIXELS

BG: str = "#1d1f24"
PANEL: str = "#272a31"
EDGE: str = "#3a3e47"
FG: str = "#e8e8e8"
MUTED: str = "#8b909a"
ACCENT: str = "#4fa3ff"
FIG_BG: str = "#22252b"
DEMO: str = "Demo (no hardware)"
AUTO: str = "Auto (USB HID)"


def _it_label(it: IntegrationTime) -> str:
    s: float = it.seconds
    if s < 1e-3:
        return f"{s * 1e6:g} us"
    if s < 1:
        return f"{s * 1e3:g} ms"
    return f"{s:g} s"


IT_AUTO: str = "Auto"
IT_BY_LABEL: dict[str, IntegrationTime] = {
    _it_label(it): it for it in IntegrationTime
}
IT_LABELS: list[str] = [IT_AUTO, *IT_BY_LABEL]
PLACEHOLDER: str = "-"
CSV_META: list[str] = [
    "timestamp", "integration_time_s", "photopic_lx", "radiometric_W_m2",
    "Y", "u_prime", "v_prime",
]

Yuv = tuple[float, float, float]
T = TypeVar("T")


def analyse(wavelengths: np.ndarray, spectrum: np.ndarray) -> tuple[float, float, Yuv]:
    """Photopic integral (lx), radiometric integral (W/m2) and CIE 1976
    (Y, u', v') from one spectrum.

    luxpy 1.11.4 API as verified in this environment: spd_to_xyz with
    relative=False returns absolute XYZ, so Y is the photopic integral in lx.
    """
    S: np.ndarray = np.vstack([wavelengths, spectrum])  # 2 x N
    xyz: np.ndarray = lx.spd_to_xyz(S, relative=False)  # absolute XYZ
    photopic_lx: float = float(xyz[0, 1])  # Y is the photopic integral in lx
    radiometric_wm2: float = float(np.trapz(spectrum, wavelengths))
    y, u, v = (float(c) for c in lx.xyz_to_Yuv(xyz)[0])  # [[Y, u', v']]
    return photopic_lx, radiometric_wm2, (y, u, v)


def append_csv(
    path: Path, m: Measurement, photopic_lx: float, radiometric_wm2: float,
    yuv: Yuv,
) -> None:
    """Append m as one row: CSV_META columns, then one column per wavelength
    (header = wavelength in nm). A new or empty file gets the header first;
    an existing file with a different header (other wavelength grid) raises
    ValueError instead of mixing incompatible rows."""
    header: list[str] = CSV_META + [f"{wl:.2f}" for wl in m.wavelengths]
    new: bool = not path.exists() or path.stat().st_size == 0
    if not new:
        with path.open(newline="") as f:
            if next(csv.reader(f), None) != header:
                raise ValueError(
                    f"{path.name} has a different header (other device or "
                    "wavelength grid); choose another file"
                )
    with path.open("a", newline="") as f:
        writer = csv.writer(f)
        if new:
            writer.writerow(header)
        writer.writerow(
            [
                datetime.now().isoformat(timespec="seconds"),
                f"{m.integration_time:.6g}",
                f"{photopic_lx:.7g}",
                f"{radiometric_wm2:.7g}",
                *(f"{c:.7g}" for c in yuv),
            ]
            + [f"{v:.7g}" for v in m.spectrum]
        )


class DemoCSS45:
    """Stand-in with the subset of the CSS45 API the GUI uses.

    A warm-white LED: blue pump at 448 nm plus a phosphor bump at 596 nm,
    deterministic, in honest W m^-2 nm^-1 (~49 lx, ~0.15 W/m2, ~3000 K).
    """

    def __init__(self) -> None:
        wl: np.ndarray = np.linspace(380.0, 780.0, N_PIXELS)
        blue: np.ndarray = 3.0e-4 * np.exp(-0.5 * ((wl - 448.0) / 15.0) ** 2)
        phosphor: np.ndarray = 9.3e-4 * np.exp(-0.5 * ((wl - 596.0) / 58.0) ** 2)
        self._wavelengths: np.ndarray = wl
        self._spectrum: np.ndarray = blue + phosphor

    def measure(self) -> Measurement:
        return self._measurement(160000.0)  # us; vendor table index MS160

    def measure_manual(self, integration_time: IntegrationTime) -> Measurement:
        return self._measurement(integration_time.seconds * 1e6)

    def _measurement(self, us: float) -> Measurement:
        return Measurement(
            wavelengths=self._wavelengths.copy(),
            spectrum=self._spectrum.copy(),
            integration_time=us / 1e6,  # s
            cct=3008.0,
            photopic=48.7,
            par=0.67,
        )

    def connected(self) -> bool:
        return True

    def disconnect(self) -> None:
        pass

    def get_serial_number(self) -> str:
        return "DEMO0121"


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root: tk.Tk = root
        self.dev: CSS45 | DemoCSS45 | None = None
        self.status: tk.StringVar = tk.StringVar(value="Not connected")

        root.title("CSS45 Spectrum")
        root.configure(bg=BG)
        root.resizable(False, False)
        self._style()

        top = ttk.Frame(root, padding=(12, 10))
        top.pack(fill="x")
        ttk.Label(top, text="Device").pack(side="left")
        self.paths: dict[str, str | None] = {}  # label -> HID path (None: auto)
        self.dev_var: tk.StringVar = tk.StringVar()
        self.dev_box = ttk.Combobox(
            top, textvariable=self.dev_var, width=28, state="readonly",
        )
        self.dev_box.pack(side="left", padx=6)
        ttk.Button(top, text="Rescan", command=self.scan_devices).pack(side="left")
        self.conn_btn = ttk.Button(top, text="Connect", command=self.toggle_connection)
        self.conn_btn.pack(side="left", padx=6)
        self.scan_devices()

        tabs = ttk.Notebook(root, padding=(12, 0))
        tabs.pack(fill="both", expand=True)
        body = ttk.Frame(tabs, padding=(10, 10))
        tabs.add(body, text="Measure")
        writer_tab = ttk.Frame(tabs, padding=(12, 12))
        tabs.add(writer_tab, text="File writer")

        self.fig: Figure = Figure(figsize=(6.4, 4.6), dpi=100, facecolor=FIG_BG)
        self.fig.subplots_adjust(left=0.10, right=0.97, top=0.95, bottom=0.11)
        self.ax: Axes = self.fig.add_subplot(111)
        self.canvas = FigureCanvasTkAgg(self.fig, master=body)
        self.canvas.get_tk_widget().pack(side="left", fill="both", expand=True)

        panel = ttk.Frame(body, padding=(12, 0, 0, 0))
        panel.pack(side="left", fill="y")
        ttk.Label(panel, text="Integration time", foreground=MUTED).pack(anchor="w")
        self.it_var: tk.StringVar = tk.StringVar(value=IT_LABELS[0])
        ttk.Combobox(
            panel, textvariable=self.it_var, width=12, state="readonly",
            values=IT_LABELS,
        ).pack(fill="x", pady=(2, 6))
        buttons = ttk.Frame(panel)
        buttons.pack(fill="x")
        ttk.Button(
            buttons, text="Measure", style="Go.TButton", command=self.measure
        ).pack(side="left", fill="x", expand=True)
        ttk.Button(buttons, text="Clear", command=self.clear).pack(
            side="left", fill="y", padx=(6, 0)
        )
        ttk.Label(
            panel, text="auto integration time can block the UI for a few seconds",
            foreground=MUTED, wraplength=216, justify="left",
        ).pack(fill="x", pady=(4, 10))

        self.photopic_var: tk.StringVar = self._card(
            panel, "Photopic integral (lx)", "Value.TLabel"
        )[1]
        self.radiometric_var: tk.StringVar = self._card(
            panel, "Radiometric integral (W/m^2)", "Value.TLabel"
        )[1]
        colour_box: ttk.Frame
        colour_box, self.colour_var = self._card(
            panel, "Colour (CIE 1976 u'v')", "Wide.TLabel"
        )
        self._build_diagram(colour_box)

        self._build_writer_tab(writer_tab)

        ttk.Label(
            root, textvariable=self.status, foreground=MUTED, padding=(12, 8)
        ).pack(fill="x")

        root.protocol("WM_DELETE_WINDOW", self.close)
        self._draw_empty()

    def _card(
        self, parent: ttk.Frame, title: str, style: str,
    ) -> tuple[ttk.Frame, tk.StringVar]:
        """Titled value card; returns the card and the variable holding its
        value."""
        box = ttk.Frame(parent, style="Card.TFrame", padding=(10, 6))
        box.pack(fill="x", pady=(0, 8))
        ttk.Label(box, text=title, style="CardMuted.TLabel").pack(anchor="w")
        var = tk.StringVar(value=PLACEHOLDER)
        ttk.Label(
            box, textvariable=var, style=style, wraplength=190, justify="left"
        ).pack(anchor="w")
        return box, var

    def _build_diagram(self, parent: ttk.Frame) -> None:
        """CIE 1976 u'v' diagram with the measured colour marked by an x."""
        fig: Figure = Figure(figsize=(2.3, 2.3), dpi=100, facecolor=PANEL)
        fig.subplots_adjust(left=0.18, right=0.97, top=0.97, bottom=0.14)
        ax: Axes = fig.add_subplot(111)
        lx.plotSL(
            cspace="Yuv", BBL=True, DL=False, axh=ax, show=False,
            diagram_colors=True, formatstr="w-",
        )
        ax.set_facecolor(PANEL)
        ax.set_xlim(0.0, 0.65)
        ax.set_ylim(0.0, 0.62)
        ax.set_xlabel("u'", color=FG, fontsize=8, labelpad=1)
        ax.set_ylabel("v'", color=FG, fontsize=8, labelpad=4, rotation=0)
        ax.tick_params(colors=MUTED, labelsize=7)
        for spine in ax.spines.values():
            spine.set_color(EDGE)
        self._marker: Line2D = ax.plot(
            [], [], "x", color="black", markersize=9, markeredgewidth=2.2
        )[0]
        self.diagram = FigureCanvasTkAgg(fig, master=parent)
        self.diagram.get_tk_widget().pack(anchor="w", pady=(6, 0))
        self.diagram.draw()

    def _build_writer_tab(self, tab: ttk.Frame) -> None:
        self.csv_path: tk.StringVar = tk.StringVar()
        self.csv_on: tk.BooleanVar = tk.BooleanVar(value=False)
        self.csv_rows: int = 0
        self.csv_info: tk.StringVar = tk.StringVar(value="Writer off")

        ttk.Label(tab, text="CSV file", foreground=MUTED).pack(anchor="w")
        row = ttk.Frame(tab)
        row.pack(fill="x", pady=(2, 10))
        ttk.Entry(row, textvariable=self.csv_path, width=70).pack(
            side="left", fill="x", expand=True
        )
        ttk.Button(row, text="Browse...", command=self._browse_csv).pack(
            side="left", padx=(6, 0)
        )
        ttk.Checkbutton(
            tab, text="Append every measurement to this file",
            variable=self.csv_on, command=self._toggle_csv,
        ).pack(anchor="w")
        ttk.Label(tab, textvariable=self.csv_info).pack(anchor="w", pady=(10, 0))
        ttk.Label(
            tab,
            text=(
                "One row per measurement: " + ", ".join(CSV_META) + ", then "
                "one column per wavelength (header = wavelength in nm). "
                "Existing files are appended to if their header matches."
            ),
            foreground=MUTED, wraplength=640, justify="left",
        ).pack(anchor="w", pady=(10, 0))

    def _browse_csv(self) -> None:
        path: str = filedialog.asksaveasfilename(
            parent=self.root, title="Write spectra to", defaultextension=".csv",
            filetypes=[("CSV", "*.csv"), ("All files", "*")],
            confirmoverwrite=False,  # rows are appended, nothing is overwritten
        )
        if path:
            self.csv_path.set(path)

    def _toggle_csv(self) -> None:
        if self.csv_on.get() and not self.csv_path.get().strip():
            self.csv_on.set(False)
            self.csv_info.set("Choose a file first")
            return
        self.csv_rows = 0
        self.csv_info.set(
            f"Writing to {self.csv_path.get().strip()}" if self.csv_on.get()
            else "Writer off"
        )

    def _style(self) -> None:
        s = ttk.Style(self.root)
        s.theme_use("clam")
        s.configure(".", background=BG, foreground=FG, fieldbackground=PANEL)
        s.configure(
            "TButton", background=PANEL, foreground=FG, borderwidth=0, padding=6
        )
        s.map("TButton", background=[("active", "#353943")])
        s.configure(
            "Go.TButton", font=("TkDefaultFont", 13, "bold"), padding=(18, 12),
            background=ACCENT, foreground="#0d1117",
        )
        s.map("Go.TButton", background=[("active", "#7bbcff")])
        edge: dict[str, str] = {
            "bordercolor": EDGE, "lightcolor": PANEL, "darkcolor": PANEL
        }
        s.configure("TCombobox", arrowcolor=FG, background=PANEL, **edge)
        s.map(
            "TCombobox",
            fieldbackground=[("readonly", PANEL)],
            foreground=[("readonly", FG)],
            background=[("active", "#353943"), ("readonly", PANEL)],
            selectbackground=[("readonly", PANEL)],
            selectforeground=[("readonly", FG)],
        )
        for opt, val in (("background", PANEL), ("foreground", FG),
                         ("selectBackground", ACCENT), ("selectForeground", BG)):
            self.root.option_add(f"*TCombobox*Listbox.{opt}", val)
        s.configure("TEntry", foreground=FG, insertcolor=FG, **edge)
        s.configure("TCheckbutton", indicatorbackground=PANEL, indicatorforeground=FG)
        s.map("TCheckbutton", background=[("active", BG)])
        s.configure("TNotebook", background=BG, tabmargins=0, **edge)
        s.configure("TNotebook.Tab", background=PANEL, foreground=MUTED,
                    padding=(14, 5), **edge)
        s.map("TNotebook.Tab", background=[("selected", BG)],
              foreground=[("selected", FG)])
        s.configure("Card.TFrame", background=PANEL)
        s.configure("CardMuted.TLabel", background=PANEL, foreground=MUTED)
        s.configure(
            "Value.TLabel", background=PANEL, font=("TkDefaultFont", 15, "bold")
        )
        s.configure(
            "Wide.TLabel", background=PANEL, font=("TkDefaultFont", 11, "bold")
        )

    # --- plot -------------------------------------------------------------

    def _style_axes(self) -> None:
        ax = self.ax
        ax.set_facecolor(FIG_BG)
        ax.tick_params(colors=MUTED, labelsize=8)
        for side, spine in ax.spines.items():
            spine.set_color(EDGE)
            spine.set_visible(side in ("bottom", "left"))
        ax.grid(True, color=EDGE, linewidth=0.5, alpha=0.6)
        ax.set_axisbelow(True)
        ax.set_xlabel("Wavelength (nm)", color=FG, fontsize=9)
        ax.set_ylabel(
            r"Spectral irradiance (W m$^{-2}$ nm$^{-1}$)", color=FG, fontsize=9
        )

    def _draw_empty(self) -> None:
        self.ax.clear()
        self._style_axes()
        self.ax.set_xlim(380, 780)
        self.ax.set_ylim(0, 1)
        self.ax.text(
            0.5, 0.5, "no measurement yet", color=MUTED,
            transform=self.ax.transAxes, ha="center", va="center",
        )
        self.canvas.draw()

    def _draw_spectrum(self, wavelengths: np.ndarray, spectrum: np.ndarray) -> None:
        self.ax.clear()
        self._style_axes()
        peak: float = float(np.max(spectrum)) if spectrum.size else 0.0
        self.ax.set_xlim(float(wavelengths[0]), float(wavelengths[-1]))
        self.ax.set_ylim(0, peak * 1.08 if peak > 0 else 1.0)
        self.ax.fill_between(
            wavelengths, spectrum, color=ACCENT, alpha=0.30, linewidth=0
        )
        self.ax.plot(wavelengths, spectrum, color=FG, linewidth=1.4)
        self.canvas.draw()

    def _mark_colour(self, uv: tuple[float, float] | None) -> None:
        if uv is None:
            self._marker.set_data([], [])
        else:
            self._marker.set_data([uv[0]], [uv[1]])
        self.diagram.draw()

    def clear(self) -> None:
        """Reset the plot, the values and the colour marker."""
        for var in (self.photopic_var, self.radiometric_var, self.colour_var):
            var.set(PLACEHOLDER)
        self._mark_colour(None)
        self._draw_empty()

    # --- connection -------------------------------------------------------

    def scan_devices(self) -> None:
        """Fill the device list with the CSS45s found over USB HID."""
        self.paths = {AUTO: None, DEMO: None}
        try:
            for e in CSS45.find("hid"):
                label: str = e["product"] or "CSS45"
                if e["serial"]:
                    label += f"  |  SN {e['serial']}"
                self.paths[label] = e["path"]
        except OSError as e:  # hidapi missing or HID blocked
            self.status.set(f"Scan failed: {e}")
        labels: list[str] = list(self.paths)
        self.dev_box.config(values=labels)
        if self.dev_var.get() not in labels:
            self.dev_var.set(labels[0])

    def toggle_connection(self) -> None:
        if self.dev is not None:
            self.disconnect()
            return
        choice: str = self.dev_var.get()
        try:
            if choice == DEMO:
                dev: CSS45 | DemoCSS45 = DemoCSS45()
            elif self.paths.get(choice) is not None:
                dev = CSS45(transport="hid", path=self.paths[choice])
            else:
                dev = CSS45(transport="hid")
            sn: str = dev.get_serial_number()
        except (Css45Error, LookupError, OSError) as e:
            self.status.set(f"Connect failed: {e}")
            return
        self.dev = dev
        self.conn_btn.config(text="Disconnect")
        self.status.set(f"Connected to {choice}  |  SN {sn}")

    def disconnect(self) -> None:
        if self.dev is not None:
            try:
                self.dev.disconnect()
            except (Css45Error, OSError):
                pass
        self.dev = None
        self.conn_btn.config(text="Connect")
        self.clear()
        self.status.set("Not connected")

    def _call(self, what: str, fn: Callable[..., T], *args: object) -> T | None:
        """Run a device call; show errors in the status bar instead of raising."""
        try:
            return fn(*args)
        except Css45Error as e:
            self.status.set(f"{what}: {e}")
        except (OSError, RuntimeError) as e:
            self.disconnect()
            self.status.set(f"{what}: connection lost ({e})")
        return None

    # --- measurement ------------------------------------------------------

    def measure(self) -> None:
        dev = self.dev
        if dev is None:
            self.status.set("Connect first")
            return
        # Single shot on the Tk loop: the vendor's auto integration time
        # blocks the event loop for up to a few seconds. Accepted for this
        # minimal viewer; no polling loop, no threads.
        label: str = self.it_var.get()
        m: Measurement | None
        if label == IT_AUTO:
            m = self._call("Measure", dev.measure)
        else:
            m = self._call("Measure", dev.measure_manual, IT_BY_LABEL[label])
        if m is not None:
            self._show(m)

    def _show(self, m: Measurement) -> None:
        photopic_lx, radiometric_wm2, yuv = analyse(m.wavelengths, m.spectrum)
        self._draw_spectrum(m.wavelengths, m.spectrum)
        self.photopic_var.set(f"{photopic_lx:.1f}")
        self.radiometric_var.set(f"{radiometric_wm2:.3f}")
        self.colour_var.set(f"Y = {yuv[0]:.1f}, u' = {yuv[1]:.3f}, v' = {yuv[2]:.3f}")
        self._mark_colour((yuv[1], yuv[2]))
        status: str = f"Measured  |  integration time {m.integration_time:.4g} s"
        if self.csv_on.get():
            status += "  |  " + self._write_csv(m, photopic_lx, radiometric_wm2, yuv)
        self.status.set(status)

    def _write_csv(
        self, m: Measurement, photopic_lx: float, radiometric_wm2: float, yuv: Yuv,
    ) -> str:
        """Append m to the CSV file; returns a status note. A failed write
        turns the writer off so the error is not repeated silently."""
        path: Path = Path(self.csv_path.get().strip()).expanduser()
        try:
            append_csv(path, m, photopic_lx, radiometric_wm2, yuv)
        except (OSError, ValueError) as e:
            self.csv_on.set(False)
            self.csv_info.set(f"Writer stopped: {e}")
            return "CSV write failed, writer stopped (see File writer tab)"
        self.csv_rows += 1
        self.csv_info.set(f"Writing to {path}  |  {self.csv_rows} rows this session")
        return f"saved to {path.name}"

    def close(self) -> None:
        self.disconnect()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
