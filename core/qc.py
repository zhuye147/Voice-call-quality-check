# -*- coding: utf-8 -*-
"""AI 质检引擎
================
规则声明式（rules/*.json）：输入列 → 提示词 → 输出列映射。
相同 group 的规则合并成**一次** API 调用（默认 2 次：content / compliance）。
"""
import json
import os

from . import contract


def load_rules(rules_dir):
    """读取规则目录下所有 *.json，按文件名排序。"""
    if not os.path.isdir(rules_dir):
        raise ValueError("规则目录不存在: %s" % rules_dir)
    rules = []
    for name in sorted(os.listdir(rules_dir)):
        if not name.endswith(".json"):
            continue
        with open(os.path.join(rules_dir, name), "r", encoding="utf-8") as f:
            payload = json.load(f)
        # 一个文件可以放一条规则（dict）或多条同组规则（list）
        items = payload if isinstance(payload, list) else [payload]
        for idx, rule in enumerate(items):
            rule["_file"] = name if len(items) == 1 else "%s#%d" % (name, idx + 1)
            rule.setdefault("group", os.path.splitext(name)[0])
            rule.setdefault("max_tokens", 2000)
            rule.setdefault("input_col", "语音内容")
            rule.setdefault("outputs", [])
            rules.append(rule)
    if not rules:
        raise ValueError("规则目录里没有 .json 规则: %s" % rules_dir)
    return rules


def group_rules(rules):
    """{group: [rule, ...]}，保持首次出现的顺序。"""
    groups = {}
    for rule in rules:
        groups.setdefault(rule["group"], []).append(rule)
    return groups


def build_system_prompt(group):
    """同一 group 的多条规则的提示词拼接。"""
    return "\n\n".join(r["system_prompt"].strip() for r in group)


def build_user_prompt(rule, content):
    tpl = rule.get("user_template") or "请分析以下客服通话转写文本：\n\n{content}"
    return tpl.replace("{content}", content)


def _get_path(obj, path):
    """支持 'a.b' 取嵌套字段。"""
    cur = obj
    for part in str(path).split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _set_path(obj, path, value):
    """按 'a.b' 写嵌套字段（干跑造占位数据用）。"""
    parts = str(path).split(".")
    cur = obj
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


# ---------------------------------------------------------------- 取值清洗

def _clean_enum(value, allowed, default=""):
    v = contract.text(value)
    if v in allowed:
        return v
    for a in allowed:
        if a and (a in v or v in a):
            return a
    return default


def format_items(items, sep="、"):
    """把「明细项」渲染成一行文本。

    既支持字符串数组（["项A", "项B"]），也支持对象数组
    （[{"code": "1", "name": "项A", "severity": "严重"}]），
    渲染成 "1-项A（严重）、2-项B" 这种形式。
    """
    if not items:
        return ""
    if not isinstance(items, (list, tuple)):
        items = [items]
    parts = []
    for item in items:
        if isinstance(item, dict):
            code = contract.text(item.get("code"))
            name = contract.text(item.get("name"))
            severity = contract.text(item.get("severity"))
            seg = "%s-%s" % (code, name) if code and name else (name or code)
            if seg and severity:
                seg += "（%s）" % severity
        else:
            seg = contract.text(item)
        if seg:
            parts.append(seg)
    return sep.join(parts)


def join_list(value, sep="、"):
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return sep.join(contract.text(v) for v in value if contract.text(v))
    return contract.text(value)


def _join_parts(*parts):
    return " | ".join(p for p in parts if p)


def _rule_applies(rule, data):
    """规则级开关：result 字段决定是否填写。"""
    cond = rule.get("when")
    if not cond:
        return True
    if isinstance(cond, dict):
        field = cond.get("field")
        values = cond.get("in") or []
        actual = _get_path(data, field)
        if actual in values:
            return True
        return contract.text(actual) in [contract.text(v) for v in values]
    return True


def apply_outputs(rule, data, rec):
    """把一条规则的解析结果写进标准记录的对应列。"""
    for out in rule.get("outputs", []):
        col = out["col"]
        if not _rule_applies(out, data):
            continue
        kind = out.get("type", "path")
        if kind == "const":
            value = contract.text(out.get("value"))
        elif kind == "list":
            value = join_list(_get_path(data, out["path"]))
        elif kind == "items":
            value = format_items(_get_path(data, out["path"]),
                                 sep=out.get("sep", "、"))
        elif kind == "bool_enum":
            raw = _get_path(data, out["path"])
            mapping = out.get("bool") or {}
            if raw is None:
                value = contract.text(out.get("default"))
            else:
                value = contract.text(mapping.get("true" if raw else "false",
                                               out.get("default")))
        elif kind == "detail":
            parts = []
            for seg in out.get("parts", []):
                raw = _get_path(data, seg["path"])
                txt = join_list(raw) if seg.get("join") else contract.text(raw)
                if txt:
                    parts.append("%s：%s" % (seg.get("label", ""), txt) if seg.get("label") else txt)
            value = _join_parts(*parts)
        else:
            raw = _get_path(data, out["path"])
            if out.get("enum"):
                value = _clean_enum(raw, out["enum"], default=contract.text(out.get("default")))
            elif isinstance(raw, (list, tuple)):
                value = join_list(raw)
            elif isinstance(raw, bool):
                value = "是" if raw else "否"
            else:
                value = contract.text(raw)
        if value:
            rec[col] = value
    return rec


def rule_done(rule, rec):
    """该规则是否已有结果（用于断点续跑）。"""
    cols = rule.get("done_cols")
    if not cols:
        cols = [o["col"] for o in rule.get("outputs", []) if o.get("primary")]
    if not cols:
        cols = [o["col"] for o in rule.get("outputs", [])][:1]
    return all(not contract.is_blank(rec.get(c)) for c in cols) if cols else False


class QCProfile(object):
    """一套质检方案：接电质检（全项）/ 外呼质检（仅转写+内容总结）。"""

    def __init__(self, name, rules, label=None, desc=""):
        self.name = name
        self.rules = rules
        self.groups = group_rules(rules)
        self.label = label or name
        self.desc = desc

    def __repr__(self):
        return "<QCProfile %s: %s>" % (self.name, "/".join(self.groups))


class QCEngine(object):
    """按通话类型给每条记录选用对应的质检方案。"""

    def __init__(self, profiles, llm, default_profile=None, selector=None,
                 min_content_len=10, truncate=4000, overwrite=False,
                 clear_out_of_scope=True):
        if not profiles:
            raise ValueError("至少要有一套质检方案")
        self.profiles = profiles
        self.default_name = default_profile if default_profile in profiles else list(profiles)[0]
        self.selector = selector
        self.llm = llm
        self.min_content_len = int(min_content_len)
        self.truncate = int(truncate)
        self.overwrite = bool(overwrite)
        self.clear_out_of_scope = bool(clear_out_of_scope)

    # ------------------------------------------------------------ 方案选择
    def profile_of(self, rec):
        name = None
        if self.selector:
            name = self.selector(rec)
        if not name or name not in self.profiles:
            name = self.default_name
        return self.profiles[name]

    def group_names(self):
        names = []
        for p in self.profiles.values():
            for g in p.groups:
                if g not in names:
                    names.append(g)
        return names

    def summary(self):
        return "；".join("%s → %s" % (p.label, "/".join(p.groups))
                         for p in self.profiles.values())

    # ------------------------------------------------------------ 待处理判断
    def pending_groups(self, rec):
        """返回这条记录（按其质检方案）还需要跑的 group 列表。"""
        if not contract.has_content(rec, self.min_content_len):
            return []
        out = []
        for name, rules in self.profile_of(rec).groups.items():
            if all(rule_done(r, rec) for r in rules):
                continue
            out.append(name)
        return out

    def needs_work(self, rec):
        """这条记录是否还需要（重新）质检：有内容，且缺结果或缺方案标记。"""
        if not contract.has_content(rec, self.min_content_len):
            return False
        return bool(self.pending_groups(rec)) or contract.is_blank(rec.get("质检方案"))

    # ------------------------------------------------------------ 执行
    def run(self, rec):
        """对一条记录按其方案跑所有待处理 group，返回 (是否成功, 备注)。"""
        profile = self.profile_of(rec)
        # 打上质检方案标记，便于在结果里区分接电/外呼口径
        rec["质检方案"] = profile.label
        self._clear_out_of_scope(rec, profile)
        if not contract.has_content(rec, self.min_content_len):
            return True, "内容为空，跳过"
        pending = self.pending_groups(rec)
        if not pending:
            return True, "已全部完成"
        errors = []
        for name in pending:
            ok, err = self.run_group(rec, name, profile)
            if not ok:
                errors.append("%s:%s" % (name, err))
        if errors:
            return False, "; ".join(errors)
        return True, "完成"

    def _clear_out_of_scope(self, rec, profile):
        """清掉不属于当前方案的质检列（例如外呼记录上残留的接电质检结论）。"""
        if not self.clear_out_of_scope:
            return
        scope = set()
        for rules in profile.groups.values():
            for rule in rules:
                for out in rule.get("outputs", []):
                    scope.add(out["col"])
        for col in contract.QC_COLUMNS:
            if col == "质检方案" or col in scope:
                continue
            if not contract.is_blank(rec.get(col)):
                rec[col] = ""

    def run_group(self, rec, group_name, profile=None):
        profile = profile or self.profile_of(rec)
        rules = profile.groups[group_name]
        # 不覆盖已有结果（overwrite=False 时只补空值）
        snapshot = {}
        if not self.overwrite:
            for rule in rules:
                for out in rule.get("outputs", []):
                    col = out["col"]
                    if col not in snapshot and not contract.is_blank(rec.get(col)):
                        snapshot[col] = rec.get(col)
        # 输入内容：取第一条 rule 的 input_col，为空时回退 fallback_col
        head = rules[0]
        content = contract.text(rec.get(head.get("input_col", "语音内容")))
        if not content:
            fb = head.get("input_fallback_col")
            if fb:
                content = contract.text(rec.get(fb))
        if len(content) < self.min_content_len:
            return True, "输入为空，跳过"
        system_prompt = build_system_prompt(rules)
        user_prompt = build_user_prompt(head, content[: self.truncate])
        max_tokens = max(int(r.get("max_tokens", 2000)) for r in rules)
        if getattr(self.llm, "mock", False):
            # 干跑模式：按规则自己声明的输出造一份占位结果，不调用大模型
            ok, data = True, self._mock_data(rules)
        else:
            ok, data = self.llm.chat_json(system_prompt, user_prompt, max_tokens=max_tokens)
        if not ok:
            return False, str(data.get("_error") if isinstance(data, dict) else data)
        for rule in rules:
            # 单条规则组：模型可能直接返回字段（没有外层包裹）
            sub = data
            wrap = rule.get("result_key")
            if wrap:
                inner = data.get(wrap) if isinstance(data, dict) else None
                sub = inner if isinstance(inner, dict) else {}
            apply_outputs(rule, sub, rec)
        for col, value in snapshot.items():
            rec[col] = value
        return True, "完成"

    @staticmethod
    def _mock_data(rules):
        """干跑用的占位数据：按每条规则的 outputs 生成，字段类型跟着声明走。"""
        data = {}
        for rule in rules:
            for out in rule.get("outputs", []):
                path = out.get("path")
                if not path:
                    continue
                kind = out.get("type", "path")
                if kind == "bool_enum":
                    value = False
                elif kind in ("items", "list"):
                    value = []
                elif kind == "const":
                    value = out.get("value", "")
                elif out.get("enum"):
                    value = out["enum"][0]
                else:
                    value = "【干跑】占位内容，用于验证流水线"
                _set_path(data, path, value)
        return data
