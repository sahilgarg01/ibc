"""Audit every imported PDF without changing records; --apply refreshes draft cases."""
import argparse
import json
from pathlib import Path

from backend.case_extraction import extract_case
from backend.db import ROOT, connection
from backend.extraction import extract_pdf
from backend.models import CaseRecord, metrics


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--cached', action='store_true', help='Use previously extracted pages')
    args = parser.parse_args()
    output = ROOT / 'data/extraction-audit/batch'
    output.mkdir(parents=True, exist_ok=True)
    with connection(ROOT / 'data') as db:
        rows = [dict(row) for row in db.execute('''SELECT d.id,d.case_id,d.filename,d.content,d.extraction,
            c.record,c.version,c.review_status FROM documents d JOIN cases c ON c.id=d.case_id''').fetchall()]
    report = []
    for row in rows:
        try:
            applied = False
            if args.apply and row['review_status'] == 'draft':
                backup = {key: value for key, value in row.items() if key != 'content'}
                backup_path = output / (row['id'] + '-before.json')
                if not backup_path.exists():
                    backup_path.write_text(json.dumps(backup, indent=2), encoding='utf-8')
                from backend.main import app
                from fastapi.testclient import TestClient
                client = TestClient(app)  # Do not start collection/scheduler lifecycle.
                response = client.post('/api/documents/' + row['id'] + '/extract-facts')
                response.raise_for_status()
                with connection(ROOT / 'data') as db:
                    stored = db.execute('SELECT extraction FROM documents WHERE id=?', (row['id'],)).fetchone()
                    extraction = json.loads(stored['extraction'])
                applied = True
            else:
                extraction = json.loads(row['extraction']) if args.cached else extract_pdf(bytes(row['content']))
            facts, details = extract_case(extraction, row['id'])
            extraction['case_facts'] = details
            (output / (row['id'] + '.json')).write_text(json.dumps(extraction, indent=2), encoding='utf-8')
            item = {'id': row['id'], 'filename': row['filename'], 'review_status': row['review_status'],
                'populated_fields': details['populated_fields'], 'missing_fields': details['missing_fields'],
                'metrics': metrics(CaseRecord.model_validate(facts)), 'warnings': details['warnings'],
                'suggested_record': facts}
            item['applied'] = applied
            report.append(item)
            print(row['filename'], len(details['populated_fields']), 'fields;',
                  len(facts['asset_values']), 'assets;', len(facts['plan_payments']), 'payments;',
                  len(facts['claims']), 'claims', flush=True)
        except Exception as exc:
            report.append({'id': row['id'], 'filename': row['filename'], 'error': str(exc)})
            print(row['filename'], 'FAILED:', type(exc).__name__, str(exc), flush=True)
    (output / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print('Report:', output / 'report.json', flush=True)
    if any('error' in item for item in report):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
