# 小歌 资源消耗实测报告

> 全部为**实测数据**(非估算)。测量机:Intel Core Ultra 7 155H(16 物理 / 22 逻辑核)、16GB 内存、Intel 核显;`onnxruntime` 仅 CPU(`CPUExecutionProvider`,无 CUDA)。
> 方法:`start.ps1` 启动 console 模式,用 `AGENT_SCENARIO` 注入录音回放驱动负载(免真麦);每 2 秒按进程 `TotalProcessorTime` 增量算 CPU、`WorkingSet64` 算 RAM,多次采样取均值。CPU 为**单核口径百分比**(100% = 1 个逻辑核;本机共 22 核)。远端 LLM/STT/TTS 网关测时可达,active 为真实对话负载。

## 1. 架构:重模型全在远端,本地只跑"轻"件 + KWS
| 组件 | 位置 | 说明 |
|---|---|---|
| LLM Qwen3-4B | **远端** | `60.205.197.165:10092`,本地不占算力 |
| STT FunASR 2pass | **远端** | `60.205.197.165:10090`(WS 流) |
| TTS CosyVoice | **远端** | DashScope 云 |
| KWS sherpa-onnx zipformer | **本地** | 每帧流式推理(常驻) |
| VAD silero | **本地** | 每帧,轻量 |
| turn-detector(判停) | **本地** | 按轮触发;模型常驻内存 |
| 音频 I/O / Web 面板 / WS 客户端 / 录音 | **本地** | |

**含义:本地设备无需 GPU/显存,也不承担大模型算力;本地开销 = 音频管线 + 本地小模型。**

本地模型磁盘体积:turn-detector **461MB**(最大,RAM 主体)、KWS 目录 ~39MB(实际用 int8 编码器 ~4.4MB)、silero VAD 2.3MB。

## 2. 总体资源消耗(实测)
进程结构:监督进程(~4MB,~0 CPU)+ 工作进程(载模型、跑管线,下表即其消耗)。

| 状态 | CPU(单核%) | ≈逻辑核 | 占 22 核 | RAM(WorkingSet) |
|---|---|---|---|---|
| ACTIVE 对话中(KWS 开) | 326%(峰 336) | 3.3 | ~15% | 400MB |
| IDLE 静音(KWS 开) | 321%(峰 324) | 3.2 | ~14% | 360MB |
| IDLE 静音(**KWS 关**) | **12.5%(峰 15.5)** | **0.13** | **~0.6%** | **290MB** |

> 注:IDLE 是静音 8 分钟、回放早结束后的稳态(`scripted_audio.py` 无循环,确为真静音)。

## 3. 分模块归因(实测差值)
| 模块 | CPU | RAM | 依据 |
|---|---|---|---|
| **KWS(sherpa-onnx,常驻每帧推理)** | **~3.1 核** | **~70MB** | IDLE 开/关之差(321%→12.5%;360→290MB) |
| 其余常驻(VAD + turn-detector 载入 + 音频 + WS + Web + Python) | ~0.13 核 | ~290MB(turn-detector 为主体) | KWS 关时的 IDLE |
| 对话本身(STT/LLM/TTS 的**本地侧** + turn-detector 推理 + TTS 播放) | 仅 +~5%(+~0.2 核) | +~40MB | ACTIVE − IDLE(均 KWS 开) |

**头条结论:本地 CPU ~96% 被 KWS 常驻推理占据;真正的"对话"在本地几乎不耗(因为算力在远端)。KWS 之所以吃满 ~3 核,是 sherpa-onnx zipformer 每帧跑编码器、且 onnxruntime 默认开多线程(intra-op = 核数)。**

## 4. 当前可运行的最低配置(基于实测)
- **CPU**:现状(KWS = fp32 + 默认线程)峰值约 **3.3 核**常驻。需 **≥4 物理核**(给 KWS ~3 核 + 系统余量)。**若按 §5 优化 KWS,可降到双核低端设备**。
- **内存**:工作集 ~**400MB**;**2GB 内存**设备即可(留 OS)。turn-detector 是内存大头。
- **GPU/显存**:**不需要**(本地全 CPU)。
- **网络**:必须能稳定连远端 LLM/STT/TTS(60.205.197.165 等);带宽需求小(音频帧 + 文本流),但**延迟/稳定性**直接决定体验。
- **磁盘**:本地模型 ~0.5GB + venv。

## 5. 降耗路径(保证效果,适配低配)— 按收益排序
> KWS 是唯一的 CPU 大头,优化它就够。以下措施需逐一**实测确认幅度**(本报告只确证了"KWS=3.1 核"这一事实,降幅是建议方向)。

1. **限制 onnxruntime 线程数**(KWS/VAD 的 session 设 `intra_op_num_threads=1~2`、`inter_op=1`):3 核大概率主要是线程过订阅造成,这是**最小改动、最大收益**的首选,预计 idle 从 3.2 核大幅下降。**(待实测)**
2. **VAD 门控 KWS**:silero VAD 极廉价;只在 VAD 判定有声时才跑 KWS 编码器。静音期(占大多数时间)KWS 开销→~0。**(待实测,体验无损)**
3. **KWS 用 int8 量化模型**(已有 `encoder...int8.onnx` 4.4MB):比 fp32(12MB)更快更省,精度基本无损。
4. **加大 KWS 处理 chunk / 降召回频率**:`XIAOGE_KWS_NUM_TRAILING_BLANKS` 等参数权衡响应速度与算力。
5. **不需要 KWS 强打断时直接关**(`XIAOGE_KWS_ENABLE_NATIVE=0`):idle 立降到 0.13 核 / 290MB——适配极低配设备的兜底。
6. **内存**:如需进一步压 RAM,turn-detector(461MB 模型)是大头;可评估更小判停模型或 VAD-only 判停(权衡判停质量)。

## 6. 复现方法
```
# 设设备规格
powershell -Command "Get-CimInstance Win32_Processor | Select Name,NumberOfCores,NumberOfLogicalProcessors"
# 启动(场景注入免麦)
$env:AGENT_SCENARIO="runs\<ts>\user.wav"; .\start.ps1 -Background
# 采样:对本仓 python 进程,每2s 取 TotalProcessorTime 增量/wall 求 CPU、WorkingSet64 求 RAM
# 消融:对比 默认 vs $env:XIAOGE_KWS_ENABLE_NATIVE="0" 两次 IDLE
```
