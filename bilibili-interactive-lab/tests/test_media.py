import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

from fastapi.testclient import TestClient
from app import services as s
from app import media as m
from app.main import app


class MediaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.patcher = patch.object(s, 'DATA', self.root)
        self.patcher.start()
        self.client = TestClient(app)
        self.headers = {'X-Workbench': 'local'}

    def tearDown(self):
        self.client.close()
        self.patcher.stop()
        self.temp.cleanup()

    def wait_job(self, job_id):
        for _ in range(100):
            job = self.client.get('/api/jobs/' + job_id).json()
            if job['status'] != 'running': return job
            time.sleep(.01)
        self.fail('task did not complete')

    def test_normal_video_api_maps_subtitles(self):
        client = Mock()
        client.__enter__ = Mock(return_value=client)
        client.__exit__ = Mock(return_value=False)
        with patch.object(m, 'bili_client', return_value=client), patch.object(m, 'bili_json', side_effect=[
            {'cid':123, 'title':'测试视频', 'duration':60},
            {'subtitle':{'subtitles':[{'lan':'zh-CN', 'subtitle_url':'//example.com/subtitle.json'}]}}
        ]) as request:
            info = m.video_info('https://www.bilibili.com/video/BV1FCopBrEy9/')
        self.assertEqual(info['subtitles']['zh-CN'][0]['url'], 'https://example.com/subtitle.json')
        self.assertEqual(request.call_args_list[1].args[2]['cid'], 123)

    def test_inline_subtitle_does_not_need_network(self):
        content = '这是一段足够长的字幕内容，用来验证下载器直接提供字幕时可以正常使用。' * 2
        with patch.object(m.httpx, 'Client') as client:
            result = m.subtitles({'subtitles':{'zh':[{'ext':'srt','data':content}]}}, 'https://www.bilibili.com/')
        self.assertIn(content, result)
        client.assert_not_called()

    def test_cookie_and_speech_key_are_scoped_and_not_exposed(self):
        values = {'transcriber':'local', 'local_model':'base', 'speech_model':'whisper-1', 'speech_key':'secret-speech', 'sessdata':'secret-cookie'}
        response = self.client.post('/api/media/config', headers=self.headers, json=values)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('secret', response.text)
        self.assertNotIn('secret', self.client.get('/api/media/config').text)
        cookie = self.root / 'bilibili-cookies.txt'
        self.assertEqual(cookie.stat().st_mode & 0o777, 0o600)
        self.assertIn('.bilibili.com\tTRUE', cookie.read_text())
        values.update(sessdata='', speech_key='', clear_cookie=True)
        self.client.post('/api/media/config', headers=self.headers, json=values)
        self.assertFalse(cookie.exists())
        self.assertEqual(m.settings()['speech_key'], 'secret-speech')
        values.update(clear_cookie=False, sessdata='bad;cookie')
        self.assertEqual(self.client.post('/api/media/config', headers=self.headers, json=values).status_code, 400)

    def test_independent_extraction_does_not_need_generation_key(self):
        transcript = '这是足够长的测试视频文本，用于验证内容获取功能无需模型密钥。' * 3
        result = {'title':'测试视频', 'text':transcript, 'method':'视频字幕'}
        with patch.object(m, 'acquire', return_value=result), patch.object(s, 'call_model') as model:
            r = self.client.post('/api/jobs', headers=self.headers, json={'url':'https://www.bilibili.com/video/BV1FCopBrEy9/', 'extract_only':True})
            self.assertEqual(r.status_code, 202)
            job = self.wait_job(r.json()['id'])
            self.assertEqual(job['status'], 'done')
            model.assert_not_called()
        self.assertEqual(self.client.get('/api/jobs/' + job['id'] + '/text').json()['text'], transcript)
        self.assertEqual(self.client.get('/api/jobs/' + job['id'] + '/preview').status_code, 409)
        self.assertIn('attachment', self.client.get('/api/jobs/' + job['id'] + '/text/download').headers['content-disposition'])
        s.save_config('deepseek', 'test', 'test-key')
        with patch.object(m, 'acquire') as acquire, patch.object(s, 'call_model', side_effect=['设计', '<html><body>完成</body></html>']):
            r = self.client.post('/api/jobs', headers=self.headers, json={'source_id':job['id']})
            next_job = self.wait_job(r.json()['id'])
            self.assertEqual(next_job['status'], 'done')
            acquire.assert_not_called()

    def test_412_has_clear_action_and_does_not_trigger_transcription(self):
        error = m.classify_download_error('HTTP Error 412: Precondition Failed')
        self.assertEqual(error.code, 'bilibili_412')
        with patch.object(m, 'video_info', side_effect=error), patch.object(m, 'transcribe_local') as local:
            with self.assertRaises(m.MediaError): m.acquire('https://www.bilibili.com/video/BV1FCopBrEy9/', None, self.root, m.DEFAULTS, lambda *args: None)
            local.assert_not_called()

    def test_no_subtitles_falls_back_to_audio(self):
        audio = self.root / 'audio.mp3'
        with patch.object(m, 'video_info', return_value={'title':'无字幕视频'}), patch.object(m, 'subtitles', return_value=''), patch.object(m, 'download_audio', return_value=audio) as download, patch.object(m, 'prepare_audio', return_value=(audio, 30)), patch.object(m, 'transcribe_local', return_value='真实的转写结果占位测试。' * 5):
            result = m.acquire('https://www.bilibili.com/video/BV1FCopBrEy9/', None, self.root, m.DEFAULTS, lambda *args: None)
            download.assert_called_once()
            self.assertEqual(result['method'], '本机 Whisper 音频转写')

    def test_upload_validation_and_upload_path_is_safe(self):
        self.assertEqual(self.client.post('/api/media/upload?filename=a.exe', headers=self.headers, content=b'x'*300).status_code, 400)
        r = self.client.post('/api/media/upload?filename=../../a.mp4', headers=self.headers, content=b'x'*300)
        self.assertEqual(r.status_code, 201)
        upload_id = r.json()['upload_id']
        self.assertTrue((self.root / 'uploads' / upload_id / 'media.mp4').exists())
        with patch.object(m, 'MAX_UPLOAD', 100):
            r = self.client.post('/api/media/upload?filename=a.mp4', headers=self.headers, content=b'x'*300)
            self.assertEqual(r.status_code, 400)
        # An uploaded media bypasses Bilibili fetching.
        audio = self.root / 'audio.mp3'
        with patch.object(m, 'video_info') as info, patch.object(m, 'prepare_audio', return_value=(audio, 30)), patch.object(m, 'transcribe_local', return_value='上传文件转写内容。'*10):
            r = self.client.post('/api/jobs', headers=self.headers, json={'upload_id':upload_id, 'extract_only':True})
            job = self.wait_job(r.json()['id'])
            self.assertEqual(job['status'], 'done')
            info.assert_not_called()


if __name__ == '__main__': unittest.main()
