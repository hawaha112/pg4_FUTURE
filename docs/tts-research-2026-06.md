# 口播 TTS 选型记录(2026-06-11 定稿)

> 防止重复调研。结论:**Kokoro-82M v1.1-zh · 音色 zf_017**(用户盲听选定)。
> 配置在 [morning-briefing.yml](../.github/workflows/morning-briefing.yml) 的
> `BROADCAST_ENGINE=kokoro` / `BROADCAST_VOICE=zf_017`;引擎实现在
> [tts_broadcast.py](../ai-morning-news/tts_broadcast.py)(失败自动回退 edge-tts)。

## 试听淘汰史(用户裁决)
1. Yunyang(edge 新闻男声)→ "换甜美女声"
2. Xiaoxiao(edge 最佳女声)→ "不太行,要更自然"
3. **A/B = Kokoro zf_001/zf_017 → 选 B** ✅;C = Edge Ava 说中文 → 没选
4. D = ZipVoice-Distill(克隆 zf_017 音色, sherpa-onnx int8, 本机 RTF 0.81)→ "D 不太行"

## 硬约束(再选型时不变)
GitHub Actions 4 vCPU 无 GPU(RTF≤2.5)· 免费免账号、权重公开直链 · 中文为主+中英夹杂 · 许可允许产品使用 · 项目仍维护。

## 调研结论(2026-06-11, 4 路联网核实)
- **被 CPU 筛掉**(宣传 RTF 全是 GPU 数字):CosyVoice2/3(CPU 实测 ~3.2)、IndexTTS2(RTX3060 都 RTF 8-13)、F5-TTS(CPU 一句话 7 分钟)、VibeVoice/VoxCPM/OmniVoice(无 CPU 证据)。
- **被许可筛掉**(权重非商用):Fish Speech/OpenAudio(CC-BY-NC-SA)、F5-TTS、Spark-TTS(Apache→降级 NC)、ChatTTS(NC+限学术)。
- **质量不升级**:MeloTTS/Piper/Matcha(CPU 快但中文不如 Kokoro)。
- **ZipVoice-Distill**(k2-fsa, Apache-2.0, zh CER 1.34≈CosyVoice2 档):约束内纸面最优、已实测可跑(int8 104MB+vocos 51MB, RTF 0.81),但**用户盲听否决**(2026-06-11)。残留集成代码无;复活路径:sherpa-onnx `OfflineTtsZipvoiceModelConfig` + 参考音频克隆(test: /tmp 流程见 git history)。
- **Kokoro 上游已停更**(2025-08),中英夹杂/多音字/儿化是已知无解项;我们用 espeak G2P 兜英文、按句分块绕了大半。

## 观察名单(值得再看的触发条件)
- **OmniVoice-0.8B**(k2-fsa 2026-03, zh CER 0.84 逼近 CosyVoice3, Apache-2.0):等它进 sherpa-onnx / 出 ONNX(上游 issue #151)。k2-fsa 出品大概率会支持——**落地后应重测一轮**。
- **Qwen3-TTS-0.6B + 纯C引擎**(gabriele-mastrapasqua/qwen3-tts, MIT):LLM 级自然度,EPYC INT8 RTF 1.64,GHA 上估 2-3 卡线;若用户对 Kokoro 不满意且 OmniVoice 未落地,花一次实测预算赌它。
