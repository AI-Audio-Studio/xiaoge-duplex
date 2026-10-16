# 机器人端模型量化与本地部署指南

> 面向小歌机器人产品形态：设备端可以运行 7B 级模型，但算力、内存、功耗和散热有限。
> 本文解释量化是什么、什么时候需要量化、怎么选量化格式，以及如何把量化后的本地
> LLM 接入当前项目。

## 1. 先说结论

如果机器人端要本地跑 7B LLM，量化通常是必选项。

推荐优先级：

1. 不要自己从零量化，先使用官方或可信仓库发布的预量化模型。
2. CPU / 低功耗 GPU / 内存紧张：优先用 GGUF + llama.cpp，起点选 `Q4_K_M`。
3. NVIDIA GPU：优先用 AWQ 或 GPTQ Int4 + vLLM，起点选 AWQ。
4. 先量化 LLM。ASR/TTS 先服务化或换轻模型，最后再考虑量化。
5. 机器人对话要限制上下文长度，例如 4096 或 8192 tokens，不要默认开超长上下文。

对当前项目，最小适配方式是启动一个本地 OpenAI-compatible LLM 服务，然后改 `.env`：

```env
QWEN_BASE_URL=http://127.0.0.1:8000/v1
QWEN_API_KEY=EMPTY
QWEN_MODEL=local-qwen2.5-7b-q4
QWEN_VERIFY_SSL=false
```

当前代码的 `build_llm()` 已经使用 OpenAI 兼容接口，所以不需要改业务代码。

## 2. 量化是什么

大模型权重本质上是一堆数字。默认训练/推理常见格式有：

| 精度 | 含义 | 7B 权重粗略体积 | 特点 |
| --- | --- | ---: | --- |
| FP32 | 32 位浮点 | 约 28GB | 训练常见，端侧推理基本不用 |
| FP16/BF16 | 16 位浮点 | 约 14GB | GPU 推理常见，质量好但占内存 |
| INT8/Q8 | 8 位整数/量化 | 约 7-8GB | 质量稳定，压缩约一半 |
| INT4/Q4 | 4 位整数/量化 | 约 3.5-5GB | 端侧 7B 常用，质量略降 |
| Q2/Q3 | 2/3 位量化 | 约 2-3GB | 极限压缩，质量风险明显 |

量化就是把 FP16/BF16 这类高精度数字压成 INT8/INT4 等低精度表示。收益是：

- 模型文件更小；
- 显存/内存占用更低；
- 某些硬件上推理更快；
- 功耗更低，散热压力更小。

代价是：

- 回答质量可能下降；
- 小概率更容易胡说、漏细节、格式不稳定；
- 不同硬件和推理框架的速度差异很大；
- 某些量化格式只适合特定后端。

## 3. 不要只看模型权重

部署时内存/显存不只包含模型权重，还包括：

- KV cache：上下文越长，占用越高；
- 推理框架运行时；
- tokenizer 和请求队列；
- ASR/TTS/VAD/KWS 等其他模块；
- 操作系统和机器人业务进程。

一个 7B Q4 模型文件可能只有 4GB 左右，但实际运行预算不能只按 4GB 算。

机器人产品建议先按下面的顺序压资源：

1. 量化 LLM；
2. 限制上下文长度；
3. 限制并发，通常机器人端单会话即可；
4. 关闭不必要的 KWS、debug timeline、录音；
5. ASR/TTS 服务化或换轻模型；
6. 再评估 ASR/TTS 的 INT8/ONNX/TensorRT 优化。

## 4. 常见量化格式怎么选

| 格式 | 常用后端 | 适合硬件 | 推荐场景 |
| --- | --- | --- | --- |
| GGUF | llama.cpp / Ollama / LM Studio | CPU、Metal、CUDA、部分嵌入式 GPU | 端侧、易部署、可 CPU 跑 |
| AWQ | vLLM / TensorRT-LLM / Transformers | NVIDIA GPU | GPU 端侧或边缘服务器，高吞吐 |
| GPTQ | vLLM / Transformers / ExLlama | NVIDIA GPU | GPU 推理，模型来源丰富 |
| bitsandbytes 4bit | Transformers | NVIDIA GPU | Python 快速验证，不一定适合产品部署 |
| INT8 ONNX/TensorRT | ONNX Runtime / TensorRT | CPU/GPU/NPU 视支持而定 | ASR/TTS 或定制部署 |

本项目的机器人端建议：

- CPU 或不确定硬件：`Qwen/Qwen2.5-7B-Instruct-GGUF:Q4_K_M`
- NVIDIA GPU：`Qwen/Qwen2.5-7B-Instruct-AWQ` 或 `Qwen/Qwen2.5-7B-Instruct-GPTQ-Int4`
- 质量优先且内存够：Q8 或 FP16
- 低功耗优先：Q4，不建议一开始用 Q2/Q3

## 5. 推荐方案 A：GGUF + llama.cpp

这是最适合机器人端先落地的方案。优点是部署简单、CPU 可跑、对 OpenAI-compatible
接口支持好。

### 5.1 直接运行预量化模型

如果机器能联网，llama.cpp 支持直接从 Hugging Face 拉取 GGUF 量化模型。

Linux / WSL / macOS 示例：

```bash
git clone https://github.com/ggml-org/llama.cpp.git
cd llama.cpp
cmake -B build
cmake --build build --config Release -j

./build/bin/llama-server \
  -hf Qwen/Qwen2.5-7B-Instruct-GGUF:Q4_K_M \
  --host 127.0.0.1 \
  --port 8000 \
  --ctx-size 4096 \
  --alias local-qwen2.5-7b-q4
```

Windows PowerShell 示例：

```powershell
git clone https://github.com/ggml-org/llama.cpp.git
cd llama.cpp
cmake -B build
cmake --build build --config Release

.\build\bin\Release\llama-server.exe `
  -hf Qwen/Qwen2.5-7B-Instruct-GGUF:Q4_K_M `
  --host 127.0.0.1 `
  --port 8000 `
  --ctx-size 4096 `
  --alias local-qwen2.5-7b-q4
```

如果是 NVIDIA GPU，可以尝试 CUDA 构建：

```bash
cmake -B build -DGGML_CUDA=ON
cmake --build build --config Release -j
```

然后启动时加 GPU offload 层数。具体层数要按显存实测：

```bash
./build/bin/llama-server \
  -hf Qwen/Qwen2.5-7B-Instruct-GGUF:Q4_K_M \
  --host 127.0.0.1 \
  --port 8000 \
  --ctx-size 4096 \
  --n-gpu-layers 99 \
  --alias local-qwen2.5-7b-q4
```

### 5.2 接入当前项目

在仓库根目录 `.env` 写：

```env
QWEN_BASE_URL=http://127.0.0.1:8000/v1
QWEN_API_KEY=EMPTY
QWEN_MODEL=local-qwen2.5-7b-q4
QWEN_VERIFY_SSL=false

STT_BACKEND=funasr-stream
TTS_BACKEND=cosyvoice
XIAOGE_KWS_ENABLE_NATIVE=0
TURN_PREEMPTIVE_TTS=false
```

先单独测 LLM 服务：

```powershell
$body = @{
  model = "local-qwen2.5-7b-q4"
  messages = @(
    @{ role = "user"; content = "用一句话介绍你自己。" }
  )
  max_tokens = 64
  temperature = 0.2
} | ConvertTo-Json -Depth 5

Invoke-RestMethod `
  -Uri "http://127.0.0.1:8000/v1/chat/completions" `
  -Method Post `
  -ContentType "application/json" `
  -Body $body
```

再启动小歌：

```powershell
.\start.ps1
```

### 5.3 自己把 HF 模型转成 GGUF 并量化

如果没有现成量化文件，或者要固定自己的模型版本，可以自己转换。

```bash
git clone https://github.com/ggml-org/llama.cpp.git
cd llama.cpp
cmake -B build
cmake --build build --config Release -j

python -m pip install -U huggingface_hub transformers sentencepiece
huggingface-cli download Qwen/Qwen2.5-7B-Instruct \
  --local-dir ../models/llm/Qwen2.5-7B-Instruct

python convert_hf_to_gguf.py ../models/llm/Qwen2.5-7B-Instruct \
  --outfile ../models/llm/qwen2.5-7b-instruct-f16.gguf \
  --outtype f16

./build/bin/llama-quantize \
  ../models/llm/qwen2.5-7b-instruct-f16.gguf \
  ../models/llm/qwen2.5-7b-instruct-q4_k_m.gguf \
  Q4_K_M
```

Windows 下二进制通常在 `build\bin\Release\`：

```powershell
python .\convert_hf_to_gguf.py ..\models\llm\Qwen2.5-7B-Instruct `
  --outfile ..\models\llm\qwen2.5-7b-instruct-f16.gguf `
  --outtype f16

.\build\bin\Release\llama-quantize.exe `
  ..\models\llm\qwen2.5-7b-instruct-f16.gguf `
  ..\models\llm\qwen2.5-7b-instruct-q4_k_m.gguf `
  Q4_K_M
```

然后用本地文件启动：

```powershell
.\build\bin\Release\llama-server.exe `
  -m ..\models\llm\qwen2.5-7b-instruct-q4_k_m.gguf `
  --host 127.0.0.1 `
  --port 8000 `
  --ctx-size 4096 `
  --alias local-qwen2.5-7b-q4
```

## 6. 推荐方案 B：AWQ/GPTQ + vLLM

如果机器人有 NVIDIA GPU，vLLM 通常更适合产品服务化。它提供 OpenAI-compatible
API，当前项目可以直接接入。

### 6.1 AWQ 示例

```bash
python -m venv .venv-vllm
source .venv-vllm/bin/activate
pip install -U vllm

vllm serve Qwen/Qwen2.5-7B-Instruct-AWQ \
  --host 127.0.0.1 \
  --port 8000 \
  --max-model-len 4096 \
  --gpu-memory-utilization 0.85
```

项目 `.env`：

```env
QWEN_BASE_URL=http://127.0.0.1:8000/v1
QWEN_API_KEY=EMPTY
QWEN_MODEL=Qwen/Qwen2.5-7B-Instruct-AWQ
QWEN_VERIFY_SSL=false
```

### 6.2 GPTQ Int4 示例

```bash
vllm serve Qwen/Qwen2.5-7B-Instruct-GPTQ-Int4 \
  --host 127.0.0.1 \
  --port 8000 \
  --max-model-len 4096 \
  --gpu-memory-utilization 0.85
```

项目 `.env`：

```env
QWEN_BASE_URL=http://127.0.0.1:8000/v1
QWEN_API_KEY=EMPTY
QWEN_MODEL=Qwen/Qwen2.5-7B-Instruct-GPTQ-Int4
QWEN_VERIFY_SSL=false
```

### 6.3 AWQ 和 GPTQ 怎么选

经验起点：

- 优先试 AWQ：部署简单，vLLM 支持好，很多场景速度和质量都稳。
- AWQ 不稳定或模型只有 GPTQ：试 GPTQ Int4。
- 只做快速 Python 验证：bitsandbytes 4bit 可以用，但不建议作为第一版产品服务。

最终选择以真机指标为准：

- 首 token 延迟 TTFT；
- tokens/s；
- 显存峰值；
- 连续 30 分钟温度；
- 机器人端到端语音延迟；
- 业务问答准确率。

## 7. ASR/TTS 是否也要量化

短答案：不要一开始就做。

当前项目的本地化优先级应该是：

1. LLM 本地量化；
2. ASR 本地或局域网服务化；
3. TTS 局域网/云端服务化；
4. 最后再优化 ASR/TTS 的推理格式。

原因：

- ASR 错字会直接污染 LLM 上下文；
- TTS 量化可能影响音色、稳定性和首包；
- LLM 是最大内存/算力头，先解决它收益最大；
- ASR/TTS 可以独立做成服务，便于替换和灰度。

机器人标准版建议：

```env
QWEN_BASE_URL=http://127.0.0.1:8000/v1
QWEN_MODEL=local-qwen2.5-7b-q4

STT_BACKEND=funasr-stream
FUNASR_WS_URL=ws://127.0.0.1:10090

TTS_BACKEND=cosyvoice
XIAOGE_KWS_ENABLE_NATIVE=0
```

如果设备算力不够，则保持：

```env
STT_BACKEND=funasr-stream
FUNASR_WS_URL=wss://60.205.197.165:10090
TTS_BACKEND=cosyvoice
```

即 LLM 本地，ASR/TTS 远端或局域网。

## 8. 量化后的验收方法

不要只看模型能不能启动。机器人产品至少测这些：

| 指标 | 建议目标 | 说明 |
| --- | --- | --- |
| LLM TTFT | 越低越好，优先 < 800ms | 首 token 慢会直接拖语音首响 |
| LLM tokens/s | 满足短答即可 | 机器人回答应短，不追求长文吞吐 |
| 端到端延迟 | 用户停说到开始播报 | 比单模型指标更重要 |
| 内存/显存峰值 | 留 20%-30% 余量 | 防止长时间运行抖动 |
| 温度 | 连续运行不降频 | 机器人机身散热通常弱 |
| 回答质量 | 用真实业务问题集 | 量化后必须回归 |
| 中断体验 | 停止词、插话 | KWS 关闭后要确认兜底体验 |

### 8.1 快速冒烟测试

```powershell
.\start.ps1 -Text
```

输入：

```text
你是谁？用一句话回答。
```

再测试几类问题：

- 短问短答；
- 数字和时间；
- 多轮追问；
- 停止词打断；
- 机器人业务知识；
- 无意义输入或噪声转写。

### 8.2 记录建议

每次测试记录：

```text
模型: Qwen2.5-7B-Instruct-GGUF Q4_K_M
后端: llama.cpp
ctx: 4096
硬件: CPU/GPU/RAM/显存
ASR: 本地/远端
TTS: 本地/远端
KWS: 开/关
TTFT:
tokens/s:
端到端延迟:
峰值内存/显存:
温度:
主观问题:
```

## 9. 常见问题

### Q1：Q4 会不会明显变笨？

通常会有一点损失，但 7B Instruct 的 Q4_K_M 在机器人短对话里往往可以接受。
如果业务对知识准确率要求高，先试 Q5_K_M 或 Q8，再和 Q4 对比。

### Q2：为什么不能直接用 FP16？

7B FP16 权重约 14GB，还要加 KV cache 和运行时。很多机器人端设备会被内存、显存、
功耗或散热卡住。FP16 适合高配 GPU，不适合作为低功耗默认方案。

### Q3：上下文越长越好吗？

不是。上下文越长，KV cache 越大，首 token 和内存压力都会上升。机器人对话建议短历史、
摘要记忆和业务状态结合，不要把整段聊天无限塞进上下文。

### Q4：能不能把 Qwen3 开 thinking？

机器人实时语音场景通常不建议默认开启。thinking 会增加 token 和延迟。除非明确需要推理，
否则用 non-thinking 或短答策略。

### Q5：如果端侧只有 CPU，能跑 7B 吗？

可以尝试 GGUF Q4，但速度取决于 CPU、内存带宽和散热。能跑不等于体验好。语音机器人更看重
TTFT 和端到端延迟，CPU-only 需要真机实测。

### Q6：量化是不是训练？

大多数部署量化是 PTQ，也就是 post-training quantization，训练后量化，不需要重新训练模型。
QAT 是 quantization-aware training，成本高，通常不是产品第一步。

## 10. 当前项目推荐落地路线

第一阶段：LLM 本地化，ASR/TTS 仍远端。

```env
QWEN_BASE_URL=http://127.0.0.1:8000/v1
QWEN_API_KEY=EMPTY
QWEN_MODEL=local-qwen2.5-7b-q4
QWEN_VERIFY_SSL=false

STT_BACKEND=funasr-stream
FUNASR_WS_URL=wss://60.205.197.165:10090
TTS_BACKEND=cosyvoice
XIAOGE_KWS_ENABLE_NATIVE=0
TURN_PREEMPTIVE_TTS=false
```

第二阶段：ASR 本地化。

```env
STT_BACKEND=funasr-stream
FUNASR_WS_URL=ws://127.0.0.1:10090
```

第三阶段：TTS 本地或局域网化。

```env
TTS_BACKEND=http
HTTP_TTS_URL=http://127.0.0.1:8001
```

第四阶段：按实测决定是否恢复 KWS。

```env
XIAOGE_KWS_ENABLE_NATIVE=1
XIAOGE_KWS_NUM_THREADS=1
```

## 11. 参考资料

- Hugging Face Transformers Quantization overview: https://huggingface.co/docs/transformers/en/quantization/overview
- Hugging Face bitsandbytes docs: https://huggingface.co/docs/transformers/en/quantization/bitsandbytes
- llama.cpp: https://github.com/ggml-org/llama.cpp
- llama.cpp quantize tool: https://github.com/ggml-org/llama.cpp/blob/master/tools/quantize/README.md
- vLLM quantization docs: https://docs.vllm.ai/en/latest/features/quantization/
- Qwen GGUF docs: https://qwen.readthedocs.io/en/v1.5/quantization/gguf.html
- Qwen GPTQ docs: https://qwen.readthedocs.io/en/latest/quantization/gptq.html
- Qwen2.5-7B-Instruct-GGUF: https://huggingface.co/Qwen/Qwen2.5-7B-Instruct-GGUF
- Qwen2.5-7B-Instruct-AWQ: https://huggingface.co/Qwen/Qwen2.5-7B-Instruct-AWQ
- Qwen2.5-7B-Instruct-GPTQ-Int4: https://huggingface.co/Qwen/Qwen2.5-7B-Instruct-GPTQ-Int4
