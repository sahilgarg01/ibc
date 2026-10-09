"""One catalogue row per IBBI order, plus locally imported files."""

from urllib.parse import unquote, urlsplit

from .document_matching import candidates as document_candidates


ENTRIES = """
SELECT o.id AS id, o.id AS source_order_id, o.pdf_url AS official_pdf_url,
       o.order_date, o.subject, o.remarks, o.bench_id, o.bench_name,
       CASE WHEN d.id IS NOT NULL THEN 'downloaded' ELSE o.pdf_status END AS pdf_status,
       o.pdf_error, o.first_seen_at AS created_at,
       o.pdf_fetched_at AS imported_at, o.document_id,
       COALESCE(d.case_id,o.case_id) AS case_id, d.filename,
       o.pdf_url AS source_url, COALESCE(f.duplicate,0) AS duplicate,
       f.job_id, d.source_url AS stored_source_url,
       d.created_at AS document_created_at, d.sha256, d.order_kind,
       d.subject AS pdf_subject
FROM source_orders o
LEFT JOIN documents d ON d.id=o.document_id
LEFT JOIN import_files f ON f.id=(
    SELECT fi.id FROM import_files fi WHERE fi.source_url=o.pdf_url AND fi.document_id=o.document_id
    ORDER BY fi.created_at DESC,fi.id DESC LIMIT 1)
UNION ALL
SELECT f.id,NULL,NULL,NULL,d.subject,'',NULL,NULL,'downloaded',NULL,
       f.created_at,f.created_at,d.id,d.case_id,d.filename,
       f.source_url,f.duplicate,f.job_id,d.source_url,d.created_at,
       d.sha256,d.order_kind,d.subject
FROM import_files f JOIN documents d ON d.id=f.document_id
WHERE NOT EXISTS(SELECT 1 FROM source_orders o WHERE o.pdf_url=f.source_url AND o.document_id=f.document_id)
UNION ALL
SELECT d.id,NULL,NULL,NULL,d.subject,'',NULL,NULL,'downloaded',NULL,
       d.created_at,d.created_at,d.id,d.case_id,d.filename,
       d.source_url,0,NULL,d.source_url,d.created_at,d.sha256,
       d.order_kind,d.subject
FROM documents d
WHERE NOT EXISTS(SELECT 1 FROM import_files f WHERE f.document_id=d.id)
  AND NOT EXISTS(SELECT 1 FROM source_orders o WHERE o.document_id=d.id)
"""


def catalogue_entries(db, *, page=1, page_size=25, q='', category='', bench='',
                      remark='', adverse='', date_from='', date_to='', duplicate='all'):
    where, args = ['1=1'], []
    if q:
        where.append("(LOWER(COALESCE(e.subject,'')) LIKE ? OR LOWER(COALESCE(e.remarks,'')) LIKE ? OR LOWER(COALESCE(e.filename,'')) LIKE ? OR LOWER(COALESCE(e.source_url,'')) LIKE ?)")
        args.extend([f'%{q.casefold()}%'] * 4)
    if category:
        where.append('''(EXISTS(SELECT 1 FROM order_categories c WHERE c.order_id=e.source_order_id AND c.category=?)
                        OR (e.source_order_id IS NULL AND EXISTS(SELECT 1 FROM document_categories c WHERE c.document_id=e.document_id AND c.category=?)))''')
        args.extend([category, category])
    if bench:
        where.append('e.bench_id=?')
        args.append(bench)
    if remark:
        where.append('e.remarks=?')
        args.append(remark)
    if adverse:
        where.append('EXISTS(SELECT 1 FROM source_order_filters f WHERE f.order_id=e.source_order_id AND f.filter_key=?)')
        args.append(f'adverse:{adverse}')
    if date_from:
        where.append('COALESCE(e.order_date,SUBSTR(e.imported_at,1,10),SUBSTR(e.created_at,1,10))>=?')
        args.append(date_from)
    if date_to:
        where.append('COALESCE(e.order_date,SUBSTR(e.imported_at,1,10),SUBSTR(e.created_at,1,10))<=?')
        args.append(date_to)
    if duplicate != 'all':
        where.append('e.document_id IS NOT NULL AND e.duplicate=?')
        args.append(int(duplicate == 'yes'))
    filtered = 'FROM ibbi_order_catalogue e WHERE ' + ' AND '.join(where)
    total = db.execute('SELECT COUNT(*) ' + filtered, args).fetchone()[0]
    rows = [dict(row) for row in db.execute('SELECT * ' + filtered + ''' ORDER BY
        COALESCE(order_date,SUBSTR(imported_at,1,10),SUBSTR(created_at,1,10)) DESC,
        created_at DESC,id LIMIT ? OFFSET ?''', (*args,page_size,(page-1)*page_size))]
    for row in rows:
        if row['source_order_id']:
            row['categories'] = [r['category'] for r in db.execute(
                'SELECT category FROM order_categories WHERE order_id=? ORDER BY category',
                (row['source_order_id'],))]
        else:
            row['categories'] = [r['category'] for r in db.execute(
                'SELECT category FROM document_categories WHERE document_id=? ORDER BY category',
                (row['document_id'],))]
        row['imported_filename'] = unquote(urlsplit(row['source_url'] or '').path.rsplit('/',1)[-1]) or row['filename']
        if row['document_id']:
            row['possible_matches'] = document_candidates(db,row['document_id'])
            row['duplicate_of'] = ({'document_id':row['document_id'],
                                    'filename':row['filename'],
                                    'source_url':row['stored_source_url'],
                                    'created_at':row['document_created_at']}
                                   if row['duplicate'] else None)
        else:
            row['possible_matches'] = []
            row['duplicate_of'] = None
    return {'items':rows,'total':total,'page':page,'page_size':page_size}
