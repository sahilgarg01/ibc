import json
from fastapi import HTTPException
from .models import Approval, CaseRecord, SaveRecord, metrics
from .db import Database, decode

class CaseService:
    """Coordinate evidence validation and versioned case review transactions."""

    def __init__(self, context):
        self.context = context

    def get_case(self, db, case_id):
        row = db.case_by_id(case_id)
        if not row:
            raise HTTPException(404, 'Case not found')
        return decode(row)

    def audit(self, db, case_id, action, record, actor=None):
        db.insert_audit(case_id, action, json.dumps(record), actor, Database.timestamp())

    def validate_evidence(self, db, case_id, record):
        docs = {r['id']: len(json.loads(r['extraction'])['pages']) for r in db.case_document_extractions(case_id)}

        def visit(value):
            if isinstance(value, dict):
                if 'document_id' in value:
                    if value['document_id'] not in docs or not 1 <= value['page'] <= docs[value['document_id']]:
                        raise HTTPException(
                            422,
                            'Evidence must reference a valid page in a document attached to this case',
                        )
                    for field in ('fair_value_page', 'liquidation_value_page'):
                        if value.get(field) is not None and (not 1 <= value[field] <= docs[value['document_id']]):
                            raise HTTPException(
                                422,
                                'Valuation evidence must reference a valid source page',
                            )
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)
        visit(record.model_dump(mode='json'))

    def entities_match(self, name: str='', din: str | None=None, cin: str | None=None):
        if not name and (not din) and (not cin):
            raise HTTPException(422, 'Provide name, DIN or CIN')
        with self.context.database.session() as db:
            return db.match_candidates(name, din, cin)

    def list_cases(self, q: str='', status: str='all', summary: bool=False):
        with self.context.database.session() as db:
            cases = [decode(r) for r in (db.all_cases() if status == 'all' else db.filtered_cases(status))]
            subjects = {}
            pdf_documents = {}
            for row in db.case_document_summaries():
                pdf_documents.setdefault(row['case_id'], []).append(
                    {'id': row['id'], 'filename': row['filename']},
                )
            for row in db.case_subjects():
                if row['subject']:
                    subjects.setdefault(row['case_id'], []).append(row['subject'])
        result = []
        for case in cases:
            case['subjects'] = subjects.get(case['id'], [])
            case['pdf_documents'] = pdf_documents.get(case['id'], [])
            record = CaseRecord.model_validate(case['record'])
            search = ' '.join(
                [f.value for f in [record.company, record.case_number, record.professional, record.cin] if f] + case['subjects'],
            )
            if q.casefold() not in search.casefold():
                continue
            if status != 'all' and case['review_status'] != status:
                continue
            case['metrics'] = metrics(record)
            if summary:
                case['record'] = {key: case['record'].get(key) for key in
                                  ('company', 'case_number', 'professional', 'cin', 'outcome')}
            result.append(case)
        return result

    def case_detail(self, case_id: str):
        with self.context.database.session() as db:
            case = self.context.cases.get_case(db, case_id)
            case['documents'] = [decode(r) for r in db.case_documents(case_id)]
            for document in case['documents']:
                source = db.document_official_subject(document['id'])
                document['pdf_subject'] = document['subject']
                if source:
                    document['subject'] = source['subject']
            case['audit'] = [decode(r) for r in db.case_audit(case_id)]
        case['metrics'] = metrics(CaseRecord.model_validate(case['record']))
        return case

    def save(self, case_id: str, payload: SaveRecord):
        with self.context.database.session() as db:
            db.begin()
            case = self.context.cases.get_case(db, case_id)
            if payload.version != case['version']:
                raise HTTPException(409, 'Case changed; reload before saving')
            self.context.cases.validate_evidence(db, case_id, payload.record)
            updated = db.save_case_draft(payload.record.model_dump_json(), case_id, payload.version)
            if updated != 1:
                raise HTTPException(409, 'Case changed; reload before saving')
            self.context.cases.audit(
                db,
                case_id,
                'saved_draft',
                payload.record.model_dump(mode='json'),
            )
            db.project(case_id, payload.record.model_dump(mode='json'))
        return self.context.cases.case_detail(case_id)

    def approve(self, case_id: str, payload: Approval):
        with self.context.database.session() as db:
            db.begin()
            case = self.context.cases.get_case(db, case_id)
            if payload.version != case['version']:
                raise HTTPException(409, 'Case changed; reload before approving')
            record = CaseRecord.model_validate(case['record'])
            if not payload.confirmed or not record.company or (not record.case_number):
                raise HTTPException(422, 'Review confirmation, company and case number are required')
            if record.outcome != 'unknown' and (not record.outcome_evidence):
                raise HTTPException(422, 'The outcome needs page evidence')
            self.context.cases.validate_evidence(db, case_id, record)
            updated = db.approve_case(payload.reviewer, Database.timestamp(), case_id, payload.version)
            if updated != 1:
                raise HTTPException(409, 'Case changed; reload before approving')
            self.context.cases.audit(
                db,
                case_id,
                'approved',
                record.model_dump(mode='json'),
                payload.reviewer,
            )
        return self.context.cases.case_detail(case_id)
