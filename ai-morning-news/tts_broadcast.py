#!/usr/bin/env python3
"""tts_broadcast.py — 把口播稿(output/broadcast.txt)合成 5-6 分钟带背景音乐的音频节目。

流程: edge-tts(免费, 微软 zh-CN-YunyangNeural 新闻男声) 合成人声
      → ffmpeg 与 assets/bgm_loop.mp3(CC0 公有领域氛围乐) 混音:
        2.3s 片头音乐独奏 → 人声进入时音乐压低(0.13) → 人声结束音乐回升 → 3s 淡出
      → loudnorm 到播客标准响度(-16 LUFS)
      → output/archive/audio/YYYY-MM-DD-{shift}.mp3 (部署块 cp -r archive/* 自动带上)

时长控制: 口播稿在 LLM 端已控 1650-1850 字(≈5.2-5.8 分钟 @317字/分);
          这里再按实际字数微调语速(±10% 内), 把落点收敛到 5-6 分钟。

任何失败(edge-tts 网络/ffmpeg 缺失/稿件缺失)都 exit 0 只打警告 —— 音频是增值件,
绝不允许它挡住早报出报。由 run_daily.sh 在渲染后、部署前调用。
"""
import asyncio
import os
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
# 引擎: edge(微软云声, 零依赖) / kokoro(开源本地模型, 更自然; 失败自动回退 edge)。
ENGINE = os.environ.get('BROADCAST_ENGINE', 'edge').lower()
# 声音: edge 用 zh-CN-XiaoxiaoNeural 等; kokoro 用 zf_001/zf_017 等(v1.1-zh 音色)。
VOICE = os.environ.get('BROADCAST_VOICE', 'zh-CN-XiaoxiaoNeural')
# kokoro 音色名误配到 edge 时的兜底声
EDGE_FALLBACK_VOICE = 'zh-CN-XiaoxiaoNeural'
# 实测语速: Xiaoxiao≈290 字/分钟(Yunyang 317 / Kokoro-zh@1.0≈300); 换声音要重校
CHARS_PER_MIN = float(os.environ.get('BROADCAST_CPM', '290'))
TARGET_SEC = float(os.environ.get('BROADCAST_TARGET_SEC', '330'))
MIN_CHARS = 200          # 稿子太短(生成失败的残片)不值得做节目
INTRO_SEC = 2.3          # 片头音乐独奏
OUTRO_SEC = 5.0          # 人声结束后音乐回升时长(含 3s 淡出)
BGM = SCRIPT_DIR / 'assets' / 'bgm_loop.mp3'


def log(msg):
    print(f"[tts_broadcast] {msg}", flush=True)


def _voice_rate_pct(n_chars: int) -> int:
    """按字数微调语速(±10% 封顶, 听感自然优先), 把时长收敛到 TARGET_SEC 附近。"""
    est = n_chars / CHARS_PER_MIN * 60.0
    ratio = est / TARGET_SEC - 1.0
    return max(-10, min(10, round(ratio * 100)))


async def _synth(text: str, rate: str, out_path: Path):
    import edge_tts
    comm = edge_tts.Communicate(text, VOICE, rate=rate)
    await comm.save(str(out_path))


# ── Qwen3-TTS 引擎(BROADCAST_ENGINE=qwen3): LLM 级自然度, 纯 C 推理, 免账号 ──
# 用户 2026-06-11 盲听: "E(Qwen3 vivian)确实质量更好" → 转正。
# 引擎: gabriele-mastrapasqua/qwen3-tts(MIT) + Qwen3-TTS-12Hz-0.6B(Apache-2.0)。
# 慢是已知代价(GHA 估 RTF 2-3, 6 分钟音频合成 12-18 分钟) → 预算看门狗 + 失败回退。
# 二进制/模型由 workflow 提供(每跑现编译 — 缓存 -march=native 产物跨机型会 SIGILL;
# 模型 1.8GB 走 actions/cache)。本地测试放 models/ 同路径。
QWEN3_BIN = Path(os.environ.get('QWEN3_TTS_BIN',
                                str(SCRIPT_DIR / 'models' / 'qwen3-bin' / 'qwen_tts')))
QWEN3_MODEL = Path(os.environ.get('QWEN3_TTS_MODEL',
                                  str(SCRIPT_DIR / 'models' / 'qwen3-tts-0.6b')))
QWEN3_BUDGET_SEC = int(os.environ.get('QWEN3_BUDGET_SEC', '1500'))   # 总预算 25 分钟
QWEN3_THREADS = os.environ.get('QWEN3_THREADS', '4')


def _synth_qwen3(text: str, out_path: Path) -> bool:
    """Qwen3-TTS 0.6B INT8 分块合成(~500字/块, 段落边界优先), ffmpeg 拼接。

    超预算(QWEN3_BUDGET_SEC)立刻放弃 → 调用方回退下一引擎; 部分块成功不拼残品。
    """
    import re as _re
    import time as _time
    if not (QWEN3_BIN.exists() and (QWEN3_MODEL / 'model.safetensors').exists()):
        log("⚠️ qwen3 二进制/模型缺失, 回退下一引擎")
        return False
    # 分块: 先按空行(段落), 段落过长再按句切, 目标 ≤500 字/块
    paras = [p.strip() for p in _re.split(r'\n\s*\n', text) if p.strip()]
    chunks, cur = [], ''
    for p in paras:
        if len(cur) + len(p) <= 500:
            cur = (cur + '\n\n' + p).strip()
            continue
        if cur:
            chunks.append(cur)
            cur = ''
        if len(p) <= 500:
            cur = p
        else:
            for s in _re.split(r'(?<=[。！？；])', p):
                if len(cur) + len(s) > 500 and cur:
                    chunks.append(cur)
                    cur = s
                else:
                    cur += s
    if cur.strip():
        chunks.append(cur.strip())
    log(f"qwen3: {len(text)} 字 → {len(chunks)} 块 (预算 {QWEN3_BUDGET_SEC}s)")

    # 服务模式: 模型加载+INT8 量化只做一次(每块单独起进程会重复这 1-2 分钟开销,
    # 实测 620 字两块被拖到 17.9 分钟; server 模式一次加载、分块走 HTTP)。
    import json as _json
    import urllib.request as _ur
    t0 = _time.time()
    part_files = []
    tmp_dir = out_path.parent
    port = int(os.environ.get('QWEN3_PORT', '3457'))
    spk = VOICE if not VOICE.startswith('zh-') else 'vivian'
    srv = None
    try:
        srv = subprocess.Popen(
            [str(QWEN3_BIN), '-d', str(QWEN3_MODEL), '--serve', str(port),
             '--int8', '-j', QWEN3_THREADS, '--seed', '7', '-S'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # 等加载+量化完成(最多 5 分钟)
        up = False
        for _ in range(150):
            if srv.poll() is not None:
                log("⚠️ qwen3 server 进程退出, 回退")
                return False
            try:
                with _ur.urlopen(f'http://127.0.0.1:{port}/v1/health', timeout=2):
                    up = True
                    break
            except Exception:
                _time.sleep(2)
        if not up:
            log("⚠️ qwen3 server 5 分钟未就绪, 回退")
            return False
        log(f"qwen3 server 就绪({(_time.time()-t0):.0f}s), 开始分块合成")

        for i, c in enumerate(chunks):
            remain = QWEN3_BUDGET_SEC - (_time.time() - t0)
            if remain < 60:
                log(f"⚠️ qwen3 预算耗尽(块 {i}/{len(chunks)}), 放弃回退")
                return False
            part = tmp_dir / f'_q3_part{i}.wav'
            body = _json.dumps({'text': c, 'speaker': spk,
                                'language': 'chinese'}).encode('utf-8')
            req = _ur.Request(f'http://127.0.0.1:{port}/v1/tts', data=body,
                              headers={'Content-Type': 'application/json'})
            try:
                with _ur.urlopen(req, timeout=remain) as resp:
                    part.write_bytes(resp.read())
            except Exception as e:
                log(f"⚠️ qwen3 块 {i} 失败({str(e)[:100]}), 回退")
                return False
            if not part.exists() or part.stat().st_size < 20000:
                log(f"⚠️ qwen3 块 {i} 产物异常, 回退")
                return False
            part_files.append(part)
        # 拼接(同编码 wav, concat demuxer 零转码)
        lst = tmp_dir / '_q3_concat.txt'
        lst.write_text(''.join(f"file '{p.name}'\n" for p in part_files),
                       encoding='utf-8')
        subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-f', 'concat',
                        '-safe', '0', '-i', str(lst), '-c', 'copy', str(out_path)],
                       check=True, timeout=120, capture_output=True, cwd=str(tmp_dir))
        log(f"qwen3: 合成完成, 用时 {(_time.time()-t0)/60:.1f} 分钟")
        return out_path.exists() and out_path.stat().st_size > 100000
    except Exception as e:
        log(f"⚠️ qwen3 合成异常({str(e)[:120]}), 回退")
        return False
    finally:
        if srv is not None:
            try:
                srv.terminate()
                srv.wait(timeout=10)
            except Exception:
                try:
                    srv.kill()
                except Exception:
                    pass
        for p in part_files:
            try:
                p.unlink()
            except OSError:
                pass
        try:
            (tmp_dir / '_q3_concat.txt').unlink()
        except OSError:
            pass


# ── Kokoro 开源引擎(BROADCAST_ENGINE=kokoro): 比 Edge 云声更自然, 全本地推理免账号 ──
# 模型 ~400MB 首次从 GitHub releases 公开直链下载(CI 用 actions/cache 复用),
# CPU ≈2-3x 实时(6 分钟音频合成 ~3 分钟)。失败自动回退 edge-tts。
KOKORO_DIR = SCRIPT_DIR / 'models'
KOKORO_FILES = {
    'kokoro-v1.1-zh.onnx':
        'https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.1/kokoro-v1.1-zh.onnx',
    'voices-v1.1-zh.bin':
        'https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.1/voices-v1.1-zh.bin',
    'kokoro-zh-config.json':
        'https://huggingface.co/hexgrad/Kokoro-82M-v1.1-zh/raw/main/config.json',
}


def _kokoro_ensure_models() -> bool:
    import urllib.request
    KOKORO_DIR.mkdir(parents=True, exist_ok=True)
    for name, url in KOKORO_FILES.items():
        p = KOKORO_DIR / name
        if p.exists() and p.stat().st_size > 1000:
            continue
        log(f"下载 Kokoro 模型 {name} ...")
        try:
            urllib.request.urlretrieve(url, str(p))
        except Exception as e:
            log(f"⚠️ 模型下载失败 {name}: {e}")
            return False
    return True


def _synth_kokoro(text: str, rate_pct: int, out_path: Path) -> bool:
    """Kokoro v1.1-zh 合成(按句分块, 英文走 espeak G2P)。成功返回 True。"""
    try:
        import re as _re
        import numpy as np
        import soundfile as sf
        from kokoro_onnx import Kokoro
        from misaki.zh import ZHG2P
        from misaki import espeak as mespeak
    except ImportError as e:
        log(f"⚠️ Kokoro 依赖缺失({e}), 回退 edge-tts")
        return False
    if not _kokoro_ensure_models():
        return False
    try:
        _eng = mespeak.EspeakG2P(language='en-us')

        def _en(t):
            try:
                return _eng(t)[0]
            except Exception:
                return ''
        g2p = ZHG2P(version='1.1', en_callable=_en)
        k = Kokoro(str(KOKORO_DIR / 'kokoro-v1.1-zh.onnx'),
                   str(KOKORO_DIR / 'voices-v1.1-zh.bin'),
                   vocab_config=str(KOKORO_DIR / 'kokoro-zh-config.json'))
        # ⚠️ 该 ONNX 导出 speed<1.0 必崩(Expand 节点负尺寸), 只允许 ≥1.0;
        # 时长控制主要靠稿件字数(LLM 端), 放慢不是必需。
        speed = max(1.0, min(1.15, 1.0 + rate_pct / 100.0))
        # 按句切块(≤80 字, Kokoro 单次 510 音素上限), 块间 0.25s 停顿
        sents = _re.split(r'(?<=[。！？；\n])', text)
        chunks, cur = [], ''
        for s in sents:
            if len(cur) + len(s) > 80 and cur:
                chunks.append(cur)
                cur = s
            else:
                cur += s
        if cur.strip():
            chunks.append(cur)
        parts, sr = [], 24000
        for c in chunks:
            c = c.strip()
            if not c:
                continue
            ph, _ = g2p(c)
            if not ph:
                continue
            # qwen3 音色名(vivian 等)落到 kokoro 回退时, 用用户上一轮选定的 zf_017
            _kv = VOICE if VOICE[:3] in ('zf_', 'zm_', 'af_', 'bf_') else 'zf_017'
            samples, sr = k.create(ph, voice=_kv, speed=speed, is_phonemes=True)
            parts.append(samples)
            parts.append(np.zeros(int(sr * 0.25), dtype=samples.dtype))
        if not parts:
            log("⚠️ Kokoro 无产出, 回退 edge-tts")
            return False
        sf.write(str(out_path), np.concatenate(parts), sr)
        return True
    except Exception as e:
        log(f"⚠️ Kokoro 合成失败({str(e)[:120]}), 回退 edge-tts")
        return False


def _dur_sec(path: Path) -> float:
    """优先 mutagen(纯 python), 回退 ffprobe。"""
    try:
        from mutagen.mp3 import MP3
        return float(MP3(str(path)).info.length)
    except Exception:
        out = subprocess.run(
            ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
             '-of', 'default=noprint_wrappers=1:nokey=1', str(path)],
            capture_output=True, text=True, timeout=30)
        return float(out.stdout.strip())


def main() -> int:
    txt_path = SCRIPT_DIR / 'output' / 'broadcast.txt'
    if not txt_path.exists():
        log("无 broadcast.txt, 跳过(本班无口播稿)")
        return 0
    text = txt_path.read_text(encoding='utf-8').strip()
    if len(text) < MIN_CHARS:
        log(f"口播稿仅 {len(text)} 字 (<{MIN_CHARS}), 跳过")
        return 0

    out_rel = sys.argv[1] if len(sys.argv) > 1 else ''
    if not out_rel:
        from datetime import datetime
        shift = os.environ.get('BRIEFING_SHIFT', '').lower()
        suffix = f'-{shift}' if shift in ('am', 'pm') else ''
        out_rel = f"archive/audio/{datetime.now().strftime('%Y-%m-%d')}{suffix}.mp3"
    out_path = SCRIPT_DIR / 'output' / out_rel
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # ── 1. TTS 人声: 引擎链 qwen3 → kokoro → edge, 逐级回退, 永不空手 ──
    # BROADCAST_NO_FALLBACK=true(HQ 离线重制用): 所选引擎失败就直接放弃不产文件,
    # 调用方据此保留已有的快引擎音频, 而不是用同档引擎白白覆盖一遍。
    no_fallback = os.environ.get('BROADCAST_NO_FALLBACK', '').lower() == 'true'
    rate_pct = _voice_rate_pct(len(text))
    voice_path = None
    wav = SCRIPT_DIR / 'output' / '_broadcast_voice.wav'
    if ENGINE == 'qwen3':
        if _synth_qwen3(text, wav) and wav.exists() and wav.stat().st_size > 50000:
            voice_path = wav
    if voice_path is None and no_fallback:
        log("引擎失败且 NO_FALLBACK, 不产出(保留既有音频)")
        return 0
    if voice_path is None and ENGINE in ('kokoro', 'qwen3'):
        if _synth_kokoro(text, rate_pct, wav) \
                and wav.exists() and wav.stat().st_size > 50000:
            voice_path = wav
    if voice_path is None:
        mp3 = SCRIPT_DIR / 'output' / '_broadcast_voice.mp3'
        # kokoro 音色名(zf_xxx)不能漏给 edge
        global VOICE
        if not VOICE.startswith('zh-') and not VOICE.startswith('en-'):
            VOICE = EDGE_FALLBACK_VOICE
        try:
            asyncio.run(_synth(text, f"{rate_pct:+d}%", mp3))
        except Exception as e:
            log(f"⚠️ edge-tts 合成失败, 跳过音频: {e}")
            return 0
        if not mp3.exists() or mp3.stat().st_size < 10000:
            log("⚠️ TTS 产物异常(空/过小), 跳过音频")
            return 0
        voice_path = mp3
    try:
        vdur = _dur_sec(voice_path)
    except Exception as e:
        log(f"⚠️ 无法读人声时长({e}), 按估算继续")
        vdur = len(text) / CHARS_PER_MIN * 60.0
    log(f"人声({ENGINE}/{VOICE}): {len(text)} 字 · {rate_pct:+d}% · {vdur/60:.1f} 分钟")

    # 超长保险: qwen3 不支持语速参数, 稿子偏长时音频会超 6 分钟 → atempo 轻微提速
    # (≤1.12x, 听感几乎无损)收回 6 分钟附近。
    tempo = 1.0
    if vdur > 385:
        tempo = min(1.12, vdur / 360.0)
        log(f"超长 {vdur/60:.1f} 分钟 → atempo {tempo:.2f} 收到 {vdur/tempo/60:.1f} 分钟")
        vdur = vdur / tempo
    _tempo_flt = f"atempo={tempo:.3f}," if tempo > 1.0 else ""

    # ── 2. 与底乐混音(无 ffmpeg / 无底乐 → 纯人声也照发) ──
    title_date = out_rel.rsplit('/', 1)[-1].replace('.mp3', '')
    _rpt = {'am': 'AI 早报', 'pm': 'AI 晚报'}.get(
        os.environ.get('BRIEFING_SHIFT', '').lower(), 'AI 日报')
    meta = ['-metadata', f'title={_rpt} · {title_date}',
            '-metadata', 'artist=AI Morning Briefing']
    mixed = False
    if BGM.exists():
        total = INTRO_SEC + vdur + OUTRO_SEC
        vend = INTRO_SEC + vdur
        fade_st = max(0.0, total - 3.0)
        delay_ms = int(INTRO_SEC * 1000)
        flt = (
            f"[0:a]aresample=44100,aformat=channel_layouts=stereo,{_tempo_flt}"
            f"adelay={delay_ms}|{delay_ms}[v];"
            f"[1:a]volume='if(lt(t,{INTRO_SEC}),0.42,"
            f"if(lt(t,{vend:.2f}),0.13,0.36))':eval=frame,"
            f"atrim=0:{total:.2f},afade=t=out:st={fade_st:.2f}:d=3[b];"
            f"[v][b]amix=inputs=2:duration=longest:normalize=0,"
            f"loudnorm=I=-16:TP=-1.5:LRA=11[out]"
        )
        cmd = ['ffmpeg', '-y', '-loglevel', 'error',
               '-i', str(voice_path), '-stream_loop', '-1', '-i', str(BGM),
               '-filter_complex', flt, '-map', '[out]',
               '-c:a', 'libmp3lame', '-b:a', '80k', '-ar', '44100',
               *meta, str(out_path)]
        try:
            subprocess.run(cmd, check=True, timeout=600,
                           capture_output=True, text=True)
            mixed = True
        except FileNotFoundError:
            log("⚠️ 无 ffmpeg, 退回纯人声")
        except subprocess.CalledProcessError as e:
            log(f"⚠️ ffmpeg 混音失败({(e.stderr or '')[:200]}), 退回纯人声")
        except Exception as e:
            log(f"⚠️ 混音异常({e}), 退回纯人声")
    else:
        log(f"⚠️ 底乐缺失 {BGM}, 退回纯人声")

    if not mixed:
        # 纯人声兜底: 仍尽量 loudnorm + 打标; 连 ffmpeg 都没有就直接拷贝
        try:
            subprocess.run(['ffmpeg', '-y', '-loglevel', 'error',
                            '-i', str(voice_path),
                            '-af', f'{_tempo_flt}loudnorm=I=-16:TP=-1.5:LRA=11',
                            '-c:a', 'libmp3lame', '-b:a', '80k',
                            *meta, str(out_path)],
                           check=True, timeout=600, capture_output=True)
        except Exception:
            import shutil
            shutil.copyfile(voice_path, out_path)

    try:
        voice_path.unlink()
    except OSError:
        pass
    try:
        fdur = _dur_sec(out_path)
        log(f"✅ 音频就绪: {out_rel} · {fdur/60:.1f} 分钟 · "
            f"{out_path.stat().st_size//1024}KB · {'混音' if mixed else '纯人声'}")
    except Exception:
        log(f"✅ 音频就绪: {out_rel}")
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as e:   # 最后防线: 任何意外都不挡出报
        log(f"⚠️ 未捕获异常(跳过音频): {e}")
        sys.exit(0)
