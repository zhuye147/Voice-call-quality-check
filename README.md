# 通话录音质检流水线

把客服通话录音的「抓取 → 语音转文字 → AI 质检 → Excel 结果」串成一条流水线，一条命令或一个面板跑完。

> 这是一个通用框架：**质检项和判定标准都在 `rules/` 里配置，代码里不含任何业务规则**。
> 仓库里带的是一份示例规则，把 `rules/example/qc_sample.json` 换成你自己的规则即可。

## 它能做什么

```
业务系统单据接口 ─┐
                  ├─ 匹配/去重 ─┐
话务系统话单接口 ─┘              ▼
                     下载录音 → FunASR 语音转文字 → AI 质检 → 结果 Excel
```

- **语音转文字**：FunASR（Paraformer-large + VAD + 标点 + 说话人分离），输出 `说话人代号：文本`
- **AI 质检**：按通话类型选不同质检方案，结果直接写回 Excel
- **断点续跑**：随时中断，重跑自动跳过已完成的，不重复下载、不重复转写、不重复消耗 token
- **分文件交付**：不同类型的通话分别输出到不同 Excel

## 目录结构

```
├─ run_pipeline.py          主控（抓取 / 转写 / 质检 / 导入 / 重判）
├─ 质检控制面板.py           图形界面（推荐入口）
├─ 启动质检面板.bat          双击打开面板
├─ 一键听音质检.bat          命令行一键入口
├─ config.example.json      配置模板（复制成 config.json 后填写）
├─ core/                    核心模块
│   ├─ contract.py          标准数据契约（列定义 / 归一化）
│   ├─ scraper.py           单据、话单、录音下载
│   ├─ asr.py               FunASR 语音转文字
│   ├─ llm.py               大模型调用（JSON 输出 + 重试）
│   ├─ qc.py                质检引擎（按通话类型选方案）
│   ├─ store.py             结果存储（流式写 Excel + 断点日志）
│   ├─ settings.py          配置加载
│   └─ sysutil.py           内存监控 / 进程优先级
└─ rules/                   质检规则（改规则只改这里）
    └─ example/qc_sample.json   示例规则，替换成你自己的
```

## 安装

```bash
pip install -r requirements.txt
```

`torch` / `funasr` 只在真的做语音转写时加载；只跑质检（`--mode qc`）或干跑自测时不需要。

## 配置

```bash
copy config.example.json config.json     # Windows
```

然后填写 `config.json`：

- `paths.cookies`：登录态文件路径
- `api.*`：三个接口地址、签名方式（`api.sign`）、各系统专有请求头/参数
- `field_map`：接口字段名 → 内部语义键的映射（换系统只改这里）
- `field_aliases`：导入历史表格时的列名映射（可选）
- `llm.deepseek.api_key`：大模型 API Key（也可改用 `llm.menshen`，改 `llm.provider` 即可）
- `qc.profiles` / `qc.call_type_profile`：质检方案与「哪种通话用哪套方案」

`config.json` 已加入 `.gitignore`，**不会**被提交。

接口部分的可配置项一览：

| 配置 | 说明 |
|------|------|
| `api.scrm_list_url` / `ivs_cdr_url` / `voice_url` | 单据列表、话单、录音下载三个接口地址 |
| `api.sign` | 签名密钥、签名头名（默认 `X-Signature`/`X-Timestamp`）、待签名内容模板（默认 `{path}:{ts}`） |
| `api.scrm_request_fields` | 单据接口需要返回的字段名列表 |
| `api.scrm_extra_headers` / `scrm_extra_body` | 单据接口的专有请求头 / 请求体附加项 |
| `api.ivs_extra_headers` / `ivs_extra_cookies` / `ivs_extra_params` | 话单接口的专有请求头 / Cookie / 表单参数 |

签名用的路径会从 `scrm_list_url` 自动推导，不需要单独配。

## 使用

**图形界面**：双击 `启动质检面板.bat`，选好日期范围、抓取类型、质检方案，点「开始运行」。运行中可随时「中断」，界面下方有进度条和实时日志（含内存占用）。

**命令行**：

```bash
# 抓取 + 转写 + 质检（默认今天 / 外呼+接电 / 接通）
python run_pipeline.py --mode full

# 指定日期范围
python run_pipeline.py --mode full --start 2026-09-01 --end 2026-09-30

# 只补跑尚未完成的质检（不抓取、不转写）
python run_pipeline.py --mode qc

# 导入历史转写结果（xlsx / csv 都行）
python run_pipeline.py --mode import --inputs "历史结果.xlsx"

# 清空「不合格」结论后重判
python run_pipeline.py --mode recheck --recheck failed

# 干跑自测：不联网、不加载模型、不花钱
python run_pipeline.py --mode full --limit 10 --mock-llm --mock-asr
```

常用参数：`--call-type out|in|all`、`--status connected|all|not_connected`、`--limit N`、`--concurrency N`、`--workers N`、`--batch N`、`--rules <方案名或规则目录>`、`--overwrite`、`--force-asr`。

## 质检规则怎么写

规则是一个 JSON 文件，放进 `rules/` 下的某个目录（一个目录 = 一套方案）：

```json
{
  "group": "sample",
  "name": "示例质检项",
  "input_col": "语音内容",
  "system_prompt": "给模型的提示词，写清判定标准……",
  "user_template": "请对以下通话转写文本做质检：\n\n{content}",
  "max_tokens": 1200,
  "outputs": [
    {"col": "AI摘要", "path": "summary", "primary": true},
    {"col": "示例质检-结果", "path": "violation", "type": "bool_enum",
     "bool": {"true": "不合格", "false": "合格"}, "primary": true}
  ]
}
```

要点：

- **`group` 相同的多条规则会合并成一次 API 调用**（例如把 3 个检查项放进同一个 group，就只花 1 次调用）。想更细的判定粒度，就让每个规则单独一个 group。
- `outputs` 里 `path` 支持 `a.b` 取嵌套字段；`type` 可选 `bool_enum`（布尔映射成文字）、`items`（明细数组拼成一行）、`detail`（多字段拼详情）、`list`、`const`。
- `primary: true` 的列用于判断这条规则是否已跑完（断点续跑的依据）。
- `when` 可以控制「只在某种结果下才填这一列」。

新增质检项后，记得在 `config.json` 的 `qc.profiles` 注册方案，并在 `contract.QC_COLUMNS` 里补上输出列名。

## 结果文件

默认按通话类型分文件（改 `config.json` 的 `paths` 可调）：

- `output/接电质检结果.xlsx`
- `output/外呼质检结果.xlsx`

列分四组：业务信息（单据编号 / 客户姓名 / 客户手机号 / 客户编号 / 单据创建时间 / 业务分类L1-L3 / 备注 / 创建人 / 创建人部门）、通话信息（通话时间 / 主叫号码 / 被叫号码 / 通话类型 / 通话状态 / 通话时长 / 接通时长 / 录音下载地址）、转写信息（录音id / 语音内容 / 转写状态 / 转写时间 / ASR模型版本）、质检结果（各规则的输出列 + `质检方案`）。

`质检方案` 列标明每条记录走的是哪套方案；`转写状态` 取值：成功 / 无内容 / 下载失败 / 转写失败 / 无音频。

## 断点续跑

两层保障：

1. `output/_cache/journal.jsonl`：每处理一条立即追加，断电或强杀也不丢
2. `output/_cache/state.json`：记录每条录音的转写与各质检环节状态

跳过判定以「结果列是否有值」为准，日志只做安全网。中断后重跑不会重复下载、不会重复转写、不会重复消耗 token。

## 内存与性能

语音转写是内存大户（模型本身 1~2GB，推理中间结果更大）。默认已做这些控制：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `asr.batch_size_s` | 60 | 一次推理的音频秒数，越小越省内存（不影响转写结果） |
| `asr.files_per_call` | 1 | 一次喂几个录音给模型 |
| `run.asr_batch` | 2 | 队列里攒几条再送模型 |
| `run.cpu_priority` | below_normal | 进程降优先级，跑的时候不耽误用电脑 |
| `run.download_workers` | 3 | 并发下载线程数 |

转写跑完会立即卸载模型，质检阶段不占 ASR 的内存；结果 Excel 用流式写入，上万行也不会把整张表堆在内存里。运行日志每 10 条打印一次内存占用。
