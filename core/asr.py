# -*- coding: utf-8 -*-
"""语音转文字
===============
FunASR：Paraformer-large + VAD + 标点 + 说话人分离。
输出统一为「说话人编号：文本」（同说话人连续句合并，不带时间戳）。
模型懒加载，首次调用约需 1~3 分钟。
"""
import os
import tempfile
import threading

from . import sysutil

MODEL_TAG = "seaco_paraformer_large+vad+punc+campplus"


class ASREngine(object):
    def __init__(self, conf, mock=False):
        self.conf = conf or {}
        self.mock = mock
        self._model = None
        self._lock = threading.Lock()
        self._chunks_since_recycle = 0

    # ---------------------------------------------------------------- 模型
    def _device(self):
        want = str(self.conf.get("device", "auto")).lower()
        if want in ("cpu", "cuda"):
            return want
        try:
            import torch
            return "cuda" if torch.cuda.is_available() else "cpu"
        except Exception:
            return "cpu"

    def model(self):
        if self._model is None:
            with self._lock:
                if self._model is None:
                    os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
                    os.environ.setdefault("OMP_NUM_THREADS", "2")
                    os.environ.setdefault("MKL_NUM_THREADS", "2")
                    import torch
                    from funasr import AutoModel
                    m = self.conf.get("models", {})
                    device = self._device()
                    threads = int(self.conf.get("torch_threads", 2))
                    torch.set_num_threads(threads)
                    print("加载 ASR 模型（device=%s，线程 %d，首次约 1~3 分钟）... %s"
                          % (device, threads, sysutil.memory_text()))
                    self._model = AutoModel(
                        model=m.get("asr"),
                        model_revision=m.get("asr_rev"),
                        vad_model=m.get("vad"),
                        vad_model_revision=m.get("vad_rev"),
                        punc_model=m.get("punc"),
                        punc_model_revision=m.get("punc_rev"),
                        spk_model=m.get("spk"),
                        spk_model_revision=m.get("spk_rev"),
                        ngpu=1 if device == "cuda" else 0,
                        ncpu=int(self.conf.get("ncpu", 2)),
                        device=device,
                        disable_pbar=True,
                        disable_log=True,
                        disable_update=True,
                        batch_size_s=int(self.conf.get("batch_size_s", 60)),
                        use_fp16=(device == "cuda"),
                    )
                    print("ASR 模型就绪（%s）" % sysutil.memory_text())
        return self._model

    def release(self):
        """卸载 ASR 模型，把内存还给系统（转写阶段结束后调用）。"""
        if self._model is None:
            return
        with self._lock:
            self._model = None
        try:
            import torch
            del torch
        except Exception:
            pass
        sysutil.release_memory(trim_working_set=True)
        print("已卸载 ASR 模型，%s" % sysutil.memory_text())

    # ---------------------------------------------------------------- 转写
    @staticmethod
    def format_result(rec):
        """ASR 原始结果 → 「说话人：文本」多行文本。"""
        if not rec or not rec.get("text"):
            return ""
        infos = rec.get("sentence_info") or []
        if not infos:
            return rec.get("text", "")
        sentences = []
        for sent in infos:
            txt = sent.get("text", "")
            spk = sent.get("spk")
            if sentences and spk == sentences[-1]["spk"]:
                sentences[-1]["text"] += " " + txt
            else:
                sentences.append({"spk": spk, "text": txt})
        return "\n".join("%s：%s" % (s["spk"], s["text"]) for s in sentences)

    def transcribe_batch(self, audios):
        """audios: [(录音id, bytes 或 None), ...] → {录音id: (文本, 转写状态)}

        为控制内存峰值，默认一次只喂 1 个录音给模型（files_per_call 可调）；
        每处理完一批主动回收临时文件与张量。
        """
        out = {}
        if self.mock:
            for key, data in audios:
                if data:
                    out[key] = ("1：您好，请问有什么可以帮您？\n0：【干跑】客户诉求", "成功")
                else:
                    out[key] = ("", "下载失败")
            return out

        per_call = max(1, int(self.conf.get("files_per_call", 1)))
        gc_every = max(1, int(self.conf.get("gc_every_chunks", 10)))
        for start in range(0, len(audios), per_call):
            chunk = audios[start: start + per_call]
            out.update(self._transcribe_chunk(chunk))
            self._chunks_since_recycle += 1
            if self._chunks_since_recycle >= gc_every:
                # 每处理若干条回收一次：太频繁反而拖慢速度
                self._chunks_since_recycle = 0
                self._recycle()
        self._recycle()
        return out

    def _recycle(self):
        """把这一批用掉的临时内存还回去。"""
        try:
            import torch
            sysutil.release_memory(torch)
        except Exception:
            sysutil.release_memory()

    def _transcribe_chunk(self, chunk):
        """一次模型调用的最小单元。"""
        out = {}
        tmp_files = []
        valid = []
        try:
            for key, data in chunk:
                if data and len(data) > 1024:
                    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
                    tmp.write(data)
                    tmp.close()
                    tmp_files.append(tmp.name)
                    valid.append((key, tmp.name))
                else:
                    out[key] = ("", "下载失败" if data is not None else "无音频")
            del chunk
            if valid:
                paths = [p for _, p in valid]
                results = []
                try:
                    results = self.model().generate(
                        input=paths,
                        batch_size_s=int(self.conf.get("batch_size_s", 60)),
                        is_final=True,
                        sentence_timestamp=True,
                    )
                except Exception as e:
                    print("  [ERROR] 批量 ASR 失败(%d 条): %s" % (len(paths), e))
                    results = []
                for i, (key, _) in enumerate(valid):
                    rec = results[i] if i < len(results) else None
                    txt = self.format_result(rec)
                    out[key] = (txt, "成功" if txt else "转写失败")
                del results
        finally:
            for p in tmp_files:
                try:
                    os.remove(p)
                except OSError:
                    pass
            del tmp_files
            del valid
        return out
