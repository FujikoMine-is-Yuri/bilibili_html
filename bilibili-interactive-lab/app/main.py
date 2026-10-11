"""A local-only video-to-interactive-HTML workbench."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app import services as s
from app import media as m

app = FastAPI(title='视频交互实验室')
app.add_middleware(TrustedHostMiddleware, allowed_hosts=['127.0.0.1', 'localhost', 'testserver'])
app.mount('/static', StaticFiles(directory=s.ROOT / 'static'), name='static')
POOL = ThreadPoolExecutor(max_workers=1)


@app.middleware('http')
async def local_requests(request: Request, call_next):
    if request.url.path.startswith('/api/') and request.method != 'GET':
        origin = request.headers.get('origin')
        expected = str(request.base_url).rstrip('/')
        if request.headers.get('x-workbench') != 'local' or (origin and origin != expected):
            return JSONResponse({'detail': '请从本地工具页面发起操作。'}, status_code=403)
    response = await call_next(request)
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response


@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    return JSONResponse({'detail': '输入格式不正确或内容过长，请检查输入。'}, status_code=422)


@app.exception_handler(s.UserError)
async def user_error(request, exc):
    return JSONResponse({'detail': str(exc)}, status_code=400)


class ConfigInput(BaseModel):
    provider: str = Field(max_length=30)
    model: str = Field(max_length=160)
    key: str = Field(default='', max_length=2048)


class JobInput(BaseModel):
    url: str = Field(default='', max_length=2048)
    title: str = Field(default='', max_length=160)
    transcript: str = Field(default='', max_length=40000)
    instructions: str = Field(default='', max_length=2000)
    upload_id: str = Field(default='', max_length=36)
    source_id: str = Field(default='', max_length=36)
    extract_only: bool = False


class MediaConfigInput(BaseModel):
    transcriber: str = 'local'
    local_model: str = 'base'
    speech_model: str = 'whisper-1'
    speech_key: str = Field(default='', max_length=2048)
    sessdata: str = Field(default='', max_length=2048)
    clear_cookie: bool = False


def upload_path(upload_id):
    try:
        if str(uuid.UUID(upload_id)) != upload_id:
            raise ValueError()
    except ValueError:
        raise s.UserError('上传文件不存在，请重新上传。')
    metadata = s.read_json(s.DATA / 'uploads' / upload_id / 'metadata.json', None)
    if not metadata:
        raise s.UserError('上传文件不存在，请重新上传。')
    return s.DATA / 'uploads' / upload_id / metadata['stored_name']


def job_dir(job_id):
    try:
        if str(uuid.UUID(job_id)) != job_id:
            raise ValueError()
    except ValueError:
        raise HTTPException(404, '作品不存在。')
    return s.DATA / 'jobs' / job_id


def job_data(job_id):
    data = s.read_json(job_dir(job_id) / 'job.json', None)
    if data is None:
        raise HTTPException(404, '作品不存在。')
    return data


def update_job(job_id, **fields):
    with s.LOCK:
        data = job_data(job_id)
        data.update(fields)
        s.write_json(job_dir(job_id) / 'job.json', data)


def run_job(job_id, source, settings, media_settings):
    directory = job_dir(job_id)
    try:
        text = source.transcript.strip()
        title = source.title.strip() or '我的交互工具'
        method = '粘贴文本'
        if not text:
            media_file = upload_path(source.upload_id) if source.upload_id else None
            result = m.acquire(source.url, media_file, directory, media_settings,
                               lambda stage, progress: update_job(job_id, stage=stage, progress=progress))
            title = source.title.strip() or result['title']
            text = result['text']
            method = result['method']
        if len(text) < 30:
            raise s.UserError('文本太短，请提供至少 30 个字符的字幕或内容。')
        (directory / 'transcript.txt').write_text(text)
        update_job(job_id, title=title, text_ready=True, method=method)
        if source.extract_only:
            update_job(job_id, status='done', stage='内容获取完成', progress=100)
            return
        if len(text) > 40000:
            raise s.UserError('文本超过生成的 4 万字符上限，内容已保留。请选取主要部分粘贴后生成。')
        update_job(job_id, title=title, stage='设计交互功能', progress=40)
        context = json.dumps({'title': title, 'source_url': source.url, 'transcript': text, 'content_method': method,
                              'note': '音频转写可能有错字，勿将转写推断为经验证事实。'}, ensure_ascii=False)
        design = s.call_model(settings, s.DESIGN_PROMPT + '\n只设计2到3个核心模块，控制在800字以内。包含关键观点、必要参数与来源，供下一步直接实现，不复述全文。', '用户要求：' + source.instructions + '\n资料：' + context, limit=2200)
        (directory / 'design.txt').write_text(design)
        update_job(job_id, stage='生成完整网页', progress=65)
        brief = json.dumps({'title': title, 'source_url': source.url, 'content_method': method}, ensure_ascii=False)
        html = s.clean_html(s.call_model(settings, s.HTML_PROMPT + '\n控制完整代码在6000 token以内。使用简洁可维护的CSS和JS，避免重复样式、长篇文章、装饰性SVG与复杂动画。优先完整实现核心交互并闭合文档。', '用户要求：' + source.instructions + '\n工具设计：' + design + '\n来源：' + brief, limit=10000))
        update_job(job_id, stage='保存网页', progress=95)
        (directory / 'index.html').write_text(html)
        update_job(job_id, status='done', stage='生成完成', progress=100, preview_url='/api/jobs/' + job_id + '/preview', download_url='/api/jobs/' + job_id + '/download')
    except s.UserError as exc:
        update_job(job_id, status='failed', stage='需要处理', error=str(exc),
                   error_code=getattr(exc, 'code', 'generation_error'), action=getattr(exc, 'action', ''))
    except Exception:
        update_job(job_id, status='failed', stage='需要处理', error='任务未完成。已保存的内容仍在本机，请重试或联系我检查。')


@app.on_event('startup')
def recover_jobs():
    for path in (s.DATA / 'jobs').glob('*/job.json'):
        data = s.read_json(path, {})
        if data.get('status') in ('queued', 'running'):
            data.update(status='failed', stage='任务已中断', error='后台重启导致任务中断，请重新生成。')
            s.write_json(path, data)


@app.get('/')
def index():
    return FileResponse(s.ROOT / 'static' / 'index.html')


@app.get('/api/health')
def health():
    return {'status': 'ok', 'generation_ready': s.public_config()['has_key']}


@app.get('/api/config')
def get_config():
    return s.public_config()


@app.post('/api/config')
def set_config(value: ConfigInput):
    return s.save_config(value.provider, value.model, value.key)


@app.post('/api/config/test')
def test_config():
    s.call_model(s.config(), '只回答 OK。', '连接测试', limit=128)
    return {'message': '模型连接成功，可以生成网页了。'}


@app.get('/api/media/config')
def get_media_config():
    return m.public_settings()


@app.post('/api/media/config')
def set_media_config(value: MediaConfigInput):
    return m.save_settings(**value.model_dump())


@app.post('/api/media/login/test')
def test_bilibili_login():
    return m.check_login()


@app.post('/api/media/upload', status_code=201)
async def upload_media(request: Request, filename: str):
    suffix = Path(filename).suffix.lower()
    if suffix not in m.MEDIA_EXTENSIONS:
        raise s.UserError('请选择支持的视频或音频文件，例如 MP4、MP3、M4A、WAV。')
    upload_id = str(uuid.uuid4())
    directory = s.DATA / 'uploads' / upload_id
    directory.mkdir(parents=True)
    target = directory / ('media' + suffix)
    size = 0
    try:
        with target.open('wb') as stream:
            async for chunk in request.stream():
                size += len(chunk)
                if size > m.MAX_UPLOAD:
                    raise s.UserError('文件超过 200 MB，请上传压缩音频或较短片段。')
                stream.write(chunk)
        if size < 256:
            raise s.UserError('文件为空或过小，请重新选择媒体文件。')
        s.write_json(directory / 'metadata.json', {'stored_name': target.name, 'size': size})
    except BaseException:
        target.unlink(missing_ok=True)
        directory.rmdir()
        raise
    return {'upload_id': upload_id, 'size': size}


@app.get('/api/jobs')
def list_jobs():
    jobs = [s.read_json(path, {}) for path in (s.DATA / 'jobs').glob('*/job.json')]
    return sorted(jobs, key=lambda x: x.get('created_at', ''), reverse=True)


@app.post('/api/jobs', status_code=202)
def create_job(value: JobInput):
    value.url = s.canonical_video(value.url)
    if value.source_id:
        previous = job_data(value.source_id)
        text_path = job_dir(value.source_id) / 'transcript.txt'
        if not previous.get('text_ready') or not text_path.exists():
            raise s.UserError('这个任务没有已保存文本，请重新获取内容。')
        value.transcript = text_path.read_text()
        value.title = value.title or previous['title']
        value.url = value.url or previous.get('url', '')
    if value.upload_id:
        upload_path(value.upload_id)
    if not value.url and not value.transcript.strip() and not value.upload_id:
        raise s.UserError('请填写 B 站链接、上传媒体文件或粘贴视频文本。')
    settings = s.config()
    if not value.extract_only and (not settings.get('key') or not settings.get('model')):
        raise s.UserError('请先保存模型设置。')
    if value.transcript.strip() and len(value.transcript.strip()) < 30:
        raise s.UserError('文本太短，请提供至少 30 个字符的内容。')
    with s.LOCK:
        if any(x.get('status') in ('queued', 'running') for x in list_jobs()):
            raise s.UserError('当前已有一个生成任务，请等它完成后再生成。')
        job_id = str(uuid.uuid4())
        directory = job_dir(job_id)
        directory.mkdir(parents=True)
        data = {'id': job_id, 'kind': 'source' if value.extract_only else 'html', 'text_ready': False,
                'title': value.title.strip() or '新的视频工具', 'url': value.url, 'provider': settings['provider'], 'model': settings['model'], 'status': 'running', 'stage': '准备获取内容' if value.extract_only else '准备生成', 'progress': 5, 'created_at': datetime.now(timezone.utc).isoformat(), 'error': ''}
        s.write_json(directory / 'job.json', data)
        s.write_json(directory / 'source.json', value.model_dump())
        POOL.submit(run_job, job_id, value, settings, m.snapshot())
    return data


@app.get('/api/jobs/{job_id}')
def get_job(job_id: str):
    return job_data(job_id)


@app.get('/api/jobs/{job_id}/text')
def get_text(job_id: str):
    job = job_data(job_id)
    if not job.get('text_ready'):
        raise HTTPException(409, '文本尚未获取完成。')
    return {'text': (job_dir(job_id) / 'transcript.txt').read_text(), 'title': job['title'], 'method': job.get('method', '')}


@app.get('/api/jobs/{job_id}/text/download')
def download_text(job_id: str):
    if not job_data(job_id).get('text_ready'):
        raise HTTPException(409, '文本尚未获取完成。')
    return FileResponse(job_dir(job_id) / 'transcript.txt', filename='transcript-' + job_id[:8] + '.txt')


@app.get('/api/jobs/{job_id}/preview')
def preview(job_id: str):
    if job_data(job_id)['status'] != 'done' or not (job_dir(job_id) / 'index.html').exists():
        raise HTTPException(409, '网页尚未生成。')
    return FileResponse(job_dir(job_id) / 'index.html', media_type='text/html', headers={
        'Content-Security-Policy': "sandbox allow-scripts allow-downloads; default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; connect-src 'none'; form-action 'none'",
    })


@app.get('/api/jobs/{job_id}/download')
def download(job_id: str):
    if job_data(job_id)['status'] != 'done' or not (job_dir(job_id) / 'index.html').exists():
        raise HTTPException(409, '网页尚未生成。')
    return FileResponse(job_dir(job_id) / 'index.html', media_type='application/octet-stream', filename='interactive-' + job_id[:8] + '.html')
