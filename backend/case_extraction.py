"""Evidence-backed draft extraction from explicit labels, order captions and financial tables."""
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from .models import AssetValue, CaseRecord, Claim, Plan, preferred_asset_values
from .final_orders import final_decision
from .order_narrative import enrich, enrich_extended
from .section_facts import enrich_sections, enrich_claim_tables, enrich_asset_tables
from .party_extraction import enrich_parties
from .caption_facts import enrich_captions
from .ocr_financial_rows import enrich_ocr_rows, enrich_verified_ocr_claims, enrich_ocr_coc, enrich_ocr_claim_summary
from .plan_details import enrich_rejected_claims, enrich_plan_allocations, enrich_merged_operational_table
from .field_rules import field_review

VERSION = 'ocr-label-spacing-27'
AMOUNT = r'(?:Rs\.?|INR|₹)?\s*([\d,]+(?:\.\d+)?)\s*(crores?|cr\.?|lakhs?|lacs?|millions?)?'
CIN = r'[LU]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6}'


def money(value, scale=Decimal(1)):
    value=re.sub(r'/\s*-\s*$', '', value.strip())
    value=re.sub(r'(?<=[\d,])\s+(?=[\d,])', '', value)
    if value.casefold() in {'nil', 'zero'}:
        return '0.00'
    match = re.fullmatch(AMOUNT,value.strip(),re.I)
    if not match:
        return None
    unit=(match.group(2) or '').lower().rstrip('.')
    factor=Decimal(10000000) if unit.startswith(('cr','crore')) else Decimal(100000) if unit.startswith(('la','lac')) else Decimal(1000000) if unit.startswith('million') else scale
    try:
        return str((Decimal(match.group(1).replace(',',''))*factor).quantize(Decimal('.01')))
    except InvalidOperation:
        return None


def category(name):
    text=name.casefold()
    if 'unsecured' in text and 'financial' in text:
        return 'unsecured_financial'
    if 'secured' in text and 'financial' in text:
        return 'secured_financial'
    if 'financial' in text:
        return 'unsecured_financial' if 'unsecured' in text else 'other'
    if 'government' in text or 'statutory' in text:
        return 'government'
    if 'operational' in text:
        return 'operational'
    if any(word in text for word in ('employee','workmen','worker')):
        return 'employee'
    if any(word in text for word in ('government','statutory')):
        return 'government'
    return 'other'


def iso_date(value):
    # Scan recognition can omit the gaps around a written month.
    months = r'(?:January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec)'
    value=re.sub(r'(\d)(st|nd|rd|th)?(?='+months+r'\b|'+months+r'\d)', r'\1\2 ', value, flags=re.I)
    value=re.sub(r'('+months+r')(?=\d)', r'\1 ', value, flags=re.I)
    value=re.sub(r'(\d{1,2})(?:st|nd|rd|th)\b', r'\1', value.strip(), flags=re.I)
    value=' '.join(value.replace(',', ' ').replace('，', ' ').split())
    value=re.sub(r'(?<=\d)\s*([./-])\s*(?=\d)', r'\1', value)
    for fmt in ('%Y-%m-%d','%d.%m.%Y','%d/%m/%Y','%d-%m-%Y','%d %B %Y','%d %b %Y','%B %d %Y','%b %d %Y'):
        try:
            return datetime.strptime(value,fmt).date().isoformat()
        except ValueError:
            pass
    return None


def extract_case(extraction, document_id):
    # Hybrid PDFs can contain readable paragraphs and separate image-only
    # sections. Every narrative reader must see recovered OCR as well as the
    # embedded text, even when the embedded text is longer. Keep the cached
    # reader output and the original page references unchanged.
    pages=[]
    for page in extraction['pages']:
        sources=[]
        for key in ('text', 'embedded_text', 'ocr_text'):
            text=page.get(key) or ''
            if text.strip() and text.strip() not in sources:
                sources.append(text.strip())
        combined='\n\n'.join(sources)
        pages.append({**page, 'text':combined, 'embedded_text':combined})
    extraction={**extraction, 'pages':pages}
    record=CaseRecord().model_dump(mode='json')
    evidence=[]
    warnings=[]
    def put(field,value,page,excerpt):
        if not value:
            return
        value=' '.join(value.split()).strip(' :;,"“”�')
        if value and not record.get(field):
            record[field]={'value':value[:500],'document_id':document_id,'page':page}
            evidence.append({'field':field,'page':page,'excerpt':excerpt[:1000]})
    labels={
        'company':r'(?:(?:Name of (?:the )?)?corporate debtor|Company name)', 'cin':r'(?:Corporate debtor CIN|Debtor CIN|CIN of (?:the )?corporate debtor)',
        'registered_office':r'(?:Corporate debtor registered office|Registered office of (?:the )?corporate debtor)',
        'industry':r'(?:Industry|Sector|Nature of business)',
        'case_number':r'(?:Case number|Case no\.)', 'tribunal':r'(?:NCLT bench|Tribunal)',
        'irp':r'(?:Interim resolution professional|IRP)', 'professional':r'(?:Resolution professional|RP)',
        'liquidator':r'Liquidator', 'applicant':r'(?:Successful resolution applicant|Resolution applicant)',
        'applicant_cin':r'(?:Successful resolution applicant CIN|Resolution applicant CIN)',
        'fair_value':r'Fair value', 'liquidation_value':r'Liquidation value',
        'admission_date':r'(?:Admission date|CIRP commencement date)',
        'resolution_date':r'(?:Resolution approval date|Resolution date)', 'order_date':r'(?:Order date|Pronounced)',
        'total_received_claims':r'(?:Total claims received|Total received claims)',
        'total_admitted_claims':r'(?:Total admitted claims|Total claims admitted)',
        'applicant_incorporated':r'(?:Resolution applicant incorporation date|Applicant incorporation date)',
        'applicant_business':r'(?:Resolution applicant business|Applicant business)',
        'applicant_location':r'(?:Resolution applicant registered office|Applicant registered office)',
        'coc_voting_share':r'(?:CoC voting share|CoC voting rights)',
    }
    claim_rows=[]
    table_header=None
    admitted_total=None
    enrich_captions(extraction, put, money, iso_date)
    enrich_sections(record, extraction, document_id, put, evidence, money, iso_date)
    for page in extraction['pages']:
        number=page['page']
        text=page['text']
        flat=' '.join(text.split())
        # OCR may contain image-only fields even when embedded text is longer.
        # Keep the original page number and try both sources for explicit labels.
        for field,label in labels.items():
            for source in (text, page.get('ocr_text', ''), page.get('embedded_text', '')):
                match=re.search(r'^[ \t]*'+label+r'[ \t]*(?::|[–—-]|\t| {2,})[ \t]*([^\n;]+)',source,re.I|re.M)
                if not match:
                    continue
                value=match.group(1).strip()
                if field.endswith('date') or field == 'applicant_incorporated':
                    value=iso_date(value)
                if field in {'cin','applicant_cin'} and not re.fullmatch(CIN,value or '',re.I):
                    value=None
                if field in {'fair_value','liquidation_value','total_received_claims','total_admitted_claims'}:
                    value=money(value)
                if value:
                    put(field,value,number,match.group(0))
        if record['applicant'] and not record['applicant_cin']:
            applicant_key=re.sub(r'\W+','',record['applicant']['value']).casefold()
            for match in re.finditer(r'\bCIN\s*[:\-]?\s*('+CIN+r')\b',flat,re.I):
                preceding=flat[max(0,match.start()-160):match.start()]
                if applicant_key in re.sub(r'\W+','',preceding).casefold():
                    put('applicant_cin',match.group(1).upper(),number,preceding[-100:]+match.group(0))
                    break
        # Caption roles, rather than the first company/CIN appearing anywhere in the PDF.
        if number<=3:
            # Opening headings identify this order. Cited decisions later in
            # the PDF must not replace its own court, case number or date.
            if not record['tribunal']:
                for line in text.splitlines()[:30]:
                    compact=''.join(line.upper().split())
                    if 'DEBTSRECOVERYTRIBUNAL' in compact:
                        court=re.search(r'DEBTSRECOVERYTRIBUNAL(?:NO\.?|[-–—])?([IVX]+|\d+)?(?:AT|,)?(CHENNAI|MUMBAI|KOLKATA|DELHI|BENGALURU|BANGALORE|HYDERABAD|AHMEDABAD|CHANDIGARH|JAIPUR|LUCKNOW|PUNE|CUTTACK|GUWAHATI|ERNAKULAM|MADURAI)',compact)
                        if court:
                            suffix=' '+court.group(1) if court.group(1) else ''
                            put('tribunal','DRT'+suffix+' '+court.group(2).title(),number,line)
                            break
            case_heading=re.search(r'\bIBC\s*NO\.?\s*(\d{1,5})\s*(?:/|OF|0F)\s*(\d{4})\b',text,re.I)
            if case_heading:
                put('case_number',f'IBC No. {case_heading.group(1)} of {case_heading.group(2)}',number,case_heading.group(0))
            if not record['case_number']:
                insolvency_case=re.search(r'\bINSOLVENCY\s*APPLICATION\s*NO\.?\s*(\d{1,5})\s*(?:/|OF|0F)\s*(\d{4})\b',text,re.I)
                if insolvency_case:
                    put('case_number',f'Insolvency Application No. {insolvency_case.group(1)} of {insolvency_case.group(2)}',number,insolvency_case.group(0))
            if number==1:
                for pattern in (
                    r'\bDated\s+this\s+(\d{1,2})(?:st|nd|rd|th)?\s+day\s+of\s+([A-Za-z]+),?\s+(\d{4})\b',
                    r'\b(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+),?\s+(\d{4})\s*\.?\s*(?:\n|$)',
                ):
                    date_heading=re.search(pattern,text,re.I)
                    if date_heading:
                        date=iso_date(' '.join(date_heading.groups()))
                        if date:
                            put('order_date',date,number,date_heading.group(0))
                            break
                dated_label=re.search(r'DATE\s*OF\s*(?:DELIVERY\s*OF\s*)?ORDER\s*[:\-]?\s*(\d{1,2}[./-]\d{1,2}[./-]\d{4})',text,re.I)
                if dated_label:
                    put('order_date',iso_date(dated_label.group(1)),number,dated_label.group(0))
            caption_tail=r'[.… \t]*(?:\n[.… \t]*)?(?:(?:Respondent|Applicant)[ \t]*[-/][ \t]*)?Corporate[ \t]+Debtor\b'
            company=re.search(r'(?m)^[ \t]*([A-Z][A-Za-z0-9 &.,()\-]{3,120}?(?:Private Limited|Pvt\.? Ltd\.?|Limited|Ltd\.?))'+caption_tail,text,re.I)
            if company and company.group(1).strip().casefold() in {'private limited','pvt ltd','pvt. ltd.','limited','ltd.'}:
                company=None
            if not company:
                company=re.search(r'(?m)^[ \t]*([A-Z][A-Za-z0-9 &.,()\-]{8,120}[ \t]*\n[ \t]*(?:Private Limited|Pvt\.? Ltd\.?|Limited|Ltd\.?))'+caption_tail,text,re.I)
            if company:
                name=company.group(1).split('V/s.')[-1].split('Vs.')[-1].strip()
                name=re.sub(r'^s\.\s+','',name)
                put('company',name,number,company.group(0))
            if not record['company']:
                named_debtor=re.search(r'\bCorporate\s*Debtor\s*(?:being|namely|:)?\s*[“"‘\']\s*([A-Z][A-Za-z0-9 &.,()\-]{3,120}?(?:Private Limited|Pvt\.?\s*Ltd\.?|Limited|Ltd\.?))\s*[”"’\']',flat,re.I)
                if named_debtor:
                    put('company',named_debtor.group(1),number,named_debtor.group(0))
            rp=re.search(r'(?:Mr\.?|Ms\.?|Mrs\.?)\s+([A-Z][A-Za-z .]+?)\s*\(Resolution Professional',flat)
            if rp:
                put('professional',rp.group(1),number,rp.group(0))
            applicant=re.search(r'([A-Z][A-Za-z0-9 &.,\-]+?(?:Private Limited|Pvt\.? Ltd\.?|Limited|Ltd\.?))\s*\(Successful Resolution Applicant\)',flat)
            if applicant:
                put('applicant',applicant.group(1).split('V/s.')[-1],number,applicant.group(0))
            bench=re.search(r'IN THE NATIONAL COMPANY LAW TRIBUNAL[ ,]+([A-Z ]{3,40})(?:\s+COURT\s*[-–�]?\s*([IVX]+))?',text)
            if bench:
                put('tribunal','NCLT '+bench.group(1).strip()+(' Court '+bench.group(2) if bench.group(2) else ''),number,bench.group(0))
            if not record['tribunal']:
                bench_heading=re.search(r'\b(?:IN\s+)?THE\s+NATIONAL\s+COMPANY\s+LAW\s+TRIBUNAL\s+([A-Z][A-Z ]{3,40}?)\s+BENCH\b',text)
                if bench_heading:
                    put('tribunal','NCLT '+bench_heading.group(1).title().strip(),number,bench_heading.group(0))
            case=re.search(r'C\.?\s*P\.?\s*\(IB\)\s*NO\.?\s*[A-Z0-9 /,.\-]+?(?:19|20)\d{2}\b',flat,re.I)
            if case:
                put('case_number',case.group(0),number,case.group(0))
        irp=re.search(r'(?:Mr\.?|Ms\.?|Mrs\.?)\s+([A-Z][A-Za-z .]+?),?\s+was\s+appointed\s+as\s+the\s+Interim Resolution Professional',flat)
        if irp:
            put('irp',irp.group(1),number,irp.group(0))
        rp_appointment=re.search(r'(?:Mr\.?|Ms\.?|Mrs\.?)\s+([A-Z][A-Za-z .]+?),?\s+was\s+appointed\s+as\s+(?:the\s+)?Resolution Professional',flat,re.I)
        if rp_appointment:
            put('professional',rp_appointment.group(1),number,rp_appointment.group(0))
        admitted=re.search(r'(?:initiated|commenced)\s+the\s+Corporate Insolvency Resolution Process.{0,180}?Order dated\s+(\d{2}[./-]\d{2}[./-]\d{4})',flat,re.I)
        if admitted:
            put('admission_date',iso_date(admitted.group(1)),number,admitted.group(0))
        # Outcome is assigned only by the operative-order check below. Earlier
        # paragraphs often quote an approval issued in a different case.
        # Explicit before/after role labels; never infer ownership from plan approval.
        roles={'previous_owners':r'(?:Previous|Former|Erstwhile|Pre-resolution) owners?',
               'current_owners':r'(?:Current|Present|Existing) (?:owners?|shareholders?)',
               'subsequent_owners':r'(?:Subsequent|New|Post-resolution) owners?',
               'previous_directors':r'(?:Previous|Former|Erstwhile|Pre-resolution) directors?',
               'current_directors':r'(?:Current|Present|Existing) directors?',
               'subsequent_directors':r'(?:Subsequent|New|Post-resolution) directors?',
               'proposed_owners':r'Proposed (?:owners?|shareholders?)',
               'proposed_directors':r'Proposed directors?',
               'applicant_directors':r'(?:Applicant|Resolution applicant|SRA) directors?',
               'financial_creditors':r'Financial creditors?',
               'plan_submitters':r'(?:Resolution )?Plan submitters?',
               'withdrawn_submitters':r'Withdrawn (?:resolution )?plan submitters?',
               'judges':r'(?:Judges?|Coram member)'}
        def add_person(field, raw, excerpt):
            din=re.search(r'\bDIN\s*[:\-]?\s*(\d{8})\b',raw,re.I)
            cin=re.search(CIN,raw,re.I)
            name=re.split(r'\s*[,;(]\s*(?:DIN|CIN)\b',raw,flags=re.I)[0].strip(' .,:;\t')
            if not name or len(name)>120 or re.search(r'\b(?:application|tribunal|regional director|resolution professional)\b',name,re.I):
                return
            fact={'value':name[:500],'document_id':document_id,'page':number}
            if din: fact['din']=din.group(1)
            if cin: fact['cin']=cin.group().upper()
            if not any(existing['value'].casefold()==name.casefold() for existing in record[field]):
                record[field].append(fact)
                evidence.append({'field':field,'page':number,'excerpt':excerpt[:1000]})
        for field,label in roles.items():
            for match in re.finditer(r'^\s*(?:'+label.replace(' ', r'\s*')+r')\s*:\s*([^\n]+)',text,re.I|re.M):
                add_person(field,match.group(1),match.group(0))
        sentence_role={'previous':'previous','former':'previous','erstwhile':'previous',
                       'current':'current','present':'current','existing':'current',
                       'new':'subsequent','subsequent':'subsequent'}
        for line in text.splitlines():
            mention=re.match(r'^\s*(?P<name>[A-Z][A-Za-z0-9 .,&()\-]{2,100}?)\s+(?:is|was|became)\s+(?:the\s+|an?\s+)?(?P<period>previous|former|erstwhile|current|present|existing|new|subsequent)\s+(?P<role>owner|shareholder|director)\s+of\s+(?:the\s+)?(?P<company>corporate debtor|company)\b',line,re.I)
            if mention:
                role='directors' if mention.group('role').lower()=='director' else 'owners'
                add_person(sentence_role[mention.group('period').lower()]+'_'+role,mention.group('name'),line)
        # Explicit creditor records, with unknown values left null.
        for match in re.finditer(r'^\s*Creditor\s*:\s*([^\n]+)',text,re.I|re.M):
            parts=[p.strip() for p in match.group(1).split(';')]
            values=dict(p.split(':',1) for p in parts[1:] if ':' in p)
            # Normalize known metadata keys, not creditor identities or amounts.
            keys={'planamount':'plan amount','actualpaid':'actual paid','actualpayment':'actual payment'}
            values={keys.get(re.sub(r'\s+', '', k.casefold()),k.strip().casefold()):v.strip() for k,v in values.items()}
            c={'creditor':parts[0],'category':category(values.get('type','other')),'document_id':document_id,'page':number,
               'claimed':money(values.get('claimed',values.get('filed',''))),'admitted':money(values.get('admitted','')),
               'plan_amount':money(values.get('plan amount','')),
               'actual_paid':money(values.get('actual paid',values.get('actual payment',''))),
               'notes':'Automatically extracted; verify source.'}
            if any(c[field] is not None for field in ('claimed','admitted','plan_amount','actual_paid')):
                claim_rows.append(c)
                evidence.append({'field':'claims','page':number,'excerpt':match.group(0)[:1000]})
        layout=page.get('layout_text',text)
        # Recognized three-column table, possibly continued on the following page.
        if re.search(r'Claimed[^\n]*Admitted[^\n]*(?:Proposed Payment|Plan amount)',layout,re.I) and re.search(r'Rs\.?|INR|₹',layout):
            table_header=number
        if table_header and number-table_header<=1:
            lines=layout.splitlines()
            total=re.search(r'^\s*Total\s{2,}([\d,]+(?:\.\d+)?)\s{2,}([\d,]+(?:\.\d+)?)\s{2,}([\d,]+(?:\.\d+)?)\s*$',layout,re.M|re.I)
            if total and record['applicant'] and not record['plans']:
                header_text=extraction['pages'][table_header-1].get('layout_text','')
                scale=Decimal(10000000) if re.search(r'in\s+crores?',header_text,re.I) else Decimal(100000) if re.search(r'in\s+lakhs?',header_text,re.I) else Decimal(1)
                record['plans'].append(Plan(applicant=record['applicant']['value'],amount=money(total.group(3),scale),
                    admitted=money(total.group(2),scale) if Decimal(total.group(2).replace(',',''))>0 else None,
                    document_id=document_id,page=number).model_dump(mode='json'))
                evidence.append({'field':'plans','page':number,'excerpt':total.group().strip()})
            for index,line in enumerate(lines):
                match=re.match(r'^\s*\d+[.)]?\s+([A-Za-z][A-Za-z ]+?)\s{2,}([\d,]+(?:\.\d+)?)\s{2,}([\d,]+(?:\.\d+)?)\s{2,}([\d,]+(?:\.\d+)?)\s*$',line)
                if not match: continue
                name=match.group(1).strip()
                for continuation in lines[index+1:index+4]:
                    tail=continuation.strip()
                    if re.fullmatch(r'(?:Financial|Creditors|Financial Creditors)',tail,re.I):
                        name+=' '+tail
                    else: break
                scale=Decimal(1)
                header_text=' '.join(extraction['pages'][table_header-1].get('layout_text','').split())
                if re.search(r'(?:Rs\.?|INR|amounts?).{0,20}\bin\s+crores?',header_text,re.I): scale=Decimal(10000000)
                elif re.search(r'(?:Rs\.?|INR|amounts?).{0,20}\bin\s+lakhs?',header_text,re.I): scale=Decimal(100000)
                claim_rows.append({'creditor':name,'category':category(name),'scope':'category_total' if 'creditors' in name.casefold() else 'individual',
                    'claimed':money(match.group(2),scale),'admitted':money(match.group(3),scale),'plan_amount':money(match.group(4),scale),
                    'document_id':document_id,'page':number,'notes':'Automatically extracted from financial table; verify column units.'})
                evidence.append({'field':'claims','page':number,'excerpt':line.strip()})
        plan=re.search(r'^\s*(?:Resolution\s*plan\s*amount|Total (?:resolution|plan) amount|(?:Total )?amount under (?:the )?resolution plan|Plan\s*amount)\s*:\s*([^\n;]+)',text,re.I|re.M)
        if plan and record.get('applicant'):
            amount=money(plan.group(1))
            if amount is not None:
                p=Plan(applicant=record['applicant']['value'],amount=amount,document_id=document_id,page=number).model_dump(mode='json')
                if not record['plans']: record['plans'].append(p)
                evidence.append({'field':'plans','page':number,'excerpt':plan.group(0)})
        admitted_match=re.search(r'^\s*(?:Total admitted claims|Total claims admitted)\s*:\s*([^\n;]+)',text,re.I|re.M)
        if admitted_match and admitted_total is None:
            amount=money(admitted_match.group(1))
            if amount is not None and Decimal(amount)>0:
                admitted_total=(amount,number,admitted_match.group(0))
    enrich(record, extraction, document_id, put, claim_rows, evidence, money, iso_date, category)
    enrich_extended(record, extraction, document_id, put, claim_rows, evidence, money, iso_date, category)
    if not record['irp'] and record['professional']:
        for page in extraction['pages']:
            match=re.search(r'(?:appointed\s+the\s+Applicant\s+as|the\s+Applicant\s+was\s+appointed\s+as)\s+the\s+Interim\s+Resolution\s+Professional\b', ' '.join(page['text'].split()), re.I)
            if match:
                put('irp',record['professional']['value'],page['page'],match.group())
                break
    enrich_parties(record, extraction, document_id, evidence, iso_date)
    enrich_asset_tables(record, extraction, document_id, evidence, money)
    if any(row['asset'] != 'Corporate debtor (total)' for row in record['asset_values']):
        record['asset_values'] = [row for row in record['asset_values'] if row['asset'] != 'Corporate debtor (total)']
    enrich_claim_tables(record, extraction, document_id, claim_rows, evidence, money, category)
    enrich_ocr_rows(record, extraction, document_id, claim_rows, evidence, money, category)
    enrich_verified_ocr_claims(record, extraction, document_id, claim_rows, evidence, money)
    enrich_ocr_coc(record, extraction, document_id, claim_rows, evidence, money, warnings)
    enrich_ocr_claim_summary(record, extraction, document_id, claim_rows, evidence, warnings)
    enrich_rejected_claims(record, extraction, document_id, claim_rows, evidence, money)
    enrich_plan_allocations(record, extraction, document_id, evidence, money, warnings)
    enrich_merged_operational_table(record, extraction, document_id, claim_rows, evidence, money)
    if admitted_total and len(record['plans'])==1 and record['plans'][0]['admitted'] is None:
        record['plans'][0]['admitted']=admitted_total[0]
        evidence.append({'field':'plans','page':admitted_total[1],'excerpt':admitted_total[2]})
    # Some supporting judgments describe a debtor's earlier CIRP. Only link
    # their narrative to this debtor when the judgment explicitly calls its
    # captioned petitioner the corporate debtor; never treat it as this order's
    # plan approval.
    opening=' '.join(p['text'] for p in extraction['pages'][:4])
    if re.search(r'\bIN\s+THE\s+HIGH\s+COURT\b',extraction['pages'][0]['text'][:1200],re.I) and re.search(r'Petitioner[-\s]+Corporate\s+Debtor',opening,re.I):
        first=extraction['pages'][0]
        if not record['order_date']:
            dated=re.search(r'\bJUDGMENT\s+DATED\s*:\s*(\d{1,2}[./-]\d{1,2}[./-]\d{4})',first['text'],re.I)
            if dated: put('order_date',iso_date(dated.group(1)),first['page'],dated.group(0))
        if not record['company']:
            caption=re.search(r'(?m)^\s*([A-Z][A-Z0-9 &.,\-]+?(?:PVT\.?\s*LTD\.?|PRIVATE\s+LIMITED|LIMITED|LTD\.?))\s*\n\s*Versus\b',first['text'],re.I)
            if caption:
                put('company',caption.group(1).title().replace('Pvt. Ltd.','Pvt. Ltd.'),first['page'],caption.group(0))
        for page in extraction['pages'][:4]:
            flat=' '.join(page['text'].split())
            if not record['case_number']:
                case=re.search(r'\bC\.?\s*P\.?\s*\(IB\)\s*(?:No\.?\s*)?(\d+)\s*(?:of|/)\s*(\d{4})',flat,re.I)
                if case: put('case_number',f'CP (IB) No. {case.group(1)} of {case.group(2)}',page['page'],case.group(0))
            if not record['tribunal']:
                bench=re.search(r'National Company Law Tribunal\s*\("?NCLT"?\)?\s*,?\s*([A-Z][A-Za-z]+)',flat,re.I)
                if bench: put('tribunal','NCLT '+bench.group(1),page['page'],bench.group(0))
            if not record['industry']:
                business=re.search(r'The Petitioner is a company engaged in the business of\s+([^.\n]{5,100})',flat,re.I)
                if business: put('industry',business.group(1),page['page'],business.group(0))
            if not record['applicant']:
                applicant=re.search(r'approved the resolution plan submitted by\s+([A-Z][A-Za-z0-9 &.,\-]+?(?:Private Limited|Pvt\.? Ltd\.?|Limited|Ltd\.?))\b',flat,re.I)
                if applicant: put('applicant',applicant.group(1),page['page'],applicant.group(0))
            if not record['admission_date']:
                admission=re.search(r'NCLT,?\s+by an order dated\s+(\d{1,2}[./-]\d{1,2}[./-]\d{4}),?\s+admitted\b',flat,re.I)
                if admission: put('admission_date',iso_date(admission.group(1)),page['page'],admission.group(0))
            if not record['resolution_date']:
                approval=re.search(r'NCLT,?\s+by an order dated\s+(\d{1,2}[./-]\d{1,2}[./-]\d{4}).{0,100}?approved the resolution plan',flat,re.I)
                if approval: put('resolution_date',iso_date(approval.group(1)),page['page'],approval.group(0))
    decision=final_decision(extraction)
    if decision and (record['outcome']!=decision['outcome'] or not record['outcome_evidence']):
        record['outcome']=decision['outcome']
        record['outcome_evidence']={'value':decision['excerpt'],'document_id':document_id,'page':decision['page']}
        evidence.append({'field':'outcome_evidence','page':decision['page'],'excerpt':decision['excerpt']})
    # Preserve evidence suggestions, avoid mixing totals with individual creditor rows.
    seen=set()
    for c in claim_rows:
        try:
            row=Claim.model_validate(c).model_dump(mode='json')
        except ValueError:
            warnings.append('A financial row could not be validated; review its source page.')
            continue
        key=(row['category'],row['creditor'].casefold())
        if key in seen: continue
        same=[r for r in record['claims'] if r['category']==row['category']]
        if same and (row['scope']=='category_total' or any(r['scope']=='category_total' for r in same)):
            warnings.append('Conflicting category totals and individual claims need review; no automatic combination was made.')
            continue
        record['claims'].append(row);seen.add(key)
        # A labeled creditor's proposed payment is also an allocation. Reuse
        # its evidence when a separate allocation table is missing/unreadable.
        # Do not convert category totals, rejected schedules or actual payments.
        if (row['scope'] == 'individual' and row['plan_amount'] is not None and
                row['notes'] == 'Automatically extracted; verify source.' and
                not any(item['recipient'].casefold() == row['creditor'].casefold()
                        for item in record['plan_payments'])):
            record['plan_payments'].append({'recipient': row['creditor'],
                'amount': row['plan_amount'], 'admitted': row['admitted'],
                'document_id': row['document_id'], 'page': row['page']})
            source = next((item['excerpt'] for item in evidence
                           if item['field'] == 'claims' and item['page'] == row['page']
                           and row['creditor'] in item['excerpt']), row['notes'])
            evidence.append({'field': 'plan_payments', 'page': row['page'], 'excerpt': source})
    if record['outcome']=='resolution_approved' and record['order_date'] and not record['resolution_date']:
        record['resolution_date']=record['order_date'].copy()
    if record['admission_date'] and record['resolution_date'] and record['admission_date']['value']>record['resolution_date']['value']:
        record['resolution_date']=None
        warnings.append('Extracted dates conflict; resolution date requires review.')
    record['notes']='Automatically populated from explicit PDF evidence. Verify fields and page references before approval.'
    record = CaseRecord.model_validate(record).model_dump(mode='json')
    fields = [field for field in CaseRecord.model_fields if field != 'notes']
    populated = [field for field in fields if record[field] and record[field] != 'unknown']
    return record, {'version':VERSION,'evidence':evidence,'warnings':warnings,
                    'field_review':field_review(record, extraction, document_id, evidence),
                    'populated_fields':populated,
                    'field_status':{field: 'extracted' if field in populated else 'not_found' for field in fields},
                    'missing_fields':[field for field in fields if field not in populated]}


def merge_asset_values(current, extracted):
    """Replace old valuer cards when an average exists; retain unresolved evidence."""
    rows = []
    for item in current + extracted:
        row = AssetValue.model_validate(item)
        if row not in rows:
            rows.append(row)
    chosen = preferred_asset_values(rows)
    chosen_keys = {" ".join(item.asset.casefold().replace("&", "and").split())
                   for item in chosen}
    unresolved = [item for item in current
                  if " ".join(item["asset"].casefold().replace("&", "and").split())
                  not in chosen_keys]
    return unresolved + [item.model_dump(mode="json") for item in chosen]


def fill_missing(current, extracted):
    """Existing entered facts take precedence over automatically extracted suggestions."""
    merged=CaseRecord.model_validate(current).model_dump(mode='json')
    ownership_fields={'financial_creditors','plan_submitters','applicant_directors','judges','withdrawn_submitters',
                      'previous_owners','current_owners','subsequent_owners',
                      'previous_directors','current_directors','subsequent_directors','proposed_owners','proposed_directors'}
    for field,value in extracted.items():
        if field=='notes': continue
        if field=='outcome':
            if merged[field]=='unknown': merged[field]=value
        elif field in ownership_fields and value:
            known={fact['value'].casefold().strip() for fact in merged[field]}
            for fact in value:
                key=fact['value'].casefold().strip()
                if key not in known:
                    merged[field].append(fact)
                    known.add(key)
        elif field == 'claims' and value:
            by_key = {(row['category'], row['creditor'].casefold(), row['scope']): row
                      for row in merged['claims']}
            for row in value:
                key = (row['category'], row['creditor'].casefold(), row['scope'])
                existing = by_key.get(key)
                if existing is None:
                    same_category = [item for item in merged['claims'] if item['category'] == row['category']]
                    if not same_category or (row['scope'] == 'individual' and
                            all(item['scope'] == 'individual' for item in same_category)):
                        merged['claims'].append(row)
                        by_key[key] = row
                    continue
                for amount_field in ('claimed', 'admitted', 'plan_amount', 'actual_paid'):
                    if existing.get(amount_field) is None and row.get(amount_field) is not None:
                        existing[amount_field] = row[amount_field]
                        if amount_field == 'plan_amount':
                            existing['notes'] = ((existing.get('notes') or '') + " " + (row.get('notes') or '')).strip()[:2000]
        elif field == 'asset_values' and value:
            merged[field] = merge_asset_values(merged[field], value)
        elif field == 'plan_payments' and value:
            known = {item['recipient'].casefold() for item in merged[field]}
            for item in value:
                key = item['recipient'].casefold()
                if key not in known:
                    merged[field].append(item)
                    known.add(key)
        elif value and not merged.get(field):
            merged[field]=value
    merged['asset_values'] = merge_asset_values(merged['asset_values'], [])
    if merged['admission_date'] and merged['resolution_date'] and merged['admission_date']['value']>merged['resolution_date']['value']:
        for field in ('admission_date','resolution_date'):
            if not current.get(field): merged[field]=None
    return CaseRecord.model_validate(merged).model_dump(mode='json')
