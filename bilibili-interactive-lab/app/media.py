"""Bilibili subtitles, scoped login cookies, media processing and transcription."""
import json
import os
import re
import shutil
import subprocess
import sys
import http.cookiejar
from pathlib import Path

import httpx
from app import services as s

MAX_UPLOAD = 200 * 1024 * 1024
MAX_DURATION = 7200
MEDIA_EXTENSIONS = {'.mp3', '.mp4', '.m4a', '.wav', '.webm', '.mov', '.mkv', '.aac', '.flac', '.ogg', '.mpeg', '.mpga'}
DEFAULTS = {'transcriber': 'local', 'local_model': 'base', 'speech_model': 'whisper-1', 'speech_key': ''}
_MODEL = None
_MODEL_NAME = None


class MediaError(s.UserError):
    def __init__(self, message, code='media_error', action=''):
        super().__init__(message)
        self.code = code
        self.action = action


def settings():
    return {**DEFAULTS, **s.read_json(s.DATA / 'media-config.json', {})}


def public_settings():
    c = settings()
    return {k: c[k] for k in ('transcriber', 'local_model', 'speech_model')} | {
        'has_speech_key': bool(c['speech_key']),
        'has_cookie': (s.DATA / 'bilibili-cookies.txt').exists(),
    }


def save_settings(transcriber, local_model, speech_model, speech_key, sessdata, clear_cookie=False):
    if transcriber not in ('local', 'openai') or local_model not in ('base', 'small'):
        raise s.UserError('请选择支持的转写方式和本地模型。')
    if speech_model not in ('whisper-1', 'gpt-4o-transcribe', 'gpt-4o-mini-transcribe'):
        raise s.UserError('请选择支持的 OpenAI 转写模型。')
    old = settings()
    c = {'transcriber': transcriber, 'local_model': local_model, 'speech_model': speech_model, 'speech_key': speech_key.strip() or old['speech_key']}
    cookie_path = s.DATA / 'bilibili-cookies.txt'
    sessdata = sessdata.strip()
    if sessdata and (len(sessdata) > 2048 or re.search(r'[\s;\r\n]', sessdata)):
        raise s.UserError('请只填写 SESSDATA 的值，不包含 Cookie 名称或其他字段。')
    with s.LOCK:
        s.write_json(s.DATA / 'media-config.json', c, private=True)
        if clear_cookie:
            cookie_path.unlink(missing_ok=True)
        elif sessdata:
            fd = os.open(str(cookie_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w') as f:
                f.write('# Netscape HTTP Cookie File\n.bilibili.com\tTRUE\t/\tTRUE\t0\tSESSDATA\t' + sessdata + '\n')
            cookie_path.chmod(0o600)
    return public_settings()


def snapshot():
    c = settings()
    if c['transcriber'] == 'openai' and not c['speech_key']:
        generation = s.config()
        if generation.get('provider') == 'openai':
            c['speech_key'] = generation.get('key', '')
    return c


def yt_args():
    newer = s.ROOT / '.downloader-venv' / 'bin' / 'python'
    executable = str(newer) if (newer.parent.parent / '.ready').exists() else sys.executable
    args = [executable, '-m', 'yt_dlp', '--ignore-config', '--no-playlist', '--socket-timeout', '20', '--retries', '1', '--no-warnings']
    if (s.DATA / 'bilibili-cookies.txt').exists():
        args += ['--cookies', str(s.DATA / 'bilibili-cookies.txt')]
    return args


def classify_download_error(stderr):
    text = stderr.lower()
    if '412' in text or 'precondition failed' in text or '-352' in text:
        if (s.DATA / 'bilibili-cookies.txt').exists():
            return MediaError('B 站仍然限制了这次请求（412），工具已使用你保存的登录信息。', 'bilibili_412', '不需要重复填写登录值。先在浏览器确认能观看；若仍受限，上传已取得的视频／音频继续。')
        return MediaError('B 站限制了这次请求（412）。这不是 AI 配置问题。', 'bilibili_412', '在下方配置 B 站登录状态后重试；仍受限时上传已取得的视频／音频。避免连续重复请求。')
    if any(word in text for word in ('login', 'logged', 'sign in', 'premium', 'members-only', '大会员')):
        return MediaError('这个视频需要有效的 B 站登录或观看权限。', 'login_required', '使用自己有观看权限的账号配置登录状态，或者上传你已取得的媒体文件。')
    if 'certificate' in text or 'ssl' in text:
        return MediaError('视频请求的 HTTPS 证书验证失败。', 'tls_error', '检查电脑网络或证书环境；不会关闭证书验证。也可以上传文件继续。')
    if '404' in text or 'not found' in text or 'not exist' in text:
        return MediaError('没有找到视频，可能已删除或链接不正确。', 'not_found', '在浏览器确认视频能打开，然后重新复制链接。')
    return MediaError('视频请求失败，可能是网络、权限或下载器兼容性问题。', 'download_failed', '先在浏览器确认可以观看，再配置登录状态；也可以上传视频／音频。')


def run_yt(args, timeout=120):
    try:
        result = subprocess.run(yt_args() + args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise MediaError('视频请求超时。', 'download_timeout', '稍后重试，或上传视频／音频。')
    except OSError:
        raise MediaError('视频获取工具无法启动。', 'downloader_missing', '请联系我检查本地环境。')
    if result.returncode:
        raise classify_download_error(result.stderr)
    return result.stdout


def bili_client():
    jar = http.cookiejar.MozillaCookieJar()
    cookie_path = s.DATA / 'bilibili-cookies.txt'
    if cookie_path.exists():
        jar.load(str(cookie_path), ignore_discard=True, ignore_expires=True)
        for cookie in jar:
            if cookie.expires == 0:
                cookie.expires = None
                cookie.discard = True
    return httpx.Client(cookies=jar, timeout=20, headers={
        'Referer': 'https://www.bilibili.com/', 'User-Agent': 'Mozilla/5.0',
    })


def bili_json(client, path, params=None):
    try:
        response = client.get('https://api.bilibili.com' + path, params=params)
        if response.status_code == 412:
            raise classify_download_error('HTTP Error 412')
        response.raise_for_status()
        body = response.json()
        if body.get('code') in (-101, -403):
            raise MediaError('B 站登录已失效或没有观看权限。', 'login_required', '重新登录 B 站并更新登录值，确认账号可以观看该视频。')
        if body.get('code') != 0:
            raise MediaError('B 站视频接口未返回可用信息。', 'bilibili_api_error', '先在浏览器确认视频可以观看，或上传视频／音频。')
        return body.get('data') or {}
    except httpx.HTTPError:
        raise MediaError('无法连接 B 站视频接口。', 'bilibili_network', '检查网络，或上传媒体文件继续。')
    except ValueError:
        raise MediaError('B 站接口返回了无法解析的内容。', 'metadata_invalid', '稍后重试或上传媒体文件。')


def check_login():
    if not (s.DATA / 'bilibili-cookies.txt').exists():
        raise s.UserError('还没有保存 B 站登录信息。')
    with bili_client() as client:
        nav = bili_json(client, '/x/web-interface/nav')
    return {'logged_in': bool(nav.get('isLogin')),
            'message': 'B 站登录有效。' if nav.get('isLogin') else 'B 站登录无效，请更新登录信息。'}


def video_info(url):
    # Read the site's normal video and subtitle APIs, rather than requiring
    # webpage extraction (which may be blocked even while these APIs work).
    video_id = url.rstrip('/').rsplit('/', 1)[-1]
    params = {'bvid': video_id} if video_id.startswith('BV') else {'aid': video_id[2:]}
    with bili_client() as client:
        data = bili_json(client, '/x/web-interface/view', params)
        cid = data.get('cid')
        if not cid:
            raise MediaError('视频接口没有返回播放器 ID。', 'metadata_invalid', '确认是普通 B 站单视频链接，或上传媒体文件。')
        info = {'title': data.get('title'), 'duration': data.get('duration'), 'subtitles': {}}
        player = bili_json(client, '/x/player/wbi/v2', {**params, 'cid': cid})
        for entry in (player.get('subtitle') or {}).get('subtitles') or []:
            subtitle_url = entry.get('subtitle_url')
            if subtitle_url:
                if subtitle_url.startswith('//'):
                    subtitle_url = 'https:' + subtitle_url
                if subtitle_url.startswith('https://'):
                    info['subtitles'].setdefault(entry.get('lan') or 'unknown', []).append({'ext': 'json', 'url': subtitle_url})
    duration = info.get('duration')
    if duration and float(duration) > MAX_DURATION:
        raise MediaError('当前个人版支持不超过两小时的媒体。', 'media_too_long', '请截取需要处理的内容后上传。')
    return info


def subtitles(info, url):
    choices = []
    for group in ('subtitles', 'automatic_captions'):
        for language, entries in (info.get(group) or {}).items():
            for entry in entries:
                if entry.get('data') and entry.get('ext') in ('srt', 'vtt', 'json'):
                    text = s.subtitle_text(entry['data'])
                    if len(text.strip()) >= 30:
                        return text
                if entry.get('url') and entry.get('ext') in ('json', 'srt', 'vtt'):
                    choices.append((0 if language.startswith(('zh', 'ai-zh')) else 1, entry))
    for _, entry in sorted(choices, key=lambda x: x[0]):
        try:
            with httpx.Client(timeout=30, follow_redirects=True) as client:
                response = client.get(entry['url'], headers={'Referer': url, 'User-Agent': 'Mozilla/5.0'})
                response.raise_for_status()
                if 'text/html' in response.headers.get('content-type', ''):
                    continue
                if entry.get('ext') == 'json':
                    obj = response.json()
                    if not isinstance(obj, dict) or not isinstance(obj.get('body'), list):
                        continue
                text = s.subtitle_text(response.text)
            if len(text.strip()) >= 30:
                return text
        except (httpx.HTTPError, ValueError):
            continue
    return ''


def download_audio(url, directory):
    raw = run_yt(['-f', 'bestaudio/best', '--max-filesize', '200M', '--no-part', '--restrict-filenames', '-o', str(directory / 'audio.%(ext)s'), '--print', 'after_move:filepath', url], timeout=900)
    # Only accept actual files under the task directory, never downloader-provided arbitrary paths.
    for line in reversed(raw.splitlines()):
        candidate = Path(line.strip())
        if candidate.parent.resolve() == directory.resolve() and candidate.is_file():
            if candidate.stat().st_size > MAX_UPLOAD:
                raise MediaError('媒体文件超过 200 MB。', 'media_too_large', '请上传压缩后的音频或截取片段。')
            return candidate
    raise MediaError('未取得音频文件。', 'audio_missing', '上传视频或音频文件继续。')


def ffmpeg_exe():
    value = shutil.which('ffmpeg')
    if value:
        return value
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError):
        raise MediaError('本机音频处理依赖尚未安装。', 'ffmpeg_missing', '请联系我检查 FFmpeg 环境。')


def prepare_audio(media, directory):
    # The input is a stored local file; protocols are restricted to prevent file content opening URLs.
    target = directory / 'normalized.mp3'
    try:
        result = subprocess.run([ffmpeg_exe(), '-nostdin', '-hide_banner', '-loglevel', 'error', '-protocol_whitelist', 'file,pipe', '-i', str(media), '-map', '0:a:0', '-vn', '-ac', '1', '-ar', '16000', '-c:a', 'libmp3lame', '-b:a', '48k', '-t', str(MAX_DURATION + 2), '-y', str(target)], capture_output=True, timeout=300)
    except subprocess.TimeoutExpired:
        raise MediaError('音频转换超时。', 'convert_timeout', '请上传较短的音频片段。')
    if result.returncode or not target.exists() or target.stat().st_size < 256:
        raise MediaError('无法从这个文件提取音频。', 'invalid_media', '请选择含有声音的有效视频或音频文件。')
    import av
    try:
        with av.open(str(target)) as container:
            duration = float(container.duration or 0) / av.time_base
    except Exception:
        raise MediaError('音频文件无法读取。', 'invalid_media', '请换一个媒体文件。')
    if duration > MAX_DURATION:
        raise MediaError('媒体超过两小时，尚未进行转写。', 'media_too_long', '请截取片段后再上传。')
    return target, duration


def transcribe_local(audio, c, progress):
    global _MODEL, _MODEL_NAME
    name = c['local_model']
    progress('加载本机 Whisper（首次使用需下载模型）', 27)
    try:
        from faster_whisper import WhisperModel
        if _MODEL is None or _MODEL_NAME != name:
            _MODEL = WhisperModel(name, device='cpu', compute_type='int8', download_root=str(s.DATA / 'models'))
            _MODEL_NAME = name
        progress('本机转写音频', 30)
        segments, info = _MODEL.transcribe(str(audio), beam_size=3, vad_filter=True)
        lines = []
        for segment in segments:
            lines.append(segment.text.strip())
            percent = min(38, 30 + int(8 * segment.end / max(info.duration, 1)))
            progress('本机转写音频', percent)
        text = '\n'.join(lines)
    except ImportError:
        raise MediaError('本机 Whisper 依赖未安装。', 'whisper_missing', '请联系我安装转写依赖，或改用 OpenAI 转写。')
    except Exception:
        raise MediaError('本机转写失败，可能是模型下载网络或运行资源问题。', 'local_transcription_failed', '检查网络后重试，或选择 OpenAI 转写。')
    return text


def transcribe_openai(audio, directory, c, progress):
    if not c.get('speech_key'):
        raise MediaError('OpenAI 转写尚未配置密钥。', 'speech_key_missing', '在视频获取设置中填写 OpenAI API Key；DeepSeek 的密钥不能用于 OpenAI 转写。')
    chunk_dir = directory / 'chunks'
    chunk_dir.mkdir(exist_ok=True)
    result = subprocess.run([ffmpeg_exe(), '-nostdin', '-hide_banner', '-loglevel', 'error', '-i', str(audio), '-f', 'segment', '-segment_time', '600', '-c', 'copy', '-y', str(chunk_dir / '%03d.mp3')], capture_output=True, timeout=120)
    if result.returncode:
        raise MediaError('音频分段失败。', 'segment_failed', '上传较短片段重试。')
    chunks = sorted(chunk_dir.glob('*.mp3'))
    if not chunks:
        raise MediaError('没有可转写的音频片段。', 'empty_audio', '请确认文件包含声音。')
    texts = []
    for i, chunk in enumerate(chunks):
        if chunk.stat().st_size >= 25 * 1024 * 1024:
            raise MediaError('音频片段超过转写服务大小限制。', 'speech_file_too_large', '上传较短的音频。')
        progress('OpenAI 转写音频：' + str(i + 1) + '/' + str(len(chunks)), 30 + int(8 * i / len(chunks)))
        try:
            with httpx.Client(timeout=httpx.Timeout(300, connect=20)) as client, chunk.open('rb') as stream:
                r = client.post('https://api.openai.com/v1/audio/transcriptions', headers={'Authorization': 'Bearer ' + c['speech_key']}, data={'model': c['speech_model'], 'response_format': 'json'}, files={'file': (chunk.name, stream, 'audio/mpeg')})
            if r.status_code >= 400:
                messages = {401: 'OpenAI 转写密钥无效。', 403: '账号无权使用 OpenAI 转写。', 429: 'OpenAI 转写余额或额度不足，或请求受限。'}
                raise MediaError(messages.get(r.status_code, 'OpenAI 转写请求失败。'), 'speech_api_error', '检查转写配置和余额，或改用本机 Whisper。已成功转写的片段会保留在本机。')
            text = r.json().get('text', '').strip()
            if not text:
                raise ValueError()
            texts.append(text)
            (chunk_dir / (chunk.stem + '.txt')).write_text(text)
        except httpx.HTTPError:
            raise MediaError('无法连接 OpenAI 转写服务。', 'speech_network', '检查网络，或改用本机 Whisper。')
        except ValueError:
            raise MediaError('转写服务没有返回可用文本。', 'speech_empty', '确认音频含有人声，或尝试其他转写模型。')
    return '\n'.join(texts)


def acquire(url, upload_path, directory, c, progress):
    title = '上传媒体交互工具' if upload_path else '视频交互工具'
    if upload_path:
        media = upload_path
    else:
        progress('读取 B 站视频信息', 10)
        info = video_info(url)
        title = info.get('title') or title
        progress('获取视频字幕', 18)
        text = subtitles(info, url)
        if text:
            return {'title': title, 'text': text, 'method': '视频字幕'}
        progress('没有可用字幕，正在下载音频', 22)
        media = download_audio(url, directory)
    progress('处理音频', 25)
    audio, duration = prepare_audio(media, directory)
    if c['transcriber'] == 'local':
        text = transcribe_local(audio, c, progress)
        method = '本机 Whisper 音频转写'
    else:
        text = transcribe_openai(audio, directory, c, progress)
        method = 'OpenAI 音频转写'
    if len(text.strip()) < 30:
        raise MediaError('转写文本太短，可能没有清晰的人声。', 'transcript_short', '检查音频内容，或粘贴文本继续。')
    return {'title': title, 'text': text, 'method': method, 'duration': duration}
