# -*- coding: utf-8 -*-
"""结果存储
============
交付文件只有一个：output\\听音质检结果.xlsx
内部缓存（不交付）：_cache\\journal.jsonl（逐条追加，防丢） + _cache\\state.json（环节进度）
"""
import json
import os
import threading
import time
from datetime import datetime

from . import contract

# 结果列着色：不写死具体质检项，凡列名以「-结果」结尾的都按取值上色
RESULT_GOOD = {"合格", "合规", "不适用", "通过", "是", "正常"}
RESULT_BAD = {"不合格", "违规", "不合规", "不通过", "否", "异常"}


class ResultStore(object):
    """内存记录表 + 追加日志 + xlsx 落盘。"""

    def __init__(self, output_file, cache_dir, save_every_n=200,
                 save_interval_sec=300, lock_retry=3, save_retry_interval=60,
                 split_map=None, split_by="通话类型", split_default=None):
        self.output_file = output_file
        # 分文件输出：{"接电": 路径, "外呼": 路径}；为空则只写 output_file 一个文件
        self.split_map = dict(split_map or {})
        self.split_by = split_by
        self.split_default = split_default or (list(self.split_map)[0] if self.split_map else None)
        self.cache_dir = cache_dir
        self.journal_path = os.path.join(cache_dir, "journal.jsonl")
        self.state_path = os.path.join(cache_dir, "state.json")
        self.save_every_n = max(1, int(save_every_n))
        self.save_interval_sec = float(save_interval_sec)
        self.lock_retry = int(lock_retry)
        self.save_retry_interval = float(save_retry_interval)

        self.records = {}          # key -> 标准记录
        self.state = {}            # key -> {"asr": ..., "qc": {group: ...}}
        self._lock = threading.RLock()
        self._pending_since_save = 0
        self._last_save_ts = time.time()
        self._save_failed_at = 0.0
        self._legacy_loaded = False
        self._journal_fp = None
        self._seq = 0

    # ------------------------------------------------------------ 加载 / 保存
    def load(self):
        os.makedirs(self.cache_dir, exist_ok=True)
        sources = []
        if self.split_map:
            for label, path in self.split_map.items():
                if os.path.exists(path):
                    sources.append((path, label))
            if not sources and os.path.exists(self.output_file):
                # 首次启用分文件：从旧的合并文件里读一次，之后按类型拆开放
                sources.append((self.output_file, "（旧合并文件）"))
                self._legacy_loaded = True
                print("  检测到旧的合并结果文件，将按通话类型拆分")
        elif os.path.exists(self.output_file):
            sources.append((self.output_file, None))

        for path, label in sources:
            try:
                rows = contract.read_table(path)
                for raw in rows:
                    rec = contract.normalize_record(raw)
                    key = contract.record_key(rec)
                    if key:
                        self.records[key] = rec
                print("  已读取 %s%s: 累计 %d 条"
                      % (os.path.basename(path),
                         "" if label is None else "（%s）" % label,
                         len(self.records)))
            except Exception as e:
                print("  [WARN] 读取结果文件失败(%s)，将从缓存日志恢复" % e)
        if os.path.exists(self.state_path):
            try:
                with open(self.state_path, "r", encoding="utf-8") as f:
                    self.state = json.load(f)
            except Exception:
                self.state = {}
        self._replay_journal()
        return self

    def _replay_journal(self):
        """把日志里比 xlsx 更新的记录合并回来。

        合并规则：非空值覆盖，空值不动 —— 这样既不会丢 xlsx 里的数据，
        也不会因为日志里的旧主键（例如老版本写的 录音id）多出一行重复记录。
        """
        if not os.path.exists(self.journal_path):
            return
        by_uid = {}
        for key, rec in self.records.items():
            uid = contract.text(rec.get(contract.KEY_COLUMN))
            if uid:
                by_uid.setdefault(uid, []).append(key)

        count = 0
        unmatched = 0
        try:
            with open(self.journal_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        item = json.loads(line)
                    except Exception:
                        continue
                    raw = item.get("rec") or {}
                    incoming = contract.normalize_record(raw)
                    uid = contract.text(incoming.get(contract.KEY_COLUMN))
                    key = item.get("k") or contract.record_key(incoming)

                    target = key if key in self.records else None
                    if target is None and uid:
                        cands = by_uid.get(uid) or []
                        if len(cands) == 1:
                            target = cands[0]
                    if target is None:
                        if key:
                            self.records[key] = incoming
                            if uid:
                                by_uid.setdefault(uid, []).append(key)
                        else:
                            unmatched += 1
                        count += 1
                        continue
                    base = self.records[target]
                    for col in contract.COLUMNS:
                        val = incoming.get(col)
                        if not contract.is_blank(val):
                            base[col] = val
                    count += 1
        except Exception as e:
            print("  [WARN] 缓存日志读取失败: %s" % e)
            return
        if count:
            print("  已从缓存日志合并 %d 条" % count)
        if unmatched:
            print("  [WARN] 缓存日志中有 %d 条没有录音id，已忽略" % unmatched)

    # ------------------------------------------------------------ 写入
    def _open_journal(self):
        if self._journal_fp is None:
            os.makedirs(self.cache_dir, exist_ok=True)
            self._journal_fp = open(self.journal_path, "a", encoding="utf-8")
        return self._journal_fp

    def put(self, key, rec):
        """按指定主键写入（同时落追加日志），不做周期性落盘判断。"""
        if not key:
            return None
        with self._lock:
            self.records[key] = rec
            fp = self._open_journal()
            fp.write(json.dumps({"k": key, "rec": rec}, ensure_ascii=False) + "\n")
            fp.flush()
            self._pending_since_save += 1
        return key

    def upsert(self, rec, save=False):
        """写入/更新一条记录（按 录音id 作主键，同时落追加日志）。"""
        key = self.put(contract.record_key(rec), rec)
        if not key:
            return None
        with self._lock:
            now = time.time()
            # 上次保存失败（例如 Excel 占用文件）后先退避，避免每条记录都去撞一遍
            backing_off = (self._save_failed_at
                           and (now - self._save_failed_at) < self.save_retry_interval)
            need_save = (not backing_off
                         and (self._pending_since_save >= self.save_every_n
                              or (now - self._last_save_ts) >= self.save_interval_sec))
        if save or need_save:
            self.save()
        return key

    def get(self, key):
        return self.records.get(key)

    def all_records(self):
        return list(self.records.values())

    def __len__(self):
        return len(self.records)

    def mark(self, key, stage, status, group=None):
        """记录环节进度：stage in (asr, qc)；group 仅 qc 用。"""
        with self._lock:
            st = self.state.setdefault(key, {})
            if stage == "qc" and group:
                st.setdefault("qc", {})[group] = status
            else:
                st[stage] = status
            st["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def save_state(self):
        try:
            os.makedirs(self.cache_dir, exist_ok=True)
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False)
            os.replace(tmp, self.state_path)
        except Exception as e:
            print("  [WARN] 进度保存失败: %s" % e)

    # ------------------------------------------------------------ xlsx 落盘
    def partition_of(self, rec):
        """这条记录该进哪个文件（未启用分文件时返回 None）。"""
        if not self.split_map:
            return None
        val = contract.text(rec.get(self.split_by))
        if val in self.split_map:
            return val
        return self.split_default

    def output_paths(self):
        """当前会写出的所有结果文件。"""
        return list(self.split_map.values()) if self.split_map else [self.output_file]

    def migration_pending(self):
        """启用了分文件、且是从旧合并文件读进来的：需要落一次盘把两个文件建出来。"""
        if not self._legacy_loaded or not self.split_map:
            return False
        return not all(os.path.exists(p) for p in self.split_map.values())

    def save(self, verbose=True):
        with self._lock:
            rows = list(self.records.values())
            self._seq += 1
        if self.split_map:
            groups = []
            for label, path in self.split_map.items():
                part = [r for r in rows if self.partition_of(r) == label]
                groups.append((label, path, part))
        else:
            groups = [(None, self.output_file, rows)]

        ok = True
        failed = []
        for label, path, part in groups:
            if not self._write_xlsx(part, path, label, verbose):
                ok = False
                failed.append(os.path.basename(path))

        if not ok:
            with self._lock:
                self._save_failed_at = time.time()
            print("  [ERROR] 结果文件写入失败：%s" % "、".join(failed))
            print("          请关闭 Excel 中打开的这些文件；"
                  "本次结果已保存在 %s，重新运行会自动写入，不会丢数据"
                  % os.path.relpath(self.cache_dir, os.path.dirname(self.output_file) or "."))
            return False

        with self._lock:
            self._pending_since_save = 0
            self._last_save_ts = time.time()
            self._save_failed_at = 0.0
        self._archive_legacy_file()
        self._compact_journal()
        self.save_state()
        if verbose:
            detail = "，".join("%s %d 条" % (l or "全部", len(p)) for l, _, p in groups)
            print("  💾 已保存（%s）" % detail)
        return True

    def _write_xlsx(self, rows, path, label=None, verbose=True):
        """把一批记录写成一个 xlsx（流式写入 + 临时文件 + 原子替换）。

        用 openpyxl 的 write_only 模式：一行一行写盘，不再把整张表的所有单元格
        都堆在内存里 —— 上万行时内存占用能降一个数量级。
        """
        from openpyxl import Workbook
        from openpyxl.cell import WriteOnlyCell
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter
        try:
            columns = contract.COLUMNS
            wb = Workbook(write_only=True)
            ws = wb.create_sheet(title=(label or "听音质检")[:31])

            # 写模式下落盘顺序要求：列宽 / 冻结窗格必须在写入数据之前设置
            # 列宽也不写死具体质检项：长文本列宽一点，详情/问题分类列适中
            widths = {"语音内容": 60, "备注": 20, "录音下载地址": 30}
            for col in columns:
                if col.endswith("-详情"):
                    widths[col] = 45
                elif col.endswith("-问题分类"):
                    widths[col] = 28
                elif col.endswith("-结果"):
                    widths[col] = 16
            for i, col in enumerate(columns, 1):
                ws.column_dimensions[get_column_letter(i)].width = widths.get(col, 14)
            ws.freeze_panes = "A2"

            head_fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
            head_font = Font(bold=True, color="FFFFFF", size=11)
            head_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
            header = []
            for col in columns:
                c = WriteOnlyCell(ws, value=col)
                c.fill = head_fill
                c.font = head_font
                c.alignment = head_align
                header.append(c)
            ws.append(header)
            del header

            # 质检列起点：以主键/转写组之后的第一个质检列为准，避免写死业务列名
            qc_start = len(contract.BUSINESS_COLUMNS) + len(contract.CALL_COLUMNS) \
                + len(contract.ASR_COLUMNS)
            fill_qc = PatternFill(start_color="E8F0FE", end_color="E8F0FE", fill_type="solid")
            fill_bad = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
            fill_ok = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")
            align = Alignment(wrap_text=True, vertical="top")

            result_idx = {i for i, col in enumerate(columns) if col.endswith("-结果")}
            status_idx = columns.index("转写状态")
            ncols = len(columns)

            for rec in rows:
                sv = rec.get("转写状态", "")
                status_bad = bool(sv) and sv != contract.ASR_STATUS_OK
                row = []
                for i in range(ncols):
                    c = WriteOnlyCell(ws, value=rec.get(columns[i], ""))
                    c.alignment = align
                    if i == status_idx and status_bad:
                        c.fill = fill_bad
                    elif i in result_idx:
                        v = contract.text(rec.get(columns[i]))
                        if v in RESULT_GOOD:
                            c.fill = fill_ok
                        elif v in RESULT_BAD:
                            c.fill = fill_bad
                        else:
                            c.fill = fill_qc
                    elif i >= qc_start:
                        c.fill = fill_qc
                    row.append(c)
                ws.append(row)
                del row

            tmp = path + ".tmp"
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            wb.save(tmp)
            wb.close()
            last_err = None
            for attempt in range(self.lock_retry):
                try:
                    os.replace(tmp, path)
                    last_err = None
                    break
                except PermissionError as e:
                    last_err = e
                    if attempt < self.lock_retry - 1:
                        if verbose:
                            print("  [WARN] %s 被占用（Excel 打开中？），2 秒后重试 %d/%d"
                                  % (os.path.basename(path), attempt + 1, self.lock_retry))
                        time.sleep(2)
            if last_err is not None:
                return False
            if verbose:
                print("  💾 已保存 %d 条 -> %s" % (len(rows), path))
            return True
        except Exception as e:
            print("  [ERROR] 保存 %s 失败: %s" % (os.path.basename(path), e))
            return False

    def _archive_legacy_file(self):
        """启用分文件后，把旧的合并文件改名归档，避免它悄悄过期。"""
        if not self.split_map or not os.path.exists(self.output_file):
            return
        if self.output_file in self.split_map.values():
            return
        base, ext = os.path.splitext(self.output_file)
        target = base + "_旧合并文件" + ext
        try:
            os.replace(self.output_file, target)
            print("  旧合并文件已归档为: %s" % os.path.basename(target))
        except Exception:
            print("  [提示] 旧的合并文件 %s 已停用，可自行删除或改名"
                  % os.path.basename(self.output_file))

    def _compact_journal(self):
        """xlsx 已经是最新时，追加日志可以清空，避免无限增长。"""
        try:
            if self._journal_fp:
                self._journal_fp.close()
                self._journal_fp = None
            if os.path.exists(self.journal_path):
                os.remove(self.journal_path)
        except Exception:
            pass

    def close(self):
        try:
            if self._journal_fp:
                self._journal_fp.close()
                self._journal_fp = None
        except Exception:
            pass
