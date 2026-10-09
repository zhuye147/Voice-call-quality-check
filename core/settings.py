# -*- coding: utf-8 -*-
"""配置加载：读取 config.json，解析相对路径。"""
import json
import os

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT_DIR, "config.json")


def _resolve(path):
    """相对路径按项目根目录解析。"""
    if not path:
        return path
    if os.path.isabs(path):
        return path
    return os.path.normpath(os.path.join(ROOT_DIR, path))


class Settings(object):
    """配置对象，支持 settings["run"]["limit"] 与 settings.get("qc.rules_dir") 两种读法。"""

    def __init__(self, data, root=ROOT_DIR):
        self.data = data
        self.root = root

    def get(self, dotted, default=None):
        cur = self.data
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def __getitem__(self, key):
        return self.data[key]

    @property
    def cookies_path(self):
        return _resolve(self.get("paths.cookies"))

    @property
    def output_file(self):
        return _resolve(self.get("paths.output_file"))

    @property
    def cache_dir(self):
        return _resolve(self.get("paths.cache_dir"))

    @property
    def rules_dir(self):
        return _resolve(self.get("qc.rules_dir"))

    def resolve(self, path):
        """把配置里的相对路径解析成绝对路径。"""
        return _resolve(path)

    def llm_config(self):
        """返回当前 provider 的 {base_url, api_key, model}。"""
        provider = self.get("llm.provider", "deepseek")
        conf = dict(self.get("llm.%s" % provider) or {})
        if not conf:
            raise ValueError("config.json 中未找到 llm.%s 配置" % provider)
        conf["provider"] = provider
        conf["temperature"] = self.get("llm.temperature", 0.1)
        conf["max_retries"] = self.get("llm.max_retries", 3)
        conf["retry_delay"] = self.get("llm.retry_delay", 5)
        conf["timeout"] = self.get("llm.timeout", 180)
        return conf

    def ensure_dirs(self):
        for p in (os.path.dirname(self.output_file), self.cache_dir):
            if p and not os.path.isdir(p):
                os.makedirs(p, exist_ok=True)


def load(path=None):
    path = path or CONFIG_PATH
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return Settings(data, root=os.path.dirname(os.path.abspath(path)))
