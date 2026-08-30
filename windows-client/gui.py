"""Tkinter control panel for the SeedVC Windows client and RunPod tunnel."""

from __future__ import annotations

import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

import client
from controller import (
    ControllerError,
    REMOTE_ACTIVATE_REFERENCE_COMMAND,
    REMOTE_START_COMMAND,
    RunPodAPI,
    Settings,
    get_api_key,
    load_settings,
    pod_connection,
    save_settings,
    scp_upload_command,
    ssh_base_command,
    tcp_open,
    tunnel_command,
    validate_reference_file,
    wait_for_port,
    set_api_key,
)


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
        self.input_devices: dict[str, int] = {}
        self.output_devices: dict[str, int] = {}

        try:
            self.settings = load_settings()
        except ControllerError:
            self.settings = Settings()
        if not self.settings.ssh_key:
            candidate = Path.home() / ".ssh" / "runpod_seedvc_v2_ed25519"
            self.settings.ssh_key = str(candidate)
        self.reference_file_var = tk.StringVar(value=self.settings.reference_file)
        self.active_reference_var = tk.StringVar(value=self.settings.active_reference)

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
        style.configure("Start.TButton", font=("Segoe UI Semibold", 11), padding=(18, 9))
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
        ttk.Label(frame, text="Active reference").grid(
            row=3, column=0, sticky="w", padx=(0, 12), pady=6
        )
        ttk.Label(frame, textvariable=self.active_reference_var).grid(
            row=3, column=1, columnspan=2, sticky="w", pady=6
        )
        ttk.Label(
            frame,
            text="Use clear speech of at least 5 seconds. The server accepts WAV, MP3, M4A, FLAC, OGG, Opus, AAC, MP4, and WMA.",
            style="Subtitle.TLabel",
        ).grid(row=4, column=0, columnspan=3, sticky="w", pady=(7, 0))
        ttk.Label(
            frame,
            text="Your calling app should use CABLE Output as its microphone.",
            style="Subtitle.TLabel",
        ).grid(row=5, column=0, columnspan=3, sticky="w", pady=(4, 0))

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
            ("SSH private key", self.key_var, False),
        )
        for row, (label, variable, secret) in enumerate(fields, start=1):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=4)
            entry = ttk.Entry(frame, textvariable=variable, show="•" if secret else "")
            entry.grid(row=row, column=1, sticky="ew", pady=4)
            if label == "SSH private key":
                ttk.Button(frame, text="Browse…", command=self.browse_key).grid(row=row, column=2, padx=(8, 0))

        ttk.Label(
            frame,
            text="The API key is stored in Windows Credential Manager, never in settings.json.",
            style="Subtitle.TLabel",
        ).grid(row=6, column=0, columnspan=3, sticky="w", pady=(7, 0))
        ttk.Checkbutton(
            frame,
            text="Stop the RunPod automatically when this app closes",
            variable=self.stop_on_exit_var,
        ).grid(row=7, column=0, columnspan=3, sticky="w", pady=(7, 0))

    def _load_api_key(self) -> None:
        try:
            self.api_key_var.set(get_api_key())
        except ControllerError as exc:
            self._log(str(exc), "warning")

    def browse_key(self) -> None:
        path = filedialog.askopenfilename(title="Select SSH private key", initialdir=str(Path.home() / ".ssh"))
        if path:
            self.key_var.set(path)

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
        try:
            ssh_port = int(self.port_var.get())
        except ValueError as exc:
            raise ControllerError("SSH port must be a number") from exc
        return Settings(
            input_device=input_device,
            output_device=output_device,
            pod_id=self.pod_id_var.get().strip(),
            manage_pod=self.manage_var.get(),
            ssh_host=self.host_var.get().strip(),
            ssh_port=ssh_port,
            ssh_key=self.key_var.get().strip(),
            local_port=8042,
            stop_pod_on_exit=self.stop_on_exit_var.get(),
            reference_file=self.reference_file_var.get().strip(),
            active_reference=self.active_reference_var.get(),
        )

    def _resolve_connection(self, settings: Settings, api_key: str) -> tuple[str, int]:
        host, port = settings.ssh_host, settings.ssh_port
        if settings.manage_pod:
            api = RunPodAPI(api_key)
            connection = pod_connection(api.get_pod(settings.pod_id))
            if connection is None:
                self._event("status", ("pod", "Starting", "busy"))
                self._event("log", ("Starting or resuming the RunPod pod…", "info"))
                api.start_pod(settings.pod_id)
                connection = api.wait_for_ssh(
                    settings.pod_id,
                    progress=lambda line: self._event("log", (line, "info")),
                )
            else:
                self._event("log", ("RunPod is already online.", "info"))
            host, port = connection.host, connection.ssh_port
            self._event("connection", (host, port))
        return host, port

    def start(self) -> None:
        if self.working or (self.client_process and self.client_process.poll() is None):
            return
        try:
            settings = self._current_settings()
            save_settings(settings)
            if settings.manage_pod:
                set_api_key(self.api_key_var.get())
                if not settings.pod_id:
                    raise ControllerError("Pod ID is required for RunPod automation")
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
        try:
            host, port = self._resolve_connection(settings, api_key)
            self._event("status", ("pod", "Online", "ok"))

            self._event("status", ("server", "Starting", "busy"))
            self._event("log", ("Checking the Fast-VC service on the pod…", "info"))
            command = ssh_base_command(host, port, settings.ssh_key) + [REMOTE_START_COMMAND]
            result = subprocess.run(
                command,
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
                raise ControllerError(f"pod service startup failed (SSH exit {result.returncode})")
            self._event("status", ("server", "Ready", "ok"))

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
        except (ControllerError, OSError) as exc:
            self._event("error", str(exc))
        finally:
            self._event("finished", None)

    def upload_reference(self) -> None:
        if self.uploading:
            return
        try:
            reference = validate_reference_file(self.reference_file_var.get())
            settings = self._current_settings()
            if settings.manage_pod:
                if not settings.pod_id or not self.api_key_var.get().strip():
                    raise ControllerError("Pod ID and RunPod API key are required")
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
        self.upload_button.configure(state="disabled")
        self.start_button.configure(state="disabled")
        self.local_button.configure(state="disabled")
        self.stop_pod_button.configure(state="disabled")
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
                + [REMOTE_ACTIVATE_REFERENCE_COMMAND],
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
            self._event("reference_done", (reference.name, str(reference), resume_voice))
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
        if not self.pod_id_var.get().strip() or not self.api_key_var.get().strip():
            messagebox.showinfo("Stop RunPod", "Enter the Pod ID and RunPod API key first.")
            return
        if not messagebox.askyesno(
            "Stop RunPod", "Stop the GPU pod now? The voice connection will end."
        ):
            return
        self.stop_voice()
        api_key = self.api_key_var.get().strip()
        pod_id = self.pod_id_var.get().strip()

        def worker() -> None:
            try:
                RunPodAPI(api_key).stop_pod(pod_id)
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
                    host, port = value  # type: ignore[misc]
                    self.host_var.set(host)
                    self.port_var.set(str(port))
                elif name == "error":
                    self._log(str(value), "error")
                    if not self.closing:
                        messagebox.showerror("SeedVC", str(value))
                elif name == "finished":
                    self._finish_state()
                elif name == "reference_done":
                    display_name, path, resume_voice = value  # type: ignore[misc]
                    self.active_reference_var.set(display_name)
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
                    self.stop_pod_button.configure(state="normal")
                    if not (self.client_process and self.client_process.poll() is None):
                        self.start_button.configure(state="normal")
                        self.local_button.configure(state="normal")
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
