#!/usr/bin/env python3
# 句光：给网站版每一句生成录音（微软 Edge 神经语音：美式 en-US-AriaNeural 全部句子；英式 en-GB-SoniaNeural 只做日常口语和职场英语），在仓库根目录运行。
# 需要：pip install edge-tts ；以及 ffmpeg（apt install ffmpeg，或 pip install imageio-ffmpeg）
# 用法：python3 gen_audio.py [--sets daily,work] [--jobs 12] [--max-minutes 100]
#   --sets   要生成的组（逗号分隔）：daily（日常口语）、work（职场五个系列）、books（全部原著）、单个组名（biz trade med fin it
#            prince oldman alice sherlock pride）、uk（英式：日常口语+职场）、uk/daily、uk/biz …，或 all（默认，美式全部 + 英式）。
#            试听句 x（和英式时的 uk/x）会顺带生成。英式文件在 audio/uk/<组>/ 下。
#   读 data/daily.json、data/decks.json、data/book_*.json，写 audio/<组>/<序号>.mp3（书：audio/<书>/<章>/<句>.mp3）
#   每句：Edge 语音（48kbps）→ ffmpeg 去掉首尾静音 → 22.05kHz 单声道 24kbps mp3
#   可以随时中断、重跑接着做：已生成且文字没变的句子会跳过（audio/<组>/_crc.json 记录每句文字的指纹）
#   全组生成完才写进 audio/index.json；网页只给 index.json 里列出的组播放录音，其余用浏览器语音
import argparse, asyncio, hashlib, json, os, random, re, shutil, subprocess, sys, time, zlib
try:
    import edge_tts
except ImportError:
    sys.exit('请先安装：pip install edge-tts')

VOICES = {'us': 'en-US-AriaNeural', 'uk': 'en-GB-SoniaNeural'}
BITRATE = '24k'
WORK = ['biz', 'trade', 'med', 'fin', 'it']
BOOKS = ['prince', 'oldman', 'alice', 'sherlock', 'pride']
X = ['Nice to meet you.', 'How are you doing today?']   # 和 app.js 里的 AU_X 一致
FILTER = ('silenceremove=start_periods=1:start_threshold=-50dB:start_silence=0.08,areverse,'
          'silenceremove=start_periods=1:start_threshold=-50dB:start_silence=0.12,areverse')

def find_ffmpeg():
    f = shutil.which('ffmpeg')
    if f: return f
    try:
        import imageio_ffmpeg; return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        sys.exit('找不到 ffmpeg：请先 apt-get install -y ffmpeg，或 pip install imageio-ffmpeg')

def voice_of(name): return VOICES['uk' if name.startswith('uk/') else 'us']

def items_of(name):
    """返回 [(相对路径, 英文句子), ...]，序号规则和 app.js 的 auAdd() 一致"""
    if name.startswith('uk/'):
        if name[3:] not in ['x', 'daily'] + WORK: sys.exit('英式只做日常口语和职场英语：' + name)
        return [('uk/' + r, t) for r, t in items_of(name[3:])]
    if name == 'x': return [('x/%d' % i, t) for i, t in enumerate(X)]
    if name == 'daily': return [('daily/%d' % i, s[0]) for i, s in enumerate(json.load(open('data/daily.json', encoding='utf-8')))]
    if name in WORK:
        d = json.load(open('data/decks.json', encoding='utf-8'))[name]
        return [('%s/%d' % (name, i), s[0]) for i, s in enumerate(d['s'])]
    if name in BOOKS:
        bk, out = json.load(open('data/book_%s.json' % name, encoding='utf-8')), []
        for ci, c in enumerate(bk['ch']):
            i = 0
            for p in c['p']:
                for s in p: out.append(('%s/%d/%d' % (name, ci, i), s[0])); i += 1
        return out
    sys.exit('未知的组：' + name)

def fp(text, voice): return '%08x' % zlib.crc32((voice + '|' + BITRATE + '|' + text).encode())

def wjson(path, o):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f: json.dump(o, f, ensure_ascii=False, separators=(',', ':'), sort_keys=True)
    os.replace(tmp, path)

class Gen:
    def __init__(s, ff, jobs, deadline):
        s.ff, s.jobs, s.deadline, s.limit = ff, jobs, deadline, 0
        s.done = s.fail = s.bytes = 0; s.t0 = time.time(); s.stop = False

    async def tts(s, text, voice):
        last = None
        for k in range(8):
            try:
                buf = bytearray()
                async for ch in edge_tts.Communicate(text, voice).stream():
                    if ch['type'] == 'audio': buf += ch['data']
                if buf: return bytes(buf)
                raise edge_tts.exceptions.NoAudioReceived('empty')
            except edge_tts.exceptions.NoAudioReceived as e:
                last = e
                if k >= 2: break          # 没声音（比如全是符号）：别反复试
            except Exception as e:
                last = e
            await asyncio.sleep(min(60, 2 ** k) + random.random() * 2)   # 被限流/断线：退避重试
        raise RuntimeError(repr(last)[:200])

    async def encode(s, mp3):
        p = await asyncio.create_subprocess_exec(s.ff, '-hide_banner', '-loglevel', 'error', '-f', 'mp3', '-i', 'pipe:0',
            '-af', FILTER, '-ac', '1', '-ar', '22050', '-c:a', 'libmp3lame', '-b:a', BITRATE, '-f', 'mp3', 'pipe:1',
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = await p.communicate(mp3)
        if p.returncode or len(out) < 500: raise RuntimeError('ffmpeg: ' + err.decode()[-200:])
        return out

    async def one(s, rel, text, crc, key, voice):
        async with s.sem:
            if s.stop or time.time() > s.deadline: s.stop = True; return
            path = 'audio/' + rel + '.mp3'
            try:
                if not re.search(r'[A-Za-z0-9]', text): crc[key] = fp(text, voice); return   # 全是符号：不生成，网页用浏览器语音
                data = await s.encode(await s.tts(text, voice))
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path + '.part', 'wb') as f: f.write(data)
                os.replace(path + '.part', path)
                crc[key] = fp(text, voice); s.done += 1; s.bytes += len(data)
            except Exception as e:
                crc[key] = '-'; s.fail += 1   # 记下失败，这句网页上用浏览器语音；重跑时会再试
                print('  失败', rel, str(e)[:120], flush=True)

    async def run_set(s, name):
        s.sem = asyncio.Semaphore(s.jobs)
        items = items_of(name)
        if s.limit: items = items[:s.limit]
        v = voice_of(name); cut = len(name) + 1   # 文件 = audio/<组>/<键>.mp3
        cpath = 'audio/%s/_crc.json' % name
        os.makedirs('audio/' + name, exist_ok=True)
        crc = json.load(open(cpath)) if os.path.exists(cpath) else {}
        todo = [(r, t) for r, t in items if not (crc.get(r[cut:]) == fp(t, v) and os.path.exists('audio/' + r + '.mp3'))]
        print('[%s] 共 %d 句，要生成 %d 句' % (name, len(items), len(todo)), flush=True)
        for i in range(0, len(todo), 400):
            await asyncio.gather(*[s.one(r, t, crc, r[cut:], v) for r, t in todo[i:i + 400]])
            wjson(cpath, crc)
            el = time.time() - s.t0
            print('  %s %d/%d · 已用 %.1f 分钟 · %.1f 句/秒 · 平均 %.1f KB/句' % (name, min(i + 400, len(todo)), len(todo), el / 60,
                  s.done / max(el, 1), s.bytes / 1024 / max(s.done, 1)), flush=True)
            if s.stop: break
        keys = {r[cut:] for r, t in items}
        for k in list(crc):
            if k not in keys: del crc[k]
        wjson(cpath, crc)
        ok = all(crc.get(r[cut:]) == fp(t, v) for r, t in items)   # 用了 --limit 时只看前几句（仅测试用）
        ix = json.load(open('audio/index.json')) if os.path.exists('audio/index.json') else {'voices': VOICES, 'sets': {}}
        if ok: ix['sets'][name] = {'n': len(items), 'h': hashlib.sha256(''.join(fp(t, v) for r, t in items).encode()).hexdigest()[:8]}
        else: ix['sets'].pop(name, None)
        wjson('audio/index.json', ix)
        print('[%s] %s' % (name, '完成 ✓ 已写入 audio/index.json' if ok else '还没做完（重跑本脚本会接着做）'), flush=True)
        return ok

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--sets', default='all'); ap.add_argument('--jobs', type=int, default=12)
    ap.add_argument('--max-minutes', type=float, default=0, help='到时间就停（做完手上的），方便分批提交')
    ap.add_argument('--limit', type=int, default=0, help='每组只做前几句（仅测试用）')
    a = ap.parse_args()
    if not os.path.exists('data/daily.json'): sys.exit('请在仓库根目录运行（找不到 data/daily.json）')
    names = []
    for n in a.sets.split(','):
        n = n.strip()
        uk = ['uk/daily'] + ['uk/' + w for w in WORK]
        names += {'all': ['daily'] + WORK + BOOKS + uk, 'work': WORK, 'books': BOOKS, 'uk': uk}.get(n, [n])
    names = [n for i, n in enumerate(names) if n and n not in names[:i] and n not in ('x', 'uk/x')]
    names = ['x'] + (['uk/x'] if any(n.startswith('uk/') for n in names) else []) + names
    if not os.path.exists('.nojekyll'): open('.nojekyll', 'w').close()   # 文件很多，跳过 Jekyll 让 Pages 部署快一些
    g = Gen(find_ffmpeg(), a.jobs, time.time() + a.max_minutes * 60 if a.max_minutes else 1e18)
    g.limit = a.limit; res = {}
    for n in names:
        res[n] = asyncio.run(g.run_set(n))
        if g.stop: break
    total = sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk('audio') for f in fs)
    print('本次：生成 %d 句，失败 %d 句，用时 %.1f 分钟；audio/ 现在共 %.1f MB' % (g.done, g.fail, (time.time() - g.t0) / 60, total / 1048576))
    print('完成的组：', ' '.join(k for k, v in res.items() if v) or '无', '｜没做完：', ' '.join(k for k in names if not res.get(k)) or '无')
    sys.exit(0 if all(res.get(k) for k in names) else 2)

if __name__ == '__main__':
    main()
