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

---

# 第二轮调研补充(2026-06-11 下午): 免费云 API + Qwen3-TTS 实测

## Qwen3-TTS-0.6B 纯 C 引擎(候选 E, 样本已发用户)
- gabriele-mastrapasqua/qwen3-tts (MIT) + Qwen3-TTS-12Hz-0.6B (Apache-2.0), `make blas` + `--int8`
- **本机实测**(Intel Mac 4 线程): RTF 3.01, 54.7s 音频耗时 164.8s。社区同代 Zen3(Ryzen 6800H) RTF 2.02
- → GHA(EPYC 7763, Zen3 无 VNNI)估每班多 10-18 分钟, timeout 40 内勉强可行
- ⚠️ 务必 ≥v0.9.0(之前 x86 `--int8` 有静默 bug); VM 上先测 -j1 vs -j4
- 中文音色: vivian/serena/dylan/uncle_fu; 支持克隆(.qvoice)

## Gemini TTS(免费云端, 质量上限最高但中文口碑两极)
- 模型 `gemini-3.1-flash-tts-preview`: 免费层输入输出全免(ai.google.dev/gemini-api/docs/pricing), **AI Studio key 不绑卡**
- 接入: REST generateContent + responseModalities:["AUDIO"] → base64 PCM(s16le/24k/mono) → ffmpeg; 1700 字建议切 2-3 段同音色再拼(官方提示长文音质漂移); 新闻女声首选 voiceName=Kore(备选 Despina)
- ⚠️ 免费层数据用于 Google 训练(早报内容公开, 可接受); preview 政策可变 → 必须保留 kokoro 回退
- 中文口碑两极: 53AI 评测"几乎可直接作成品" vs 用户实测"换 speaker 不生效, 不如豆包" → 必须真稿耳测

## 近乎免费的付费区(需国内实名, 用户暂不要账号)
- 阿里百炼 qwen3-tts-flash: ¥0.8/万字 ≈ ¥8/月, 10 元新人额度白嫖 5 周, 中文第一梯队
- 火山豆包 TTS 2.0: ¥30-50/月, 中文公认顶级(Gemini 差评拿它当标杆)
- 魔搭 API-Inference: 每日 2000 次免费, TTS 是否在列未确认(要登录控制台查)

## 第二轮排除(查实勿复查)
ElevenLabs/Cartesia/PlayHT(免费层仅需求 3-20% 且禁商用) · Azure F0(要绑卡+音色同 edge) ·
讯飞免费档(传统参数合成) · OmniVoice 社区 ONNX(顶级桌面 CPU int8 RTF 2.79 → GHA 估 8+, 死) ·
dots.tts(小红书 2026-06-03, Apache, 中文强, **GPU-only → 新观察名单**, 等量化)
