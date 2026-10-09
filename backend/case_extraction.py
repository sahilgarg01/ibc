"""Evidence-backed draft extraction from explicit labels, order captions and financial tables."""
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from .models import CaseRecord, Claim, Plan

VERSION = 'rules-3'
AMOUNT = r'(?:Rs\.?|INR|₹)?\s*([\d,]+(?:\.\d+)?)\s*(crores?|cr\.?|lakhs?|lacs?|millions?)?'
CIN = r'[LU]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6}'


def money(value, scale=Decimal(1)):
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
    if 'operational' in text:
        return 'operational'
    if any(word in text for word in ('employee','workmen','worker')):
        return 'employee'
    if any(word in text for word in ('government','statutory')):
        return 'government'
    return 'other'


def iso_date(value):
    value=value.strip()
    for fmt in ('%Y-%m-%d','%d.%m.%Y','%d/%m/%Y','%d-%m-%Y','%d %B %Y','%d %b %Y'):
        try:
            return datetime.strptime(value,fmt).date().isoformat()
        except ValueError:
            pass
    return None


def extract_case(extraction, document_id):
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
        'company':r'(?:Corporate debtor|Company name)', 'cin':r'(?:Corporate debtor CIN|Debtor CIN)',
        'case_number':r'(?:Case number|Case no\.)', 'tribunal':r'(?:NCLT bench|Tribunal)',
        'irp':r'(?:Interim resolution professional|IRP)', 'professional':r'(?:Resolution professional|RP)',
        'liquidator':r'Liquidator', 'applicant':r'(?:Successful resolution applicant|Resolution applicant)',
        'admission_date':r'(?:Admission date|CIRP commencement date)',
        'resolution_date':r'(?:Resolution approval date|Resolution date)', 'order_date':r'(?:Order date|Pronounced)',
    }
    claim_rows=[]
    table_header=None
    for page in extraction['pages']:
        number=page['page']
        text=page['text']
        flat=' '.join(text.split())
        for field,label in labels.items():
            match=re.search(r'^\s*'+label+r'\s*:\s*([^\n;]+)',text,re.I|re.M)
            if match:
                value=match.group(1).strip()
                if field.endswith('date'):
                    value=iso_date(value)
                if field=='cin' and not re.fullmatch(CIN,value or ''):
                    value=None
                if value:
                    put(field,value,number,match.group(0))
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
        admitted=re.search(r'(?:initiated|commenced)\s+the\s+Corporate Insolvency Resolution Process.{0,180}?Order dated\s+(\d{2}[./-]\d{2}[./-]\d{4})',flat,re.I)
        if admitted:
            put('admission_date',iso_date(admitted.group(1)),number,admitted.group(0))
        # Outcome must be an operative order, not a request, precedent or CoC vote.
        outcome=re.search(r'(?:The|This)\s+Resolution Plan(?:\s+annexed to the Application)?\s+is\s+(?:hereby\s+)?approved\b',flat,re.I)
        if outcome and record['outcome']=='unknown':
            record['outcome']='resolution_approved'
            put('outcome_evidence',outcome.group(0),number,outcome.group(0))
        liquidation=re.search(r'(?:we\s+hereby\s+order\s+liquidation\s+of\s+the\s+corporate\s+debtor|the\s+corporate\s+debtor\s+is\s+(?:hereby\s+)?ordered\s+to\s+be\s+liquidated)',flat,re.I)
        if liquidation and record['outcome']=='unknown':
            record['outcome']='liquidation_ordered'
            put('outcome_evidence',liquidation.group(0),number,liquidation.group(0))
        # Explicit before/after role labels; never infer ownership from plan approval.
        roles={'previous_owners':r'(?:Previous|Former|Erstwhile|Pre-resolution) owners?',
               'current_owners':r'(?:Current|Present|Existing) (?:owners?|shareholders?)',
               'subsequent_owners':r'(?:Subsequent|New|Post-resolution) owners?',
               'previous_directors':r'(?:Previous|Former|Erstwhile|Pre-resolution) directors?',
               'current_directors':r'(?:Current|Present|Existing) directors?',
               'subsequent_directors':r'(?:Subsequent|New|Post-resolution) directors?'}
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
            for match in re.finditer(r'^\s*(?:'+label+r')\s*:\s*([^\n]+)',text,re.I|re.M):
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
            values={k.strip().casefold():v.strip() for k,v in values.items()}
            c={'creditor':parts[0],'category':category(values.get('type','other')),'document_id':document_id,'page':number,
               'claimed':money(values.get('claimed',values.get('filed',''))),'admitted':money(values.get('admitted','')),
               'plan_amount':money(values.get('plan amount','')),'notes':'Automatically extracted; verify source.'}
            if c['claimed'] is not None or c['admitted'] is not None:
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
        plan=re.search(r'^\s*(?:Resolution plan amount|Total resolution amount|Plan amount)\s*:\s*([^\n;]+)',text,re.I|re.M)
        if plan and record.get('applicant'):
            amount=money(plan.group(1))
            if amount is not None:
                p=Plan(applicant=record['applicant']['value'],amount=amount,document_id=document_id,page=number).model_dump(mode='json')
                if not record['plans']: record['plans'].append(p)
                evidence.append({'field':'plans','page':number,'excerpt':plan.group(0)})
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
    if record['outcome']=='resolution_approved' and record['order_date'] and not record['resolution_date']:
        record['resolution_date']=record['order_date'].copy()
    if record['admission_date'] and record['resolution_date'] and record['admission_date']['value']>record['resolution_date']['value']:
        record['resolution_date']=None
        warnings.append('Extracted dates conflict; resolution date requires review.')
    record['notes']='Automatically populated from explicit PDF evidence. Verify fields and page references before approval.'
    return CaseRecord.model_validate(record).model_dump(mode='json'), {'version':VERSION,'evidence':evidence,'warnings':warnings}


def fill_missing(current, extracted):
    """Existing entered facts take precedence over automatically extracted suggestions."""
    merged=CaseRecord.model_validate(current).model_dump(mode='json')
    ownership_fields={'previous_owners','current_owners','subsequent_owners',
                      'previous_directors','current_directors','subsequent_directors'}
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
        elif value and not merged.get(field):
            merged[field]=value
    if merged['admission_date'] and merged['resolution_date'] and merged['admission_date']['value']>merged['resolution_date']['value']:
        for field in ('admission_date','resolution_date'):
            if not current.get(field): merged[field]=None
    return CaseRecord.model_validate(merged).model_dump(mode='json')
