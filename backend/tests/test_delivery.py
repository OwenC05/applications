from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_compose_local_boundary():
    config = yaml.safe_load((ROOT / 'compose.yaml').read_text())
    assert config['services']['app']['ports'] == ['127.0.0.1:3001:3001']
    assert 'ports' not in config['services']['chroma']
    assert '@sha256:' in config['services']['chroma']['image']
    assert config['services']['app']['cap_drop'] == ['ALL']
    assert len(config['volumes']) == 3


def test_build_context_allowlist_and_nonroot():
    ignore = (ROOT / '.dockerignore').read_text().splitlines()
    assert ignore[0] == '**'
    assert '!backend/uv.lock' in ignore
    assert not any(item.startswith('!') and any(secret in item for secret in ['.env', '.data', 'evidence-data', 'models/']) for item in ignore)
    dockerfile = (ROOT / 'Dockerfile').read_text()
    assert 'USER 10001:10001' in dockerfile
    assert 'uv sync --locked --no-dev' in dockerfile
    assert '@sha256:' in dockerfile.splitlines()[0]
    assert 'COPY . ' not in dockerfile


def test_cpu_dependency_lock():
    lock = (ROOT / 'backend/uv.lock').read_text()
    assert '2.8.0+cpu' in lock
    assert 'name = "nvidia-' not in lock


def test_ui_has_no_interpolated_html_or_inline_script():
    html = (ROOT / 'backend/ui/index.html').read_text()
    js = (ROOT / 'backend/ui/app.js').read_text()
    assert 'innerHTML' not in js
    assert 'textContent' in js
    assert 'stamp !== epoch' in js
    assert '<script type="module" src=' in html
    assert 'semantic support' in html


def test_citation_inspector_uses_unicode_code_points_not_utf16():
    # Browser JS slices UTF-16; canonical Python spans count Unicode code points.
    import subprocess
    js = (ROOT / 'backend/ui/app.js').read_text()
    assert 'const canonical = Array.from(resolved.canonical_text)' in js
    result = subprocess.run(['node', '-e', "const canonical=Array.from('🚀 Python evidence');console.log(canonical.slice(0,2).join('')+'Python'+canonical.slice(8).join(''))"],
                            text=True, capture_output=True, check=True)
    assert result.stdout.strip() == '🚀 Python evidence'
