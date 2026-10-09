# -*- coding: utf-8 -*-
"""标准数据契约
================
两个任务合并后的唯一表结构：
  业务组 + 通话组 + 转写组 + 质检组
唯一主键：录音id（话务系统给的录音唯一编号）
"""
import csv
import os

# ---------------------------------------------------------------- 列定义

# A 组：业务信息（来自单据接口）
BUSINESS_COLUMNS = [
    "单据编号",
    "客户姓名",
    "客户手机号",
    "客户编号",
    "单据创建时间",
    "业务分类L1",
    "业务分类L2",
    "业务分类L3",
    "备注",
    "创建人",
    "创建人部门",
]

# B 组：通话信息（话单）
CALL_COLUMNS = [
    "通话时间",
    "主叫号码",
    "被叫号码",
    "通话类型",
    "通话状态",
    "通话时长(秒)",
    "接通时长(秒)",
    "录音下载地址",
]

# C 组：转写信息（ASR）
ASR_COLUMNS = [
    "录音id",
    "语音内容",
    "转写状态",
    "转写时间",
    "ASR模型版本",
]

# D 组：质检结果（大模型）
# 这里只是「示例规则」对应的输出列；换成自己的规则时，按 rules/ 里各规则的 outputs 同步调整。
QC_COLUMNS = [
    "AI摘要",
    "示例质检-结果",
    "示例质检-问题分类",
    "示例质检-详情",
    "质检方案",
]

COLUMNS = BUSINESS_COLUMNS + CALL_COLUMNS + ASR_COLUMNS + QC_COLUMNS

KEY_COLUMN = "录音id"

# ---------------------------------------------------------------- 字段映射
# config.json 的 field_map 会覆盖这里：{section: {语义键: 接口字段名}}
FIELD_MAP = {"order": {}, "cdr": {}}


def configure(field_map=None, aliases=None):
    """读入字段映射与列名别名（run_pipeline 启动时调用）。"""
    for section in ("order", "cdr"):
        val = (field_map or {}).get(section)
        if isinstance(val, dict):
            FIELD_MAP[section] = {k: v for k, v in val.items() if v}
    # aliases 传的是 {"历史表列名": "标准列名"}，这里转成 {标准列名: [历史列名]}
    for old, new in (aliases or {}).items():
        if str(old).startswith("_") or not isinstance(new, str) or not new:
            continue
        ALIASES.setdefault(new, [])
        if old not in ALIASES[new]:
            ALIASES[new].insert(0, old)
    return FIELD_MAP

# ---------------------------------------------------------------- 取值域

ASR_STATUS_OK = "成功"
ASR_STATUS_EMPTY = "无内容"
ASR_STATUS_DOWNLOAD_FAIL = "下载失败"
ASR_STATUS_ASR_FAIL = "转写失败"
ASR_STATUS_NO_ID = "无录音id"

# 通话类型取值（按需改；config.json 的 qc.call_type_profile 用它来选质检方案）
CALL_TYPE_OUTBOUND = "外呼"
CALL_TYPE_INBOUND = "接电"

# 质检的判定标准全部写在 rules/ 的规则文件里，代码里不放任何业务规则。

# ---------------------------------------------------------------- 列名别名
# 导入历史表格时用：config.json 的 field_aliases 里填 {"老列名": "标准列名"}
ALIASES = {}

# 旧表里存在、但不再进入标准契约的列（导入历史文件时自动丢弃）
DROPPED_COLUMNS = {
    "关联系统单据编码", "单据类型", "涉及部门",
    "月", "中继号码", "客服渠道", "一级渠道", "二级渠道", "三级渠道",
    "音频情感", "文字情感", "综合情感",
}


# ---------------------------------------------------------------- 基础工具

def blank_record():
    """一条全空的标准记录。"""
    return {c: "" for c in COLUMNS}


def text(value):
    """转成干净字符串（None / nan / 空白 统一成空串）。"""
    if value is None:
        return ""
    s = str(value).strip()
    if s.lower() in ("nan", "none", "nat"):
        return ""
    return s


def normalize_record(raw):
    """任意来源的一行 → 标准记录（缺失列补空，多余列丢弃）。"""
    rec = blank_record()
    lower_index = {str(k).strip(): v for k, v in (raw or {}).items() if k is not None}
    for col in COLUMNS:
        if col in lower_index:
            rec[col] = text(lower_index[col])
            continue
        for alias in ALIASES.get(col, []):
            if alias in lower_index:
                rec[col] = text(lower_index[alias])
                break
    return rec


def is_blank(value):
    return text(value) == ""


def record_key(rec):
    """标准记录的主键 = 录音id（同一录音对应多个单据时，拼上单据编号保证行不丢失）。

    ⚠️ 只用于内部去重/断点，**不能拿去调接口**（下载录音要用录音id）。
    """
    base = text(rec.get(KEY_COLUMN))
    if not base:
        return ""
    scrm = text(rec.get("单据编号"))
    return "%s#%s" % (base, scrm) if scrm else base


def audio_id(rec):
    """调用录音接口要用的真实 id（录音唯一编号）。"""
    return text(rec.get(KEY_COLUMN))


def has_content(rec, min_len=10):
    return len(text(rec.get("语音内容"))) >= min_len


# ---------------------------------------------------------------- 旧文件读取

def read_table(path):
    """读取 xlsx / csv 为 [{表头: 值}, ...]。"""
    ext = os.path.splitext(path)[1].lower()
    if ext in (".xlsx", ".xlsm"):
        import openpyxl
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        ws = wb.active
        rows = ws.iter_rows(values_only=True)
        try:
            header = [text(c) for c in next(rows)]
        except StopIteration:
            wb.close()
            return []
        out = []
        for row in rows:
            if row is None or all(c is None or text(c) == "" for c in row):
                continue
            out.append({header[i]: row[i] for i in range(min(len(header), len(row)))})
        wb.close()
        return out
    if ext in (".csv", ".txt"):
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f))
    raise ValueError("不支持的文件类型: %s" % path)


def load_records_from_files(paths, quiet=False):
    """把一批旧文件读成标准记录列表（用于导入历史数据）。"""
    out = []
    for p in paths:
        rows = read_table(p)
        for raw in rows:
            rec = normalize_record(raw)
            if record_key(rec) or has_content(rec):
                out.append(rec)
        if not quiet:
            print("  导入 %-40s %d 行" % (os.path.basename(p), len(rows)))
    return out
