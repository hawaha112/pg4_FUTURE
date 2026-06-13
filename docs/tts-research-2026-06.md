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

## ❌ Qwen3-TTS GHA 实测裁决(2026-06-11, bench run 27312454008)
- server 模式(模型只加载一次)+ INT8 + 4 线程, 1700 字: **40 分钟预算内只完成 ~2/3 块 → runner 真实 RTF≈10**
- 社区 "Zen3 RTF 2.02"(Ryzen 6800H 物理机)在 GHA 共享 vCPU(EPYC 7763, 无 AVX-512/VNNI)上不成立
- **结论: 免费 CI CPU 上不可行**。代码路径(tts_broadcast._synth_qwen3, 链式回退)保留,
  触发条件: 自托管 runner / GPU / 引擎未来出大幅加速。回退链实测两次完美兜底。
- E 级以上质量的现实路径: Gemini TTS 免费 key(待用户提供) 或 付费区(百炼 ¥8/月)。

## ☠️ HQ 两段式管线停用(2026-06-13 事故复盘)
- 6/12am/pm + 6/13am 三班音频被 GHA 上的 Qwen3 重制版覆盖, **实际产物是乱码**(用户实听
  11 分钟杂音)。疑因: int8 kernel 在共享 EPYC(无 VNNI)数值劣化 / AR 模型长文本复读退化。
- **根因教训: 管线只验证了"时长+流程", CI 产物从未经人耳验证就自动上线替换**。
  以后任何 TTS 引擎变更, 必须先把 CI 实际产物发 TG 人耳验收, 才允许接自动管线。
- 处置: 两 workflow 已 disable(pg4/hq-tg-edit + 部署仓/hq-audio), run_daily 停发 hq_job
  (注释保留); 三班 mp3 从 git 历史恢复; 6/12-pm 的 TG 消息用 restore 模式换回好版本。
- 架构(发布文本→公开仓重制→覆盖 mp3→editMessageMedia)本身已验证可用, 复活条件:
  换"人耳验证过的引擎"(Gemini key / 付费 TTS)即可原样启用。
- **现役: Kokoro zf_017(B), 全链路稳定。**
