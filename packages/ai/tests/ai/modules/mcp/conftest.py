# Copyright 2026 Aparavi Software AG. MIT License.
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[4] / 'src'))

import pytest


class FakeEngineClient:
    def __init__(
        self,
        tasks=None,
        nodes=None,
        token='tok-1',
        result='RESULT',
        services=None,
        service_defs=None,
        validate_result=None,
        task_statuses=None,
        public_token='pub-1',
        base_url='http://localhost:5565',
        fs_stat_result=None,
        fs_get_url_result='https://signed.example/f?sig=abc',
        deploy_status_result=None,
    ):
        self._tasks = (
            tasks
            if tasks is not None
            else [
                {'name': 'my_pipeline', 'description': 'A running pipeline', 'token': 'live-tok'},
            ]
        )
        self._nodes = nodes if nodes is not None else [{'type': 'parse'}]
        self._token = token
        self._result = result
        self._public_token = public_token
        self.base_url = base_url
        self.sent = []
        self.used = []
        self.terminated = []
        self.sent_files = []
        self.saved_templates = []
        self._template_store = {}
        self.deploys_added = []
        self.list_tasks_calls = 0
        self.deploy_list_calls = 0
        self._fs_stat_result = (
            fs_stat_result
            if fs_stat_result is not None
            else {'exists': True, 'type': 'file', 'size': 12, 'modified': 1700000000}
        )
        self._fs_get_url_result = fs_get_url_result
        self._deploy_status_result = (
            deploy_status_result if deploy_status_result is not None else {'project_id': 'dep-1', 'state': 'active'}
        )
        self.fs_stat_calls = []
        self.fs_get_url_calls = []
        self.deploy_status_calls = []
        self.deploy_removed = []
        self.deploy_updated = []
        self.add_monitor_calls = []
        self.log_chapters_result = {'chapters': [], 'horizonSeq': 0}
        self.log_read_result = {'events': [], 'nextSeq': None}
        self.log_traces_result = {'open': [], 'closed': []}
        self.log_trace_result = {'summary': None, 'events': []}
        self.log_calls = []

        # -- introspection (list_components / describe_component / validate) --
        self._services = (
            services
            if services is not None
            else {
                'services': {
                    'ocr': {
                        'title': 'OCR',
                        'protocol': 'ocr',
                        'classType': ['source'],
                        'description': 'Optical character recognition component',
                    },
                    'anthropic': {
                        'title': 'Anthropic LLM',
                        'protocol': 'anthropic',
                        'classType': ['agent', 'tool'],
                        'description': 'Anthropic-backed LLM component',
                    },
                },
                'version': 'x',
            }
        )
        self._service_defs = service_defs if service_defs is not None else dict(self._services.get('services', {}))
        self._validate_result = validate_result if validate_result is not None else {'errors': [], 'warnings': []}
        self.get_services_calls = 0
        self.get_service_calls = []
        self.validate_calls = []

        # -- visibility (monitor / get_task_status) --
        # A list of scripted responses, consumed one per call; the last
        # entry repeats once exhausted. Each entry is either a status dict
        # or an Exception instance/class to be raised.
        self._task_statuses = list(task_statuses) if task_statuses is not None else [{'state': 5, 'completed': True}]
        self.get_task_status_calls = []

    async def list_tasks(self):
        self.list_tasks_calls += 1
        return list(self._tasks)

    async def list_nodes(self):
        return list(self._nodes)

    async def send(self, token, data, objinfo=None, mimetype=None, on_sse=None):
        self.sent.append({'token': token, 'data': data, 'objinfo': objinfo, 'mimetype': mimetype})
        return self._result

    async def get_services(self):
        self.get_services_calls += 1
        return self._services

    async def get_service(self, name):
        self.get_service_calls.append(name)
        definition = self._service_defs.get(name)
        if definition is None:
            return None
        return {'name': name, **definition}

    async def validate(self, pipeline, source=None):
        self.validate_calls.append({'pipeline': pipeline, 'source': source})
        return dict(self._validate_result)

    async def use(self, **kwargs):
        self.used.append(kwargs)
        return {
            'token': self._token,
            'publicToken': self._public_token,
            'id': 'abcd1234.websrc',
            'projectId': 'proj-fake',
            'source': 'src-fake',
            **kwargs,
        }

    async def terminate(self, token):
        self.terminated.append(token)

    async def send_files(self, files, token):
        self.sent_files.append({'files': files, 'token': token})
        return {'uploaded': len(files)}

    async def fs_read_string(self, path):
        return 'file contents'

    async def fs_list_dir(self, path=''):
        return {'entries': []}

    async def save_template(self, template_id, pipeline):
        # Mirrors rocketride.mixins.store.save_template: writes the bare
        # pipeline dict to `.templates/<id>.json`, with no wrapping record.
        self.saved_templates.append({'template_id': template_id, 'pipeline': pipeline})
        self._template_store[template_id] = pipeline

    async def get_template(self, template_id):
        # Mirrors rocketride.mixins.store.get_template: reads back the bare
        # pipeline dict saved above -- a symmetric, unwrapped round-trip.
        return self._template_store.get(template_id)

    async def deploy_add(self, pipeline, schedule=None):
        self.deploys_added.append({'pipeline': pipeline, 'schedule': schedule})
        return {'project_id': 'dep-1'}

    async def deploy_list(self):
        self.deploy_list_calls += 1
        return [{'project_id': 'dep-1'}]

    async def get_task_status(self, token):
        self.get_task_status_calls.append(token)
        index = min(len(self.get_task_status_calls) - 1, len(self._task_statuses) - 1)
        response = self._task_statuses[index]
        if isinstance(response, BaseException):
            raise response
        if isinstance(response, type) and issubclass(response, BaseException):
            raise response()
        return dict(response)

    async def fs_stat(self, path):
        self.fs_stat_calls.append(path)
        return dict(self._fs_stat_result)

    async def fs_get_url(self, path, expires_in=3600, download_name=None):
        self.fs_get_url_calls.append({'path': path, 'expires_in': expires_in, 'download_name': download_name})
        return self._fs_get_url_result

    async def deploy_status(self, project_id):
        self.deploy_status_calls.append(project_id)
        return dict(self._deploy_status_result)

    async def deploy_remove(self, project_id):
        self.deploy_removed.append(project_id)

    async def deploy_update(self, project_id, pipeline=None, schedule=None):
        self.deploy_updated.append({'project_id': project_id, 'pipeline': pipeline, 'schedule': schedule})

    async def add_monitor(self, key, types):
        self.add_monitor_calls.append((key, types))

    async def log_chapters(self, project_id, source, run_kind='dev'):
        self.log_calls.append(('chapters', project_id, source, run_kind))
        return self.log_chapters_result

    async def log_read(
        self, project_id, source, run_kind='dev', *, from_seq=None, cursor=None, max_events=None, types=None
    ):
        self.log_calls.append(('read', project_id, source, run_kind, from_seq, cursor, max_events, types))
        return self.log_read_result

    async def log_traces(self, project_id, source, run_kind='dev', *, n=20, chapter_begin_seq=None):
        self.log_calls.append(('traces', project_id, source, run_kind, n, chapter_begin_seq))
        if isinstance(self.log_traces_result, Exception):
            raise self.log_traces_result
        return self.log_traces_result

    async def log_trace(self, project_id, source, run_kind='dev', *, begin_seq=0):
        self.log_calls.append(('trace', project_id, source, run_kind, begin_seq))
        if isinstance(self.log_trace_result, Exception):
            raise self.log_trace_result
        return self.log_trace_result


@pytest.fixture
def fake_engine():
    return FakeEngineClient()
