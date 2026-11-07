"""Indexed review candidates; only a byte hash can suppress storage."""
import hashlib
import json
import re
from pathlib import PurePosixPath

class DocumentMatcher:

    @staticmethod
    def fingerprint(db, doc_id, filename, extraction, created_at):
        text = ' '.join(' '.join((p['text'] for p in extraction['pages'])).casefold().split())
        text_hash = hashlib.sha256(text.encode()).hexdigest() if len(text) > 200 else None
        name, subject = (normalized(PurePosixPath(filename).stem), normalized(extraction.get('subject') or ''))
        day = created_at[:10]
        db.insert_document_fingerprint(doc_id, text_hash, name, subject, day)
        identifiers = set(re.findall('\\b[LU]\\d{5}[A-Z]{2}\\d{4}[A-Z]{3}\\d{6}\\b', text.upper()))
        ids = [('CIN', value) for value in identifiers]
        ids += [('DIN', value) for value in re.findall('\\bDIN\\s*[:\\-]?\\s*(\\d{8})\\b', text.upper())]
        for kind, value in set(ids):
            db.insert_document_identifier(doc_id, kind, value)
        candidates = {}
        for column, value, reason in [
            ('text_hash', text_hash, 'same_extracted_text'),
            ('normalized_name', name, 'same_filename'),
            ('normalized_subject', subject, 'same_subject'),
        ]:
            if value:
                for row in db.matching_fingerprints(column, value, doc_id):
                    reasons = candidates.setdefault(row['document_id'], set())
                    reasons.add(reason)
                    if row['ingest_day'] == day and reason == 'same_filename':
                        reasons.add('same_day_and_filename')
        for kind, value in set(ids):
            for row in db.matching_identifiers(kind, value, doc_id):
                candidates.setdefault(row['document_id'], set()).add('shared_' + kind)
        for matched, reasons in candidates.items():
            db.insert_document_match(doc_id, matched, json.dumps(sorted(reasons)))
            db.insert_document_match(matched, doc_id, json.dumps(sorted(reasons)))

    @staticmethod
    def candidates(db, doc_id):
        matches = {row['document_id']: {**dict(row), 'reasons': json.loads(row['reasons'])} for row in db.stored_document_matches(doc_id)}
        order = db.document_official_order(doc_id)
        if order:
            name = normalized(order['subject'] or '')
            for row in db.related_official_documents(doc_id, order['order_date'], order['subject']):
                reasons = []
                if len(name) >= 12 and normalized(row['subject'] or '') == name:
                    reasons.append('same_official_order_name')
                    if row['order_date'] == order['order_date']:
                        reasons.append('same_order_date_and_name')
                if row['order_date'] == order['order_date'] and normalized(PurePosixPath(row['filename']).stem) == normalized(PurePosixPath(order['filename']).stem):
                    reasons.append('same_order_date_and_filename')
                if not reasons:
                    continue
                match = matches.setdefault(
                    row['document_id'],
                    {
                        'document_id': row['document_id'],
                        'filename': row['filename'],
                        'source_url': row['source_url'],
                        'reasons': [],
                    },
                )
                match['reasons'] = sorted(set(match['reasons']) | set(reasons))
        return list(matches.values())[:20]

    @staticmethod
    def normalized(name):
        return re.sub('[^a-z0-9]', '', name.casefold())
fingerprint = DocumentMatcher.fingerprint
candidates = DocumentMatcher.candidates
normalized = DocumentMatcher.normalized
