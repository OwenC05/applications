"""Static deployment boundaries, not a Docker runtime qualification."""
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_worker_is_separate_unexposed_and_uses_the_same_owned_storage():
    services = yaml.safe_load((ROOT / 'compose.yaml').read_text())['services']
    worker, app = services['worker'], services['app']
    assert worker['command'] == ['python', '-m', 'copilot', 'worker']
    assert worker['build'] == app['build'] == '.'
    assert not worker.get('ports')
    assert worker['init'] is True
    assert worker['volumes'] == ['evidence:/data', 'models:/models:ro']
    for name in ('COPILOT_DATA_DIR', 'COPILOT_MODEL_DIR',
                 'COPILOT_CHROMA_HOST', 'COPILOT_CHROMA_PORT'):
        assert worker['environment'][name] == app['environment'][name]
    assert not any('KEY' in name or 'TOKEN' in name for name in worker['environment'])
    assert not worker.get('env_file')
    assert worker['cap_drop'] == ['ALL']
    assert worker['security_opt'] == ['no-new-privileges:true']
    assert worker['stop_grace_period'] == '60s'


def test_only_loopback_api_port_is_published_and_image_stays_non_root():
    services = yaml.safe_load((ROOT / 'compose.yaml').read_text())['services']
    assert services['app']['ports'] == ['127.0.0.1:3001:3001']
    assert not services['chroma'].get('ports')
    dockerfile = (ROOT / 'Dockerfile').read_text()
    assert 'USER 10001:10001' in dockerfile
    assert 'HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1' in dockerfile
    assert 'COPY backend/copilot/ ./copilot/' in dockerfile
