# -*- coding: utf-8 -*-
"""听音质检 控制面板
======================
一个窗口跑完整条流水线：抓取录音 → 语音转文字 → AI质检 → 结果文件。

启动：双击「启动质检面板.bat」，或运行  python 质检控制面板.py
"""
import os
import queue
import sys
import threading
import traceback
from collections import Counter
from datetime import date, datetime, timedelta

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import run_pipeline
from core import contract
from core import settings as settings_mod

RULES_CHOICES = [
    ("自动：按通话类型选方案（推荐）", None),
    ("强制全部使用示例方案（rules/example）", "sample"),
]


def open_path(path):
    """用系统默认程序打开文件或目录。"""
    try:
        os.startfile(path)          # noqa: S606 (Windows 专用)
    except Exception as e:
        messagebox.showerror("打开失败", "%s\n%s" % (path, e))


class _QueueWriter(object):
    """把 print 输出转发到 GUI 队列。"""

    def __init__(self, q):
        self.q = q

    def write(self, s):
        self.q.put(("log", s))

    def flush(self):
        pass


class QCApp(object):
    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self.stop_flag = threading.Event()
        self.running = False
        self.import_files = []
        self.cfg = settings_mod.load()

        root.title("听音质检控制面板")
        root.geometry("980x760")
        root.minsize(840, 620)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        self._build_ui()
        self._refresh_output_label()
        self.append_log(
            "欢迎使用听音质检控制面板\n"
            "流程：抓取录音 → 语音转文字 → AI质检 → 输出一个 Excel\n"
            "质检规则：写在 rules/ 目录下，代码里不带业务规则；\n"
            "          抓「全部」时会按每条通话的类型自动选用对应方案。\n"
            "1. 选好时间范围（默认今天），必要时先用「小样本条数」试跑\n"
            "2. 点「开始运行」，可随时「中断」；下次运行自动跳过已完成的\n"
            "3. 结果：%s\n\n" % self.cfg.output_file)

    # ============================================================ 界面
    def _build_ui(self):
        main = ttk.Frame(self.root, padding=10)
        main.pack(fill="both", expand=True)

        # ---------- 时间范围 ----------
        f_time = ttk.LabelFrame(main, text="① 时间范围（格式 YYYY-MM-DD）", padding=8)
        f_time.pack(fill="x", pady=(0, 6))
        today = date.today().strftime("%Y-%m-%d")
        ttk.Label(f_time, text="开始").pack(side="left")
        self.start_var = tk.StringVar(value=today)
        ttk.Entry(f_time, textvariable=self.start_var, width=12).pack(side="left", padx=(4, 12))
        ttk.Label(f_time, text="结束").pack(side="left")
        self.end_var = tk.StringVar(value=today)
        ttk.Entry(f_time, textvariable=self.end_var, width=12).pack(side="left", padx=(4, 12))
        ttk.Button(f_time, text="今天", width=6, command=lambda: self._set_range(0)).pack(side="left", padx=2)
        ttk.Button(f_time, text="近7天", width=7, command=lambda: self._set_range(6)).pack(side="left", padx=2)
        ttk.Button(f_time, text="本月", width=6, command=lambda: self._set_range(-1)).pack(side="left", padx=2)

        # ---------- 抓取范围 ----------
        row = ttk.Frame(main)
        row.pack(fill="x", pady=(0, 6))

        f_type = ttk.LabelFrame(row, text="② 抓取类型", padding=8)
        f_type.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.type_var = tk.StringVar(value="all")
        for text, val in (("外呼", "out"), ("接电", "in"), ("全部（外呼+接电）", "all")):
            ttk.Radiobutton(f_type, text=text, value=val, variable=self.type_var).pack(side="left", padx=8)

        f_status = ttk.LabelFrame(row, text="③ 通话状态", padding=8)
        f_status.pack(side="left", fill="x", expand=True)
        self.status_var = tk.StringVar(value="connected")
        for text, val in (("全部", "all"), ("接通", "connected"), ("未接", "not_connected")):
            ttk.Radiobutton(f_status, text=text, value=val, variable=self.status_var).pack(side="left", padx=8)

        # ---------- 质检与运行设置 ----------
        f_qc = ttk.LabelFrame(main, text="④ 质检与运行设置", padding=8)
        f_qc.pack(fill="x", pady=(0, 6))

        ttk.Label(f_qc, text="质检方案").grid(row=0, column=0, sticky="w")
        self.rules_box = ttk.Combobox(f_qc, state="readonly", width=42,
                                      values=[c[0] for c in RULES_CHOICES])
        self.rules_box.current(0)
        self.rules_box.grid(row=0, column=1, sticky="w", padx=(6, 18))

        ttk.Label(f_qc, text="质检并发").grid(row=0, column=2, sticky="w")
        self.conc_var = tk.StringVar(value=str(self.cfg.get("run.qc_concurrency", 10)))
        ttk.Spinbox(f_qc, from_=1, to=50, width=5, textvariable=self.conc_var).grid(
            row=0, column=3, sticky="w", padx=(6, 18))

        ttk.Label(f_qc, text="下载线程").grid(row=0, column=4, sticky="w")
        self.workers_var = tk.StringVar(value=str(self.cfg.get("run.download_workers", 3)))
        ttk.Spinbox(f_qc, from_=1, to=8, width=5, textvariable=self.workers_var).grid(
            row=0, column=5, sticky="w", padx=(6, 18))

        ttk.Label(f_qc, text="小样本条数").grid(row=0, column=6, sticky="w")
        self.limit_var = tk.StringVar(value="0")
        ttk.Spinbox(f_qc, from_=0, to=100000, width=7, textvariable=self.limit_var).grid(
            row=0, column=7, sticky="w", padx=(6, 0))
        ttk.Label(f_qc, text="（0 = 不限）", foreground="gray").grid(row=0, column=8, sticky="w", padx=(4, 0))

        ttk.Label(
            f_qc, foreground="#1F4E79",
            text="质检项与判定标准在 rules/ 里配置；哪种通话用哪套方案由 config.json 的 qc.call_type_profile 决定"
        ).grid(row=1, column=0, columnspan=9, sticky="w", pady=(6, 0))

        self.overwrite_var = tk.IntVar(value=0)
        ttk.Checkbutton(f_qc, text="覆盖已有质检结果（默认只补空值，不重算）",
                        variable=self.overwrite_var).grid(row=2, column=0, columnspan=4, sticky="w", pady=(4, 0))

        self.mock_llm_var = tk.IntVar(value=0)
        self.mock_asr_var = tk.IntVar(value=0)
        ttk.Checkbutton(f_qc, text="干跑：不调用大模型", variable=self.mock_llm_var).grid(
            row=2, column=4, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Checkbutton(f_qc, text="干跑：不下载/不转写", variable=self.mock_asr_var).grid(
            row=2, column=6, columnspan=3, sticky="w", pady=(4, 0))

        # ---------- 操作按钮 ----------
        f_btn = ttk.Frame(main)
        f_btn.pack(fill="x", pady=(0, 6))
        self.btn_start = ttk.Button(f_btn, text="▶ 开始运行", width=14, command=self.start_full)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(f_btn, text="■ 中断", width=10, state="disabled", command=self.stop_run)
        self.btn_stop.pack(side="left", padx=6)
        self.btn_only_qc = ttk.Button(f_btn, text="只补质检", width=10, command=self.start_only_qc)
        self.btn_only_qc.pack(side="left", padx=6)
        self.btn_recheck = ttk.Button(f_btn, text="重判违规", width=10, command=self.start_recheck)
        self.btn_recheck.pack(side="left", padx=6)
        self.btn_import = ttk.Button(f_btn, text="导入文件", width=10, command=self.start_import)
        self.btn_import.pack(side="left", padx=6)
        self.btn_stats = ttk.Button(f_btn, text="查看统计", width=10, command=self.show_stats)
        self.btn_stats.pack(side="left", padx=6)
        self.btn_open_in = ttk.Button(f_btn, text="打开接电结果", width=12,
                                      command=lambda: self.open_result("接电"))
        self.btn_open_in.pack(side="left", padx=6)
        self.btn_open_out = ttk.Button(f_btn, text="打开外呼结果", width=12,
                                       command=lambda: self.open_result("外呼"))
        self.btn_open_out.pack(side="left", padx=6)

        # ---------- 进度 ----------
        f_prog = ttk.Frame(main)
        f_prog.pack(fill="x", pady=(0, 4))
        self.progress = ttk.Progressbar(f_prog, maximum=100, value=0)
        self.progress.pack(side="left", fill="x", expand=True)
        self.status_label = ttk.Label(f_prog, text="就绪", width=18, anchor="e")
        self.status_label.pack(side="left", padx=8)

        self.output_label = ttk.Label(main, text="", foreground="#1F4E79")
        self.output_label.pack(fill="x", pady=(0, 4))

        # ---------- 日志 ----------
        f_log = ttk.LabelFrame(main, text="运行日志", padding=4)
        f_log.pack(fill="both", expand=True)
        self.log_text = scrolledtext.ScrolledText(f_log, height=20, state="disabled",
                                                  font=("Consolas", 9))
        self.log_text.pack(fill="both", expand=True)

    def _refresh_output_label(self):
        paths = self.result_paths()
        self.output_label.config(
            text="结果文件：" + "　|　".join("%s → %s" % (l, p) for l, p in paths))

    def result_paths(self):
        """当前的结果文件列表 [(标签, 路径)]。"""
        if self.cfg.get("paths.split_by_call_type", True):
            files = self.cfg.get("paths.output_files") or {}
            if files:
                return [(label, self.cfg.resolve(p)) for label, p in files.items()]
        return [(None, self.cfg.output_file)]

    def on_close(self):
        if self.running:
            if not messagebox.askyesno(
                    "任务进行中",
                    "任务正在运行，直接关闭会中断它。\n"
                    "已处理的结果都已保存，下次打开会自动接着跑。\n\n确定关闭？"):
                return
            self.stop_flag.set()
        self.root.destroy()

    def _set_range(self, days):
        end = date.today()
        if days == -1:
            start = end.replace(day=1)
        else:
            start = end - timedelta(days=days)
        self.start_var.set(start.strftime("%Y-%m-%d"))
        self.end_var.set(end.strftime("%Y-%m-%d"))

    def append_log(self, msg):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", msg)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    # ============================================================ 参数收集
    @staticmethod
    def _parse_date(s):
        s = (s or "").strip()
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
            try:
                return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
        raise ValueError("无法解析日期「%s」，请使用 YYYY-MM-DD 格式" % s)

    def collect_args(self, mode="full"):
        start = self._parse_date(self.start_var.get())
        end = self._parse_date(self.end_var.get())
        if start > end:
            raise ValueError("开始日期不能晚于结束日期")
        try:
            limit = int(self.limit_var.get() or 0)
        except ValueError:
            limit = 0
        try:
            concurrency = int(self.conc_var.get() or 10)
            workers = int(self.workers_var.get() or 3)
        except ValueError:
            raise ValueError("并发 / 线程数必须是数字")
        return run_pipeline.make_args(
            mode=mode,
            start=start,
            end=end,
            call_type=self.type_var.get(),
            status=self.status_var.get(),
            limit=limit or None,
            concurrency=concurrency,
            workers=workers,
            rules=RULES_CHOICES[self.rules_box.current()][1],
            overwrite=bool(self.overwrite_var.get()),
            mock_llm=bool(self.mock_llm_var.get()),
            mock_asr=bool(self.mock_asr_var.get()),
            inputs=list(self.import_files),
        )

    # ============================================================ 运行控制
    def _lock_buttons(self, running):
        state = "disabled" if running else "normal"
        for b in (self.btn_start, self.btn_only_qc, self.btn_recheck,
                  self.btn_import):
            b.config(state=state)
        # 「查看统计 / 打开结果」运行中也可用，方便随时看进度
        self.btn_stats.config(state="normal")
        self.btn_open_in.config(state="normal")
        self.btn_open_out.config(state="normal")
        self.btn_stop.config(state="normal" if running else "disabled")

    def _launch(self, args, title):
        if self.running:
            return
        self.running = True
        self.stop_flag = threading.Event()
        self._lock_buttons(True)
        self.progress.config(value=0)
        self.status_label.config(text="准备中...")
        self.append_log("\n%s\n%s\n%s\n" % ("=" * 60, title, "=" * 60))
        threading.Thread(target=self._worker, args=(args,), daemon=True).start()
        self.root.after(200, self.poll_queue)

    def start_full(self):
        try:
            args = self.collect_args("full")
        except ValueError as e:
            messagebox.showerror("参数错误", str(e))
            return
        mock = "（干跑模式）" if (args.mock_llm or args.mock_asr) else ""
        self._launch(args, "新任务：抓取 + 转写 + 质检 %s\n%s ~ %s | %s | %s" % (
            mock, args.start, args.end, args.call_type, args.status))

    def start_only_qc(self):
        try:
            args = self.collect_args("qc")
        except ValueError as e:
            messagebox.showerror("参数错误", str(e))
            return
        self._launch(args, "补跑质检：对结果文件里尚未质检的记录继续处理")

    def start_recheck(self):
        try:
            args = self.collect_args("recheck")
        except ValueError as e:
            messagebox.showerror("参数错误", str(e))
            return
        if not messagebox.askyesno(
                "确认重判",
                "将清空所有「不合格 / 违规」记录的质检结论，然后重新判定。\n"
                "（合格、合规、不适用的记录不受影响）\n\n确定继续？"):
            return
        self._launch(args, "重判：清空不合格/违规结论后重新质检")

    def start_import(self):
        files = filedialog.askopenfilenames(
            title="选择要导入的历史文件（可多选）",
            filetypes=[("表格文件", "*.xlsx *.xlsm *.csv"), ("全部文件", "*.*")])
        if not files:
            return
        self.import_files = list(files)
        try:
            args = self.collect_args("import")
        except ValueError as e:
            messagebox.showerror("参数错误", str(e))
            return
        self._launch(args, "导入历史数据：%d 个文件" % len(files))

    def stop_run(self):
        if not self.running:
            return
        self.stop_flag.set()
        self.btn_stop.config(state="disabled")
        self.append_log("\n[操作] 已点击「中断」，正在收尾（已处理的结果会保存，下次自动接着跑）...\n")

    def open_result(self, label=None):
        paths = self.result_paths()
        target = None
        for name, path in paths:
            if label is None or name == label or name is None:
                target = path
                break
        if not target:
            target = paths[0][1]
        if os.path.exists(target):
            open_path(target)
        else:
            messagebox.showinfo("提示", "结果文件还不存在：\n%s" % target)

    # ============================================================ 后台
    def _worker(self, args):
        old_out, old_err = sys.stdout, sys.stderr
        writer = _QueueWriter(self.q)
        sys.stdout = writer
        sys.stderr = writer
        try:
            run_pipeline.run(args, stop_event=self.stop_flag, progress_cb=self._progress)
        except Exception as e:
            self.q.put(("log", "\n[异常] %s\n%s\n" % (e, traceback.format_exc())))
        finally:
            sys.stdout, sys.stderr = old_out, old_err
            self.q.put(("done",))

    def _progress(self, done, total):
        self.q.put(("progress", done, total))

    def poll_queue(self):
        try:
            while True:
                item = self.q.get_nowait()
                kind = item[0]
                if kind == "log":
                    self.append_log(item[1])
                elif kind == "progress":
                    done, total = item[1], item[2]
                    pct = (done / total * 100) if total else 0
                    self.progress.config(value=pct)
                    self.status_label.config(text="%d / %d" % (done, total))
                elif kind == "stats":
                    self.append_log(item[1])
                elif kind == "done":
                    self.running = False
                    self._lock_buttons(False)
                    self.status_label.config(text="就绪")
                    self._refresh_output_label()
                    self.append_log("\n[任务结束]\n")
        except queue.Empty:
            pass
        if self.running:
            self.root.after(200, self.poll_queue)

    # ============================================================ 统计
    def show_stats(self):
        paths = [(label, path) for label, path in self.result_paths() if os.path.exists(path)]
        if not paths:
            messagebox.showinfo("提示", "还没有结果文件：\n%s"
                                % "\n".join(p for _, p in self.result_paths()))
            return
        self.append_log("\n正在统计结果...\n")
        threading.Thread(target=self._stats_worker, args=(paths,), daemon=True).start()

    def _stats_worker(self, paths):
        for label, path in paths:
            try:
                rows = contract.read_table(path)
                total = len(rows)
                lines = ["\n" + "=" * 52,
                         "结果统计：%s%s" % (os.path.basename(path),
                                             "" if label is None else "（%s）" % label),
                         "总记录数：%d" % total, "=" * 52]

                def dist(col, order=None):
                    c = Counter(contract.text(r.get(col)) or "（空）" for r in rows)
                    items = c.most_common()
                    if order:
                        items = [(k, c.get(k, 0)) for k in order]
                    return "  %s：%s" % (col, "，".join("%s %d" % (k, v) for k, v in items))

                lines.append(dist("转写状态"))
                # 质检结果列按实际表头自动识别（不同规则集的列不一样）
                for col in (rows[0].keys() if rows else []):
                    if col not in contract.QC_COLUMNS or col == "质检方案":
                        continue
                    if col.endswith("-问题分类") or col.endswith("-详情"):
                        continue
                    lines.append(dist(col))
                lines.append("=" * 52 + "\n")
                self.q.put(("log", "\n".join(lines)))
            except Exception as e:
                self.q.put(("log", "[统计失败] %s: %s\n" % (path, e)))


def main():
    root = tk.Tk()
    QCApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
