const $ = id => document.getElementById(id);
let selected = null, busy = false, submitting = false, loadedTextId = null;
let mediaConfig = null;
const links = {deepseek:'https://platform.deepseek.com/',openai:'https://platform.openai.com/',doubao:'https://console.volcengine.com/ark/apiKey'};

async function parseResponse(response) {
  let data;
  try { data = await response.json(); }
  catch { throw new Error('后台返回异常，请确认后台正在运行。'); }
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '请求未完成，请检查输入。');
  return data;
}
async function api(path, body) {
  const options = body === undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json','X-Workbench':'local'},body:JSON.stringify(body)};
  return parseResponse(await fetch(path, options));
}
function setButtons() {
  const disabled = busy || submitting;
  $('generateBtn').disabled = disabled;
  $('extractBtn').disabled = disabled;
  $('useText').disabled = disabled;
  $('generateBtn').textContent = disabled ? '正在处理，请稍候…' : '一键生成交互网页 →';
}
function providerLink() { $('providerLink').href = links[$('provider').value]; }
$('provider').addEventListener('change', () => {
  $('key').value = ''; $('model').value = '';
  $('key').placeholder = '填写该服务的 API Key';
  $('settingsStatus').textContent = '切换服务后，请填写对应的模型名称和密钥并保存。';
  providerLink();
});
async function loadConfig() {
  const c = await api('/api/config');
  $('provider').value = c.provider; $('model').value = c.model;
  $('configBadge').textContent = c.has_key ? '已保存配置' : '待配置';
  $('key').placeholder = c.has_key ? '密钥已保存；不更换时留空' : '粘贴你的 API Key';
  providerLink();
}
$('settings').addEventListener('submit', async event => {
  event.preventDefault(); const button = event.submitter; button.disabled = true;
  $('settingsStatus').textContent = '正在保存…';
  try {
    await api('/api/config', {provider:$('provider').value,model:$('model').value,key:$('key').value});
    $('key').value = ''; await loadConfig();
    $('settingsStatus').textContent = '已保存。现在点击“测试连接”。';
  } catch(error) { $('settingsStatus').textContent = error.message; }
  finally { button.disabled = false; }
});
$('test').addEventListener('click', async () => {
  $('test').disabled = true; $('settingsStatus').textContent = '正在测试已保存的配置…';
  try { $('settingsStatus').textContent = (await api('/api/config/test', {})).message; }
  catch(error) { $('settingsStatus').textContent = error.message; }
  finally { $('test').disabled = false; }
});
function speechFields() {
  const local = $('transcriber').value === 'local';
  $('localSpeech').hidden = !local; $('cloudSpeech').hidden = local;
}
$('transcriber').addEventListener('change', speechFields);
async function loadMediaConfig() {
  mediaConfig = await api('/api/media/config');
  $('transcriber').value = mediaConfig.transcriber;
  $('localModel').value = mediaConfig.local_model;
  $('speechModel').value = mediaConfig.speech_model;
  $('mediaBadge').textContent = (mediaConfig.transcriber === 'local' ? '本机转写' : 'OpenAI 转写') + (mediaConfig.has_cookie ? ' · 已有登录' : ' · 未配置登录');
  $('sessdata').placeholder = mediaConfig.has_cookie ? '登录值已保存；不更换时留空' : '可选：填写你自己的 SESSDATA 值';
  $('speechKey').placeholder = mediaConfig.has_speech_key ? '转写密钥已保存；不更换时留空' : '可复用 OpenAI 生成密钥，或单独填写';
  speechFields();
}
function mediaValues(clear = false) {
  return {transcriber:$('transcriber').value,local_model:$('localModel').value,speech_model:$('speechModel').value,speech_key:$('speechKey').value,sessdata:$('sessdata').value,clear_cookie:clear};
}
$('mediaSettings').addEventListener('submit', async event => {
  event.preventDefault(); const button = event.submitter; button.disabled = true;
  try {
    await api('/api/media/config', mediaValues());
    $('speechKey').value = ''; $('sessdata').value = '';
    await loadMediaConfig(); $('mediaStatus').textContent = '获取设置已保存。';
  } catch(error) { $('mediaStatus').textContent = error.message; }
  finally { button.disabled = false; }
});
$('checkLogin').addEventListener('click', async () => {
  $('checkLogin').disabled = true;
  $('mediaStatus').textContent = '正在检查已保存的登录信息…';
  try {
    const result = await api('/api/media/login/test', {});
    $('mediaStatus').textContent = result.message;
  } catch(error) { $('mediaStatus').textContent = error.message; }
  finally { $('checkLogin').disabled = false; }
});
$('forgetCookie').addEventListener('click', async () => {
  $('forgetCookie').disabled = true;
  try {
    const values = mediaValues(true); values.sessdata = ''; values.speech_key = '';
    await api('/api/media/config', values); $('sessdata').value = '';
    await loadMediaConfig(); $('mediaStatus').textContent = '已移除本机保存的 B 站登录信息。';
  } catch(error) { $('mediaStatus').textContent = error.message; }
  finally { $('forgetCookie').disabled = false; }
});
async function loadText(job) {
  if (!job.text_ready || loadedTextId === job.id) return;
  const response = await api('/api/jobs/' + job.id + '/text');
  if (selected !== job.id) return;
  loadedTextId = job.id; $('sourceText').value = response.text;
}
function renderJob(job) {
  $('result').hidden = false; $('resultTitle').textContent = job.title;
  $('resultStage').textContent = job.stage + (job.method ? ' · ' + job.method : '');
  $('progress').value = job.progress;
  $('resultError').textContent = [job.error, job.action].filter(Boolean).join(' 下一步：');
  const htmlDone = job.status === 'done' && Boolean(job.preview_url);
  $('download').hidden = !htmlDone; $('preview').hidden = !htmlDone; $('previewHint').hidden = !htmlDone;
  $('sourceResult').hidden = !job.text_ready;
  if (job.text_ready) {
    $('downloadText').href = '/api/jobs/' + job.id + '/text/download';
    loadText(job).catch(error => { $('resultError').textContent = error.message; });
  } else { $('sourceText').value = ''; }
  if (htmlDone) {
    $('download').href = job.download_url;
    if ($('preview').dataset.job !== job.id) { $('preview').src = job.preview_url; $('preview').dataset.job = job.id; }
  } else { $('preview').removeAttribute('src'); delete $('preview').dataset.job; }
  if (job.status === 'failed') {
    $('textOptions').open = true;
    if (['bilibili_412','login_required','speech_key_missing','local_transcription_failed'].includes(job.error_code)) $('mediaOptions').open = true;
  }
}
async function loadHistory() {
  const jobs = await api('/api/jobs');
  busy = jobs.some(job => ['running','queued'].includes(job.status)); setButtons();
  const container = $('history'); container.replaceChildren();
  if (!jobs.length) {
    const p = document.createElement('p'); p.className = 'empty'; p.textContent = '你的内容与作品会出现在这里。'; container.append(p);
  }
  for (const job of jobs) {
    const button = document.createElement('button'); button.className = 'work'; button.type = 'button';
    const title = document.createElement('strong'); title.textContent = job.title;
    const meta = document.createElement('span'); meta.textContent = (job.kind === 'source' ? '内容 · ' : '网页 · ') + job.stage + ' · ' + new Date(job.created_at).toLocaleString('zh-CN');
    button.append(title, meta);
    button.addEventListener('click', () => { selected = job.id; renderJob(job); $('result').scrollIntoView({behavior:'smooth'}); });
    container.append(button);
  }
  if (selected) { const job = jobs.find(item => item.id === selected); if (job) renderJob(job); }
  else { const job = jobs.find(item => ['running','queued'].includes(item.status)); if (job) {selected = job.id; renderJob(job);} }
}
async function submitTask(extractOnly, sourceId = '') {
  if (busy || submitting) return;
  submitting = true; setButtons(); $('generateStatus').textContent = '正在提交任务…';
  try {
    let uploadId = '';
    const file = $('mediaFile').files[0];
    if (file && !$('transcript').value.trim() && !sourceId) {
      if (file.size > 200 * 1024 * 1024) throw new Error('文件超过 200 MB，请上传压缩音频或较短片段。');
      $('generateStatus').textContent = '正在上传文件到本机后台…';
      const uploaded = await parseResponse(await fetch('/api/media/upload?filename=' + encodeURIComponent(file.name), {method:'POST',headers:{'Content-Type':'application/octet-stream','X-Workbench':'local'},body:file}));
      uploadId = uploaded.upload_id;
    }
    const job = await api('/api/jobs', {url:sourceId ? '' : $('url').value,title:$('title').value,transcript:sourceId ? '' : $('transcript').value,instructions:$('instructions').value,upload_id:uploadId,source_id:sourceId,extract_only:extractOnly});
    selected = job.id; renderJob(job);
    $('generateStatus').textContent = extractOnly ? '正在获取内容。完成后可检查文本，再生成网页。' : '任务已提交，完成后会自动保存。';
    await loadHistory(); $('result').scrollIntoView({behavior:'smooth'});
  } catch(error) { $('generateStatus').textContent = error.message; }
  finally { submitting = false; setButtons(); }
}
$('generate').addEventListener('submit', event => {event.preventDefault(); submitTask(false);});
$('extractBtn').addEventListener('click', () => submitTask(true));
$('useText').addEventListener('click', () => {if (selected) submitTask(false, selected);});
$('refresh').addEventListener('click', () => loadHistory().catch(error => {$('generateStatus').textContent = error.message;}));
async function start() {
  try {
    await loadConfig(); await loadMediaConfig(); await loadHistory();
    setInterval(() => loadHistory().catch(() => {$('generateStatus').textContent = '后台连接中断，请等待恢复或联系我。';}), 2500);
  } catch(error) { $('settingsStatus').textContent = error.message; }
}
start();
