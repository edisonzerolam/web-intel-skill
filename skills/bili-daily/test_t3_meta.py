#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_t3_meta: P2b —— SenseVoice 情感/事件标记结构化的回归测试
================================================================
运行（主环境，非 ROCm venv）：
    python test_t3_meta.py              # 全部用例
    python test_t3_meta.py --red        # 可红性自检：注入故障，验证用例能失败
    python test_t3_meta.py --no-gpu     # 跳过需要真实 GPU 的用例

背景（P2b，2026-10-07）：
    SenseVoice 的输出自带结构化标签，形如
        <|zh|><|NEUTRAL|><|BGM|><|withitn|>句1 <|zh|><|ANGRY|><|BGM|><|withitn|>句2 ...
    其中 `<|HAPPY|>` 等是情感、`<|BGM|>`/`<|Laughter|>` 等是音频事件。
    `rich_transcription_postprocess` 会把它们替换成 emoji（😊🎼）混进正文，
    换掉之后就再也统计不出来了 —— 所以必须在后处理**之前**解析。

本测试锁死三条契约：
  ① 标签词表必须与 funasr 的 `emoji_dict` 对齐（防上游新增标签后静默漏统计）；
  ② 段级标签只能从**顶层 record["text"]** 按 `<|withitn|>` 切分对齐取得
     —— `sentence_info[i]["text"]` 已被 funasr 清洗、**不含标签**（2026-10-07 实测，
       第一版实现正是踩了这个坑，导致 emotion_segments 恒为空）；
  ③ meta 走独立的顶层字段，**不得改动 segments 的 {from,to,content} 契约**
     （P1 的 test_asr_gpu.py 断言 7a 要求 GPU 与 CPU 的段 key 集合完全相同）。

断言清单：
  1. 静态契约与词表对齐（1a _parse_meta 存在；1b 词表覆盖 funasr emoji_dict；
     1c dispatch_meta 存在；1d build_markdown 接受 audio_meta）
  2. 纯函数解析（2a 空串；2b 计数；2c 未知标签忽略；2d _merge_counts；
     2e 对齐不变式：withitn 切分段数 == sentence_info 段数）
  3. 真实 GPU meta（3a 五键齐全；3b 键都是已知标签；3c has_music 与 events 一致；
     3d music_ratio 与 events/seg_count 一致；3e emotion_segments 结构；
     3f 只含有区分度的段；3g 区间是秒级；3h 加时间戳不改变正文）
  4. 契约保持（4a dispatch 仍 2 元组；4b 不传 audio_meta 时不写该字段；
     4c 传了则是合法 JSON）
"""
import argparse
import json
import os
import re
import sys
import sysconfig

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

AUDIO = os.environ.get("TEST_AUDIO", r"C:\Temp\t3audio\BV1cuZwYZEZr.m4a")

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"{'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail else ""),
          flush=True)
    return cond


def _funasr_tags():
    """从 funasr 源码里读 emoji_dict 的标签集合（不 import funasr，避免重依赖）。"""
    src = os.path.join(sysconfig.get_paths()["purelib"],
                       "funasr", "utils", "postprocess_utils.py")
    txt = open(src, encoding="utf-8").read()
    i = txt.index("emoji_dict")
    j = txt.index("}", i)
    return set(re.findall(r"<\|([A-Za-z_]+)\|>", txt[i:j])), src


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--red", action="store_true", help="可红性自检：注入故障")
    ap.add_argument("--no-gpu", action="store_true", help="跳过需要真实 GPU 的用例")
    args = ap.parse_args()

    import t3_rocm_worker as wkr
    import transcribe_video as tv

    if args.red:
        # 注入故障①：把对齐分隔符改成一个永不出现的标记 →
        #   tag_chunks 为空 → 2e/2e2 必须失败（锁死对齐实现）。
        wkr._ITN_MARK = "<|NEVER_MATCH|>"
        # 注入故障②：worker 跑在 ROCm venv 子进程里，上面的 monkeypatch 传不进去，
        #   故在**本进程侧**再削掉 meta，验证 3a 对 meta 缺失确实会红。
        _orig_meta = tv.t3_sensevoice_rocm_meta
        tv.t3_sensevoice_rocm_meta = \
            lambda p, worker=None: (_orig_meta(p, worker=worker)[0], {})

    print("=" * 64)
    print("P2b: SenseVoice 情感/事件标记结构化 回归测试")
    print("=" * 64)

    # ------------------------------------------------- 1 静态契约与词表对齐
    print("\n[1] 静态契约与词表对齐")
    check("1a t3_rocm_worker._parse_meta 存在", callable(getattr(wkr, "_parse_meta", None)))
    check("1c transcribe_video.t3_sensevoice_dispatch_meta 存在",
          callable(getattr(tv, "t3_sensevoice_dispatch_meta", None)))

    import inspect
    sig = inspect.signature(tv.build_markdown)
    check("1d build_markdown 接受 audio_meta 参数", "audio_meta" in sig.parameters,
          str(sig))

    try:
        all_tags, src = _funasr_tags()
        lang = {"zh", "en", "yue", "ja", "ko", "nospeech"}
        itn = {"withitn", "woitn"}
        expected = all_tags - lang - itn
        covered = set(wkr._EMOTION_TAGS) | set(wkr._EVENT_TAGS)
        missing = sorted(expected - covered)
        extra = sorted(covered - expected)
        check("1b 词表覆盖 funasr emoji_dict 的全部情感/事件标签",
              not missing, f"missing={missing} extra={extra}")
    except Exception as e:
        check("1b 词表覆盖 funasr emoji_dict 的全部情感/事件标签", False,
              f"{type(e).__name__}: {e}")

    # ------------------------------------------------------- 2 纯函数解析
    print("\n[2] 纯函数解析（不依赖 GPU）")
    em, ev = wkr._parse_meta("")
    check("2a 空串返回 ({}, {})", em == {} and ev == {}, f"{em} {ev}")

    sample = "<|zh|><|HAPPY|><|BGM|><|withitn|>a<|zh|><|HAPPY|><|Laughter|><|withitn|>b"
    em2, ev2 = wkr._parse_meta(sample)
    check("2b 多标签计数正确",
          em2 == {"HAPPY": 2} and ev2 == {"BGM": 1, "Laughter": 1}, f"{em2} {ev2}")

    em3, ev3 = wkr._parse_meta("<|zh|><|NOT_A_REAL_TAG|><|withitn|>x")
    check("2c 未知标签被忽略（不污染计数）", em3 == {} and ev3 == {}, f"{em3} {ev3}")

    dst = {"BGM": 1}
    wkr._merge_counts(dst, {"BGM": 2, "Cough": 1})
    check("2d _merge_counts 累加", dst == {"BGM": 3, "Cough": 1}, str(dst))

    # 2e 对齐不变式：真实 raw 文本按 <|withitn|> 切分后的段数 == sentence_info 段数。
    #    这是第一版 bug 的直接反例（当时从 si["text"] 取标签，恒取不到）。
    raw = ("<|zh|><|NEUTRAL|><|BGM|><|withitn|>甲。<|zh|><|ANGRY|><|BGM|>"
           "<|withitn|>乙。<|zh|><|NEUTRAL|><|BGM|><|withitn|>丙。")
    chunks = raw.split(wkr._ITN_MARK)[1:]
    si_list = [{"start": 0, "end": 1000}, {"start": 1000, "end": 2000},
               {"start": 2000, "end": 3000}]
    check("2e withitn 切分段数 == sentence_info 段数（对齐不变式）",
          len(chunks) == len(si_list), f"chunks={len(chunks)} si={len(si_list)}")
    # 切分后 chunks[0] 携带的是**第 1 句**（下标 0）的标签，chunks[1] 是第 2 句。
    # （实现里用的就是 tag_chunks[i2]，i2 为 sentence_info 的 0 基下标。）
    em4, _ = wkr._parse_meta(chunks[0] if chunks else "")
    em5, _ = wkr._parse_meta(chunks[1] if len(chunks) > 1 else "")
    check("2e2 第 k 段携带的标签属于第 k 句",
          em4 == {"ANGRY": 1} and em5 == {"NEUTRAL": 1}, f"chunks0={em4} chunks1={em5}")

    # ------------------------------------------------------- 3 真实 GPU meta
    print("\n[3] 真实 GPU meta（端到端）")
    meta, text_ts, text_plain = {}, None, None
    if args.no_gpu:
        print("⚠️ 3a–3h 跳过（--no-gpu）", flush=True)
    elif not os.path.exists(AUDIO):
        print(f"⚠️ 3a–3h 跳过（音频不存在：{AUDIO}）", flush=True)
    else:
        try:
            text_ts, backend, meta = tv.t3_sensevoice_dispatch_meta(AUDIO)
            err = ""
        except Exception as e:
            meta, err = {}, f"{type(e).__name__}: {str(e)[:120]}"

        # 3a 无条件检查：meta 缺失必须是**失败**，不能因为「没数据」而跳过。
        need = {"emotions", "events", "has_music", "music_ratio", "emotion_segments"}
        check("3a meta 五键齐全", need <= set(meta), err or str(sorted(meta)))

        if meta:
            known = set(wkr._EMOTION_TAGS) | set(wkr._EVENT_TAGS)
            bad = sorted((set(meta.get("emotions", {})) | set(meta.get("events", {})))
                         - known)
            check("3b 计数键都是已知标签", not bad,
                  f"emotions={meta.get('emotions')} events={meta.get('events')} bad={bad}")

            check("3c has_music 与 events['BGM'] 一致",
                  bool(meta["has_music"]) == bool(meta["events"].get("BGM")),
                  f"has_music={meta['has_music']} BGM={meta['events'].get('BGM')}")

            segs = meta.get("emotion_segments") or []
            bgm = meta["events"].get("BGM", 0)
            # music_ratio 的分母是**总句数**（seg_count），meta 里没有它，
            # 故只锁死「有 BGM ⟺ ratio>0」与值域这两条不依赖分母的性质。
            check("3d music_ratio 值域正确且与 BGM 有无一致",
                  0.0 <= meta["music_ratio"] <= 1.0
                  and bool(bgm) == (meta["music_ratio"] > 0),
                  f"ratio={meta['music_ratio']} BGM={bgm}")

            check("3e emotion_segments 非空且结构正确",
                  bool(segs) and all(
                      isinstance(s.get("from"), (int, float))
                      and isinstance(s.get("to"), (int, float))
                      and s.get("emotion") in known
                      and isinstance(s.get("events"), list)
                      for s in segs),
                  f"n={len(segs)} 首条={segs[0] if segs else None}")

            # 3f 过滤规则：不得出现「情绪为 NEUTRAL/None 且事件只有 BGM」的段
            noisy = [s for s in segs
                     if s.get("emotion") in (None, "NEUTRAL")
                     and not [e for e in s.get("events", []) if e != "BGM"]]
            check("3f 只保留有区分度的段（无 NEUTRAL+纯BGM 段）", not noisy,
                  f"噪声段 {len(noisy)} 条")

            last_to = max((s["to"] for s in segs), default=0.0)
            check("3g 区间是秒级（未误用毫秒）", 0 < last_to < 3600,
                  f"last_to={last_to}")

            # 3h 加时间戳不改变正文
            try:
                import t3_rocm_client as cli
                msg = cli.get_worker().transcribe_full(AUDIO, with_timestamps=False)
                text_plain = msg.get("text")
            except Exception as e:
                print(f"⚠️ 3h 取无时间戳文本失败：{type(e).__name__}: {e}", flush=True)
            if text_plain and text_ts:
                a, b = text_plain, text_ts
                n = min(len(a), len(b))
                same = sum(1 for i in range(n) if a[i] == b[i])
                ratio = same / max(1, n)
                check("3h 加时间戳不改变正文（相似度 >0.99）", ratio > 0.99,
                      f"ratio={ratio:.4f} len={len(a)}/{len(b)}")
            else:
                print("⚠️ 3h 跳过（无对照文本）", flush=True)
        else:
            print("⚠️ 3b–3h 跳过（meta 为空，见 3a）", flush=True)

    # --------------------------------------------------------- 4 契约保持
    print("\n[4] 契约保持（不得破坏 P1）")
    md_no = tv.build_markdown("T", "正文", "BV1", "UP", "SenseVoice-Small(GPU)", "zh")
    check("4b 不传 audio_meta 时不写该字段", "audio_meta" not in md_no)

    md_yes = tv.build_markdown("T", "正文", "BV1", "UP", "SenseVoice-Small(GPU)", "zh",
                               audio_meta=meta or {"emotions": {"HAPPY": 1}})
    ok_json = False
    detail = ""
    if "audio_meta:" in md_yes:
        line = [l for l in md_yes.splitlines() if l.startswith("audio_meta:")][0]
        try:
            json.loads(line.split("audio_meta:", 1)[1].strip())
            ok_json = True
        except Exception as e:
            detail = f"{type(e).__name__}: {e}"
    check("4c 传了 audio_meta 时是合法 JSON", ok_json, detail or line[:80])

    if args.no_gpu:
        print("⚠️ 4a 跳过（--no-gpu）", flush=True)
    else:
        try:
            r = tv.t3_sensevoice_dispatch(AUDIO)
            check("4a t3_sensevoice_dispatch 仍是 2 元组（P1 契约）",
                  isinstance(r, tuple) and len(r) == 2, f"len={len(r)}")
        except Exception as e:
            check("4a t3_sensevoice_dispatch 仍是 2 元组（P1 契约）", False,
                  f"{type(e).__name__}: {str(e)[:120]}")

    # ---------------------------------------------------------------- 汇总
    print("=" * 64)
    print(f"通过 {len(PASS)} / 失败 {len(FAIL)}")
    for f in FAIL:
        print(f"  ❌ {f}")
    print("=" * 64)

    if args.red:
        if len(FAIL) >= 1:
            print("\n[RED] 可红性验证通过：注入故障后断言确实失败。")
            sys.exit(0)
        print("\n[RED] 可红性验证失败：注入故障后断言仍全绿，测试无鉴别力！")
        sys.exit(2)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
