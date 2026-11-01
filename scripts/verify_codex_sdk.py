"""Verify live SDK PDF extraction and persistence using a disposable database."""
import argparse
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from backend.main import ROOT, create_app
from backend.models import CaseRecord


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pdf', type=Path, default=ROOT/'samples/indo-global-resolution-order.pdf')
    args = parser.parse_args()
    pdf = args.pdf.read_bytes()
    with TemporaryDirectory(prefix='ibc-sdk-verify-') as directory:
        with TestClient(create_app(Path(directory))) as client:
            print('Extracting PDF through Codex SDK using an isolated database...', flush=True)
            response = client.post('/api/documents', files={'file': (args.pdf.name, pdf, 'application/pdf')})
            if not response.is_success:
                raise SystemExit(f'Extraction failed ({response.status_code}): {response.text}')
            ids = response.json()
            case = client.get(f'/api/cases/{ids["case_id"]}').json()
            extraction = case['documents'][0]['extraction']
            expected = set(CaseRecord.model_fields)
            assert extraction['provider'] == 'codex_sdk'
            assert set(extraction['response']['record']) == expected
            assert set(extraction['case_facts']['field_review']) == expected
            assert case['record']['company'] is not None
            source = client.get(f'/api/documents/{ids["document_id"]}/content')
            source.raise_for_status()
            assert source.json()['source_provider'] == 'python'
            print(f'Live SDK extraction passed: {len(expected)} fields and reviews persisted; Python source pages available.', flush=True)
    print('Disposable database removed. Application records were not changed.', flush=True)


if __name__ == '__main__':
    main()
