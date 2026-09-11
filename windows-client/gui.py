"""Tkinter control panel for the SeedVC Windows client and RunPod tunnel."""

from __future__ import annotations

import os
import json
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

import client
from controller import (
    ControllerError,
    REMOTE_LIST_REFERENCES_COMMAND,
    REMOTE_START_COMMAND,
    RunPodAPI,
    Settings,
    gpu_candidate_allowed,
    gpu_runtime_compatible,
    get_api_key,
    get_gemini_api_key,
    load_settings,
    pod_connection,
    parse_ssh_command,
    parse_voice_library,
    public_key_for_private,
    redact_secrets,
    reference_stored_activate_command,
    reference_upload_activate_command,
    save_settings,
    scp_upload_command,
    ssh_base_command,
    sort_gpu_candidates,
    tcp_open,
    tunnel_command,
    validate_reference_file,
    wait_for_port,
    set_api_key,
    set_gemini_api_key,
)


GEMINI_AUTOPILOT_MODEL = "gemini-3.6-flash"


CREATE_FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
CLIENT_CREATE_FLAGS = (
    subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    if os.name == "nt"
    else 0
)


class SeedVCApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("SeedVC Voice Changer")
        self.root.geometry("850x690")
        self.root.minsize(760, 620)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.client_process: subprocess.Popen[str] | None = None
        self.tunnel_process: subprocess.Popen[str] | None = None
        self.working = False
        self.uploading = False
        self.closing = False
        self.autopilot_replacement_pod_id = ""
        self.pending_replaced_pod_id = ""
        self.input_devices: dict[str, int] = {}
        self.output_devices: dict[str, int] = {}
        self.stored_voices: dict[str, str] = {}

        try:
            self.settings = load_settings()
        except ControllerError:
            self.settings = Settings()
        candidate = Path.home() / ".ssh" / "runpod_seedvc_v2_ed25519"
        if (
            not self.settings.ssh_key
            or not Path(self.settings.ssh_key).expanduser().is_file()
        ) and candidate.is_file():
            self.settings.ssh_key = str(candidate)
        self.manage_var = tk.BooleanVar(value=self.settings.manage_pod)
        self.autopilot_var = tk.BooleanVar(value=self.settings.gemini_autopilot_enabled)
        self.pod_id_var = tk.StringVar(value=self.settings.pod_id)
        self.api_key_var = tk.StringVar()
        self.gemini_api_key_var = tk.StringVar()
        self.gemini_cli_var = tk.StringVar(value=self.settings.gemini_cli_path)
        self.gpu_min_var = tk.StringVar(value=str(self.settings.gpu_vram_min_gb))
        self.gpu_max_var = tk.StringVar(value=str(self.settings.gpu_vram_max_gb))
        self.network_volume_var = tk.StringVar(
            value=self.settings.network_volume_id or "xkj0pihu6n"
        )
        self.host_var = tk.StringVar(value=self.settings.ssh_host)
        self.port_var = tk.StringVar(value=str(self.settings.ssh_port))
        self.key_var = tk.StringVar(value=self.settings.ssh_key)
        self.stop_on_exit_var = tk.BooleanVar(value=self.settings.stop_pod_on_exit)
        self.reference_file_var = tk.StringVar(value=self.settings.reference_file)
        self.active_reference_var = tk.StringVar(value=self.settings.active_reference)
        self.stored_voice_var = tk.StringVar()

        self._build_style()
        self._build_ui()
        self.refresh_devices()
        self._load_api_key()
        self.root.after(100, self._drain_events)

    def _build_style(self) -> None:
        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Title.TLabel", font=("Segoe UI Semibold", 18))
        style.configure("Subtitle.TLabel", foreground="#555555")
        style.configure("Status.TLabel", font=("Segoe UI Semibold", 10))
        style.configure("Section.TLabel", font=("Segoe UI Semibold", 11))
        style.configure("Start.TButton", font=("Segoe UI Semibold", 12), padding=(24, 12))
        style.configure("Stop.TButton", padding=(14, 9))

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=18)
        outer.pack(fill="both", expand=True)

        ttk.Label(outer, text="SeedVC Voice Changer", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            outer,
            text="Microphone → RunPod Seed-VC → VB-CABLE",
            style="Subtitle.TLabel",
        ).pack(anchor="w", pady=(0, 14))

        status_frame = ttk.Frame(outer)
        status_frame.pack(fill="x", pady=(0, 12))
        self.status_labels: dict[str, ttk.Label] = {}
        for column, (key, title) in enumerate(
            (("pod", "Pod"), ("server", "Server"), ("tunnel", "Tunnel"), ("voice", "Voice"))
        ):
            panel = ttk.Frame(status_frame, padding=(10, 7), relief="groove")
            panel.grid(row=0, column=column, sticky="ew", padx=(0 if column == 0 else 5, 0))
            status_frame.columnconfigure(column, weight=1)
            ttk.Label(panel, text=title).pack(anchor="w")
            label = ttk.Label(panel, text="● Off", foreground="#777777", style="Status.TLabel")
            label.pack(anchor="w")
            self.status_labels[key] = label

        notebook = ttk.Notebook(outer)
        notebook.pack(fill="x")
        audio_tab = ttk.Frame(notebook, padding=14)
        runpod_tab = ttk.Frame(notebook, padding=14)
        notebook.add(audio_tab, text="Audio")
        notebook.add(runpod_tab, text="RunPod & SSH")
        self._build_audio_tab(audio_tab)
        self._build_runpod_tab(runpod_tab)

        actions = ttk.Frame(outer)
        actions.pack(fill="x", pady=14)
        self.start_button = ttk.Button(
            actions, text="Start Voice Changer", style="Start.TButton", command=self.start
        )
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(
            actions, text="Stop Voice", style="Stop.TButton", command=self.stop_voice, state="disabled"
        )
        self.stop_button.pack(side="left", padx=8)
        self.local_button = ttk.Button(actions, text="5-second local test", command=self.local_test)
        self.local_button.pack(side="left")
        self.stop_pod_button = ttk.Button(
            actions, text="Stop RunPod", command=self.stop_pod
        )
        self.stop_pod_button.pack(side="right")

        log_header = ttk.Frame(outer)
        log_header.pack(fill="x")
        ttk.Label(log_header, text="Activity log", font=("Segoe UI Semibold", 10)).pack(side="left")
        ttk.Button(log_header, text="Clear", command=lambda: self.log.delete("1.0", "end")).pack(side="right")
        self.log = scrolledtext.ScrolledText(
            outer,
            height=13,
            wrap="word",
            state="disabled",
            font=("Cascadia Mono", 9),
            background="#111827",
            foreground="#e5e7eb",
            insertbackground="#ffffff",
        )
        self.log.pack(fill="both", expand=True, pady=(5, 0))
        self._log("Ready. Select devices, then click Start Voice Changer.")

    def _build_audio_tab(self, frame: ttk.Frame) -> None:
        frame.columnconfigure(1, weight=1)
        ttk.Label(frame, text="Physical microphone").grid(row=0, column=0, sticky="w", padx=(0, 12), pady=6)
        self.input_combo = ttk.Combobox(frame, state="readonly")
        self.input_combo.grid(row=0, column=1, sticky="ew", pady=6)
        ttk.Label(frame, text="VB-CABLE playback").grid(row=1, column=0, sticky="w", padx=(0, 12), pady=6)
        self.output_combo = ttk.Combobox(frame, state="readonly")
        self.output_combo.grid(row=1, column=1, sticky="ew", pady=6)
        ttk.Button(frame, text="Refresh devices", command=self.refresh_devices).grid(
            row=0, column=2, rowspan=2, padx=(10, 0)
        )
        ttk.Label(
            frame,
            text="Reference voice file",
        ).grid(row=2, column=0, sticky="w", padx=(0, 12), pady=6)
        ttk.Entry(frame, textvariable=self.reference_file_var, state="readonly").grid(
            row=2, column=1, sticky="ew", pady=6
        )
        reference_buttons = ttk.Frame(frame)
        reference_buttons.grid(row=2, column=2, padx=(10, 0))
        ttk.Button(reference_buttons, text="Choose…", command=self.choose_reference).pack(side="left")
        self.upload_button = ttk.Button(
            reference_buttons, text="Upload & use", command=self.upload_reference
        )
        self.upload_button.pack(side="left", padx=(5, 0))
        ttk.Label(frame, text="Stored voices").grid(
            row=3, column=0, sticky="w", padx=(0, 12), pady=6
        )
        self.stored_voice_combo = ttk.Combobox(
            frame, textvariable=self.stored_voice_var, state="readonly"
        )
        self.stored_voice_combo.grid(row=3, column=1, sticky="ew", pady=6)
        stored_buttons = ttk.Frame(frame)
        stored_buttons.grid(row=3, column=2, padx=(10, 0))
        self.refresh_voices_button = ttk.Button(
            stored_buttons, text="Refresh", command=self.refresh_stored_voices
        )
        self.refresh_voices_button.pack(side="left")
        self.use_voice_button = ttk.Button(
            stored_buttons, text="Use selected", command=self.use_stored_voice
        )
        self.use_voice_button.pack(side="left", padx=(5, 0))
        ttk.Label(frame, text="Active reference").grid(
            row=4, column=0, sticky="w", padx=(0, 12), pady=6
        )
        ttk.Label(frame, textvariable=self.active_reference_var).grid(
            row=4, column=1, columnspan=2, sticky="w", pady=6
        )
        ttk.Label(
            frame,
            text="Use clear speech of at least 5 seconds. The server accepts WAV, MP3, M4A, FLAC, OGG, Opus, AAC, MP4, and WMA.",
            style="Subtitle.TLabel",
        ).grid(row=5, column=0, columnspan=3, sticky="w", pady=(7, 0))
        ttk.Label(
            frame,
            text="Your calling app should use CABLE Output as its microphone.",
            style="Subtitle.TLabel",
        ).grid(row=6, column=0, columnspan=3, sticky="w", pady=(4, 0))

    def _build_runpod_tab(self, frame: ttk.Frame) -> None:
        frame.columnconfigure(1, weight=1)
        self.manage_var = tk.BooleanVar(value=self.settings.manage_pod)
        ttk.Checkbutton(
            frame,
            text="Start and discover the pod through the RunPod API",
            variable=self.manage_var,
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))

        self.pod_id_var = tk.StringVar(value=self.settings.pod_id)
        self.api_key_var = tk.StringVar()
        self.host_var = tk.StringVar(value=self.settings.ssh_host)
        self.port_var = tk.StringVar(value=str(self.settings.ssh_port))
        self.key_var = tk.StringVar(value=self.settings.ssh_key)
        self.stop_on_exit_var = tk.BooleanVar(value=self.settings.stop_pod_on_exit)

        fields = (
            ("Pod ID", self.pod_id_var, False),
            ("RunPod API key", self.api_key_var, True),
            ("SSH host", self.host_var, False),
            ("SSH port", self.port_var, False),
            ("SSH key or command", self.key_var, False),
        )
        for row, (label, variable, secret) in enumerate(fields, start=1):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=4)
            entry = ttk.Entry(frame, textvariable=variable, show="•" if secret else "")
            entry.grid(row=row, column=1, sticky="ew", pady=4)
            if label == "SSH key or command":
                key_buttons = ttk.Frame(frame)
                key_buttons.grid(row=row, column=2, padx=(8, 0))
                ttk.Button(key_buttons, text="Browse…", command=self.browse_key).pack(side="left")
                ttk.Button(
                    key_buttons, text="Apply command", command=self.apply_ssh_command
                ).pack(side="left", padx=(5, 0))

        self.configure_ssh_button = ttk.Button(
            frame, text="Install selected SSH key on Pod", command=self.configure_pod_ssh
        )
        self.configure_ssh_button.grid(row=6, column=1, sticky="w", pady=(7, 0))

        ttk.Label(
            frame,
            text="The API key is stored in Windows Credential Manager, never in settings.json.",
            style="Subtitle.TLabel",
        ).grid(row=7, column=0, columnspan=3, sticky="w", pady=(7, 0))
        ttk.Checkbutton(
            frame,
            text="Stop the RunPod automatically when this app closes",
            variable=self.stop_on_exit_var,
        ).grid(row=8, column=0, columnspan=3, sticky="w", pady=(7, 0))

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=18)
        outer.pack(fill="both", expand=True)

        header = ttk.Frame(outer)
        header.pack(fill="x")
        title_block = ttk.Frame(header)
        title_block.pack(side="left", fill="x", expand=True)
        ttk.Label(title_block, text="SeedVC Voice Changer", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            title_block,
            text="Mic to RunPod Seed-VC to VB-CABLE",
            style="Subtitle.TLabel",
        ).pack(anchor="w", pady=(0, 14))
        ttk.Button(header, text="Advanced Settings", command=self.open_settings).pack(
            side="right", anchor="n"
        )

        status_frame = ttk.Frame(outer)
        status_frame.pack(fill="x", pady=(0, 12))
        self.status_labels: dict[str, ttk.Label] = {}
        for column, (key, title) in enumerate(
            (("pod", "Pod"), ("server", "Server"), ("tunnel", "Tunnel"), ("voice", "Voice"))
        ):
            panel = ttk.Frame(status_frame, padding=(10, 7), relief="groove")
            panel.grid(row=0, column=column, sticky="ew", padx=(0 if column == 0 else 5, 0))
            status_frame.columnconfigure(column, weight=1)
            ttk.Label(panel, text=title).pack(anchor="w")
            label = ttk.Label(panel, text="Off", foreground="#777777", style="Status.TLabel")
            label.pack(anchor="w")
            self.status_labels[key] = label

        audio = ttk.Frame(outer)
        audio.pack(fill="x", pady=(0, 12))
        audio.columnconfigure(1, weight=1)
        ttk.Label(audio, text="Audio", style="Section.TLabel").grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 8)
        )
        self._build_audio_tab(audio, start_row=1)

        actions = ttk.Frame(outer)
        actions.pack(fill="x", pady=14)
        self.start_button = ttk.Button(
            actions, text="Start Pod", style="Start.TButton", command=self.start
        )
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(
            actions, text="Stop Voice", style="Stop.TButton", command=self.stop_voice, state="disabled"
        )
        self.stop_button.pack(side="left", padx=8)
        self.local_button = ttk.Button(actions, text="5-second local test", command=self.local_test)
        self.local_button.pack(side="left")
        self.stop_pod_button = ttk.Button(actions, text="Stop RunPod", command=self.stop_pod)
        self.stop_pod_button.pack(side="right")

        log_header = ttk.Frame(outer)
        log_header.pack(fill="x")
        ttk.Label(log_header, text="Activity log", font=("Segoe UI Semibold", 10)).pack(side="left")
        ttk.Button(log_header, text="Clear", command=lambda: self.log.delete("1.0", "end")).pack(side="right")
        self.log = scrolledtext.ScrolledText(
            outer,
            height=13,
            wrap="word",
            state="disabled",
            font=("Cascadia Mono", 9),
            background="#111827",
            foreground="#e5e7eb",
            insertbackground="#ffffff",
        )
        self.log.pack(fill="both", expand=True, pady=(5, 0))
        self._log("Ready. Pick devices and click Start Pod.")

    def _build_audio_tab(self, frame: ttk.Frame, start_row: int = 0) -> None:
        frame.columnconfigure(1, weight=1)
        row = start_row
        ttk.Label(frame, text="Physical microphone").grid(row=row, column=0, sticky="w", padx=(0, 12), pady=6)
        self.input_combo = ttk.Combobox(frame, state="readonly")
        self.input_combo.grid(row=row, column=1, sticky="ew", pady=6)
        ttk.Button(frame, text="Refresh devices", command=self.refresh_devices).grid(
            row=row, column=2, padx=(10, 0)
        )

        row += 1
        ttk.Label(frame, text="VB-CABLE playback").grid(row=row, column=0, sticky="w", padx=(0, 12), pady=6)
        self.output_combo = ttk.Combobox(frame, state="readonly")
        self.output_combo.grid(row=row, column=1, sticky="ew", pady=6)

        row += 1
        ttk.Label(frame, text="Reference voice file").grid(row=row, column=0, sticky="w", padx=(0, 12), pady=6)
        ttk.Entry(frame, textvariable=self.reference_file_var, state="readonly").grid(
            row=row, column=1, sticky="ew", pady=6
        )
        reference_buttons = ttk.Frame(frame)
        reference_buttons.grid(row=row, column=2, padx=(10, 0))
        ttk.Button(reference_buttons, text="Choose...", command=self.choose_reference).pack(side="left")
        self.upload_button = ttk.Button(
            reference_buttons, text="Upload & use", command=self.upload_reference
        )
        self.upload_button.pack(side="left", padx=(5, 0))

        row += 1
        ttk.Label(frame, text="Stored voices").grid(row=row, column=0, sticky="w", padx=(0, 12), pady=6)
        self.stored_voice_combo = ttk.Combobox(
            frame, textvariable=self.stored_voice_var, state="readonly"
        )
        self.stored_voice_combo.grid(row=row, column=1, sticky="ew", pady=6)
        stored_buttons = ttk.Frame(frame)
        stored_buttons.grid(row=row, column=2, padx=(10, 0))
        self.refresh_voices_button = ttk.Button(
            stored_buttons, text="Refresh", command=self.refresh_stored_voices
        )
        self.refresh_voices_button.pack(side="left")
        self.use_voice_button = ttk.Button(
            stored_buttons, text="Use selected", command=self.use_stored_voice
        )
        self.use_voice_button.pack(side="left", padx=(5, 0))

        row += 1
        ttk.Label(frame, text="Active reference").grid(row=row, column=0, sticky="w", padx=(0, 12), pady=6)
        ttk.Label(frame, textvariable=self.active_reference_var).grid(
            row=row, column=1, columnspan=2, sticky="w", pady=6
        )

        row += 1
        ttk.Label(
            frame,
            text="Use clear speech of at least 5 seconds. Your calling app should use CABLE Output as its microphone.",
            style="Subtitle.TLabel",
        ).grid(row=row, column=0, columnspan=3, sticky="w", pady=(7, 0))

    def open_settings(self) -> None:
        window = tk.Toplevel(self.root)
        window.title("Advanced Settings")
        window.transient(self.root)
        window.grab_set()
        window.geometry("760x410")
        window.minsize(680, 370)

        frame = ttk.Frame(window, padding=16)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)

        ttk.Label(frame, text="RunPod", style="Section.TLabel").grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 8)
        )
        ttk.Label(
            frame,
            text="Autopilot discovers the active pod and SSH endpoint from the network volume.",
            style="Subtitle.TLabel",
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 8))

        fields = (
            ("Network volume ID", self.network_volume_var, False),
            ("RunPod API key", self.api_key_var, True),
            ("SSH private key", self.key_var, False),
        )
        for row, (label, variable, secret) in enumerate(fields, start=2):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=4)
            entry = ttk.Entry(frame, textvariable=variable, show="*" if secret else "")
            entry.grid(row=row, column=1, sticky="ew", pady=4)
            if label == "SSH private key":
                buttons = ttk.Frame(frame)
                buttons.grid(row=row, column=2, padx=(8, 0))
                ttk.Button(buttons, text="Browse...", command=self.browse_key).pack(side="left")

        self.configure_ssh_button = ttk.Button(
            frame, text="Install SSH key on discovered pod", command=self.configure_pod_ssh
        )
        self.configure_ssh_button.grid(row=5, column=1, sticky="w", pady=(7, 0))
        ttk.Checkbutton(
            frame,
            text="Stop the RunPod automatically when this app closes",
            variable=self.stop_on_exit_var,
        ).grid(row=6, column=0, columnspan=3, sticky="w", pady=(7, 0))

        ttk.Separator(frame).grid(row=7, column=0, columnspan=3, sticky="ew", pady=14)
        ttk.Label(frame, text="Gemini Autopilot", style="Section.TLabel").grid(
            row=8, column=0, columnspan=3, sticky="w", pady=(0, 8)
        )
        ttk.Checkbutton(
            frame,
            text="Let Gemini drive zero-interaction RunPod recovery",
            variable=self.autopilot_var,
        ).grid(row=9, column=0, columnspan=3, sticky="w", pady=(0, 8))
        autopilot_fields = (
            ("Gemini API key", self.gemini_api_key_var, True),
            ("Gemini CLI path", self.gemini_cli_var, False),
            ("Minimum GPU VRAM GB", self.gpu_min_var, False),
            ("Maximum GPU VRAM GB", self.gpu_max_var, False),
        )
        for row, (label, variable, secret) in enumerate(autopilot_fields, start=10):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=4)
            ttk.Entry(frame, textvariable=variable, show="*" if secret else "").grid(
                row=row, column=1, sticky="ew", pady=4
            )

        ttk.Label(
            frame,
            text="Secrets are stored in Windows Credential Manager. Logs are summarized and redacted.",
            style="Subtitle.TLabel",
        ).grid(row=14, column=0, columnspan=3, sticky="w", pady=(8, 0))

        actions = ttk.Frame(frame)
        actions.grid(row=15, column=0, columnspan=3, sticky="e", pady=(14, 0))
        ttk.Button(actions, text="Save", command=lambda: self.save_settings_dialog(window)).pack(side="right")
        ttk.Button(actions, text="Cancel", command=window.destroy).pack(side="right", padx=(0, 8))

    def _load_api_key(self) -> None:
        try:
            self.api_key_var.set(get_api_key())
        except ControllerError as exc:
            self._log(str(exc), "warning")
        try:
            self.gemini_api_key_var.set(get_gemini_api_key())
        except ControllerError as exc:
            self._log(str(exc), "warning")

    def save_settings_dialog(self, window: tk.Toplevel) -> None:
        try:
            settings = self._current_settings()
            save_settings(settings)
            if self.api_key_var.get().strip():
                set_api_key(self.api_key_var.get())
            if self.gemini_api_key_var.get().strip():
                set_gemini_api_key(self.gemini_api_key_var.get())
        except ControllerError as exc:
            messagebox.showerror("Advanced Settings", str(exc), parent=window)
            return
        self.settings = settings
        self._log("Advanced settings saved.")
        window.destroy()

    def browse_key(self) -> None:
        path = filedialog.askopenfilename(title="Select SSH private key", initialdir=str(Path.home() / ".ssh"))
        if path:
            self.key_var.set(path)

    def apply_ssh_command(self) -> None:
        try:
            connection, key_path = parse_ssh_command(self.key_var.get().strip())
        except ControllerError as exc:
            messagebox.showerror("SSH command", str(exc))
            return
        self.host_var.set(connection.host)
        self.port_var.set(str(connection.ssh_port))
        self.key_var.set(key_path)
        self._log(
            f"Applied SSH command: {connection.host}:{connection.ssh_port}, key {key_path}"
        )

    def choose_reference(self) -> None:
        path = filedialog.askopenfilename(
            title="Choose a SeedVC reference voice",
            filetypes=(
                ("Audio files", "*.wav *.mp3 *.m4a *.flac *.ogg *.opus *.aac *.mp4 *.wma"),
                ("All files", "*.*"),
            ),
        )
        if not path:
            return
        try:
            reference = validate_reference_file(path)
        except ControllerError as exc:
            messagebox.showerror("Reference voice", str(exc))
            return
        self.reference_file_var.set(str(reference))
        self._log(f"Selected reference voice: {reference.name}")

    @staticmethod
    def _device_label(device: dict) -> str:
        return (
            f"{device['index']} — {device['name']} "
            f"[{device['hostapi_name']}, {int(round(device['default_samplerate']))} Hz]"
        )

    def refresh_devices(self) -> None:
        try:
            inputs = client.device_rows("input")
            outputs = client.device_rows("output")
        except Exception as exc:
            messagebox.showerror("Audio devices", f"Could not enumerate audio devices:\n{exc}")
            return
        self.input_devices = {self._device_label(row): int(row["index"]) for row in inputs}
        self.output_devices = {self._device_label(row): int(row["index"]) for row in outputs}
        self.input_combo["values"] = list(self.input_devices)
        self.output_combo["values"] = list(self.output_devices)
        self._select_device(self.input_combo, self.input_devices, self.settings.input_device, "microphone", "wasapi")
        self._select_device(self.output_combo, self.output_devices, self.settings.output_device, "cable input", "wasapi")
        self._log(f"Found {len(inputs)} input and {len(outputs)} output devices.")

    @staticmethod
    def _select_device(
        combo: ttk.Combobox,
        devices: dict[str, int],
        saved: int | None,
        preferred_name: str,
        preferred_api: str,
    ) -> None:
        labels = list(devices)
        selected = next((label for label, index in devices.items() if index == saved), "")
        if not selected:
            selected = next(
                (
                    label
                    for label in labels
                    if preferred_name in label.casefold() and preferred_api in label.casefold()
                ),
                "",
            )
        if not selected and labels:
            selected = labels[0]
        combo.set(selected)

    def _selected_devices(self) -> tuple[int, int]:
        try:
            return self.input_devices[self.input_combo.get()], self.output_devices[self.output_combo.get()]
        except KeyError as exc:
            raise ControllerError("select both an input and output audio device") from exc

    def _current_settings(self) -> Settings:
        input_device, output_device = self._selected_devices()
        key_value = self.key_var.get().strip()
        if key_value.casefold().startswith(("ssh ", "ssh.exe ")):
            connection, key_value = parse_ssh_command(key_value)
            self.host_var.set(connection.host)
            self.port_var.set(str(connection.ssh_port))
            self.key_var.set(key_value)
        try:
            ssh_port = int(self.port_var.get())
        except ValueError as exc:
            raise ControllerError("SSH port must be a number") from exc
        try:
            gpu_min = int(self.gpu_min_var.get())
            gpu_max = int(self.gpu_max_var.get())
        except ValueError as exc:
            raise ControllerError("GPU VRAM limits must be whole numbers") from exc
        if gpu_min <= 0 or gpu_max < gpu_min:
            raise ControllerError("GPU VRAM limits must be a valid range")
        return Settings(
            input_device=input_device,
            output_device=output_device,
            pod_id=self.pod_id_var.get().strip(),
            manage_pod=self.manage_var.get(),
            ssh_host=self.host_var.get().strip(),
            ssh_port=ssh_port,
            ssh_pod_id=(
                self.settings.ssh_pod_id
                if self.pod_id_var.get().strip() == self.settings.pod_id
                else ""
            ),
            ssh_key=key_value,
            network_volume_id=self.network_volume_var.get().strip(),
            local_port=8042,
            stop_pod_on_exit=self.stop_on_exit_var.get(),
            reference_file=self.reference_file_var.get().strip(),
            active_reference=self.active_reference_var.get(),
            gemini_autopilot_enabled=self.autopilot_var.get(),
            gemini_cli_path=self.gemini_cli_var.get().strip() or "gemini",
            gpu_vram_min_gb=gpu_min,
            gpu_vram_max_gb=gpu_max,
            gpu_selection="cheapest",
            replacement_policy="delete_old_after_verified",
            terminal_output_mode="summarized",
        )

    def _resolve_connection(self, settings: Settings, api_key: str) -> tuple[str, int]:
        host, port = settings.ssh_host, settings.ssh_port
        if settings.manage_pod:
            # RunPod's REST response can briefly (and occasionally repeatedly)
            # advertise an old direct-TCP mapping after a restart. Prefer the
            # last discovered endpoint only while it is demonstrably reachable.
            if (
                not settings.network_volume_id
                and settings.ssh_pod_id == settings.pod_id
                and host
                and tcp_open(host, port, timeout=1.0)
            ):
                self._event(
                    "log",
                    (f"Reusing reachable SSH endpoint {host}:{port}.", "info"),
                )
                return host, port
            api = RunPodAPI(api_key)
            pod_id = settings.pod_id
            if settings.network_volume_id:
                self._event(
                    "log",
                    (
                        f"Discovering RunPod attached to volume {settings.network_volume_id}...",
                        "info",
                    ),
                )
                pod = api.preferred_pod_for_network_volume(
                    settings.network_volume_id,
                    settings.gpu_vram_min_gb,
                    settings.gpu_vram_max_gb,
                )
                pod_id = str(pod.get("id") or "")
                if not pod_id:
                    raise ControllerError("RunPod API returned a pod without an ID")
                self._event("pod_id", pod_id)
            else:
                if not pod_id:
                    raise ControllerError("Network volume ID or Pod ID is required")
                pod = api.get_pod(pod_id)
            expected_public_key = public_key_for_private(settings.ssh_key)
            pod_environment = pod.get("env") or {}
            installed_public_key = (
                str(pod_environment.get("PUBLIC_KEY") or "").strip()
                if isinstance(pod_environment, dict)
                else ""
            )
            if installed_public_key != expected_public_key:
                self._event(
                    "log",
                    ("Installing the configured SSH public key on the Pod...", "info"),
                )
                api.configure_public_key(pod_id, expected_public_key)
                pod = api.get_pod(pod_id)

            connection = pod_connection(pod)
            if connection is None:
                self._event("status", ("pod", "Starting", "busy"))
                self._event("log", ("Starting or resuming the RunPod pod…", "info"))
                api.start_pod(pod_id)
            else:
                self._event(
                    "log",
                    ("RunPod is online; waiting for SSH to accept connections…", "info"),
                )
            try:
                connection = api.wait_for_ssh(
                    pod_id,
                    timeout=60 if connection is not None else 300,
                    progress=lambda line: self._event("log", (line, "info")),
                )
            except ControllerError:
                self._event(
                    "log",
                    ("SSH stayed unavailable; restarting the pod once…", "warning"),
                )
                self._event("status", ("pod", "Restarting", "busy"))
                api.restart_pod(pod_id)
                connection = api.wait_for_ssh(
                    pod_id,
                    progress=lambda line: self._event("log", (line, "info")),
                )
            host, port = connection.host, connection.ssh_port
            self._event("connection", (host, port, pod_id))
        return host, port

    def configure_pod_ssh(self) -> None:
        try:
            settings = self._current_settings()
            if not settings.manage_pod or not (settings.network_volume_id or settings.pod_id):
                raise ControllerError("Enter the RunPod network volume ID first")
            api_key = self.api_key_var.get().strip()
            if not api_key:
                raise ControllerError("RunPod API key is required")
            public_key = public_key_for_private(settings.ssh_key)
        except ControllerError as exc:
            messagebox.showerror("Configure Pod SSH", str(exc))
            return
        if not messagebox.askyesno(
            "Configure Pod SSH",
            "Install the selected public key on this Pod? RunPod will reset the container, "
            "but files under /workspace will remain.",
        ):
            return
        self.configure_ssh_button.configure(state="disabled")
        self._set_status("pod", "Configuring SSH", "busy")

        def worker() -> None:
            try:
                api = RunPodAPI(api_key)
                pod_id = settings.pod_id
                if settings.network_volume_id:
                    pod = api.preferred_pod_for_network_volume(settings.network_volume_id)
                    pod_id = str(pod.get("id") or "")
                    self._event("pod_id", pod_id)
                if not pod_id:
                    raise ControllerError("RunPod API returned no pod to configure")
                self._event("log", ("Installing the selected SSH public key on the Pod…", "info"))
                api.configure_public_key(pod_id, public_key)
                connection = api.wait_for_ssh(
                    pod_id,
                    timeout=300,
                    progress=lambda line: self._event("log", (line, "info")),
                )
                self._event(
                    "connection", (connection.host, connection.ssh_port, pod_id)
                )
                self._event("status", ("pod", "Online", "ok"))
                self._event("log", ("Pod SSH key is configured and reachable.", "info"))
            except ControllerError as exc:
                self._event("status", ("pod", "SSH error", "error"))
                self._event("error", str(exc))
            finally:
                self._event("configure_ssh_finished", None)

        threading.Thread(target=worker, daemon=True).start()

    def _load_voice_library_remote(self, host: str, port: int, key_path: str) -> None:
        result = subprocess.run(
            ssh_base_command(host, port, key_path) + [REMOTE_LIST_REFERENCES_COMMAND],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            creationflags=CREATE_FLAGS,
        )
        if result.returncode:
            detail = result.stdout.strip() or f"SSH exit {result.returncode}"
            raise ControllerError(f"could not load stored voices: {detail}")
        voices, active_id = parse_voice_library(result.stdout.strip())
        self._event("voice_library", (voices, active_id))

    def refresh_stored_voices(self) -> None:
        if self.uploading:
            return
        try:
            settings = self._current_settings()
            api_key = self.api_key_var.get().strip()
            if settings.manage_pod and (not (settings.network_volume_id or settings.pod_id) or not api_key):
                raise ControllerError("Network volume ID and RunPod API key are required")
            if not settings.manage_pod:
                ssh_base_command(settings.ssh_host, settings.ssh_port, settings.ssh_key)
        except ControllerError as exc:
            messagebox.showerror("Stored voices", str(exc))
            return
        self.refresh_voices_button.configure(state="disabled")

        def worker() -> None:
            try:
                host, port = self._resolve_connection(settings, api_key)
                self._load_voice_library_remote(host, port, settings.ssh_key)
                self._event("status", ("pod", "Online", "ok"))
            except (ControllerError, OSError, subprocess.TimeoutExpired) as exc:
                self._event("error", str(exc))
            finally:
                self._event("refresh_voices_finished", None)

        threading.Thread(target=worker, daemon=True).start()

    def start(self) -> None:
        if self.working or (self.client_process and self.client_process.poll() is None):
            return
        try:
            settings = self._current_settings()
            save_settings(settings)
            if settings.manage_pod:
                set_api_key(self.api_key_var.get())
                if not (settings.network_volume_id or settings.pod_id):
                    raise ControllerError("Network volume ID is required for RunPod automation")
            else:
                ssh_base_command(settings.ssh_host, settings.ssh_port, settings.ssh_key)
        except ControllerError as exc:
            messagebox.showerror("Cannot start", str(exc))
            return
        self.settings = settings
        self.working = True
        self.start_button.configure(state="disabled")
        self.local_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self._set_status("voice", "Starting", "busy")
        api_key = self.api_key_var.get().strip()
        threading.Thread(
            target=self._start_worker, args=(settings, api_key), daemon=True
        ).start()

    def _start_worker(self, settings: Settings, api_key: str) -> None:
        pod_connected = False
        try:
            try:
                host, port = self._resolve_connection(settings, api_key)
            except ControllerError as exc:
                if not self._should_run_autopilot(settings, str(exc)):
                    raise
                self._event("status", ("pod", "Autopilot", "busy"))
                self._event(
                    "log",
                    ("Gemini Autopilot is taking over RunPod recovery...", "warning"),
                )
                if not self._run_gemini_autopilot(settings, api_key, str(exc)):
                    return
                self._event(
                    "log",
                    ("Recovery completed; connecting to the recovered Pod...", "info"),
                )
                host, port = self._resolve_connection(settings, api_key)
            pod_connected = True
            self._event("status", ("pod", "Online", "ok"))

            self._event("status", ("server", "Starting", "busy"))
            self._event("log", ("Checking the Fast-VC service on the pod…", "info"))
            command = ssh_base_command(host, port, settings.ssh_key) + [REMOTE_START_COMMAND]
            for attempt in range(1, 3):
                process = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    creationflags=CREATE_FLAGS,
                )
                self._read_process_async(process, "pod")
                try:
                    returncode = process.wait(timeout=480)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    raise ControllerError("pod service startup timed out after 8 minutes")
                if returncode == 0:
                    break
                if attempt == 1:
                    self._event(
                        "log",
                        ("SeedVC startup failed once; retrying on the same Pod...", "warning"),
                    )
            if returncode:
                raise ControllerError(
                    f"pod service startup failed twice (SSH exit {returncode})"
                )
            self._event("status", ("server", "Ready", "ok"))
            try:
                self._load_voice_library_remote(host, port, settings.ssh_key)
            except ControllerError as exc:
                self._event("log", (str(exc), "warning"))

            local_port = settings.local_port
            if tcp_open("127.0.0.1", local_port):
                self._event("log", (f"Reusing the existing tunnel on port {local_port}.", "warning"))
            else:
                self._event("status", ("tunnel", "Connecting", "busy"))
                self.tunnel_process = subprocess.Popen(
                    tunnel_command(host, port, settings.ssh_key, local_port),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    creationflags=CREATE_FLAGS,
                )
                self._read_process_async(self.tunnel_process, "ssh")
                wait_for_port(
                    "127.0.0.1",
                    local_port,
                    20,
                    process_alive=lambda: self.tunnel_process is not None
                    and self.tunnel_process.poll() is None,
                )
            self._event("status", ("tunnel", "Connected", "ok"))
            self._finalize_autopilot_replacement(api_key)

            input_device, output_device = settings.input_device, settings.output_device
            assert input_device is not None and output_device is not None
            client_path = Path(__file__).with_name("client.py")
            command = [
                sys.executable,
                str(client_path),
                "--url",
                f"ws://127.0.0.1:{local_port}/ws",
                "--input-device",
                str(input_device),
                "--output-device",
                str(output_device),
                "--control-stdin",
            ]
            self.client_process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=CLIENT_CREATE_FLAGS,
            )
            self._event("log", ("Windows audio client started.", "info"))
            self._read_process(self.client_process, "client")
            code = self.client_process.wait()
            if code not in (0, 130) and not self.closing:
                raise ControllerError(f"Windows audio client exited with code {code}")
        except subprocess.TimeoutExpired as exc:
            self._event("error", f"Timed out while starting the pod service: {exc}")
        except ControllerError as exc:
            if pod_connected:
                self._event(
                    "log",
                    ("Keeping the reachable Pod online for recovery and retry.", "warning"),
                )
            else:
                self._rollback_autopilot_replacement(api_key)
            self._event("error", str(exc))
        except OSError as exc:
            self._event("error", str(exc))
        finally:
            self._event("finished", None)

    def _should_run_autopilot(self, settings: Settings, message: str) -> bool:
        if not settings.gemini_autopilot_enabled:
            return False
        lower = message.casefold()
        triggers = (
            "not enough free gpus",
            "no runpod pod is attached",
            "no compatible runpod pod",
            "pod ssh did not become reachable",
            "failed to start",
            "capacity",
        )
        return any(trigger in lower for trigger in triggers)

    def _gemini_command(self, settings: Settings, prompt: str | None = None) -> list[str]:
        configured = settings.gemini_cli_path.strip() or "gemini"
        resolved = shutil.which(configured)
        if not resolved and configured.casefold() == "gemini":
            resolved = shutil.which("gemini.cmd") or shutil.which("gemini.ps1")
        cli = resolved or configured
        return [
            cli,
            "--model",
            GEMINI_AUTOPILOT_MODEL,
            "--approval-mode",
            "yolo",
            "--prompt",
            prompt or self._gemini_autopilot_prompt(settings, []),
        ]

    def _gemini_autopilot_prompt(
        self,
        settings: Settings,
        candidates: list[dict],
    ) -> str:
        compact_candidates = [
            {
                "id": candidate.get("id"),
                "name": candidate.get("displayName"),
                "vram_gb": candidate.get("memoryInGb"),
                "hourly_price": (candidate.get("lowestPrice") or {}).get(
                    "uninterruptablePrice"
                ),
            }
            for candidate in candidates
        ]
        return (
            "You are choosing GPU fallback priority for an autonomous SeedVC RunPod "
            "recovery. The controller has already queried RunPod, filtered the catalog, "
            "and will enforce all policy and execute the deployment. Do not use tools. "
            f"All candidates are NVIDIA GPUs with {settings.gpu_vram_min_gb}-"
            f"{settings.gpu_vram_max_gb} GB VRAM and support the volume's cloud type. "
            "Prefer the lowest hourly price, using stronger hardware only as a tie-breaker. "
            "Select exactly one candidate. Return one JSON object and no markdown or "
            'explanation: {"selected_gpu_type_id":"exact supplied id"}.\n'
            f"Candidates: {json.dumps(compact_candidates, separators=(',', ':'))}"
        )

    def _prepare_gemini_auth_env(self, env: dict[str, str]) -> None:
        appdata = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
        home = appdata / "SeedVC" / "gemini-autopilot-home"
        gemini_dir = home / ".gemini"
        gemini_dir.mkdir(parents=True, exist_ok=True)
        settings_path = gemini_dir / "settings.json"
        settings_path.write_text(
            json.dumps(
                {
                    "selectedAuthType": "gemini-api-key",
                    "model": GEMINI_AUTOPILOT_MODEL,
                    "coreTools": [],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        system_settings = home / "system-settings.json"
        system_settings.write_text("{}", encoding="utf-8")

        env["HOME"] = str(home)
        env["USERPROFILE"] = str(home)
        env["GEMINI_DEFAULT_AUTH_TYPE"] = "gemini-api-key"
        env["GEMINI_CLI_SYSTEM_SETTINGS_PATH"] = str(system_settings)
        env.pop("GOOGLE_GENAI_USE_GCA", None)
        env.pop("GOOGLE_GENAI_USE_VERTEXAI", None)
        env.pop("GOOGLE_CLOUD_PROJECT", None)
        env.pop("GOOGLE_CLOUD_LOCATION", None)
        env["SEEDVC_AUTOPILOT_WORKDIR"] = str(self._autopilot_workdir())

    def _autopilot_workdir(self) -> Path:
        appdata = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
        workdir = appdata / "SeedVC" / "gemini-autopilot-work"
        workdir.mkdir(parents=True, exist_ok=True)
        return workdir

    @staticmethod
    def _parse_autopilot_decision(output: str) -> list[str]:
        decoder = json.JSONDecoder()
        for offset, character in enumerate(output):
            if character != "{":
                continue
            try:
                value, _ = decoder.raw_decode(output[offset:])
            except json.JSONDecodeError:
                continue
            if not isinstance(value, dict):
                continue
            selected = value.get("selected_gpu_type_id")
            if isinstance(selected, str) and selected.strip():
                return [selected.strip()]
            ordered = value.get("ordered_gpu_type_ids")
            if isinstance(ordered, list) and all(
                isinstance(item, str) and item.strip() for item in ordered
            ):
                return [item.strip() for item in ordered]
        raise ControllerError("Gemini did not return a valid GPU priority decision")

    @staticmethod
    def _replacement_payload(
        source: dict,
        volume: dict,
        ordered_gpu_ids: list[str],
        public_key: str,
    ) -> dict:
        environment = dict(source.get("env") or {})
        environment["PUBLIC_KEY"] = public_key
        ports = [
            port
            for port in (source.get("ports") or [])
            if not str(port).startswith("22/")
        ]
        ports = list(dict.fromkeys([*ports, "22/tcp"]))
        return {
            "name": "seedvc-autopilot",
            "imageName": source.get("imageName"),
            "cloudType": "SECURE" if source.get("secureCloud", True) else "COMMUNITY",
            "gpuCount": int(source.get("gpuCount") or 1),
            "gpuTypeIds": ordered_gpu_ids,
            "gpuTypePriority": "custom",
            "containerDiskInGb": int(source.get("containerDiskInGb") or 30),
            "networkVolumeId": volume.get("id"),
            "volumeMountPath": source.get("volumeMountPath") or "/workspace",
            "dataCenterIds": [volume.get("dataCenterId")],
            "dataCenterPriority": "custom",
            "ports": ports,
            "supportPublicIp": True,
            "env": environment,
        }

    def _run_gemini_autopilot(self, settings: Settings, api_key: str, failure: str) -> bool:
        gemini_key = self.gemini_api_key_var.get().strip()
        if not gemini_key:
            self._event(
                "soft_error",
                "Gemini Autopilot is enabled, but the Gemini API key is not set in Advanced Settings.",
            )
            return False
        if not api_key.strip():
            self._event(
                "soft_error",
                "Gemini Autopilot needs the RunPod API key in Advanced Settings.",
            )
            return False
        try:
            api = RunPodAPI(api_key)
            source = api.preferred_pod_for_network_volume(settings.network_volume_id)
            source_id = str(source.get("id") or "")
            source.update(api.pod_gpu_details().get(source_id, {}))
            volume = api.get_network_volume(settings.network_volume_id)
            secure_cloud = bool(source.get("secureCloud", True))
            candidates = [
                candidate
                for candidate in api.gpu_types()
                if str(candidate.get("id") or "").startswith("NVIDIA ")
                and gpu_candidate_allowed(
                    candidate,
                    settings.gpu_vram_min_gb,
                    settings.gpu_vram_max_gb,
                )
                and gpu_runtime_compatible(candidate, str(source.get("imageName") or ""))
                and bool(candidate.get("secureCloud") if secure_cloud else candidate.get("communityCloud"))
                and (candidate.get("lowestPrice") or {}).get("uninterruptablePrice") is not None
            ]
            candidates = sort_gpu_candidates(candidates)
            if not candidates:
                raise ControllerError("RunPod reported no in-policy GPU types with current availability")
            prompt = self._gemini_autopilot_prompt(settings, candidates)
            command = self._gemini_command(settings, prompt)
            env = os.environ.copy()
            env["GEMINI_API_KEY"] = gemini_key
            self._prepare_gemini_auth_env(env)
            safe_failure = redact_secrets(failure, api_key, gemini_key)
            self._event("log", (f"Autopilot trigger: {safe_failure}", "warning"))
            self._event(
                "log",
                (f"Gemini is ranking {len(candidates)} policy-approved GPU types...", "info"),
            )
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(self._autopilot_workdir()),
                env=env,
                creationflags=CREATE_FLAGS,
            )
            assert process.stdout is not None
            output_lines: list[str] = []
            for raw_line in process.stdout:
                line = redact_secrets(raw_line.strip(), api_key, gemini_key)
                if not line:
                    continue
                output_lines.append(line)
                lower = line.casefold()
                level = "error" if "failed" in lower or "error" in lower else "info"
                self._event("log", (f"gemini: {line}", level))
            code = process.wait()
            if code:
                self._event("soft_error", f"Gemini Autopilot exited with code {code}")
                return False
            selected_gpu_ids = self._parse_autopilot_decision("\n".join(output_lines))
            allowed_by_id = {str(candidate.get("id")): candidate for candidate in candidates}
            if len(selected_gpu_ids) != 1:
                raise ControllerError("Gemini returned an invalid GPU choice")
            selected_gpu_id = selected_gpu_ids[0]
            if selected_gpu_id in allowed_by_id:
                ordered_gpu_ids = [selected_gpu_id] + [
                    candidate_id
                    for candidate_id in allowed_by_id
                    if candidate_id != selected_gpu_id
                ]
            else:
                self._event(
                    "log",
                    (
                        "Gemini's preferred GPU is no longer available; using the "
                        "current policy-approved price order.",
                        "warning",
                    ),
                )
                ordered_gpu_ids = list(allowed_by_id)
            self._event(
                "log",
                (f"Deploying Gemini's first available choice: {ordered_gpu_ids[0]}", "info"),
            )
            replacement_id = ""
            connection = None
            last_deployment_error = "RunPod had no usable capacity"
            public_key = public_key_for_private(settings.ssh_key)
            for gpu_type_id in ordered_gpu_ids:
                candidate_id = ""
                self._event("log", (f"Trying {gpu_type_id}...", "info"))
                try:
                    payload = self._replacement_payload(
                        source,
                        volume,
                        [gpu_type_id],
                        public_key,
                    )
                    replacement = api.create_pod(payload)
                    candidate_id = str(replacement.get("id") or "")
                    if not candidate_id:
                        raise ControllerError(
                            "RunPod created a replacement without returning its ID"
                        )
                    status = str(
                        replacement.get("desiredStatus")
                        or replacement.get("status")
                        or ""
                    ).upper()
                    if status in {"EXITED", "STOPPED"}:
                        api.start_pod(candidate_id)
                    candidate_connection = api.wait_for_ssh(
                        candidate_id,
                        timeout=300,
                        progress=lambda line: self._event("log", (line, "info")),
                    )
                    verified = api.get_pod(candidate_id)
                    verified.update(api.pod_gpu_details().get(candidate_id, {}))
                    if not gpu_candidate_allowed(
                        verified,
                        settings.gpu_vram_min_gb,
                        settings.gpu_vram_max_gb,
                    ):
                        api.stop_pod(candidate_id)
                        api.delete_pod(candidate_id)
                        raise ControllerError(
                            "RunPod provisioned a replacement outside the GPU VRAM policy"
                        )
                    replacement_id = candidate_id
                    connection = candidate_connection
                    break
                except ControllerError as exc:
                    last_deployment_error = str(exc)
                    if candidate_id:
                        try:
                            api.stop_pod(candidate_id)
                            api.delete_pod(candidate_id)
                        except ControllerError:
                            pass
                    self._event(
                        "log",
                        (f"{gpu_type_id} was unavailable; trying the next candidate.", "warning"),
                    )
            if not replacement_id or connection is None:
                raise ControllerError(
                    f"all policy-approved GPU deployment attempts failed: "
                    f"{last_deployment_error}"
                )
            self._event("pod_id", replacement_id)
            self._event("log", (f"Replacement pod created: {replacement_id}", "info"))
            self._event(
                "connection",
                (connection.host, connection.ssh_port, replacement_id),
            )
            self.autopilot_replacement_pod_id = replacement_id
            if settings.replacement_policy == "delete_old_after_verified":
                self.pending_replaced_pod_id = source_id
            self._event("log", ("Gemini Autopilot recovery verified over SSH.", "info"))
            return True
        except OSError as exc:
            self._event("soft_error", f"Could not launch Gemini CLI: {exc}")
        except ControllerError as exc:
            self._event("soft_error", str(exc))
        return False

    def _finalize_autopilot_replacement(self, api_key: str) -> None:
        old_pod_id = self.pending_replaced_pod_id
        replacement_id = self.autopilot_replacement_pod_id
        if not old_pod_id or old_pod_id == replacement_id:
            return
        RunPodAPI(api_key).delete_pod(old_pod_id)
        self._event(
            "log",
            (f"SeedVC is ready; deleted replaced pod {old_pod_id}.", "info"),
        )
        self.pending_replaced_pod_id = ""
        self.autopilot_replacement_pod_id = ""

    def _rollback_autopilot_replacement(self, api_key: str) -> None:
        replacement_id = self.autopilot_replacement_pod_id
        if not replacement_id or not api_key:
            return
        api = RunPodAPI(api_key)
        try:
            api.stop_pod(replacement_id)
            api.delete_pod(replacement_id)
            self._event(
                "log",
                (f"Removed failed replacement pod {replacement_id}; old pod retained.", "warning"),
            )
        except ControllerError as exc:
            self._event("log", (f"Could not clean failed replacement: {exc}", "warning"))
        self.pending_replaced_pod_id = ""
        self.autopilot_replacement_pod_id = ""

    def upload_reference(self) -> None:
        if self.uploading:
            return
        try:
            reference = validate_reference_file(self.reference_file_var.get())
            settings = self._current_settings()
            if settings.manage_pod:
                if not (settings.network_volume_id or settings.pod_id) or not self.api_key_var.get().strip():
                    raise ControllerError("Network volume ID and RunPod API key are required")
            else:
                ssh_base_command(settings.ssh_host, settings.ssh_port, settings.ssh_key)
        except ControllerError as exc:
            messagebox.showerror("Reference voice", str(exc))
            return
        if not messagebox.askyesno(
            "Activate reference voice",
            "Uploading a new reference briefly stops voice conversion while Fast-VC reloads. Continue?",
        ):
            return

        was_active = self.client_process is not None and self.client_process.poll() is None
        if was_active:
            self.stop_voice()
        self.uploading = True
        for button in (
            self.upload_button,
            self.use_voice_button,
            self.refresh_voices_button,
            self.start_button,
            self.local_button,
            self.stop_pod_button,
        ):
            button.configure(state="disabled")
        self._set_status("server", "Updating voice", "busy")
        api_key = self.api_key_var.get().strip()
        threading.Thread(
            target=self._upload_reference_worker,
            args=(settings, api_key, reference, was_active),
            daemon=True,
        ).start()

    def _upload_reference_worker(
        self,
        settings: Settings,
        api_key: str,
        reference: Path,
        resume_voice: bool,
    ) -> None:
        try:
            client_process = self.client_process
            if client_process and client_process.poll() is None:
                try:
                    client_process.wait(timeout=12)
                except subprocess.TimeoutExpired:
                    client_process.terminate()
                    client_process.wait(timeout=5)

            host, port = self._resolve_connection(settings, api_key)
            self._event("status", ("pod", "Online", "ok"))
            self._event("log", (f"Uploading reference voice: {reference.name}", "info"))
            upload = subprocess.run(
                scp_upload_command(host, port, settings.ssh_key, reference),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=180,
                creationflags=CREATE_FLAGS,
            )
            for line in upload.stdout.splitlines():
                self._event("log", (f"scp: {line}", "info"))
            if upload.returncode:
                raise ControllerError(f"reference upload failed (SCP exit {upload.returncode})")

            self._event("log", ("Validating and activating the reference on RunPod…", "info"))
            activate = subprocess.run(
                ssh_base_command(host, port, settings.ssh_key)
                + [reference_upload_activate_command(reference.name)],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=480,
                creationflags=CREATE_FLAGS,
            )
            for line in activate.stdout.splitlines():
                self._event("log", (f"pod: {line}", "info"))
            if activate.returncode:
                raise ControllerError(
                    f"reference activation failed (SSH exit {activate.returncode}); the previous voice was restored"
                )
            self._event("status", ("server", "Ready", "ok"))
            self._load_voice_library_remote(host, port, settings.ssh_key)
            self._event("reference_done", (reference.name, str(reference), resume_voice))
        except (ControllerError, OSError, subprocess.TimeoutExpired) as exc:
            self._event("status", ("server", "Error", "error"))
            self._event("error", str(exc))
        finally:
            self._event("upload_finished", None)

    def use_stored_voice(self) -> None:
        if self.uploading:
            return
        label = self.stored_voice_var.get()
        voice_id = self.stored_voices.get(label)
        if not voice_id:
            messagebox.showinfo("Stored voices", "Select a stored voice first.")
            return
        try:
            settings = self._current_settings()
            api_key = self.api_key_var.get().strip()
            if settings.manage_pod and (not (settings.network_volume_id or settings.pod_id) or not api_key):
                raise ControllerError("Network volume ID and RunPod API key are required")
            if not settings.manage_pod:
                ssh_base_command(settings.ssh_host, settings.ssh_port, settings.ssh_key)
        except ControllerError as exc:
            messagebox.showerror("Stored voices", str(exc))
            return
        if not messagebox.askyesno(
            "Activate stored voice",
            f"Switch to '{label}'? Fast-VC will reload briefly.",
        ):
            return
        was_active = self.client_process is not None and self.client_process.poll() is None
        if was_active:
            self.stop_voice()
        self.uploading = True
        for button in (
            self.upload_button,
            self.use_voice_button,
            self.refresh_voices_button,
            self.start_button,
            self.local_button,
            self.stop_pod_button,
        ):
            button.configure(state="disabled")
        self._set_status("server", "Switching voice", "busy")
        threading.Thread(
            target=self._activate_stored_worker,
            args=(settings, api_key, voice_id, label, was_active),
            daemon=True,
        ).start()

    def _activate_stored_worker(
        self,
        settings: Settings,
        api_key: str,
        voice_id: str,
        display_name: str,
        resume_voice: bool,
    ) -> None:
        try:
            process = self.client_process
            if process and process.poll() is None:
                try:
                    process.wait(timeout=12)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    process.wait(timeout=5)
            host, port = self._resolve_connection(settings, api_key)
            self._event("status", ("pod", "Online", "ok"))
            self._event("log", (f"Activating stored voice: {display_name}", "info"))
            result = subprocess.run(
                ssh_base_command(host, port, settings.ssh_key)
                + [reference_stored_activate_command(voice_id)],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=480,
                creationflags=CREATE_FLAGS,
            )
            for line in result.stdout.splitlines():
                self._event("log", (f"pod: {line}", "info"))
            if result.returncode:
                raise ControllerError(
                    f"stored voice activation failed (SSH exit {result.returncode}); "
                    "the previous voice was restored"
                )
            self._event("status", ("server", "Ready", "ok"))
            self._load_voice_library_remote(host, port, settings.ssh_key)
            self._event("reference_done", (display_name, "", resume_voice))
        except (ControllerError, OSError, subprocess.TimeoutExpired) as exc:
            self._event("status", ("server", "Error", "error"))
            self._event("error", str(exc))
        finally:
            self._event("upload_finished", None)

    def _read_process_async(self, process: subprocess.Popen[str], prefix: str) -> None:
        threading.Thread(target=self._read_process, args=(process, prefix), daemon=True).start()

    def _read_process(self, process: subprocess.Popen[str], prefix: str) -> None:
        if process.stdout is None:
            return
        for raw_line in process.stdout:
            line = raw_line.rstrip()
            if not line:
                continue
            level = "error" if "[error]" in line.casefold() else "warning" if "[warning]" in line.casefold() else "info"
            self._event("log", (f"{prefix}: {line}", level))
            lower = line.casefold()
            if prefix == "client" and "connected; stream" in lower:
                self._event("status", ("voice", "Connected", "busy"))
            if prefix == "client" and "voice conversion active" in lower:
                self._event("status", ("voice", "Active", "ok"))

    def local_test(self) -> None:
        if self.working:
            return
        try:
            input_device, output_device = self._selected_devices()
        except ControllerError as exc:
            messagebox.showerror("Local test", str(exc))
            return

        def worker() -> None:
            self._event("buttons", False)
            command = [
                sys.executable,
                str(Path(__file__).with_name("client.py")),
                "--local-test",
                "--input-device",
                str(input_device),
                "--output-device",
                str(output_device),
                "--duration",
                "5",
            ]
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=CREATE_FLAGS,
            )
            self._read_process(process, "local")
            process.wait()
            self._event("buttons", True)

        threading.Thread(target=worker, daemon=True).start()

    def stop_voice(self) -> None:
        process = self.client_process
        if process and process.poll() is None:
            self._log("Stopping the voice stream cleanly…")
            try:
                assert process.stdin is not None
                process.stdin.write("stop\n")
                process.stdin.flush()
            except (OSError, ValueError):
                process.terminate()
        else:
            self._finish_state()

    def _stop_tunnel(self) -> None:
        process = self.tunnel_process
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        self.tunnel_process = None
        self._set_status("tunnel", "Off", "off")

    def stop_pod(self) -> None:
        if not (self.pod_id_var.get().strip() or self.network_volume_var.get().strip()) or not self.api_key_var.get().strip():
            messagebox.showinfo("Stop RunPod", "Enter the network volume ID and RunPod API key first.")
            return
        if not messagebox.askyesno(
            "Stop RunPod", "Stop the GPU pod now? The voice connection will end."
        ):
            return
        self.stop_voice()
        api_key = self.api_key_var.get().strip()
        pod_id = self.pod_id_var.get().strip()
        network_volume_id = self.network_volume_var.get().strip()

        def worker() -> None:
            try:
                api = RunPodAPI(api_key)
                target_pod_id = pod_id
                if network_volume_id:
                    pod = api.preferred_pod_for_network_volume(network_volume_id)
                    target_pod_id = str(pod.get("id") or "")
                    self._event("pod_id", target_pod_id)
                if not target_pod_id:
                    raise ControllerError("RunPod API returned no pod to stop")
                api.stop_pod(target_pod_id)
                self._event("status", ("pod", "Stopped", "off"))
                self._event("log", ("RunPod stop request accepted.", "info"))
            except ControllerError as exc:
                self._event("error", str(exc))

        threading.Thread(target=worker, daemon=True).start()

    def _event(self, name: str, value: object) -> None:
        self.events.put((name, value))

    def _drain_events(self) -> None:
        try:
            while True:
                name, value = self.events.get_nowait()
                if name == "log":
                    message, level = value  # type: ignore[misc]
                    self._log(message, level)
                elif name == "status":
                    key, text, state = value  # type: ignore[misc]
                    self._set_status(key, text, state)
                elif name == "connection":
                    host, port, pod_id = value  # type: ignore[misc]
                    self.pod_id_var.set(pod_id)
                    self.host_var.set(host)
                    self.port_var.set(str(port))
                    self.settings.pod_id = pod_id
                    self.settings.ssh_host = host
                    self.settings.ssh_port = port
                    self.settings.ssh_pod_id = pod_id
                elif name == "pod_id":
                    pod_id = str(value)
                    self.pod_id_var.set(pod_id)
                    self.settings.pod_id = pod_id
                elif name == "voice_library":
                    voices, active_id = value  # type: ignore[misc]
                    counts: dict[str, int] = {}
                    for voice in voices:
                        counts[voice.name] = counts.get(voice.name, 0) + 1
                    self.stored_voices = {}
                    selected = ""
                    active_name = None
                    for voice in voices:
                        label = voice.name
                        if counts[voice.name] > 1:
                            label = f"{voice.name} ({voice.id[:6]})"
                        self.stored_voices[label] = voice.id
                        if voice.id == active_id:
                            selected = label
                            active_name = voice.name
                    self.stored_voice_combo["values"] = list(self.stored_voices)
                    self.stored_voice_var.set(selected or next(iter(self.stored_voices), ""))
                    if active_name:
                        self.active_reference_var.set(active_name)
                    self._log(f"Loaded {len(voices)} stored voice(s) from the Pod.")
                elif name == "error":
                    self._log(str(value), "error")
                    if not self.closing:
                        messagebox.showerror("SeedVC", str(value))
                elif name == "soft_error":
                    self._log(str(value), "error")
                    self._set_status("pod", "Autopilot failed", "error")
                elif name == "finished":
                    self._finish_state()
                elif name == "reference_done":
                    display_name, path, resume_voice = value  # type: ignore[misc]
                    self.active_reference_var.set(display_name)
                    if path:
                        self.reference_file_var.set(path)
                    try:
                        self.settings = self._current_settings()
                        save_settings(self.settings)
                    except ControllerError as exc:
                        self._log(str(exc), "warning")
                    self._log(f"Reference voice is active: {display_name}")
                    messagebox.showinfo("Reference voice", f"Now using: {display_name}")
                    if resume_voice:
                        self.root.after(500, self.start)
                elif name == "upload_finished":
                    self.uploading = False
                    self.upload_button.configure(state="normal")
                    self.use_voice_button.configure(state="normal")
                    self.refresh_voices_button.configure(state="normal")
                    self.stop_pod_button.configure(state="normal")
                    if not (self.client_process and self.client_process.poll() is None):
                        self.start_button.configure(state="normal")
                        self.local_button.configure(state="normal")
                elif name == "refresh_voices_finished":
                    self.refresh_voices_button.configure(state="normal")
                elif name == "configure_ssh_finished":
                    button = getattr(self, "configure_ssh_button", None)
                    if button is not None and button.winfo_exists():
                        button.configure(state="normal")
                elif name == "buttons":
                    enabled = bool(value)
                    self.local_button.configure(state="normal" if enabled else "disabled")
                    self.start_button.configure(state="normal" if enabled else "disabled")
        except queue.Empty:
            pass
        if not self.closing:
            self.root.after(100, self._drain_events)

    def _set_status(self, key: str, text: str, state: str) -> None:
        colors = {"ok": "#15803d", "busy": "#b45309", "error": "#b91c1c", "off": "#777777"}
        self.status_labels[key].configure(text=f"● {text}", foreground=colors.get(state, "#777777"))

    def _log(self, message: str, level: str = "info") -> None:
        if not hasattr(self, "log"):
            return
        timestamp = time.strftime("%H:%M:%S")
        self.log.configure(state="normal")
        self.log.insert("end", f"{timestamp}  {message}\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _finish_state(self) -> None:
        self.working = False
        self.client_process = None
        self._set_status("voice", "Off", "off")
        if not self.uploading:
            self.start_button.configure(state="normal")
            self.local_button.configure(state="normal")
        self.stop_button.configure(state="disabled")

    def _set_status(self, key: str, text: str, state: str) -> None:
        colors = {"ok": "#15803d", "busy": "#b45309", "error": "#b91c1c", "off": "#777777"}
        prefix = {"ok": "Ready", "busy": "Working", "error": "Error", "off": "Off"}.get(state, "State")
        self.status_labels[key].configure(
            text=f"{prefix}: {text}", foreground=colors.get(state, "#777777")
        )

    def on_close(self) -> None:
        self.closing = True
        try:
            settings = self._current_settings()
            save_settings(settings)
        except ControllerError:
            settings = self.settings
        self.stop_voice()
        process = self.client_process
        if process and process.poll() is None:
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.terminate()
        self._stop_tunnel()
        if settings.stop_pod_on_exit and settings.pod_id and self.api_key_var.get().strip():
            try:
                RunPodAPI(self.api_key_var.get()).stop_pod(settings.pod_id)
            except ControllerError:
                pass
        self.root.destroy()


def main() -> int:
    root = tk.Tk()
    SeedVCApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
