"""Synthesised sound effects (no audio files to ship) plus user-supplied ones."""
import array
import os
import threading
import wave
from pathlib import Path

from . import ff, paths

VERSION = 2
_saw = "2*(t*{f}-floor(t*{f}+0.5))"


def _horn(envelope):
    voices = "+".join(f"{g}*{_saw.format(f=f)}" for f, g in ((466, 0.3), (470.5, 0.3), (233, 0.25), (699, 0.15)))
    return f"({envelope})*({voices})"


# name: (label, seconds, aevalsrc expression, post filter)
SOUNDS = {
    "airhorn": ("Air horn", 1.4, _horn("min(1,t/0.03)*min(1,max(0,(1.35-t)/0.2))"),
                "highpass=f=180,lowpass=f=4200"),
    "airhorn3": ("Air horn x3", 1.9,
                 _horn("(between(t,0,0.2)+between(t,0.28,0.48)+between(t,0.56,1.75))*min(1,max(0,(1.9-t)/0.2))"),
                 "highpass=f=180,lowpass=f=4200"),
    "beep": ("Beep", 0.45, "0.6*sin(2*PI*1000*t)*min(1,t/0.005)*min(1,max(0,(0.45-t)/0.04))", ""),
    "double_beep": ("Double beep", 0.5, "0.6*sin(2*PI*1200*t)*(between(t,0,0.18)+between(t,0.28,0.46))", ""),
    "buzzer": ("Game show buzzer", 0.9,
               "0.35*(sgn(sin(2*PI*110*t))+sgn(sin(2*PI*116.5*t)))*min(1,t/0.01)*min(1,max(0,(0.9-t)/0.08))",
               "lowpass=f=2400"),
    "ding": ("Ding", 1.8, "(0.5*sin(2*PI*1320*t)+0.3*sin(2*PI*2640*t)+0.15*sin(2*PI*3960*t))*exp(-3*t)", ""),
    "gong": ("Gong", 3.0, "(0.5*sin(2*PI*98*t)+0.35*sin(2*PI*196.6*t)+0.25*sin(2*PI*294*t)+"
             "0.2*sin(2*PI*411*t)+0.12*sin(2*PI*587*t))*min(1,t/0.01)*exp(-1.1*t)", ""),
    "whistle": ("Referee whistle", 0.9,
                "0.5*sin(2*PI*2900*t+6*sin(2*PI*28*t))*min(1,t/0.02)*min(1,max(0,(0.9-t)/0.06))", ""),
    "clink": ("Glass clink", 0.9,
              "(sin(2*PI*3100*t)+0.6*sin(2*PI*4720*t)+0.4*sin(2*PI*6210*t))*exp(-16*t)*0.5+"
              "gte(t,0.17)*(sin(2*PI*3180*(t-0.17))+0.6*sin(2*PI*4800*(t-0.17)))*exp(-16*(t-0.17))*0.4", ""),
    "whoosh": ("Whoosh", 0.9, "(2*random(0)-1)*sin(PI*t/0.9)^3", "bandpass=f=1400:width_type=o:w=2.5,lowpass=f=5000"),
    "tick": ("Tick", 0.25, "0.7*sin(2*PI*880*t)*min(1,t/0.003)*exp(-14*t)", ""),
    # themed
    "thunder": ("Thunder", 4.0, "(2*random(0)-1)*(min(1,t/0.03)*exp(-1.1*t)+0.6*exp(-4*abs(t-0.9)))*(1+0.4*sin(2*PI*2.5*t))",
                "lowpass=f=260,lowpass=f=400,volume=3"),
    "organ": ("Spooky organ", 3.2, "(" + "+".join(
        f"({g})*(sin(2*PI*{f}*t)+0.5*sin(4*PI*{f}*t)+0.3*sin(6*PI*{f}*t))" for f, g in
        ((110, 0.35), (220, 0.3), (261.6, 0.25), (311.1, 0.25), (415.3, 0.15)))
        + ")*(1+0.25*sin(2*PI*5.5*t))*min(1,t/0.25)*min(1,max(0,(3.2-t)/0.6))", "lowpass=f=3000"),
    "ghost": ("Ghost wail", 2.6, "0.6*sin(2*PI*(520*t-190*cos(PI*t/2.6))+4*sin(2*PI*5.5*t))*sin(PI*t/2.6)", ""),
    "bell": ("Church bell", 4.5, "0.5*sin(2*PI*110*t)*exp(-0.6*t)+0.45*sin(2*PI*220.5*t)*exp(-0.9*t)+"
             "0.3*sin(2*PI*264.7*t)*exp(-1.1*t)+0.25*sin(2*PI*330.8*t)*exp(-1.3*t)+0.2*sin(2*PI*441*t)*exp(-1.6*t)"
             "+0.12*sin(2*PI*587*t)*exp(-2.2*t)", ""),
    "sleigh": ("Sleigh bells", 1.8, "(2*random(0)-1)*pow(abs(sin(2*PI*4.2*t)),6)*min(1,max(0,(1.8-t)/0.4))",
               "highpass=f=4500,lowpass=f=11000,volume=2.5"),
    "party_horn": ("Party horn", 0.9, "0.6*(2*(t*(380+50*t)-floor(t*(380+50*t)+0.5)))*(0.75+0.25*sgn(sin(2*PI*32*t)))"
                   "*min(1,t/0.02)*min(1,max(0,(0.9-t)/0.08))", "lowpass=f=3500"),
    "scratch": ("Record scratch", 0.6, "(0.7*sin(2*PI*(380*t+70*sin(2*PI*3.3*t)))+0.3*(2*random(0)-1))"
                "*min(1,t/0.01)*min(1,max(0,(0.6-t)/0.05))", "bandpass=f=1500:width_type=o:w=3"),
    "slide_whistle": ("Slide whistle", 0.8, "0.6*sin(2*PI*(600*t+1000*t*t))*min(1,t/0.03)*min(1,max(0,(0.8-t)/0.08))", ""),
}
_lock = threading.Lock()


def catalog():
    return [{"id": k, "label": v[0]} for k, v in SOUNDS.items()]


def _peak(path):
    with wave.open(str(path), "rb") as w:
        a = array.array("h")
        a.frombytes(w.readframes(w.getnframes()))
    return max((abs(x) for x in a), default=0) / 32768.0


def builtin(name):
    """Path to a generated sound, creating it on first use."""
    if name not in SOUNDS:
        return None
    out = paths.SFX / f"{name}.v{VERSION}.wav"
    with _lock:
        if not out.exists():
            _, dur, expr, post = SOUNDS[name]
            raw = paths.SFX / f"{name}.raw.wav"
            ff.run(["-f", "lavfi", "-i", f"aevalsrc=exprs='{expr}':s=48000:d={dur}",
                    "-af", post or "anull", "-ac", "2", "-ar", "48000", "-c:a", "pcm_s16le", raw])
            peak = _peak(raw) or 1.0
            gain = 0.89 / peak  # normalise every effect to about -1 dBFS peak
            ff.run(["-i", raw, "-af", f"volume={gain:.4f}", "-c:a", "pcm_s16le", out])
            os.remove(raw)
    return out


def resolve(value, assets_dir):
    """Sound setting -> file path. Values: '' / 'none', a built-in id, or 'file:<asset name>'."""
    if not value or value == "none":
        return None
    if value.startswith("file:"):
        p = Path(assets_dir) / value[5:]
        return p if p.exists() else None
    return builtin(value)
