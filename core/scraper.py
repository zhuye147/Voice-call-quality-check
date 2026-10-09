# -*- coding: utf-8 -*-
"""数据采集
============
单据列表接口 + 话单接口 + 录音下载，输出标准契约记录。

所有接口地址、专有请求头、签名方式、字段名都从 config.json 读取，
代码里不写死任何具体系统地址或字段。

配置项（config.json → api）：
    scrm_list_url / ivs_cdr_url / voice_url   三个接口地址
    sign                                      {key, header_signature, header_timestamp, message_template}
    scrm_request_fields                       单据接口要返回的字段名列表
    scrm_module_id                            单据查询的业务模块 ID
    scrm_extra_headers / scrm_extra_body      单据接口的专有请求头 / 请求体附加项
    ivs_extra_headers / ivs_extra_cookies     话单接口的专有请求头 / cookie
    ivs_extra_params                          话单接口的专有表单参数
字段映射见 config.json → field_map（语义键 → 接口字段名）。
"""
import ast
import hashlib
import hmac
import json
import math
import re
import time
from datetime import datetime
from urllib.parse import urlsplit

from . import contract

# 话单接口返回的字段名（Asterisk/FreePBX 标准 CDR 字段，通常无需修改）
CDR_FIELDS = {
    "uniqueid": "uniqueid",
    "calldate": "calldate",
    "src": "src",
    "dst": "dst",
    "disposition": "disposition",
    "duration": "duration",
    "billsec": "billsec",
    "recordingfile": "recordingfile",
}

_api = {
    "scrm_list_url": "",
    "ivs_cdr_url": "",
    "voice_url": "",
    "sign": {},
    "scrm_request_fields": [],
    "scrm_module_id": "",
    "scrm_extra_headers": {},
    "scrm_extra_body": {},
    "ivs_extra_headers": {},
    "ivs_extra_cookies": {},
    "ivs_extra_params": {},
}


def configure(conf):
    """从配置里读入接口地址、签名方式与专有参数（run_pipeline 启动时调用）。"""
    for key in list(_api):
        val = (conf or {}).get(key)
        if val:
            _api[key] = val
    return dict(_api)


def _endpoint(key):
    val = _api.get(key)
    if not val:
        raise RuntimeError("config.json 的 api.%s 还没配置（接口地址）" % key)
    return str(val)


def voice_url():
    """录音下载接口地址（用于拼「录音下载地址」列）。"""
    return str(_api.get("voice_url") or "")


def _origin(url):
    """从接口地址推导 Origin（协议+主机）。"""
    parts = urlsplit(str(url or ""))
    if parts.scheme and parts.netloc:
        return "%s://%s" % (parts.scheme, parts.netloc)
    return ""


def _path_of(url):
    """从接口地址取出路径（签名要用）。"""
    parts = urlsplit(str(url or ""))
    return parts.path or "/"


def _cdr_field(record, key):
    """按配置取话单字段（配置没写就用标准字段名）。"""
    name = (contract.FIELD_MAP.get("cdr") or {}).get(key) or CDR_FIELDS.get(key, key)
    return contract.text((record or {}).get(name))


def _order_field(row, key):
    """按配置取单据字段（语义键 → 接口字段名）。"""
    name = (contract.FIELD_MAP.get("order") or {}).get(key)
    if not name:
        return ""
    return contract.text((row or {}).get(name))


# ---------------------------------------------------------------- cookies
def read_cookies(file_path):
    """兼容 {'cookies': {...}} 与 [{'name':..,'value':..}] 两种格式。"""
    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()
    data = ast.literal_eval(content)
    if isinstance(data, dict):
        return data.get("cookies", data)
    return {item["name"]: item["value"] for item in data}


# ---------------------------------------------------------------- 签名
def _sign_conf():
    return _api.get("sign") or {}


def _signature(path, ts):
    conf = _sign_conf()
    key = conf.get("key")
    if not key:
        raise RuntimeError("config.json 的 api.sign.key 还没配置（接口签名密钥）")
    template = conf.get("message_template") or "{path}:{ts}"
    message = template.format(path=path, ts=ts)
    return hmac.new(str(key).encode(), message.encode(), hashlib.sha256).hexdigest()


# ---------------------------------------------------------------- 单据列表接口
def _scrm_headers(path):
    ts = int(time.time() * 1000)
    url = _api.get("scrm_list_url") or ""
    origin = _origin(url)
    conf = _sign_conf()
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Content-Type": "application/json;charset=UTF-8",
        "Origin": origin,
        "Referer": origin + "/",
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36"),
        "sec-ch-ua": '"Not(A:Brand";v="8", "Chromium";v="144", "Google Chrome";v="144"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
    }
    # 业务系统专有的请求头全部由配置提供
    for k, v in (_api.get("scrm_extra_headers") or {}).items():
        headers[k] = "" if v is None else str(v)
    headers[conf.get("header_signature") or "X-Signature"] = _signature(path, ts)
    headers[conf.get("header_timestamp") or "X-Timestamp"] = str(ts)
    return headers


def fetch_transaction_list(session, cookies, start_date, end_date):
    """按日期分页查询单据列表；失败（含登录态过期）返回 None。"""
    url = _endpoint("scrm_list_url")
    path = _path_of(url)
    fields = list(_api.get("scrm_request_fields") or [])
    body = {
        "offSet": 0,
        "orderBys": [{"direction": "", "field": ""}],
        "pageIndex": 1,
        "pageSize": 100,
        "search": {
            "additionalProperties": {},
            "conditions": [
                {"left": "createTime", "op": "GE", "right": "%s 00:00:00" % start_date},
                {"left": "createTime", "op": "LE", "right": "%s 23:59:59" % end_date},
            ],
            "discrimination": "",
            "extend": {},
            "fields": fields,
            "moduleId": _api.get("scrm_module_id") or None,
            "sum": True,
        },
    }
    body.update(_api.get("scrm_extra_body") or {})

    r = session.post(url, cookies=cookies, headers=_scrm_headers(path), json=body, timeout=30)
    data = r.json()
    if data.get("data") is None:
        msg = json.dumps(data, ensure_ascii=False)[:300]
        print("[ERROR] 单据接口返回异常: %s" % msg)
        if "000006" in msg or "重新登录" in msg or "登录" in msg:
            print(">>> 登录态可能已过期，请先刷新后再运行")
        return None
    total = data["data"]["total"]
    pages = int(math.ceil(total / 100.0))
    rows = []
    for p in range(1, pages + 1):
        body["pageIndex"] = p
        r = session.post(url, cookies=cookies, headers=_scrm_headers(path), json=body, timeout=30)
        rows.extend(r.json()["data"]["rows"])
        if pages > 1:
            print("  单据分页 %d/%d" % (p, pages))
    print("单据共 %d 条" % len(rows))
    return rows


# ---------------------------------------------------------------- 话单接口
def _ivs_headers():
    url = _api.get("ivs_cdr_url") or ""
    origin = _origin(url)
    headers = {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin": origin,
        "Pragma": "no-cache",
        "Referer": origin + "/",
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"),
        "X-Requested-With": "XMLHttpRequest",
        "sec-ch-ua": '"Chromium";v="134", "Not:A-Brand";v="24", "Google Chrome";v="134"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
    }
    for k, v in (_api.get("ivs_extra_headers") or {}).items():
        headers[k] = "" if v is None else str(v)
    return headers


def _ivs_cookies(cookies):
    c = dict(cookies or {})
    for k, v in (_api.get("ivs_extra_cookies") or {}).items():
        c[k] = "" if v is None else str(v)
    return c


def fetch_cdr_records(session, cookies, start_date, end_date):
    """拉取话单（分页直到取完）。"""
    url = _endpoint("ivs_cdr_url")
    base = {
        "sEcho": "4", "iColumns": "8", "sColumns": ",,,,,,,",
        "iDisplayLength": "100",
        "mDataProp_0": "calldate", "bSortable_0": "true",
        "mDataProp_1": "src", "bSortable_1": "true",
        "mDataProp_2": "dst", "bSortable_2": "true",
        "mDataProp_3": "dcontext", "bSortable_3": "true",
        "mDataProp_4": "duration", "bSortable_4": "true",
        "mDataProp_5": "billsec", "bSortable_5": "true",
        "mDataProp_6": "disposition", "bSortable_6": "true",
        "mDataProp_7": "7", "bSortable_7": "true",
        "iSortCol_0": "0", "sSortDir_0": "desc", "iSortingCols": "1",
        "sDate": "%s 00:00:00" % start_date,
        "eDate": "%s 23:59:59" % end_date,
    }
    for k, v in (_api.get("ivs_extra_params") or {}).items():
        base[k] = "" if v is None else str(v)

    headers = _ivs_headers()
    ivs_cookies = _ivs_cookies(cookies)
    all_records = []
    start = 0
    while True:
        data = dict(base)
        data["iDisplayStart"] = str(start)
        try:
            r = session.post(url, cookies=ivs_cookies, headers=headers, data=data, timeout=30)
            j = r.json()
        except Exception as e:
            print("[WARN] 话单拉取失败(%s)，重试..." % e)
            time.sleep(3)
            continue
        records = j.get("data") or []
        all_records.extend(records)
        if len(records) < 100:
            break
        start += 100
        time.sleep(0.2)
    print("话单共 %d 条" % len(all_records))
    return all_records


# ---------------------------------------------------------------- 标识与匹配
def is_outbound(record):
    """外呼判断：主叫是 4 位分机，或录音文件名以 out- 开头（规则可按需调整）。"""
    src = _cdr_field(record, "src")
    if src.isdigit() and len(src) == 4:
        return True
    return _cdr_field(record, "recordingfile").lower().startswith("out-")


def is_inbound(record):
    """呼入判断：被叫是 4 位分机，或录音文件名以 q- 开头。"""
    dst = _cdr_field(record, "dst")
    if dst.isdigit() and len(dst) == 4:
        return True
    return _cdr_field(record, "recordingfile").lower().startswith("q-")


def status_ok(record, status_filter):
    """通话状态筛选：all / connected（接通）/ not_connected（未接）。"""
    if status_filter == "all":
        return True
    connected = _cdr_field(record, "disposition") == "接通"
    return connected if status_filter == "connected" else not connected


def extract_phone(recordingfile):
    """从录音文件名里提取完整号码（11 位，或 0+11 位）。"""
    if not recordingfile:
        return None
    for part in re.findall(r"\d+", str(recordingfile)):
        if len(part) == 11:
            return part
        if len(part) == 12 and part.startswith("0"):
            return part
    return None


def mask_key(masked_phone):
    """脱敏号码 → 前3+后4 匹配键。"""
    phone = contract.text(masked_phone)
    if not phone:
        return None
    if phone.startswith("0"):
        phone = phone[1:]
    m = re.match(r"^(\d{3}).*?(\d{4})$", phone)
    if not m:
        return None
    return m.group(1) + m.group(2)


def _parse_dt(s):
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(contract.text(s), fmt)
        except Exception:
            continue
    return None


def match_records(rows, cdr_records, call_type="all", status_filter="connected"):
    """按类型/状态筛选通话，并把单据信息「尽力匹配」上去。

    返回 [(单据行或 None, 话单记录, 号码, 通话类型), ...]
    """
    phone_rows = {}
    for row in rows:
        k = mask_key(_order_field(row, "customer_phone"))
        if k:
            phone_rows.setdefault(k, []).append(row)
    cid_row = {}
    for row in rows:
        cid = _order_field(row, "call_id")
        if cid:
            cid_row.setdefault(cid, row)

    def best_row(phone, calldate):
        k = mask_key(phone)
        if not k or k not in phone_rows:
            return None
        cands = phone_rows[k]
        if len(cands) == 1:
            return cands[0]
        t_cdr = _parse_dt(calldate)
        if t_cdr is None:
            return cands[0]

        def dist(r):
            t = _parse_dt(_order_field(r, "created_at"))
            return abs((t - t_cdr).total_seconds()) if t else float("inf")
        return min(cands, key=dist)

    matched = []
    seen_recfile = set()
    for rec in cdr_records:
        uid = _cdr_field(rec, "uniqueid")
        if not uid:
            continue
        recfile = _cdr_field(rec, "recordingfile")
        calldate = _cdr_field(rec, "calldate")

        if call_type in ("in", "all") and is_inbound(rec) and status_ok(rec, status_filter):
            if recfile:
                if recfile in seen_recfile:
                    continue
                seen_recfile.add(recfile)
            row = cid_row.get(uid)
            phone = extract_phone(recfile) or _cdr_field(rec, "src")
            if row is None:
                row = best_row(phone, calldate)
            matched.append((row, rec, phone, contract.CALL_TYPE_INBOUND))
        elif call_type in ("out", "all") and is_outbound(rec) and status_ok(rec, status_filter):
            phone = extract_phone(recfile) or _cdr_field(rec, "dst")
            row = best_row(phone, calldate)
            matched.append((row, rec, phone, contract.CALL_TYPE_OUTBOUND))
    return matched


# ---------------------------------------------------------------- 录音下载
def download_audio(session, cookies, uniqueid, retries=2):
    """下载录音。

    返回 (音频字节 或 None, 说明文本)；说明文本用于排查失败原因。
    """
    headers = _ivs_headers()
    ivs_cookies = _ivs_cookies(cookies)
    detail = ""
    for attempt in range(retries + 1):
        try:
            resp = session.get(_endpoint("voice_url"), params={"uniqueid": uniqueid},
                               cookies=ivs_cookies, headers=headers, timeout=120)
            if resp.status_code == 200 and len(resp.content) > 1024:
                return resp.content, "HTTP 200 / %d 字节" % len(resp.content)
            detail = "HTTP %s / %d 字节" % (resp.status_code, len(resp.content))
            if resp.status_code == 200:
                detail += "（内容过短，可能是错误页或空录音）"
        except Exception as e:
            detail = "请求异常: %s" % e
        if attempt < retries:
            time.sleep(2)
    return None, detail or "未知原因"


# ---------------------------------------------------------------- 建标准记录
def build_record(row, cdr_rec, call_type):
    """(单据行, 话单记录, 通话类型) → 标准契约记录（转写/质检列留空）。"""
    row = row or {}
    rec = contract.blank_record()
    uid = _cdr_field(cdr_rec, "uniqueid")
    rec.update({
        "单据编号": _order_field(row, "order_no"),
        "客户姓名": _order_field(row, "customer_name"),
        "客户手机号": _order_field(row, "customer_phone"),
        "客户编号": _order_field(row, "customer_code"),
        "单据创建时间": _order_field(row, "created_at"),
        "业务分类L1": _order_field(row, "topic1"),
        "业务分类L2": _order_field(row, "topic2"),
        "业务分类L3": _order_field(row, "topic3"),
        "备注": _order_field(row, "remark"),
        "创建人": _order_field(row, "creator"),
        "创建人部门": _order_field(row, "creator_dept"),
        "通话时间": _cdr_field(cdr_rec, "calldate"),
        "主叫号码": _cdr_field(cdr_rec, "src"),
        "被叫号码": _cdr_field(cdr_rec, "dst"),
        "通话类型": call_type,
        "通话状态": _cdr_field(cdr_rec, "disposition"),
        "通话时长(秒)": _cdr_field(cdr_rec, "duration"),
        "接通时长(秒)": _cdr_field(cdr_rec, "billsec"),
        "录音下载地址": ("%s?uniqueid=%s" % (voice_url(), uid)) if uid else "",
        "录音id": uid,
    })
    return rec
