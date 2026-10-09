# -*- coding: utf-8 -*-
"""大模型调用：OpenAI 兼容协议，强制 JSON 输出 + 宽松解析 + 重试。"""
import json
import re
import threading
import time


class LLMError(Exception):
    pass


def extract_json(text):
    """从模型返回文本里抠出 JSON 对象。"""
    if not text:
        return None
    s = text.strip()
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", s, re.DOTALL)
    if m:
        s = m.group(1)
    else:
        m = re.search(r"\{.*\}", s, re.DOTALL)
        if m:
            s = m.group(0)
    try:
        return json.loads(s)
    except Exception:
        pass
    # 兜底：修掉常见的尾逗号 / 中文引号
    try:
        fixed = re.sub(r",\s*([}\]])", r"\1", s).replace("“", '"').replace("”", '"')
        return json.loads(fixed)
    except Exception:
        return None


class LLMClient(object):
    """线程安全的 DeepSeek / 门神网关客户端。"""

    def __init__(self, conf, mock=False):
        self.base_url = str(conf["base_url"]).rstrip("/")
        self.api_key = conf["api_key"]
        self.model = conf["model"]
        self.temperature = conf.get("temperature", 0.1)
        self.max_retries = int(conf.get("max_retries", 3))
        self.retry_delay = float(conf.get("retry_delay", 5))
        self.timeout = float(conf.get("timeout", 180))
        self.mock = mock
        self._session = None
        self._lock = threading.Lock()
        self.stats = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "failed": 0}

    def _get_session(self):
        if self._session is None:
            import requests
            self._session = requests.Session()
        return self._session

    # ---------------------------------------------------------------- 统计
    def _add_usage(self, usage):
        with self._lock:
            self.stats["calls"] += 1
            self.stats["prompt_tokens"] += int(usage.get("prompt_tokens", 0) or 0)
            self.stats["completion_tokens"] += int(usage.get("completion_tokens", 0) or 0)

    def _add_failed(self):
        with self._lock:
            self.stats["failed"] += 1

    # ---------------------------------------------------------------- 调用
    def chat(self, system_prompt, user_prompt, max_tokens=2000, json_mode=True):
        """返回 (成功与否, 文本内容)。"""
        url = "%s/chat/completions" % self.base_url
        headers = {
            "Authorization": "Bearer %s" % self.api_key,
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": max_tokens,
            "temperature": self.temperature,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        last_err = None
        for attempt in range(self.max_retries):
            try:
                resp = self._get_session().post(url, headers=headers, json=payload,
                                                timeout=self.timeout)
                if resp.status_code == 200:
                    data = resp.json()
                    content = data["choices"][0]["message"]["content"]
                    self._add_usage(data.get("usage") or {})
                    return True, content
                if resp.status_code == 429:
                    wait = self.retry_delay * (attempt + 1) * 3
                    print("    ⚠️ 触发限流(429)，等待 %.0f 秒后重试..." % wait)
                    time.sleep(wait)
                    last_err = "429 限流"
                    continue
                if resp.status_code == 400 and json_mode:
                    # 个别模型不支持 response_format，降级重试一次
                    payload.pop("response_format", None)
                    json_mode = False
                    last_err = "400 参数不支持"
                    continue
                last_err = "HTTP %s: %s" % (resp.status_code, resp.text[:200])
                if attempt < self.max_retries - 1:
                    time.sleep(self.retry_delay)
            except Exception as e:
                last_err = "请求异常: %s" % e
                if attempt < self.max_retries - 1:
                    time.sleep(self.retry_delay)
        self._add_failed()
        return False, str(last_err)

    def chat_json(self, system_prompt, user_prompt, max_tokens=2000, retry_on_bad_json=True):
        """要求返回 JSON 对象；解析失败会自动追加一次纠正重试。"""
        if self.mock:
            # 干跑模式由质检引擎按规则定义自行造占位数据，这里不会真的调用
            return True, {}
        ok, content = self.chat(system_prompt, user_prompt, max_tokens=max_tokens)
        if not ok:
            return False, {"_error": content}
        parsed = extract_json(content)
        if parsed is not None:
            return True, parsed
        if retry_on_bad_json:
            fix_prompt = user_prompt + "\n\n（注意：上一次返回不是合法 JSON，请只输出 JSON 对象，不要任何解释文字。）"
            ok2, content2 = self.chat(system_prompt, fix_prompt, max_tokens=max_tokens)
            if ok2:
                parsed2 = extract_json(content2)
                if parsed2 is not None:
                    return True, parsed2
        self._add_failed()
        return False, {"_error": "返回内容无法解析为 JSON: %s" % str(content)[:200]}
