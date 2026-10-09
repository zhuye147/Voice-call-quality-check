# -*- coding: utf-8 -*-
"""听音质检 一键流水线
========================
抓取录音 → 语音转文字 → AI质检 → 单文件交付（output\\听音质检结果.xlsx）

常用命令：
  抓取+转写+质检（默认今天、接通、外呼+接电）：
      python run_pipeline.py --mode full
  指定日期范围：
      python run_pipeline.py --mode full --start 2026-09-01 --end 2026-09-30
  只补跑质检（不抓取、不转写）：
      python run_pipeline.py --mode qc
  导入历史数据（旧录音内容.xlsx / 抓取结果_*.csv）：
      python run_pipeline.py --mode import --inputs "录音内容.xlsx" "抓取结果_2026-09.csv"
  干跑自测（不联网、不加载模型）：
      python run_pipeline.py --mode import --inputs "录音内容.xlsx" --mock-llm
"""
import argparse
import os
import queue
import sys
import threading
import time
import traceback
from datetime import date, datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import contract
from core import asr as asr_mod
from core import scraper
from core import settings as settings_mod
from core import sysutil
from core.llm import LLMClient
from core.qc import QCEngine, QCProfile, load_rules, rule_done
from core.store import ResultStore


# ================================================================ 工具

class Counter(object):
    def __init__(self):
        self._lock = threading.Lock()
        self.n = 0

    def inc(self):
        with self._lock:
            self.n += 1
            return self.n

    def value(self):
        with self._lock:
            return self.n


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def put_with_stop(q, item, stop_event=None, timeout=0.5):
    """入队；若队列满，等待期间反复检查中断标记，避免中断时卡死。"""
    while True:
        if stop_event is not None and stop_event.is_set():
            return False
        try:
            q.put(item, timeout=timeout)
            return True
        except queue.Full:
            continue


def banner(title):
    print("=" * 64)
    print(title)
    print("=" * 64)


def output_display_paths(cfg):
    """当前会产出的结果文件（分文件时返回两个）。"""
    if cfg.get("paths.split_by_call_type", True):
        files = cfg.get("paths.output_files") or {}
        if files:
            return [cfg.resolve(p) for p in files.values()]
    return [cfg.output_file]


def _make_selector(cfg):
    """按「通话类型」选质检方案，例如 接电→inbound、外呼→outbound。"""
    mapping = cfg.get("qc.call_type_profile") or {}

    def selector(rec):
        return mapping.get(contract.text(rec.get("通话类型")))
    return selector


def build_engine(cfg, mock_llm, rules_override=None):
    """构建质检引擎：多套方案（接电/外呼）+ 按通话类型自动选择。"""
    profiles = {}
    forced = None

    conf_profiles = cfg.get("qc.profiles") or {}
    for name, item in conf_profiles.items():
        rules_dir = item if isinstance(item, str) else item.get("dir")
        label = name if isinstance(item, str) else (item.get("label") or name)
        desc = "" if isinstance(item, str) else (item.get("desc") or "")
        profiles[name] = QCProfile(name, load_rules(cfg.resolve(rules_dir)),
                                   label=label, desc=desc)

    if not profiles:
        # 兼容只配了 qc.rules_dir 的旧写法
        profiles["default"] = QCProfile("default", load_rules(cfg.rules_dir),
                                        label="默认质检")

    if rules_override:
        # --rules 支持两个写法：方案名（inbound/outbound）或规则目录路径
        if rules_override in profiles:
            forced = rules_override
        else:
            profiles["custom"] = QCProfile("custom", load_rules(cfg.resolve(rules_override)),
                                           label="自定义规则(%s)" % rules_override)
            forced = "custom"

    llm = LLMClient(cfg.llm_config(), mock=mock_llm)
    default_profile = forced or cfg.get("qc.default_profile")
    engine = QCEngine(
        profiles, llm,
        default_profile=default_profile,
        selector=None if forced else _make_selector(cfg),
        min_content_len=cfg.get("qc.min_content_len", 10),
        truncate=cfg.get("qc.content_truncate", 4000),
        overwrite=cfg.get("qc.overwrite", False),
        clear_out_of_scope=cfg.get("qc.clear_out_of_scope", True),
    )
    print("质检方案: %s" % engine.summary())
    if forced:
        print("  （已强制使用方案：%s）" % forced)
    else:
        print("  （按通话类型自动选择，默认 %s）" % engine.default_name)
    return engine


def apply_cli_overrides(cfg, args):
    if args.concurrency:
        cfg.data["run"]["qc_concurrency"] = args.concurrency
    if args.workers:
        cfg.data["run"]["download_workers"] = args.workers
    if args.batch:
        cfg.data["run"]["asr_batch"] = args.batch
    if args.rules:
        cfg.data["qc"]["rules_dir"] = args.rules
    if args.output:
        cfg.data["paths"]["output_file"] = args.output
        # 结果文件换了位置，缓存也要跟着走，避免污染正式结果的缓存
        cfg.data["paths"]["cache_dir"] = os.path.join(
            os.path.dirname(cfg.output_file), "_cache")
        # 手动指定单一结果文件时不再分文件（测试/临时导出用）
        cfg.data["paths"]["split_by_call_type"] = False
    if args.overwrite:
        cfg.data["qc"]["overwrite"] = True
    if args.call_type:
        cfg.data["run"]["call_type"] = args.call_type
    if args.status:
        cfg.data["run"]["status_filter"] = args.status
    if args.limit is not None:
        cfg.data["run"]["limit"] = args.limit


def make_store(cfg):
    cfg.ensure_dirs()
    split_map = None
    if cfg.get("paths.split_by_call_type", True):
        files = cfg.get("paths.output_files") or {}
        split_map = {label: cfg.resolve(p) for label, p in files.items()}
    store = ResultStore(
        cfg.output_file, cfg.cache_dir,
        save_every_n=cfg.get("run.save_every_n", 200),
        save_interval_sec=cfg.get("run.save_interval_sec", 300),
        lock_retry=cfg.get("run.excel_lock_retry", 5),
        save_retry_interval=cfg.get("run.save_retry_interval", 60),
        split_map=split_map,
        split_by="通话类型",
        split_default=cfg.get("paths.split_default", "接电"),
    )
    store.load()
    return store


def qc_done_groups(engine, rec):
    done = []
    for name, rules in engine.profile_of(rec).groups.items():
        if all(rule_done(r, rec) for r in rules):
            done.append(name)
    return done


# ================================================================ 导入

def _same_record(a, b):
    """判断两条记录是否本来就是同一条（用于识别真正的重复行）。"""
    for col in ("语音内容", "单据编号", "通话时间"):
        if contract.text(a.get(col)) != contract.text(b.get(col)):
            return False
    return True


def _unique_key(store, rec):
    """主键冲突时，给真正的重复行分配 录音id#2、录音id#3 …（保证行不丢失且可重复导入）。"""
    key = contract.record_key(rec)
    if not key:
        return None
    cur = store.get(key)
    if cur is None or _same_record(cur, rec):
        return key
    n = 2
    while True:
        cand = "%s#%d" % (key, n)
        cur = store.get(cand)
        if cur is None:
            return cand
        if _same_record(cur, rec):
            return cand
        n += 1


def cmd_import(cfg, args, store, engine):
    paths = []
    for p in args.inputs or []:
        full = p if os.path.isabs(p) else os.path.join(ROOT, p)
        if not os.path.exists(full):
            print("  [WARN] 文件不存在，跳过: %s" % p)
            continue
        paths.append(full)
    if not paths:
        print("请用 --inputs 指定要导入的文件")
        return 0

    print("读取待导入文件...")
    recs = contract.load_records_from_files(paths)
    added = updated = skipped = 0
    for rec in recs:
        key = _unique_key(store, rec)
        if not key:
            skipped += 1
            continue
        old = store.get(key)
        if old:
            # 已有记录：只补空列，不覆盖已有值
            for col in contract.COLUMNS:
                if contract.is_blank(old.get(col)) and not contract.is_blank(rec.get(col)):
                    old[col] = rec[col]
            if contract.is_blank(old.get("转写状态")) and not contract.is_blank(old.get("语音内容")):
                old["转写状态"] = contract.ASR_STATUS_OK
            store.put(key, old)
            updated += 1
        else:
            if contract.is_blank(rec.get("转写状态")):
                if not contract.is_blank(rec.get("语音内容")):
                    rec["转写状态"] = contract.ASR_STATUS_OK
                    rec["转写时间"] = rec.get("转写时间") or now_str()
                else:
                    rec["转写状态"] = contract.ASR_STATUS_EMPTY
            store.put(key, rec)
            added += 1
    print("导入完成：新增 %d，更新 %d，跳过（无录音id） %d" % (added, updated, skipped))
    store.save()
    return 0


# ================================================================ 质检

def run_qc_batch(cfg, store, engine, records, mock=False, stop_event=None, progress_cb=None):
    """并发跑质检（线程池）。"""
    concurrency = int(cfg.get("run.qc_concurrency", 10))
    print_every = int(cfg.get("run.print_every_n", 10))
    total = len(records)
    print("开始质检 %d 条，并发 %d..." % (total, concurrency))
    counter = Counter()
    from concurrent.futures import ThreadPoolExecutor, as_completed
    start = time.time()
    ok_n = fail_n = 0
    stopped = False
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        # 分批提交，保证「中断」能及时生效
        batch_size = max(concurrency * 4, 40)
        for begin in range(0, total, batch_size):
            if stop_event is not None and stop_event.is_set():
                stopped = True
                break
            chunk = records[begin: begin + batch_size]
            futures = {ex.submit(engine.run, rec): rec for rec in chunk}
            for fut in as_completed(futures):
                rec = futures[fut]
                try:
                    ok, msg = fut.result()
                except Exception as e:
                    ok, msg = False, "%s" % e
                n = counter.inc()
                if ok:
                    ok_n += 1
                else:
                    fail_n += 1
                key = store.upsert(rec)
                if key:
                    for g in qc_done_groups(engine, rec):
                        store.mark(key, "qc", g, "done")
                name = rec.get("客户姓名") or rec.get("录音id")
                if progress_cb:
                    progress_cb(n, total)
                if n % print_every == 0 or n == total or not ok:
                    flag = "✅" if ok else "❌"
                    print("  [%d/%d] %s %s %s" % (n, total, flag, name, "" if ok else msg))
    store.save()
    el = time.time() - start
    if stopped:
        print("[中断] 已停止，本次成功 %d，失败 %d，剩余 %d 条下次自动继续"
              % (ok_n, fail_n, total - counter.value()))
    else:
        print("质检完成：成功 %d，失败 %d，耗时 %.1f 秒" % (ok_n, fail_n, el))
    return fail_n


def cmd_qc(cfg, store, engine, stop_event=None, progress_cb=None):
    records = [r for r in store.all_records() if engine.needs_work(r)]
    if cfg.get("run.limit"):
        records = records[: int(cfg.get("run.limit"))]
    if not records:
        print("✅ 没有需要质检的记录")
        return 0
    return 1 if run_qc_batch(cfg, store, engine, records,
                             stop_event=stop_event, progress_cb=progress_cb) else 0


# 判定为「不合格」的常见取值（按自己规则里的取值补充即可）
BAD_RESULTS = ("不合格", "违规", "不合规", "不通过", "否", "未通过")


def recheck(store, mode):
    """重置质检结果，便于重新判定。mode: failed / all"""
    n = 0
    for rec in store.all_records():
        hit = False
        if mode == "all":
            hit = True
        else:
            for col in contract.QC_COLUMNS:
                if contract.text(rec.get(col)) in BAD_RESULTS:
                    hit = True
                    break
        if hit:
            for col in contract.QC_COLUMNS:
                rec[col] = ""
            store.upsert(rec)
            n += 1
    store.save()
    print("已重置 %d 条记录的质检结果（recheck=%s）" % (n, mode))


# ================================================================ 全流程

def build_tasks_from_source(cfg, store, source_file):
    """离线任务来源：从文件里取待处理的记录。"""
    rows = contract.read_table(source_file)
    tasks = []
    seen = set()
    for raw in rows:
        rec = contract.normalize_record(raw)
        key = contract.record_key(rec)
        if not key or key in seen:
            continue
        seen.add(key)
        old = store.get(key) or contract.blank_record()
        for col in contract.COLUMNS:
            if contract.is_blank(old.get(col)) and not contract.is_blank(rec.get(col)):
                old[col] = rec[col]
        tasks.append(old)
    return tasks


def build_tasks_from_scrm(cfg, store):
    import requests
    start_date = cfg.get("_start")
    end_date = cfg.get("_end")
    call_type = cfg.get("run.call_type", "all")
    status_filter = cfg.get("run.status_filter", "connected")
    sessions = requests.Session()
    print("读取登录状态: %s" % cfg.cookies_path)
    cookies = scraper.read_cookies(cfg.cookies_path)
    rows = scraper.fetch_transaction_list(sessions, cookies, start_date, end_date)
    if rows is None:
        print("[ERROR] 单据获取失败（可能是登录态过期），任务终止")
        return None
    cdr = scraper.fetch_cdr_records(sessions, cookies, start_date, end_date)
    if not cdr:
        print("[WARN] 该时间范围没有通话记录")
        return []
    matched = scraper.match_records(rows, cdr, call_type, status_filter)
    print("匹配到 %d 条通话（类型=%s，状态=%s）" % (len(matched), call_type, status_filter))
    tasks = []
    seen = set()
    for row, cdr_rec, phone, kind in matched:
        uid = contract.text(cdr_rec.get("uniqueid"))
        if not uid or uid in seen:
            continue
        seen.add(uid)
        rec = scraper.build_record(row, cdr_rec, kind)
        old = store.get(contract.record_key(rec))
        if old:
            for col in contract.COLUMNS:
                if contract.is_blank(rec.get(col)) and not contract.is_blank(old.get(col)):
                    rec[col] = old[col]
        if contract.is_blank(rec.get("录音下载地址")):
            rec["录音下载地址"] = scraper.voice_url() + "?uniqueid=" + uid
        tasks.append(rec)
    return tasks


def cmd_full(cfg, store, engine, args, stop_event=None, progress_cb=None):
    if args.source_file:
        src = args.source_file if os.path.isabs(args.source_file) else os.path.join(ROOT, args.source_file)
        print("任务来源：文件 %s" % src)
        tasks = build_tasks_from_source(cfg, store, src)
    else:
        tasks = build_tasks_from_scrm(cfg, store)
        if tasks is None:
            return 2

    need_asr = []
    skip_asr = []
    done_cnt = 0
    for rec in tasks:
        if contract.has_content(rec) and not args.force_asr:
            # 已有转写内容：只有当质检还没跑完时才需要处理
            if engine.needs_work(rec):
                skip_asr.append(rec)
            else:
                done_cnt += 1
        else:
            need_asr.append(rec)

    limit = cfg.get("run.limit")
    if limit:
        need_asr = need_asr[: int(limit)]
        skip_asr = skip_asr[: max(0, int(limit) - len(need_asr))]

    print("待处理：需转写 %d 条，仅需补质检 %d 条，已完成 %d 条"
          % (len(need_asr), len(skip_asr), done_cnt))
    if not need_asr and not skip_asr:
        print("✅ 没有需要处理的录音")
        return 0

    engine_llm = engine.llm
    download_workers = int(cfg.get("run.download_workers", 3))
    asr_batch = int(cfg.get("run.asr_batch", 4))
    qc_concurrency = int(cfg.get("run.qc_concurrency", 10))
    print_every = int(cfg.get("run.print_every_n", 10))
    retries = int(cfg.get("asr.download_retries", 2))

    asr_engine = asr_mod.ASREngine(cfg.data.get("asr", {}), mock=args.mock_asr)
    task_q = queue.Queue(maxsize=128)
    dl_q = queue.Queue(maxsize=max(4, download_workers * 2))
    asr_out_q = queue.Queue(maxsize=max(4, asr_batch * 2))
    write_q = queue.Queue(maxsize=256)

    session = None
    cookies = None
    if need_asr and not args.mock_asr:
        import requests
        session = requests.Session()
        cookies = scraper.read_cookies(cfg.cookies_path)

    progress = Counter()
    total = len(need_asr) + len(skip_asr)
    stats = {"asr_ok": 0, "asr_fail": 0, "qc_ok": 0, "qc_fail": 0}
    dl_fail_reasons = {}
    dl_warned = [0]
    stats_lock = threading.Lock()

    def downloader():
        while True:
            item = task_q.get()
            if item is None:
                task_q.task_done()
                return
            if stop_event is not None and stop_event.is_set():
                task_q.task_done()
                return
            rec = item
            # 注意：下载要用真实录音id，不能用内部去重主键（录音id#单据号）
            uid = contract.audio_id(rec)
            audio = None
            try:
                if args.mock_asr:
                    audio = b"MOCK" * 300
                elif not uid:
                    audio = None
                    reason = "记录里没有录音id"
                    with stats_lock:
                        dl_fail_reasons[reason] = dl_fail_reasons.get(reason, 0) + 1
                else:
                    audio, reason = scraper.download_audio(session, cookies, uid,
                                                           retries=retries)
                    if audio is None:
                        with stats_lock:
                            dl_fail_reasons[reason] = dl_fail_reasons.get(reason, 0) + 1
                            show = dl_warned[0] < 5
                            if show:
                                dl_warned[0] += 1
                        if show:
                            print("  [下载失败] 录音id=%s -> %s" % (uid, reason))
            except Exception as e:
                print("  [WARN] 下载异常 %s: %s" % (uid, e))
            dl_q.put((rec, audio))
            task_q.task_done()

    def flush_asr(batch):
        audios = [(contract.audio_id(rec), audio) for rec, audio in batch]
        results = asr_engine.transcribe_batch(audios)
        for rec, audio in batch:
            uid = contract.audio_id(rec)
            txt, status = results.get(uid, ("", "转写失败"))
            rec["语音内容"] = txt
            rec["转写状态"] = status
            rec["转写时间"] = now_str()
            rec["ASR模型版本"] = asr_mod.MODEL_TAG
            with stats_lock:
                if status == contract.ASR_STATUS_OK:
                    stats["asr_ok"] += 1
                else:
                    stats["asr_fail"] += 1
            asr_out_q.put(rec)

    def asr_worker():
        batch = []
        while True:
            item = dl_q.get()
            if item is None:
                if batch:
                    flush_asr(batch)
                break
            batch.append(item)
            if len(batch) >= asr_batch:
                flush_asr(batch)
                batch = []
        # 转写全部结束：立刻卸载 ASR 模型，把 1~2GB 内存还给系统（质检还要继续跑）
        if not args.mock_asr:
            asr_engine.release()

    def qc_worker():
        while True:
            rec = asr_out_q.get()
            if rec is None:
                asr_out_q.task_done()
                return
            if stop_event is not None and stop_event.is_set():
                # 中断：不再调用 API，直接落盘已有结果
                ok, msg = True, "已中断，未质检"
            else:
                try:
                    ok, msg = engine.run(rec)
                except Exception as e:
                    ok, msg = False, "%s" % e
                    traceback.print_exc()
            with stats_lock:
                if ok:
                    stats["qc_ok"] += 1
                else:
                    stats["qc_fail"] += 1
            write_q.put((rec, ok, msg))
            asr_out_q.task_done()

    def writer():
        while True:
            item = write_q.get()
            if item is None:
                write_q.task_done()
                return
            rec, ok, msg = item
            key = store.upsert(rec)
            if key:
                store.mark(key, "asr", rec.get("转写状态") or "")
                for g in qc_done_groups(engine, rec):
                    store.mark(key, "qc", g, "done")
            n = progress.inc()
            name = rec.get("客户姓名") or key
            if progress_cb:
                progress_cb(n, total)
            if n % print_every == 0 or n == total:
                print("  [%d/%d] %s | 转写=%s | 质检=%s | %s" % (
                    n, total, name, rec.get("转写状态"),
                    "✅" if ok else ("❌ " + msg), sysutil.memory_text()))
            write_q.task_done()

    threads = []
    for _ in range(max(1, download_workers)):
        t = threading.Thread(target=downloader, daemon=True)
        t.start()
        threads.append(("dl", t))
    t_asr = threading.Thread(target=asr_worker, daemon=True)
    t_asr.start()
    qc_threads = []
    for _ in range(max(1, qc_concurrency)):
        t = threading.Thread(target=qc_worker, daemon=True)
        t.start()
        qc_threads.append(t)
    t_writer = threading.Thread(target=writer, daemon=True)
    t_writer.start()

    start_ts = time.time()
    # 已有转写内容的记录直接进质检队列
    for rec in skip_asr:
        if contract.is_blank(rec.get("转写状态")):
            rec["转写状态"] = contract.ASR_STATUS_OK
        with stats_lock:
            stats["asr_ok"] += 1
        put_with_stop(asr_out_q, rec, stop_event)
    for rec in need_asr:
        if stop_event is not None and stop_event.is_set():
            print("[中断] 已停止投递新任务")
            break
        if not put_with_stop(task_q, rec, stop_event):
            print("[中断] 已停止投递新任务")
            break
    for _ in range(max(1, download_workers)):
        task_q.put(None)
    for _, t in threads:
        t.join()
    dl_q.put(None)
    t_asr.join()
    for _ in range(max(1, qc_concurrency)):
        asr_out_q.put(None)
    for t in qc_threads:
        t.join()
    write_q.put(None)
    t_writer.join()

    if progress.value() > 0:
        store.save()
    else:
        print("（本次没有需要写入的新结果）")
    el = time.time() - start_ts
    print("-" * 64)
    print("流水线完成：共 %d 条 | 转写成功 %d / 失败 %d | 质检成功 %d / 失败 %d"
          % (progress.value(), stats["asr_ok"], stats["asr_fail"], stats["qc_ok"], stats["qc_fail"]))
    if dl_fail_reasons:
        print("下载失败原因（共 %d 条）：" % sum(dl_fail_reasons.values()))
        for reason, n in sorted(dl_fail_reasons.items(), key=lambda x: -x[1])[:5]:
            print("   %5d 条 | %s" % (n, reason))
    print("耗时 %.1f 秒（%.1f 条/分钟）" % (el, (progress.value() / el * 60) if el > 0 else 0))
    print("当前 %s" % sysutil.memory_text())
    return 0


# ================================================================ 入口

def parse_args(argv=None):
    p = argparse.ArgumentParser(description="听音质检 一键流水线")
    p.add_argument("--mode", default="full", choices=["full", "qc", "import", "recheck"])
    p.add_argument("--start", default=None, help="起始日期 YYYY-MM-DD（默认今天）")
    p.add_argument("--end", default=None, help="结束日期 YYYY-MM-DD（默认今天）")
    p.add_argument("--call-type", dest="call_type", default=None, choices=["out", "in", "all"])
    p.add_argument("--status", default=None, choices=["all", "connected", "not_connected"])
    p.add_argument("--limit", type=int, default=None, help="最多处理多少条（测试用）")
    p.add_argument("--concurrency", type=int, default=None, help="质检并发数")
    p.add_argument("--workers", type=int, default=None, help="下载线程数")
    p.add_argument("--batch", type=int, default=None, help="ASR 批大小")
    p.add_argument("--rules", default=None, help="规则目录：rules/merged 或 rules/split")
    p.add_argument("--config", default=None, help="使用指定的配置文件（默认 config.json）")
    p.add_argument("--output", default=None, help="结果文件路径")
    p.add_argument("--overwrite", action="store_true", help="允许覆盖已有质检结果")
    p.add_argument("--inputs", nargs="*", default=None, help="import 模式：要导入的文件")
    p.add_argument("--source-file", dest="source_file", default=None,
                   help="full 模式：不从接口抓取，改从文件取任务（离线自测用）")
    p.add_argument("--recheck", default="failed", choices=["failed", "all"],
                   help="recheck 模式：重置哪些记录的质检结果")
    p.add_argument("--mock-llm", dest="mock_llm", action="store_true", help="干跑：不调用大模型")
    p.add_argument("--mock-asr", dest="mock_asr", action="store_true", help="干跑：不下载、不加载 ASR")
    p.add_argument("--force-asr", dest="force_asr", action="store_true",
                   help="即使已有语音内容也重新下载转写")
    return p.parse_args(argv)


def make_args(**kwargs):
    """构造与命令行等价的参数对象（供控制面板调用）。"""
    args = parse_args([])
    for k, v in kwargs.items():
        if not hasattr(args, k):
            raise TypeError("未知参数: %s" % k)
        setattr(args, k, v)
    return args


def build_cfg(args):
    """把命令行参数合并进 config.json。"""
    cfg = settings_mod.load(args.config) if getattr(args, "config", None) else settings_mod.load()
    cfg.data["_start"] = args.start or date.today().strftime("%Y-%m-%d")
    cfg.data["_end"] = args.end or cfg.data["_start"]
    apply_cli_overrides(cfg, args)
    return cfg


def run(args, stop_event=None, progress_cb=None):
    """执行一次任务（命令行与控制面板共用）。"""
    cfg = build_cfg(args)
    scraper.configure(cfg.get("api"))
    contract.configure(cfg.get("field_map"), cfg.get("field_aliases"))

    banner("听音质检 一键流水线")
    level = cfg.get("run.cpu_priority", "below_normal")
    if level and str(level).lower() != "normal" and sysutil.set_priority(level):
        print("已把本进程优先级调低为「%s」，尽量不影响你同时用电脑" % level)
    print("模式      : %s" % args.mode)
    if args.mode in ("full", "asr"):
        print("日期范围  : %s ~ %s" % (cfg.data["_start"], cfg.data["_end"]))
        print("通话类型  : %s" % cfg.get("run.call_type"))
        print("通话状态  : %s" % cfg.get("run.status_filter"))
    print("结果文件  : %s" % " ； ".join(output_display_paths(cfg)))
    print("模型      : %s / %s" % (cfg.get("llm.provider"), cfg.llm_config()["model"]))
    if args.mock_llm or args.mock_asr:
        print("干跑模式  : mock_llm=%s mock_asr=%s" % (args.mock_llm, args.mock_asr))
    print("")

    store = make_store(cfg)
    engine = build_engine(cfg, args.mock_llm, rules_override=args.rules)
    print("")

    code = 0
    if args.mode == "import":
        code = cmd_import(cfg, args, store, engine)
    elif args.mode == "qc":
        code = cmd_qc(cfg, store, engine, stop_event=stop_event, progress_cb=progress_cb)
    elif args.mode == "recheck":
        recheck(store, args.recheck)
        code = cmd_qc(cfg, store, engine, stop_event=stop_event, progress_cb=progress_cb)
    else:
        code = cmd_full(cfg, store, engine, args,
                        stop_event=stop_event, progress_cb=progress_cb)

    stats = engine.llm.stats
    print("")
    print("统计：API 调用 %d 次（失败 %d）| 输入 %s tokens | 输出 %s tokens"
          % (stats["calls"], stats["failed"],
             format(stats["prompt_tokens"], ","), format(stats["completion_tokens"], ",")))
    if store.migration_pending():
        print("检测到旧的合并文件，正在按通话类型拆分写入两个结果文件...")
        store.save()
    print("结果文件（共 %d 条）：" % len(store))
    records = store.all_records()
    for label, path in ((store.split_map.items() if store.split_map
                         else [(None, store.output_file)])):
        n = sum(1 for r in records if store.partition_of(r) == label)
        print("  %s（%d 条）" % (path, n))
    store.close()
    return code


def main(argv=None):
    args = parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
