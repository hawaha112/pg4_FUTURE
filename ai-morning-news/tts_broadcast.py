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
VOICE = os.environ.get('BROADCAST_VOICE', 'zh-CN-YunyangNeural')
# 实测 Yunyang ≈317 字/分钟; 目标落点 5.5 分钟
CHARS_PER_MIN = 317.0
TARGET_SEC = float(os.environ.get('BROADCAST_TARGET_SEC', '330'))
MIN_CHARS = 200          # 稿子太短(生成失败的残片)不值得做节目
INTRO_SEC = 2.3          # 片头音乐独奏
OUTRO_SEC = 5.0          # 人声结束后音乐回升时长(含 3s 淡出)
BGM = SCRIPT_DIR / 'assets' / 'bgm_loop.mp3'


def log(msg):
    print(f"[tts_broadcast] {msg}", flush=True)


def _voice_rate(n_chars: int) -> str:
    """按字数微调语速, 把时长收敛到 TARGET_SEC 附近(±10% 封顶, 听感自然优先)。"""
    est = n_chars / CHARS_PER_MIN * 60.0
    ratio = est / TARGET_SEC - 1.0
    pct = max(-10, min(10, round(ratio * 100)))
    return f"{pct:+d}%"


async def _synth(text: str, rate: str, out_path: Path):
    import edge_tts
    comm = edge_tts.Communicate(text, VOICE, rate=rate)
    await comm.save(str(out_path))


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

    # ── 1. TTS 人声 ──
    rate = _voice_rate(len(text))
    voice_path = SCRIPT_DIR / 'output' / '_broadcast_voice.mp3'
    try:
        asyncio.run(_synth(text, rate, voice_path))
    except Exception as e:
        log(f"⚠️ edge-tts 合成失败, 跳过音频: {e}")
        return 0
    if not voice_path.exists() or voice_path.stat().st_size < 10000:
        log("⚠️ TTS 产物异常(空/过小), 跳过音频")
        return 0
    try:
        vdur = _dur_sec(voice_path)
    except Exception as e:
        log(f"⚠️ 无法读人声时长({e}), 按估算继续")
        vdur = len(text) / CHARS_PER_MIN * 60.0
    log(f"人声: {len(text)} 字 · rate {rate} · {vdur/60:.1f} 分钟")

    # ── 2. 与底乐混音(无 ffmpeg / 无底乐 → 纯人声也照发) ──
    title_date = out_rel.rsplit('/', 1)[-1].replace('.mp3', '')
    meta = ['-metadata', f'title=AI 早报 · {title_date}',
            '-metadata', 'artist=AI Morning Briefing']
    mixed = False
    if BGM.exists():
        total = INTRO_SEC + vdur + OUTRO_SEC
        vend = INTRO_SEC + vdur
        fade_st = max(0.0, total - 3.0)
        delay_ms = int(INTRO_SEC * 1000)
        flt = (
            f"[0:a]aresample=44100,aformat=channel_layouts=stereo,"
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
                            '-af', 'loudnorm=I=-16:TP=-1.5:LRA=11',
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
