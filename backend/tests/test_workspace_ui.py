"""The isolated data-preview UI does not claim S6 drafting qualification."""
import re
from pathlib import Path

from fastapi.testclient import TestClient

from copilot.api import create_app
from copilot.config import Settings

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / 'backend/ui/workspace'


def test_workspace_static_preview_preserves_default_and_avoids_models(tmp_path):
    def unavailable(*_args):
        raise AssertionError('Static UI must not initialize models/Chroma')

    app = create_app(Settings(tmp_path / 'data', tmp_path / 'models'), unavailable)
    with TestClient(app, base_url='http://127.0.0.1:3001') as client:
        preview = client.get('/ui/workspace/index.html')
        assert preview.status_code == 200
        assert 'Python workspace preview' in preview.text
        assert 'Nothing is submitted for you.' in preview.text
        assert "script-src 'self'" in preview.headers['content-security-policy']
        for asset in ('app.js', 'client.js', 'dom.js', 'questions.js', 'styles.css'):
            response = client.get('/ui/workspace/' + asset)
            assert response.status_code == 200
            assert response.headers['x-content-type-options'] == 'nosniff'
        assert client.get('/').text == (ROOT / 'backend/ui/index.html').read_text()
        assert not (tmp_path / 'models').exists()


def test_workspace_strings_are_dom_text_and_python_envelopes_are_explicit():
    modules = '\n'.join(path.read_text() for path in UI.glob('*.js'))
    assert 'innerHTML' not in modules
    assert 'insertAdjacentHTML' not in modules
    assert 'eval(' not in modules
    assert 'textContent' in modules and 'createTextNode' in modules
    assert '/api/workspace' in modules and '/api/evidence' in modules
    assert '/api/profiles' not in modules
    assert 'gate.current' in modules
    assert 'new RequestGate()' in modules
    assert 'Array.from(resolved.canonical_text)' in modules


def test_workspace_has_keyboard_landmarks_and_external_modules():
    html = (UI / 'index.html').read_text()
    assert '<html lang="en">' in html
    assert 'Skip to content' in html
    assert 'aria-label="Main navigation"' in html
    assert 'role="status"' in html
    assert '<main id="main" tabindex="-1">' in html
    assert '<script type="module" src="/ui/workspace/app.js">' in html
    assert 'onclick=' not in html
    assert 'prefers-reduced-motion' in (UI / 'styles.css').read_text()
    assert "'backend/ui'" in (ROOT / 'scripts/check.mjs').read_text()


def test_workspace_matches_existing_product_palette_without_rewriting_assets():
    def tokens(path):
        return dict(re.findall(r'(--[\w-]+)\s*:\s*(#[0-9a-fA-F]{6})', path.read_text()))

    existing, preview = tokens(ROOT / 'public/styles.css'), tokens(UI / 'styles.css')
    for key in ('--canvas', '--ink', '--muted', '--line', '--navy', '--teal'):
        assert preview[key] == existing[key]
