import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

from fastapi.testclient import TestClient
from app import services as s
from app import media as m
from app.main import app


class WorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.patcher = patch.object(s, 'DATA', Path(self.temp.name))
        self.patcher.start()
        self.client = TestClient(app)
        self.headers = {'X-Workbench': 'local'}

    def tearDown(self):
        self.client.close()
        self.patcher.stop()
        self.temp.cleanup()

    def configure(self):
        r = self.client.post('/api/config', headers=self.headers, json={'provider': 'deepseek', 'model': 'test', 'key': 'test-secret'})
        self.assertEqual(r.status_code, 200)
        self.assertNotIn('test-secret', r.text)
        self.assertEqual((s.DATA / 'config.json').stat().st_mode & 0o777, 0o600)

    def test_configuration_and_request_isolation(self):
        self.assertEqual(self.client.post('/api/config', json={}).status_code, 403)
        self.assertEqual(self.client.post('/api/config', headers={**self.headers, 'Origin': 'null'}, json={}).status_code, 403)
        self.configure()
        self.assertNotIn('test-secret', self.client.get('/api/config').text)
        self.assertEqual(self.client.get('/api/jobs/not-a-uuid').status_code, 404)
        self.assertEqual(self.client.post('/api/jobs', headers=self.headers, json={'url': 'https://localhost/private'}).status_code, 400)

    def test_generation_save_preview_download(self):
        self.configure()
        html = '<!doctype html><html><body><button>测试</button></body></html>'
        with patch.object(s, 'call_model', side_effect=['测试交互设计', html]):
            r = self.client.post('/api/jobs', headers=self.headers, json={'title': '测试作品', 'transcript': '这是用于验证生成流程的视频资料。' * 5})
            self.assertEqual(r.status_code, 202)
            job_id = r.json()['id']
            for _ in range(100):
                job = self.client.get('/api/jobs/' + job_id).json()
                if job['status'] != 'running':
                    break
                time.sleep(.01)
            self.assertEqual(job['status'], 'done', job)
        preview = self.client.get(job['preview_url'])
        self.assertIn('sandbox allow-scripts', preview.headers['content-security-policy'])
        self.assertEqual(preview.text, html)
        self.assertIn('attachment', self.client.get(job['download_url']).headers['content-disposition'])
        self.assertNotIn('test-secret', (s.DATA / 'jobs' / job_id / 'job.json').read_text())

    def test_errors_are_saved_without_fake_success(self):
        self.configure()
        with patch.object(m, 'acquire', side_effect=m.MediaError('没有字幕', 'no_subtitle', '上传媒体')):
            r = self.client.post('/api/jobs', headers=self.headers, json={'url': 'https://www.bilibili.com/video/BV1FCopBrEy9/'})
            job_id = r.json()['id']
            for _ in range(100):
                job = self.client.get('/api/jobs/' + job_id).json()
                if job['status'] != 'running':
                    break
                time.sleep(.01)
            self.assertEqual(job['status'], 'failed')
            self.assertEqual(job['error'], '没有字幕')
            self.assertEqual(self.client.get('/api/jobs/' + job_id + '/preview').status_code, 409)

    def test_text_and_html_parsing(self):
        self.assertEqual(s.subtitle_text('{"body":[{"content":"你好"}]}'), '你好')
        self.assertEqual(s.subtitle_text('WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n你好'), '你好')
        self.assertEqual(s.clean_html('```html\n<html></html>\n```'), '<html></html>')
        with self.assertRaises(s.UserError):
            s.clean_html('<html>未完成')

    def test_deepseek_disables_thinking_and_rejects_truncation(self):
        response = Mock(status_code=200)
        response.json.return_value = {'choices':[{'finish_reason':'stop','message':{'content':'完成'}}]}
        client = Mock()
        client.__enter__ = Mock(return_value=client)
        client.__exit__ = Mock(return_value=False)
        client.post.return_value = response
        with patch.object(s.httpx, 'Client', return_value=client):
            self.assertEqual(s.call_model({'provider':'deepseek','model':'test','key':'test'}, '系统', '输入'), '完成')
            self.assertEqual(client.post.call_args.kwargs['json']['thinking'], {'type':'disabled'})
            response.json.return_value['choices'][0]['finish_reason'] = 'length'
            with self.assertRaises(s.UserError):
                s.call_model({'provider':'deepseek','model':'test','key':'test'}, '系统', '输入')


if __name__ == '__main__':
    unittest.main()
