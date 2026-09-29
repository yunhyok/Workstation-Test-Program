"""A small Tk control panel; all execution remains in the tested command line runner."""
from __future__ import annotations

import argparse
from collections import deque
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tkinter as tk
from tkinter import filedialog, messagebox, ttk


def default_root() -> Path:
    if os.environ.get("SPD_PI_WORK_DIR"):
        return Path(os.environ["SPD_PI_WORK_DIR"]) / "w15"
    return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "WorkstationTestProgram" / "w15"


def runner_command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [str(Path(sys.executable).with_name("WorkstationTest.exe")), "validate"]
    return [sys.executable, str(Path(__file__).with_name("app.py")), "validate"]


class Window:
    def __init__(self, master: tk.Tk, root: Path):
        self.master, self.root = master, root.resolve()
        self.child: subprocess.Popen | None = None
        self.child_kind: str | None = None
        self.pending_launch: tuple[list[str], str] | None = None
        self.log_handle = None
        self.last_status = ""
        self.last_batch = None
        self.preparation_data: dict = {}
        self.master.title("Workstation Test Program")
        icon = Path(__file__).resolve().parent / "assets" / "workstation.ico"
        if os.name == "nt" and icon.is_file():
            self.master.iconbitmap(default=str(icon))
        width = min(1100, self.master.winfo_screenwidth() - 80)
        height = min(860, self.master.winfo_screenheight() - 140)
        self.master.geometry(f"{width}x{height}+20+20")
        self.master.minsize(min(960, width), min(740, height))
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Title.TLabel", font=("Segoe UI", 20, "bold"))
        style.configure("Sub.TLabel", font=("Segoe UI", 10))
        body = ttk.Frame(master, padding=22)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Workstation validation", style="Title.TLabel").pack(anchor="w")
        ttk.Label(body, text="W15 · 엔진 검증 / 자원 매트릭스 / 재개 가능한 실행", style="Sub.TLabel").pack(anchor="w", pady=(3, 15))
        self.work_label = ttk.Label(body, text=str(self.root), wraplength=850)
        self.work_label.pack(anchor="w")
        ttk.Label(body, text="작업 폴더는 자동으로 관리됩니다.", style="Sub.TLabel").pack(anchor="w", pady=(2, 10))
        ttk.Label(body, text="정상 중단: 실행 중인 배치의 포트 완료 대기 · RAM/VRAM은 플래너 에뮬레이션 · 측정 전 엔진 게이트 필수",
                  wraplength=900).pack(side="bottom", anchor="w", pady=(12, 0))
        tabs = ttk.Notebook(body)
        tabs.pack(fill="both", expand=True)
        self.run_tab, self.settings_tab = ttk.Frame(tabs, padding=15), ttk.Frame(tabs, padding=15)
        tabs.add(self.run_tab, text="실행 및 상태")
        tabs.add(self.settings_tab, text="파일 준비")
        self.fields: dict[str, tk.StringVar] = {"data_dir": tk.StringVar()}
        self.setting_widgets: list[tk.Widget] = []
        ttk.Label(self.settings_tab, text="SPD + Touchstone 파일 폴더").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(2, 3))
        data_entry = ttk.Entry(self.settings_tab, textvariable=self.fields["data_dir"])
        data_entry.grid(row=1, column=0, sticky="ew")
        data_browse = ttk.Button(self.settings_tab, text="폴더 선택…", command=self.browse_data_dir)
        data_browse.grid(row=1, column=1, padx=(8, 0))
        self.prepare_button = ttk.Button(self.settings_tab, text="폴더 적용 / 파일 준비",
                                         command=self.prepare_selected_folder)
        self.prepare_button.grid(row=2, column=0, sticky="w", pady=(12, 6))
        self.setting_widgets.extend((data_entry, data_browse, self.prepare_button))
        self.preparation_status = tk.StringVar(value="준비 전 · SPD와 Touchstone이 있는 폴더를 선택하세요.")
        ttk.Label(self.settings_tab, textvariable=self.preparation_status, wraplength=780).grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(2, 8))
        self.runtime_status = tk.StringVar()
        ttk.Label(self.settings_tab, textvariable=self.runtime_status, wraplength=780).grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(2, 10))

        self.advanced_visible = tk.BooleanVar(value=False)
        self.advanced_toggle = ttk.Checkbutton(
            self.settings_tab, text="고급 설정 보기 · 외부 엔진 또는 노트북 비교(선택)",
            variable=self.advanced_visible, command=self.toggle_advanced)
        self.advanced_toggle.grid(row=5, column=0, columnspan=2, sticky="w", pady=(6, 2))
        self.advanced_frame = ttk.Frame(self.settings_tab)
        self.advanced_frame.grid(row=6, column=0, columnspan=2, sticky="ew")
        self.advanced_frame.columnconfigure(0, weight=1)
        for row, (key, label, directory) in enumerate([
            ("engine_python", "외부 Python 실행 파일(선택)", False),
            ("engine_root", "외부 엔진 저장소 폴더(선택)", True),
            ("laptop_receipts", "노트북 비교 영수증 폴더(선택)", True),
            ("laptop_freeze", "노트북 패키지 목록 파일(선택)", False),
        ]):
            ttk.Label(self.advanced_frame, text=label).grid(
                row=row * 2, column=0, columnspan=2, sticky="w", pady=(7, 2))
            var = tk.StringVar()
            self.fields[key] = var
            entry = ttk.Entry(self.advanced_frame, textvariable=var)
            entry.grid(row=row * 2 + 1, column=0, sticky="ew")
            button = ttk.Button(self.advanced_frame, text="찾기…",
                                command=lambda v=var, d=directory: self.browse(v, d))
            button.grid(row=row * 2 + 1, column=1, padx=(8, 0))
            self.setting_widgets.extend((entry, button))
        self.advanced_save_button = ttk.Button(
            self.advanced_frame, text="고급 설정 저장", command=self.save_settings)
        self.advanced_save_button.grid(row=8, column=0, sticky="w", pady=12)
        self.setting_widgets.extend((self.advanced_toggle, self.advanced_save_button))
        change_root = ttk.Button(self.advanced_frame, text="결과 작업 폴더 변경…", command=self.change_root)
        change_root.grid(row=8, column=1, sticky="e", padx=(8, 0), pady=12)
        self.setting_widgets.append(change_root)
        self.advanced_frame.grid_remove()
        self.settings_tab.columnconfigure(0, weight=1)
        controls = ttk.Frame(self.run_tab)
        controls.pack(fill="x")
        self.command = tk.StringVar(value="env")
        self.ports = tk.StringVar(value="P9")
        self.axis = tk.StringVar(value="all")
        for column, (label, var, values) in enumerate([
            ("실행 단계", self.command, ("env", "gates", "baseline", "matrix", "converge", "report")),
            ("포트 집합", self.ports, ("P9", "P20", "P92")),
            ("매트릭스 축", self.axis, ("all", "B", "C", "D", "E")),
        ]):
            ttk.Label(controls, text=label).grid(row=0, column=column, sticky="w", padx=(0, 12))
            combo = ttk.Combobox(controls, textvariable=var, values=values, state="readonly", width=18)
            combo.grid(row=1, column=column, sticky="ew", padx=(0, 12), pady=4)
            if var is self.command:
                combo.bind("<<ComboboxSelected>>", self.preview_stage)
            controls.columnconfigure(column, weight=1)
        automatic = ttk.LabelFrame(self.run_tab, text="자동 실행 · 조건을 순서대로 적용", padding=10)
        automatic.pack(fill="x", pady=(10, 0))
        self.batch_button = ttk.Button(automatic, text="전체 실험 순차 실행 / 재개",
                                       command=lambda: self.start_batch("all"))
        self.batch_button.grid(row=0, column=0, sticky="w", padx=(0, 12))
        self.stage_button = ttk.Button(automatic, text="선택 단계의 모든 조건 실행 / 재개",
                                       command=lambda: self.start_batch(self.command.get()))
        self.stage_button.grid(row=0, column=1, sticky="w")
        ttk.Label(automatic, text="전체: env → gates → baseline(P9·P92) → matrix(B–E) → converge(P92) → report\n"
                  "자동 실행은 등록된 조건을 사용합니다. 위 포트·축 선택은 수동 실행에만 적용됩니다.\n"
                  "독립된 단계는 실패 후에도 계속합니다. 필수 검증 실패 시 종속 측정은 건너뛰며, 중단 요청은 전체 실행에 적용됩니다.",
                  wraplength=800).grid(row=1, column=0, columnspan=2, sticky="w", pady=(7, 0))
        buttons = ttk.Frame(self.run_tab)
        buttons.pack(fill="x", pady=10)
        self.start_button = ttk.Button(buttons, text="선택 조건 수동 실행 / 재개", command=self.start)
        self.start_button.pack(side="left")
        ttk.Button(buttons, text="실행 중 포트 완료 후 중단", command=lambda: self.stop(False)).pack(side="left", padx=8)
        ttk.Button(buttons, text="즉시 중단", command=lambda: self.stop(True)).pack(side="left")
        ttk.Button(buttons, text="결과 폴더", command=self.open_root).pack(side="right")
        self.export_button = ttk.Button(buttons, text="결과 ZIP 저장", command=self.export_results)
        self.export_button.pack(side="right", padx=8)
        ttk.Label(self.run_tab, text="자동 실행·보고서 종료 시 결과 ZIP을 작업 폴더의 exports에 저장합니다.",
                  wraplength=850).pack(anchor="w")
        self.status = tk.StringVar(value="준비 · 전체 실험 버튼으로 등록된 모든 단계를 순차 실행할 수 있습니다.")
        ttk.Label(self.run_tab, textvariable=self.status, wraplength=850).pack(anchor="w", pady=5)
        self.progress = ttk.Progressbar(self.run_tab, mode="determinate")
        self.progress.pack(fill="x", pady=(2, 10))
        self.plan_label = tk.StringVar()
        ttk.Label(self.run_tab, textvariable=self.plan_label).pack(anchor="w")
        plan_frame = ttk.Frame(self.run_tab)
        plan_frame.pack(fill="x", pady=(4, 10))
        self.plan_tree = ttk.Treeview(plan_frame, columns=("step", "state"), show="headings", height=5)
        self.plan_tree.heading("step", text="실행 순서 / 조건")
        self.plan_tree.heading("state", text="상태")
        self.plan_tree.column("step", width=670, stretch=True)
        self.plan_tree.column("state", width=110, stretch=False)
        plan_scroll = ttk.Scrollbar(plan_frame, orient="vertical", command=self.plan_tree.yview)
        self.plan_tree.configure(yscrollcommand=plan_scroll.set)
        self.plan_tree.pack(side="left", fill="x", expand=True)
        plan_scroll.pack(side="right", fill="y")
        self.show_plan("all")
        ttk.Label(self.run_tab, text="최근 로그").pack(anchor="w")
        log_frame = ttk.Frame(self.run_tab)
        log_frame.pack(fill="both", expand=True, pady=(4, 0))
        self.log = tk.Text(log_frame, wrap="word", font=("Consolas", 10), state="disabled", height=10)
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=scrollbar.set)
        self.log.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.load_settings()
        if not self.fields["data_dir"].get():
            tabs.select(self.settings_tab)
        self.master.protocol("WM_DELETE_WINDOW", self.close)
        self.master.after(250, self.refresh)

    def browse(self, var: tk.StringVar, directory: bool):
        value = filedialog.askdirectory() if directory else filedialog.askopenfilename()
        if value:
            var.set(value)

    def browse_data_dir(self):
        value = filedialog.askdirectory()
        if value:
            self.fields["data_dir"].set(value)
            self.preparation_status.set("폴더가 변경되었습니다 · 실행 전에 파일 준비가 자동으로 진행됩니다.")

    def toggle_advanced(self):
        if self.advanced_visible.get():
            self.advanced_frame.grid()
        else:
            self.advanced_frame.grid_remove()

    def _show_runtime_status(self):
        runtime = None
        try:
            from common import bundled_runtime
            runtime = bundled_runtime()
        except (ImportError, AttributeError, OSError, RuntimeError):
            pass
        if runtime:
            self.runtime_status.set(
                "내장 런타임 설치 감지됨 · CPU/GPU 사용 가능 여부는 env 확인이 필요합니다. "
                "GPU 사용에는 NVIDIA 드라이버가 필요합니다.")
        else:
            self.runtime_status.set(
                "내장 런타임을 찾지 못했습니다 · 고급 설정에서 외부 엔진을 지정할 수 있습니다. "
                "GPU 사용에는 NVIDIA 드라이버와 env 확인이 필요합니다.")

    @staticmethod
    def _normal_path(value: str | Path) -> str:
        try:
            return os.path.normcase(str(Path(value).expanduser().resolve()))
        except (OSError, ValueError):
            return os.path.normcase(os.path.abspath(os.path.expanduser(str(value))))

    @staticmethod
    def _prepared_data_dir(record: dict) -> str:
        for mapping in (record, record.get("settings", {}), record.get("input", {}),
                        record.get("metadata", {})):
            if isinstance(mapping, dict) and mapping.get("data_dir"):
                return str(mapping["data_dir"])
        return ""

    @staticmethod
    def _format_preparation(record: dict) -> str:
        if not record:
            return "준비 전 · SPD와 Touchstone이 있는 폴더를 선택하세요."
        if record.get("ok") is False or record.get("error"):
            return f"파일 준비 실패 · {record.get('error') or '로그에서 원인을 확인하세요.'}"
        designs = record.get("designs", {})
        design_count = len(designs) if isinstance(designs, (dict, list)) else 0
        files = record.get("discovered_files", record.get("files", record.get("discovered", [])))
        if isinstance(files, dict):
            file_names = [str(value) for value in files.values() if isinstance(value, (str, Path))]
            file_count = len(files)
        elif isinstance(files, list):
            file_names = [str(value) for value in files]
            file_count = len(files)
        else:
            file_names, file_count = [], int(files or 0) if isinstance(files, int) else 0
        if not file_names and isinstance(designs, dict):
            metadata_designs = record.get("metadata", {}).get("designs", {})
            for design_id, design in designs.items():
                if not isinstance(design, dict):
                    continue
                source_touchstone = (metadata_designs.get(design_id, {}).get("source_touchstone")
                                     if isinstance(metadata_designs, dict) else None)
                file_names.extend(str(value) for value in (design.get("spd"), source_touchstone)
                                  if value)
            file_count = len(file_names)
        port_sets = record.get("port_sets", {})
        ports = record.get("ports")
        design_port_count = (sum(len(item.get("ports", [])) for item in designs.values()
                                 if isinstance(item, dict) and isinstance(item.get("ports"), list))
                             if isinstance(designs, dict) else 0)
        if design_port_count:
            port_count = design_port_count
        elif isinstance(ports, list):
            port_count = len(ports)
        elif isinstance(ports, int):
            port_count = ports
        elif isinstance(port_sets, dict):
            unique_ports = set()
            for values in port_sets.values():
                if isinstance(values, list):
                    unique_ports.update(str(value) for value in values)
            port_count = len(unique_ports)
        else:
            port_count = int(record.get("port_count", 0) or 0)
        names = ", ".join(Path(value).name for value in file_names[:3])
        details = f" · {names}" if names else ""
        return f"준비 완료 · 설계 {design_count}개 · 발견 파일 {file_count}개 · 포트 {port_count}개{details}"

    def _load_preparation(self):
        self.preparation_data = {}
        try:
            path = self.root / "preparation.json"
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8-sig"))
                if isinstance(data, dict):
                    self.preparation_data = data
        except (OSError, ValueError):
            self.preparation_data = {"ok": False, "error": "준비 결과를 읽을 수 없습니다."}
        self.preparation_status.set(self._format_preparation(self.preparation_data))

    def load_settings(self):
        config = {}
        try:
            config = json.loads((self.root / "config.json").read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            pass
        except (ValueError, OSError) as exc:
            messagebox.showerror("설정 읽기 실패", str(exc))
        for key, var in self.fields.items():
            var.set(config.get(key) or "")
        self._show_runtime_status()
        self._load_preparation()

    def _needs_prepare(self) -> bool:
        selected = self.fields["data_dir"].get().strip()
        if not selected:
            return True
        try:
            config = json.loads((self.root / "config.json").read_text(encoding="utf-8-sig"))
        except (FileNotFoundError, OSError, ValueError):
            return True
        configured = config.get("data_dir")
        if not configured or self._normal_path(configured) != self._normal_path(selected):
            return True
        record = self.preparation_data
        if record.get("ok") is not True:
            return True
        prepared_dir = self._prepared_data_dir(record)
        return bool(prepared_dir and self._normal_path(prepared_dir) != self._normal_path(selected))

    def _manual_configuration(self) -> bool:
        try:
            config = json.loads((self.root / "config.json").read_text(encoding="utf-8-sig"))
        except (FileNotFoundError, OSError, ValueError):
            return False
        return config.get("configuration_mode") == "manual"

    def _data_dir_changed(self) -> bool:
        try:
            config = json.loads((self.root / "config.json").read_text(encoding="utf-8-sig"))
        except (FileNotFoundError, OSError, ValueError):
            return True
        selected = self.fields["data_dir"].get().strip()
        configured = config.get("data_dir")
        return (not selected or not configured or
                self._normal_path(configured) != self._normal_path(selected))

    def save_settings(self) -> bool:
        try:
            if self.child and self.child.poll() is None:
                raise ValueError("실행이 끝난 뒤 설정을 변경하세요.")
            self.root.mkdir(parents=True, exist_ok=True)
            from common import RunLock, atomic_json
            with RunLock(self.root, "gui-settings"):
                path = self.root / "config.json"
                config = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
                original = dict(config)
                external_python = self.fields["engine_python"].get().strip()
                external_root = self.fields["engine_root"].get().strip()
                if bool(external_python) != bool(external_root):
                    raise ValueError("외부 엔진 Python과 저장소 폴더를 모두 지정하거나 둘 다 비우세요.")
                for key, field in self.fields.items():
                    value = field.get().strip()
                    if key == "data_dir" or value:
                        config[key] = value
                    elif config.get(key) not in (None, ""):
                        # Clearing an optional legacy value is explicit, while generated nulls stay
                        # byte-for-byte stable when the advanced panel is only viewed and saved.
                        config[key] = None
                if external_python and external_root:
                    config["engine_mode"] = "external"
                else:
                    from common import bundled_runtime
                    if bundled_runtime():
                        config["engine_mode"] = "bundled"
                    elif config.get("engine_mode") == "external":
                        raise ValueError("내장 런타임이 없으므로 외부 엔진 Python과 저장소 폴더가 필요합니다.")
                if config != original or not path.exists():
                    atomic_json(path, config)
            self.status.set("설정 저장 완료 · env로 연결 상태를 확인하세요.")
            return True
        except (OSError, ValueError, RuntimeError) as exc:
            messagebox.showerror("설정 저장 실패", str(exc))
            return False

    def change_root(self):
        if self.child and self.child.poll() is None:
            messagebox.showinfo("실행 중", "실행이 끝난 뒤 작업 폴더를 바꿀 수 있습니다.")
            return
        selected = filedialog.askdirectory()
        if selected:
            self.root = Path(selected).resolve()
            self.last_batch = None
            self.pending_launch = None
            self.work_label.configure(text=str(self.root))
            self.load_settings()

    def prepare_selected_folder(self):
        self.pending_launch = None
        self.start_prepare()

    def start_prepare(self):
        if self.child and self.child.poll() is None:
            return
        selected = self.fields["data_dir"].get().strip()
        if not selected:
            messagebox.showerror("파일 준비 실패", "SPD와 Touchstone이 있는 폴더를 선택하세요.")
            self.pending_launch = None
            return
        data_dir = Path(selected).expanduser()
        if not data_dir.is_dir():
            messagebox.showerror("파일 준비 실패", f"폴더를 찾을 수 없습니다: {data_dir}")
            self.pending_launch = None
            return
        stop_path = self.root / "gui-stop.request"
        try:
            from common import RunLock
            with RunLock(self.root, "gui-prepare"):
                stop_path.unlink(missing_ok=True)
        except (OSError, RuntimeError) as exc:
            self.pending_launch = None
            messagebox.showerror("파일 준비 실패", str(exc))
            return
        args = runner_command() + ["prepare", "--root", str(self.root), "--data-dir", str(data_dir),
                                   "--stop-file", str(stop_path)]
        self._spawn(args, "파일 준비", "prepare")

    def start(self):
        cmd = self.command.get()
        args = [cmd]
        if cmd in ("baseline", "matrix", "converge"):
            args += ["--ports", self.ports.get()]
        if cmd == "matrix":
            args += ["--axis", self.axis.get()]
        self.launch(args, cmd)

    def start_batch(self, stage: str):
        if self.child and self.child.poll() is None:
            return
        self.show_plan(stage)
        self.launch(["batch", "--stage", stage], "전체 실험" if stage == "all" else f"{stage} 전체 조건")

    def _set_busy(self, busy: bool):
        for button in (self.start_button, self.batch_button, self.stage_button, self.export_button):
            button.configure(state="disabled" if busy else "normal")
        for widget in self.setting_widgets:
            widget.configure(state="disabled" if busy else "normal")

    def preview_stage(self, event=None):
        if not self.child or self.child.poll() is not None:
            self.show_plan(self.command.get())

    def show_plan(self, stage: str, steps=None):
        from ws_validate import batch_plan
        steps = batch_plan(stage) if steps is None else steps
        self.plan_label.set(f"실행 계획 · {'전체 실험' if stage == 'all' else stage} · {len(steps)}단계")
        states = {"pending": "대기", "running": "실행 중", "completed": "완료", "failed": "실패",
                  "stopped": "중단", "skipped": "선행 조건 미충족"}
        existing = self.plan_tree.get_children()
        if list(existing) != [step["id"] for step in steps]:
            self.plan_tree.delete(*existing)
            for step in steps:
                self.plan_tree.insert("", "end", iid=step["id"])
        for index, step in enumerate(steps, 1):
            reason = f" · {step['error']}" if step.get("error") else ""
            self.plan_tree.item(step["id"], values=(f"{index}. {step['label']}{reason}", states.get(step.get("state"), "대기")))
            if step.get("state") == "running":
                self.plan_tree.see(step["id"])

    def launch(self, options: list[str], label: str, prepared: bool = False):
        if self.child and self.child.poll() is None:
            return
        # Folder mode refreshes discovery before every launch. The worker caches conversions, and
        # this catches a replaced SPD/Touchstone file even when the selected directory is unchanged.
        if not prepared and (not self._manual_configuration() or self._data_dir_changed()):
            self.pending_launch = (list(options), label)
            self.start_prepare()
            return
        stop_path = self.root / "gui-stop.request"
        args = runner_command() + options + ["--root", str(self.root), "--stop-file", str(stop_path)]
        try:
            from common import RunLock
            # Check the same lock as CLI/agent before clearing this launcher's old stop request.
            with RunLock(self.root, "gui-launch"):
                stop_path.unlink(missing_ok=True)
        except (OSError, RuntimeError) as exc:
            messagebox.showerror("실행 실패", str(exc))
            return
        self.pending_launch = None
        self._spawn(args, label, "run")

    def _spawn(self, args: list[str], label: str, kind: str):
        try:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
            log_dir = self.root / "launcher-logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            self.log_handle = (log_dir / f"{stamp}.txt").open("x", encoding="utf-8")
            self.child = subprocess.Popen(args, stdout=self.log_handle, stderr=subprocess.STDOUT,
                                          env={**os.environ, "PYTHONUTF8": "1"},
                                          creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            self.child_kind = kind
            self._set_busy(True)
            self.status.set(f"{label} 시작 중…")
            if kind == "prepare":
                self.preparation_status.set("파일 확인 및 설정 준비 중…")
        except (OSError, RuntimeError) as exc:
            if self.log_handle:
                self.log_handle.close()
                self.log_handle = None
            self.child = None
            self.child_kind = None
            self.pending_launch = None
            self._set_busy(False)
            messagebox.showerror("실행 실패", str(exc))

    def export_results(self):
        if self.child and self.child.poll() is None:
            return
        self.pending_launch = None
        self._spawn(runner_command() + ["export", "--root", str(self.root)], "결과 ZIP 저장", "export")

    def _finish_child(self, code: int):
        kind = self.child_kind
        pending = self.pending_launch
        self.child = None
        self.child_kind = None
        if self.log_handle:
            self.log_handle.close()
            self.log_handle = None
        self._set_busy(False)
        if kind == "prepare":
            stopped = (self.root / "gui-stop.request").exists()
            self._load_preparation()
            if code or stopped or self._needs_prepare():
                self.pending_launch = None
                if stopped:
                    self.status.set("파일 준비 중단됨 · 요청한 실행은 시작하지 않았습니다.")
                    self.preparation_status.set("파일 준비 중단됨 · 다시 실행하면 새로 준비합니다.")
                else:
                    self.status.set(f"파일 준비 실패 · 종료 코드 {code} · 로그에서 원인을 확인하세요.")
                if not stopped and not self.preparation_data.get("error"):
                    self.preparation_status.set("파일 준비 실패 · 로그에서 원인을 확인하세요.")
                return
            self.load_settings()
            self.status.set("파일 준비 완료 · 실행할 수 있습니다.")
            if pending:
                self.pending_launch = None
                self.launch(pending[0], pending[1], prepared=True)
            return
        self.pending_launch = None
        if kind == "export" and code == 0:
            try:
                record = json.loads((self.root / "export.json").read_text(encoding="utf-8"))
                messagebox.showinfo("결과 ZIP 저장 완료", str(record.get("path") or self.root / "exports"))
            except (OSError, ValueError):
                messagebox.showinfo("결과 ZIP 저장 완료", str(self.root / "exports"))
        if code:
            self.status.set(f"종료 코드 {code} · 로그에서 원인을 확인하세요.")

    def stop(self, now: bool):
        if not self.child or self.child.poll() is not None:
            self.status.set("이 창에서 실행한 작업이 없습니다. 원격 작업은 ws_ctl stop을 사용하세요.")
            return
        try:
            (self.root / "gui-stop.request").write_text("now" if now else "graceful", encoding="utf-8")
            self.status.set("즉시 중단 요청됨" if now else "현재 실행 중인 모든 포트 완료 후 중단 요청됨")
        except OSError as exc:
            messagebox.showerror("중단 요청 실패", str(exc))

    def refresh(self):
        try:
            path = self.root / "status.json"
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8-sig"))
                state = "실행 중" if data.get("running") else (
                    "중단됨" if data.get("stopped_at") else (
                        "실패" if data.get("exit_code") else "완료"))
                complete = data.get("completed", 0)
                remaining = data.get("remaining", 0)
                self.status.set(f"{state} · {data.get('subcommand', '')} · 완료 {complete} / 남음 {remaining} · {data.get('current_case') or ''}")
                batch = data.get("batch")
                if batch:
                    if batch != self.last_batch:
                        self.show_plan(batch["stage"], batch["steps"])
                        self.last_batch = batch
                    self.status.set(f"{state} · 처리 {batch.get('finished', batch['completed'])} / {batch['total']} · "
                                    f"완료 {batch['completed']} · 실패 {batch.get('failed', 0)} · 건너뜀 {batch.get('skipped', 0)} · "
                                    f"{data.get('current_case') or batch.get('current_id') or ''} · "
                                    f"현재 단계 계산 {complete} / {complete + remaining}")
                if batch:
                    self.progress.configure(maximum=max(batch["total"], 1), value=batch.get("finished", batch["completed"]))
                elif isinstance(complete, int) and isinstance(remaining, int):
                    self.progress.configure(maximum=max(complete + remaining, 1), value=complete)
            logs = sorted((self.root / "runs").glob("*/log.txt"))
            logs += sorted((self.root / "launcher-logs").glob("*.txt"))
            if logs:
                latest = max(logs, key=lambda p: p.stat().st_mtime_ns)
                with latest.open(encoding="utf-8", errors="replace") as source:
                    content = "".join(deque(source, maxlen=120))
                if content != self.last_status:
                    self.log.configure(state="normal")
                    self.log.delete("1.0", "end")
                    self.log.insert("end", content)
                    self.log.see("end")
                    self.log.configure(state="disabled")
                    self.last_status = content
            child = self.child
            if child and child.poll() is not None:
                self._finish_child(child.returncode)
        except (OSError, ValueError):
            pass  # Atomic status replacement can briefly race antivirus/file readers.
        self.master.after(1000, self.refresh)

    def open_root(self):
        self.root.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            os.startfile(self.root)

    def close(self):
        if self.child and self.child.poll() is None:
            messagebox.showinfo("작업 실행 중", "먼저 중단을 요청하고 종료 상태를 확인하세요. 진행 중인 결과를 보존합니다.")
            return
        self.master.destroy()


def main() -> int:
    parser = argparse.ArgumentParser(description="Workstation Test Program control panel")
    parser.add_argument("--root", type=Path, default=default_root())
    parser.add_argument("--smoke", action="store_true", help="Create controls, render once and exit")
    args = parser.parse_args()
    root = tk.Tk()
    window = Window(root, args.root)
    if args.smoke:
        root.update()
        assert window.start_button.winfo_exists()
        assert window.batch_button.winfo_exists() and window.stage_button.winfo_exists()
        assert len(window.plan_tree.get_children()) == 10
        root.destroy()
        print("GUI smoke PASS")
    else:
        root.mainloop()
    return 0
