"""Capture the review form and verify downloads using a temporary localhost server."""
import argparse
from contextlib import contextmanager
from pathlib import Path
import socket
from threading import Thread
import time

from playwright.sync_api import sync_playwright
import uvicorn

from backend.db import ROOT
from backend.main import app


@contextmanager
def preview_server():
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, lifespan='off', log_level='error'))
    thread = Thread(target=server.run, kwargs={'sockets': [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(.05)
        assert server.started, 'Preview server could not start'
        yield f'http://127.0.0.1:{port}'
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('case_id')
    parser.add_argument('--output', type=Path, default=ROOT / 'data/extraction-audit/screenshots')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    errors = []
    with preview_server() as base_url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel='msedge', headless=True)
        page = browser.new_page(viewport={'width': 1600, 'height': 1100}, device_scale_factor=1, accept_downloads=True)
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(base_url+'/', wait_until='networkidle')
        page.evaluate('showExplorer()')
        library = page.locator('#case-list tr').filter(has=page.locator(f'.open-case[data-id="{args.case_id}"]'))
        library_link = library.locator('.case-pdf-download').first
        if library.locator('.case-pdf-downloads').count():
            library.locator('summary').click()
        with page.expect_download() as library_download:
            library_link.click()
        assert Path(library_download.value.path()).read_bytes().startswith(b'%PDF-')
        library.screenshot(path=str(args.output / 'case-library.png'))
        page.evaluate('(id) => openCase(id)', args.case_id)
        page.locator('#asset-values').wait_for()
        assert page.locator('[data-fact="company"] .fact-value').input_value()
        # Verify that values survive browser serialization before capturing.
        record = page.evaluate('collectRecord()')
        assert record['asset_values'], 'Expected recovered asset valuations'
        assert record['plan_payments'], 'Expected recovered plan payments'
        assert page.evaluate("sourceAmount('1640100000','crore')") == '164.01 crore'
        assert page.evaluate("sourceAmount('1640100000','INR')") == ''
        assert all('fair_value_source_unit' in asset for asset in record['asset_values'])
        with page.expect_download() as source_download:
            page.locator('#download-pdf').click()
        assert Path(source_download.value.path()).read_bytes().startswith(b'%PDF-')
        page.locator('#document-select').locator('..').screenshot(path=str(args.output / 'source-download.png'))
        assert not errors, errors
        for slug, title, source_page in [('identity', 'Case identity', 2),
                                          ('claims', 'Claims and recovery', 6),
                                          ('parties', 'Creditors and plan submitters', 6),
                                          ('valuations', 'Asset valuations', 14),
                                          ('payments', 'Proposed plan payments', 6),
                                          ('ownership', 'Ownership and directors', 16)]:
            section = page.locator('section.panel').filter(has=page.get_by_role('heading', name=title, exact=True))
            page.evaluate('(number) => { state.page=number;renderPage(); }', source_page)
            section.scroll_into_view_if_needed()
            section.screenshot(path=str(args.output / (slug + '.png')))
        page.locator('.extraction-coverage').first.evaluate('(el) => el.open=true')
        page.locator('.extraction-coverage').first.screenshot(path=str(args.output / 'remaining-fields.png'))
        browser.close()
    print('Browser checks passed. Screenshots:', args.output)


if __name__ == '__main__':
    main()
