"""A small Tk control panel; all execution remains in the tested command line runner."""
from __future__ import annotations

import argparse
from collections import deque
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
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
        self.log_handle = None
        self.last_status = ""
        self.last_batch = None
        self.master.title("Workstation Test Program")
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
        ttk.Button(body, text="작업 폴더 변경…", command=self.change_root).pack(anchor="w", pady=(4, 10))
        ttk.Label(body, text="정상 중단: 실행 중인 배치의 포트 완료 대기 · RAM/VRAM은 플래너 에뮬레이션 · 측정 전 엔진 게이트 필수",
                  wraplength=900).pack(side="bottom", anchor="w", pady=(12, 0))
        tabs = ttk.Notebook(body)
        tabs.pack(fill="both", expand=True)
        self.run_tab, self.settings_tab = ttk.Frame(tabs, padding=15), ttk.Frame(tabs, padding=15)
        tabs.add(self.run_tab, text="실행 및 상태")
        tabs.add(self.settings_tab, text="엔진 연결 설정")
        self.fields: dict[str, tk.StringVar] = {}
        for row, (key, label, directory) in enumerate([
            ("engine_python", "Python 3.12.10 실행 파일", False),
            ("engine_root", "엔진 저장소 폴더", True),
            ("data_dir", "SPD / 참조 NPZ 폴더", True),
            ("laptop_receipts", "노트북 영수증 폴더", True),
            ("laptop_freeze", "노트북 pip freeze 파일", False),
        ]):
            ttk.Label(self.settings_tab, text=label).grid(row=row * 2, column=0, columnspan=2, sticky="w", pady=(9, 3))
            var = tk.StringVar()
            self.fields[key] = var
            ttk.Entry(self.settings_tab, textvariable=var).grid(row=row * 2 + 1, column=0, sticky="ew")
            ttk.Button(self.settings_tab, text="찾기…", command=lambda v=var, d=directory: self.browse(v, d)).grid(row=row * 2 + 1, column=1, padx=(8, 0))
        self.settings_tab.columnconfigure(0, weight=1)
        ttk.Button(self.settings_tab, text="설정 저장", command=self.save_settings).grid(row=10, column=0, sticky="w", pady=18)
        ttk.Label(self.settings_tab, text="계산 엔진과 데이터는 별도로 준비합니다.\n작업 폴더의 config.json에 designs와 port_sets를 입력하세요. 예시는 사용 설명서를 참고하세요.\nGPU 검증은 NVIDIA 드라이버와 엔진 GPU 의존성이 필요합니다.", wraplength=760).grid(row=11, column=0, columnspan=2, sticky="w")
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
                  "전체 측정은 수 시간 이상 걸릴 수 있습니다. 실패·중단 시 다음 단계는 실행하지 않습니다.",
                  wraplength=800).grid(row=1, column=0, columnspan=2, sticky="w", pady=(7, 0))
        buttons = ttk.Frame(self.run_tab)
        buttons.pack(fill="x", pady=10)
        self.start_button = ttk.Button(buttons, text="선택 조건 수동 실행 / 재개", command=self.start)
        self.start_button.pack(side="left")
        ttk.Button(buttons, text="실행 중 포트 완료 후 중단", command=lambda: self.stop(False)).pack(side="left", padx=8)
        ttk.Button(buttons, text="즉시 중단", command=lambda: self.stop(True)).pack(side="left")
        ttk.Button(buttons, text="결과 폴더", command=self.open_root).pack(side="right")
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
        self.master.protocol("WM_DELETE_WINDOW", self.close)
        self.master.after(250, self.refresh)

    def browse(self, var: tk.StringVar, directory: bool):
        value = filedialog.askdirectory() if directory else filedialog.askopenfilename()
        if value:
            var.set(value)

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
        if not self.fields["engine_python"].get():
            runtime = (shutil.which("python") or "") if getattr(sys, "frozen", False) else sys.executable
            self.fields["engine_python"].set(runtime)

    def save_settings(self) -> bool:
        try:
            if self.child and self.child.poll() is None:
                raise ValueError("실행이 끝난 뒤 설정을 변경하세요.")
            self.root.mkdir(parents=True, exist_ok=True)
            from common import RunLock
            with RunLock(self.root, "gui-settings"):
                path = self.root / "config.json"
                config = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
                config.update({key: value.get().strip() for key, value in self.fields.items()})
                tmp = path.with_suffix(".tmp")
                tmp.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
                tmp.replace(path)
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
            self.work_label.configure(text=str(self.root))
            self.load_settings()

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
        for button in (self.start_button, self.batch_button, self.stage_button):
            button.configure(state="disabled" if busy else "normal")

    def preview_stage(self, event=None):
        if not self.child or self.child.poll() is not None:
            self.show_plan(self.command.get())

    def show_plan(self, stage: str, steps=None):
        from ws_validate import batch_plan
        steps = batch_plan(stage) if steps is None else steps
        self.plan_label.set(f"실행 계획 · {'전체 실험' if stage == 'all' else stage} · {len(steps)}단계")
        states = {"pending": "대기", "running": "실행 중", "completed": "완료", "failed": "실패", "stopped": "중단"}
        existing = self.plan_tree.get_children()
        if list(existing) != [step["id"] for step in steps]:
            self.plan_tree.delete(*existing)
            for step in steps:
                self.plan_tree.insert("", "end", iid=step["id"])
        for index, step in enumerate(steps, 1):
            self.plan_tree.item(step["id"], values=(f"{index}. {step['label']}", states.get(step.get("state"), "대기")))
            if step.get("state") == "running":
                self.plan_tree.see(step["id"])

    def launch(self, options: list[str], label: str):
        if self.child and self.child.poll() is None:
            return
        if not self.save_settings():
            return
        stop_path = self.root / "gui-stop.request"
        args = runner_command() + options + ["--root", str(self.root), "--stop-file", str(stop_path)]
        try:
            from common import RunLock
            # Check the same lock as CLI/agent before clearing this launcher's old stop request.
            with RunLock(self.root, "gui-launch"):
                stop_path.unlink(missing_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
            log_dir = self.root / "launcher-logs"
            log_dir.mkdir(exist_ok=True)
            self.log_handle = (log_dir / f"{stamp}.txt").open("x", encoding="utf-8")
            self.child = subprocess.Popen(args, stdout=self.log_handle, stderr=subprocess.STDOUT,
                                          env={**os.environ, "PYTHONUTF8": "1"},
                                          creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            self._set_busy(True)
            self.status.set(f"{label} 시작 중…")
        except (OSError, RuntimeError) as exc:
            if self.log_handle:
                self.log_handle.close()
                self.log_handle = None
            self._set_busy(False)
            messagebox.showerror("실행 실패", str(exc))

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
                    self.status.set(f"{state} · 단계 {batch['completed']} / {batch['total']} 완료 · "
                                    f"{data.get('current_case') or batch.get('current_id') or ''} · "
                                    f"현재 단계 계산 {complete} / {complete + remaining}")
                if batch:
                    self.progress.configure(maximum=max(batch["total"], 1), value=batch["completed"])
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
            if self.child and self.child.poll() is not None:
                code = self.child.returncode
                self.child = None
                if self.log_handle:
                    self.log_handle.close()
                    self.log_handle = None
                self._set_busy(False)
                if code:
                    self.status.set(f"종료 코드 {code} · 로그에서 원인을 확인하세요.")
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
