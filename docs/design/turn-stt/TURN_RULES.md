# 判停规则速查（最终生效规则 · 以代码为准）

> 2026-07-04 依当前 main 代码整理。回答"判停到底按什么规则、为什么每次等待时长不一样"。
> 相关模块:`providers/stt/funasr_stream.py`(GAP 聚合)、`turn_config.py`(TurnConfig)、
> 框架 `audio_recognition.py`/`endpointing.py`(EOU/endpointing)。设计背景见
> [TURN_STT_DESIGN.md](TURN_STT_DESIGN.md);架构上下文见 [../../guide/ARCHITECTURE.md](../../guide/ARCHITECTURE.md) §5/§10。

## 0. 先分清两条栈（规则完全不同）

生效栈由 env 决定:**`STT_BACKEND` 显式设置优先**;未设时 `XIAOGE_STACK=optimized` → `funasr-stream`,否则 `funasr`(upstream)。

| 栈 | 主 STT | FINAL 何时产生 |
| --- | --- | --- |
| `funasr`(upstream) | 离线 FunASR + StreamAdapter | session VAD 静默满 `TURN_VAD_MIN_SILENCE` 秒 → 切段 → 整段送识别 → FINAL |
| `funasr-stream` | FunASR 2pass 流式(内置独立 VAD) | 本文 §1 的 GAP 聚合规则 |

## 1. funasr-stream 栈:两层叠加规则

判停 = **第 1 层(流式 STT 内 GAP 聚合,决定 FINAL 何时定稿)** 叠加
**第 2 层(框架 EOU+endpointing,决定 FINAL 后何时提交轮次生成回复)**。
两层的计时都锚定"**最后一个有声帧**"(由各自 VAD 判定),已流逝的静默会被抵扣。

### 第 1 层:GAP 聚合(`providers/stt/funasr_stream.py`)

以内置独立 silero VAD(逐帧概率 ≥ `XIAOGE_STREAM_VAD_ACTIVATION`,默认 0.5,算"有声")的
最后有声时刻起算静默,每 50ms 检查:

```
静默 ≥ GAP_MAX(XIAOGE_AGG_GAP,默认 1.5s)                    → 必定稿 FINAL
GAP_MIN(XIAOGE_AGG_GAP_MIN,默认 0.8s) ≤ 静默 < GAP_MAX
    且累计文本"像说完了"(_looks_complete)                     → 提前定稿 FINAL
否则                                                          → 继续攒(中途短停顿只累加,不定稿)
```

`_looks_complete` 启发式(保守:不确定一律"没说完"):

| 句尾特征 | 判定 |
| --- | --- |
| 终止标点 `。！？!?…` | 说完 → 0.8s 档 |
| 句中标点 `，,、；;：:` | 没说完 → 等满 1.5s |
| 悬尾连接词(而且/然后/因为/所以/但是/就是/还有/或者/的/和/跟/在/也/还/这个/那个/嗯/呃/那/这 等) | 没说完 → 等满 1.5s |
| 句末语气词 `了吧吗呢啊嘛呀哈` | 说完 → 0.8s 档 |
| **无明显信号** | **没说完 → 等满 1.5s** |

另:静音/底噪期 ASR 蹦出的文本被 VAD 门控丢弃(防幽灵);`GAP_MIN ≥ GAP_MAX` 时自适应关闭,退化为恒定 GAP。

### 第 2 层:EOU + endpointing(框架,`TurnConfig` 旋钮)

FINAL 后,判停模型(MultilingualModel)对**累计转写文本**打"说完概率":

```
概率 ≥ TURN_UNLIKELY_THRESHOLD → 总等待 = TURN_ENDPOINT_MIN_DELAY(默认 0.3s)
概率 <  TURN_UNLIKELY_THRESHOLD → 总等待 = TURN_ENDPOINT_MAX_DELAY(默认 0.6s)
```

等待同样锚定最后有声帧——第 1 层已消耗的静默(≥0.8s)会抵扣,因此:
MIN_DELAY 档通常**立即提交**;MAX_DELAY 档若大于第 1 层已耗静默,还要**再补差额**。
等待期间用户再开口 → 轮次保持打开,转写继续累加,重新计时。

### 合成效果(三档自适应 + 抖动)

| 情形 | 停顿→提交轮次(理论) |
| --- | --- |
| 句子"像说完" + EOU 概率 ≥ 阈值 | ≈ GAP_MIN |
| 句子"像说完" + EOU 概率 < 阈值 | ≈ max(GAP_MIN, MAX_DELAY) |
| 无收尾信号 | ≈ GAP_MAX |

再叠加:FunASR final 网络 RTT、TTS 首包(`TURN_PREEMPTIVE_TTS` 开启时与判停窗重叠)。
**"每次等待不一样"是设计行为**(内容自适应:短句快、悬句稳),不是失稳;
"随机感"来自两个判断器(_looks_complete 启发式、EOU 模型)对边缘句子的档位差异。

> 实测参照(2026-07-04 手测 36 轮,.env: GAP 0.8/1.5、ENDPOINT 0.3/1.2、阈值 0.5):
> `end_of_turn_delay` 分布 1.06~1.78s,恰为三档带 + 网络抖动。

## 2. upstream 栈:单层规则(对照)

1. session VAD(silero,`TURN_VAD_MIN_SILENCE`,默认 0.35s)静默满 → 切段 → 离线识别整段 → FINAL(结构性延迟:必须等说完整段)。
2. 第 2 层 EOU+endpointing 同 §1(此栈下这是唯一的"判停"层)。

## 3. 想要"稳定优先"怎么调(全 env,零代码)

| 目标 | 配置 | 代价 |
| --- | --- | --- |
| **恒定判停**(每次固定等 GAP_MAX) | `XIAOGE_AGG_GAP_MIN` 设为 ≥ `XIAOGE_AGG_GAP`;且 `TURN_ENDPOINT_MIN_DELAY` = `TURN_ENDPOINT_MAX_DELAY` | 短句也等满,整体变慢 |
| 收窄浮动带 | 调近 GAP_MIN 与 GAP_MAX(如 1.2/1.5) | 折中 |
| 更快(接受碎句风险) | 调低 GAP_MAX / MAX_DELAY;开 `TURN_PREEMPTIVE_TTS=true` 重叠 TTS 首包 | 句中长停顿易被切碎 |
| 更耐心(长句不碎) | 调高 GAP_MAX / MAX_DELAY / `TURN_UNLIKELY_THRESHOLD` | 每轮更迟钝 |

调参验证:`AGENT_SCENARIO` 回放同一录音扫参,对比 `runs/<ts>/turn_kpis.json` 的
`over_segmentation`(碎轮率)与 `felt_latency`(体感延迟)——见 REGRESSION_LOG.md 方法。

## 4. 易混淆点

- `TURN_VAD_MIN_SILENCE` 在 **funasr-stream 栈不决定 FINAL 时机**(那是 GAP 的事);
  它作用于 session VAD——用户说话状态、打断闸门(min_duration/backchannel)、FELT 埋点。
- 两条栈各自有独立 VAD 实例,互不共享状态。
- 打断(KWS/在线 2pass/停止词)是与判停**平行**的通路,不改变上述提交规则,只会掐断播放。
