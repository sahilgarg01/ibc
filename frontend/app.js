const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt = value => value == null ? '—' : new Intl.NumberFormat('en-IN').format(Number(value));
const label = value => ({resolution_approved:'Resolution approved',liquidation_ordered:'Liquidation ordered',liquidation_completed:'Liquidation completed',ongoing:'Ongoing',unknown:'Unknown'}[value] || value);
const state = {cases:[], detail:null, activeDoc:null, page:1, orderPage:1, orderTotal:0};

async function api(url, options={}) {
  const response = await fetch(url, options);
  let data;
  try { data = await response.json(); } catch { data = {detail: response.statusText}; }
  if (!response.ok) {
    const detail = Array.isArray(data.detail) ? data.detail.map(x => x.msg).join('; ') : data.detail;
    throw new Error(detail || `Request failed (${response.status})`);
  }
  return data;
}
function message(value, isError=false) {
  const box = $('#notice'); box.textContent = value; box.className = isError ? 'error' : ''; box.hidden = false;
  clearTimeout(message.timer); message.timer = setTimeout(() => box.hidden = true, 6500);
}
function setBusy(button, busy) { if (button) button.disabled = busy; }
async function showExplorer() { hideImportScreen(); $('#explorer').hidden=false; $('#catalogue').hidden=true; $('#detail').hidden=true; $('#nav-cases').classList.add('active'); $('#nav-orders').classList.remove('active'); history.replaceState({},'', '/'); await refresh(); }
async function showCatalogue() { hideImportScreen(); $('#explorer').hidden=true; $('#catalogue').hidden=false; $('#detail').hidden=true; $('#nav-cases').classList.remove('active'); $('#nav-orders').classList.add('active'); history.replaceState({},'', '/#orders'); await refreshOrders(); }
function showUpload() { $('#upload-dialog').showModal(); }

async function refresh() {
  try {
    const [cases, stats, sample] = await Promise.all([api('/api/cases'), api(`/api/analytics?years=${encodeURIComponent($('#analytics-years').value)}`), api('/api/sample')]);
    state.cases = cases;
    renderResearch(stats);
    $('#sample-source').href = sample.source_url;
    $('#stats').innerHTML = [
      ['TOTAL CASES', stats.total_cases, 'Across all review stages'],
      ['APPROVED', stats.approved_cases, 'Included in analytics'],
      ['RESOLVED', stats.outcomes.resolution_approved || 0, 'Approved resolution cases'],
      ['PLAN HAIRCUT', stats.weighted_plan_haircut_percent == null ? '—' : `${stats.weighted_plan_haircut_percent}%`, `${stats.covered_cases} reviewed cases in selected period`],
    ].map(([title,value,note]) => `<div class="stat"><div class="stat-title">${title}</div><div class="stat-value">${esc(value)}</div><div class="stat-note">${esc(note)}</div></div>`).join('');
    renderList();
    return true;
  } catch (error) { message(error.message, true); return false; }
}
function renderList() {
  const q = $('#search').value.trim().toLowerCase(), status = $('#status').value;
  const cases = state.cases.filter(c => {
    const r = c.record;
    const searchable = [r.company?.value,r.case_number?.value,r.professional?.value,r.cin?.value,...(c.subjects||[])].join(' ').toLowerCase();
    return searchable.includes(q) && (status === 'all' || c.review_status === status);
  });
  $('#empty').hidden = state.cases.length !== 0;
  $('#case-list').innerHTML = cases.map(c => `<tr><td><strong>${esc(c.record.company?.value || c.subjects?.[0] || 'Untitled draft')}</strong><small>${esc(c.record.case_number?.value || 'Case number needed')}</small></td><td>${esc((c.subjects||[]).join(' · ')||'Subject unavailable')}</td><td>${esc(label(c.record.outcome))}</td><td><span class="badge ${esc(c.review_status)}">${esc(c.review_status === 'approved' ? 'Approved' : 'Needs review')}</span></td><td>${c.metrics.plan_haircut_percent == null ? '—' : `${esc(c.metrics.plan_haircut_percent)}%`}${c.review_status==='draft' ? '<small>Draft calculation</small>' : ''}</td><td>${esc(createdTime(c.created_at))}</td><td><button class="text-button open-case" data-id="${esc(c.id)}">Review →</button></td></tr>`).join('');
  $$('.open-case').forEach(b => b.onclick = () => openCase(b.dataset.id));
}
function docOptions(docs, selected) { return docs.map(d => `<option value="${esc(d.id)}" ${d.id===selected?'selected':''}>${esc(d.subject||d.filename)}</option>`).join(''); }
function factRow(key,title,value,docs) {
  const required=key==='company'||key==='case_number'||(key==='outcome_evidence'&&state.detail.record.outcome!=='unknown');
  return `<div class="field-row" data-fact="${esc(key)}"><label>${esc(title)}${required?' <span class="required-mark" title="Required for approval">*</span>':''}<input class="fact-value" value="${esc(value?.value)}" placeholder="Unknown / not documented" ${required?'aria-required="true" title="Required for approval"':''}></label><label>Document<select class="fact-doc">${docOptions(docs,value?.document_id)}</select></label><label>Page<input class="fact-page" type="number" min="1" value="${esc(value?.page || 1)}"></label></div>`;
}
function markApprovalField(input, reason) {
  input.setAttribute('aria-invalid','true');
  input.title=reason;
  input.closest('label')?.classList.add('approval-missing');
}
function clearApprovalMarks() {
  $$('#detail .approval-missing').forEach(label=>label.classList.remove('approval-missing'));
  $$('#detail [aria-invalid="true"]').forEach(input=>{input.removeAttribute('aria-invalid');if(input.getAttribute('aria-required')==='true')input.title='Required for approval';else input.removeAttribute('title');});
  $$('#detail .approval-help').forEach(help=>help.remove());
}
function validateApprovalFields() {
  clearApprovalMarks();
  const missing=[];
  const requireInput=(input,name)=>{
    if(input.value.trim())return;
    markApprovalField(input,`${name} is required for approval`);
    input.closest('label')?.insertAdjacentHTML('beforeend',`<small class="approval-help">${esc(name)} is required for approval.</small>`);
    missing.push({input,name});
  };
  requireInput($('#reviewer'),'Reviewer name');
  requireInput($('[data-fact="company"] .fact-value'),'Corporate debtor');
  requireInput($('[data-fact="case_number"] .fact-value'),'Case number');
  if($('#outcome').value!=='unknown')requireInput($('[data-fact="outcome_evidence"] .fact-value'),'Outcome evidence');
  if(!$('#confirmed').checked){markApprovalField($('#confirmed'),'Confirm that you checked the values and source pages');missing.push({input:$('#confirmed'),name:'Review confirmation'});}
  if(missing.length){message(`Complete required fields: ${missing.map(x=>x.name).join(', ')}.`,true);missing[0].input.scrollIntoView({behavior:'smooth',block:'center'});missing[0].input.focus({preventScroll:true});return false;}
  return true;
}
function updateOutcomeRequirement() {
  const input=$('[data-fact="outcome_evidence"] .fact-value');
  const label=input.closest('label');
  const required=$('#outcome').value!=='unknown';
  let mark=label.querySelector('.required-mark');
  if(required&&!mark){label.insertAdjacentHTML('afterbegin','<span class="required-mark" title="Required for approval">*</span>');mark=label.querySelector('.required-mark');}
  if(!required&&mark)mark.remove();
  if(required){input.setAttribute('aria-required','true');input.title='Required for approval';}
  else{input.removeAttribute('aria-required');input.removeAttribute('title');}
}
function ownerRow(key,value,docs) {
  return `<div class="ownership-row" data-array="${esc(key)}"><input class="fact-value" value="${esc(value?.value)}" placeholder="Name / company"><input class="fact-din" value="${esc(value?.din)}" placeholder="DIN (8 digits)"><input class="fact-cin" value="${esc(value?.cin)}" placeholder="CIN"><select class="fact-doc">${docOptions(docs,value?.document_id)}</select><input class="fact-page" type="number" min="1" value="${esc(value?.page || 1)}">${value?.document_id?`<a href="/api/documents/${encodeURIComponent(value.document_id)}/pdf#page=${encodeURIComponent(value.page||1)}" target="_blank" rel="noopener">PDF page</a>`:''}<button class="text-button remove-row" type="button" aria-label="Remove row">✕</button></div>`;
}
const categories = ['secured_financial','unsecured_financial','operational','employee','government','other'];
function claimCard(value,docs) {
  const c = value || {category:'secured_financial',scope:'individual',page:1};
  return `<div class="claim-card"><div class="claim-top"><label>Creditor or category name<input class="claim-creditor" value="${esc(c.creditor)}" placeholder="Creditor name"></label><button class="text-button remove-row" type="button" aria-label="Remove claim">✕</button></div><div class="claims-grid"><label>Category<select class="claim-category">${categories.map(x => `<option value="${x}" ${c.category===x?'selected':''}>${esc(x.replace('_',' '))}</option>`).join('')}</select></label><label>Scope<select class="claim-scope"><option value="individual" ${c.scope==='individual'?'selected':''}>Individual</option><option value="category_total" ${c.scope==='category_total'?'selected':''}>Category total</option></select></label><label>Claimed ₹<input class="claim-claimed" type="number" min="0" step="0.01" value="${esc(c.claimed)}" placeholder="Unknown"></label><label>Admitted ₹<input class="claim-admitted" type="number" min="0" step="0.01" value="${esc(c.admitted)}" placeholder="Unknown"></label><label>Plan amount ₹<input class="claim-plan" type="number" min="0" step="0.01" value="${esc(c.plan_amount)}" placeholder="Unknown"></label><label>Actual paid ₹<input class="claim-paid" type="number" min="0" step="0.01" value="${esc(c.actual_paid)}" placeholder="Unknown"></label><label>Document<select class="claim-doc">${docOptions(docs,c.document_id)}</select></label><label>Page<input class="claim-page" type="number" min="1" value="${esc(c.page||1)}"></label><label class="wide">Notes<input class="claim-notes" value="${esc(c.notes)}" placeholder="Scope, conditions, caveats"></label></div></div>`;
}
function renderDetail() { hideImportScreen();
  const c=state.detail, r=c.record, docs=c.documents;
  const extractedEvidence=docs.reduce((count,doc)=>count+(doc.extraction?.case_facts?.evidence?.length||0),0);
  $('#explorer').hidden=true; $('#catalogue').hidden=true; $('#detail').hidden=false;
  $('#detail').innerHTML = `<button class="text-button" id="back">← All cases</button><div class="heading-row"><div><div class="eyebrow">CASE RECORD · ${esc(c.review_status.toUpperCase())}</div><h1>${esc(r.company?.value || docs[0]?.subject || 'Untitled case')}</h1><p>${esc(r.case_number?.value || 'Add a case number')} · ${esc(label(r.outcome))}</p></div><span class="badge ${esc(c.review_status)}">${esc(c.review_status === 'approved' ? 'Approved' : 'Needs review')}</span></div><div class="warning">${extractedEvidence?`${esc(extractedEvidence)} PDF evidence items identified. Supported fields have been filled where possible.`:'No supported case facts were found in this PDF. Some orders do not contain the company, claims or plan details this form needs.'} Check every value against its source page before approval.</div><div class="detail-grid"><div>
    <section class="panel"><div class="panel-heading"><div><h2>Case identity</h2><p>Each field needs a document and page.</p></div></div><div class="review-fields">
    ${[['company','Corporate debtor'],['cin','Debtor CIN'],['case_number','Case number'],['tribunal','NCLT bench'],['admission_date','Admission date (YYYY-MM-DD)'],['resolution_date','Resolution date (YYYY-MM-DD)'],['order_date','Order date (YYYY-MM-DD)'],['irp','Interim resolution professional'],['professional','Resolution professional'],['liquidator','Liquidator'],['applicant','Successful applicant'],['outcome_evidence','Outcome evidence text']].map(([key,title])=>factRow(key,title,r[key],docs)).join('')}
    <label>Outcome<select id="outcome">${['unknown','ongoing','resolution_approved','liquidation_ordered','liquidation_completed'].map(x=>`<option value="${x}" ${x===r.outcome?'selected':''}>${esc(label(x))}</option>`).join('')}</select></label></div></section>
    <section class="panel"><div class="panel-heading"><div><h2>Claims and recovery</h2><p>Rupees. Enter matched admitted and planned amounts for a plan haircut.</p></div><button class="secondary" id="add-claim">＋ Add claim</button></div><div class="review-fields" id="claims">${r.claims.map(x=>claimCard(x,docs)).join('')}</div></section>
    <section class="panel"><div class="panel-heading"><div><h2>Ownership and directors</h2><p>Explicit names from PDFs are filled automatically with their source page. Verify each role before approval; a resolution applicant is not automatically an owner.</p></div></div><div class="review-fields">${[['previous_owners','Previous owners'],['current_owners','Current owners / shareholders'],['subsequent_owners','Subsequent owners'],['previous_directors','Previous directors'],['current_directors','Current directors'],['subsequent_directors','Subsequent directors']].map(([key,title])=>`<div class="ownership-group" data-key="${key}"><strong>${title}</strong><div class="ownership-items">${(r[key]||[]).map(x=>ownerRow(key,x,docs)).join('')}</div><button class="text-button add-owner" data-key="${key}">＋ Add name</button></div>`).join('')}</div></section>
    <section class="panel"><div class="panel-heading"><h2>Review notes</h2></div><div class="notes"><textarea id="notes">${esc(r.notes)}</textarea></div><div class="actions"><button class="secondary" id="refill-fields" ${c.review_status==='approved'?'disabled':''}>Fill missing fields from PDF</button><button class="primary" id="save">Save draft</button><span id="save-state" class="section-caption">Version ${esc(c.version)}</span></div><div class="approval"><h3>Approve for analytics</h3><label>Reviewer name<input id="reviewer" value="${esc(c.reviewed_by)}" placeholder="Your name"></label><label class="check-label"><input id="confirmed" type="checkbox">I checked the values and source pages.</label><button class="secondary" id="approve">Approve record →</button></div></section>
    </div><div class="evidence-panel"><section class="panel"><div class="panel-heading"><div><h2>Source evidence</h2><p>Read the exact page before approving a value.</p></div></div><div class="evidence-controls"><select id="document-select">${docOptions(docs,state.activeDoc)}</select><button class="secondary" id="prev-page">←</button><span id="page-count"></span><button class="secondary" id="next-page">→</button><a id="view-pdf" target="_blank" rel="noopener">Open PDF ↗</a><a id="source-link" target="_blank" rel="noopener">Official source ↗</a></div><pre class="page-text" id="page-text"></pre><div class="notes" id="pdf-warning"></div></section><section class="panel"><div class="panel-heading"><div><h2>Record metrics</h2><p>Provisional until approved.</p></div></div><div class="review-fields"><strong>${c.metrics.plan_haircut_percent == null?'Not calculable':`${esc(c.metrics.plan_haircut_percent)}%`}</strong><p>${esc(c.metrics.matched_rows)} of ${esc(c.metrics.total_rows)} rows matched · ₹${fmt(c.metrics.matched_admitted_inr)} admitted · ₹${fmt(c.metrics.matched_plan_inr)} planned</p><p>Formula: (matched admitted − matched plan) ÷ matched admitted.</p></div></section></div></div>`;
  $('#back').onclick = showExplorer;
  $('.detail-grid > div').insertAdjacentHTML('beforeend', `<section class="panel"><div class="panel-heading"><h2>Resolution plans</h2><button class="secondary" id="add-plan">Add plan</button></div><div class="review-fields" id="plans">${(r.plans||[]).map(p=>planCard(p,docs)).join('')}</div></section>`);
  $('#add-plan').onclick=()=>$('#plans').insertAdjacentHTML('beforeend',planCard({},docs));
  $('#add-claim').onclick = () => $('#claims').insertAdjacentHTML('beforeend',claimCard(null,docs));
  $$('.add-owner').forEach(b=>b.onclick=()=> $(`.ownership-group[data-key="${b.dataset.key}"] .ownership-items`).insertAdjacentHTML('beforeend',ownerRow(b.dataset.key,null,docs)));
  $('#detail').onclick = event => { if(event.target.classList.contains('remove-row')) event.target.closest('.claim-card,.ownership-row,.plan-card').remove(); };
  $('#document-select').onchange = e => {state.activeDoc=e.target.value;state.page=1;renderPage();};
  $('#prev-page').onclick=()=>{state.page--;renderPage();}; $('#next-page').onclick=()=>{state.page++;renderPage();};
  $('#save').onclick=saveDraft; $('#approve').onclick=approve; $('#refill-fields').onclick=refillFields;
  $('#outcome').onchange=updateOutcomeRequirement;
  updateOutcomeRequirement();
  renderPage(); window.scrollTo(0,0);
}
function renderPage() {
  const doc=state.detail.documents.find(d=>d.id===state.activeDoc) || state.detail.documents[0];
  if(!doc) return;
  state.activeDoc=doc.id; $('#document-select').value=doc.id;
  const pages=doc.extraction.pages; state.page=Math.min(Math.max(1,state.page),pages.length);
  $('#page-count').textContent=`${state.page} / ${pages.length}`;
  $('#prev-page').disabled=state.page===1; $('#next-page').disabled=state.page===pages.length;
  $('#page-text').textContent=pages[state.page-1].text || '[No extractable text on this page. Open the PDF to review it.]';
  $('#view-pdf').href=`/api/documents/${encodeURIComponent(doc.id)}/pdf#page=${state.page}`;
  $('#source-link').hidden=!doc.source_url; if(doc.source_url) $('#source-link').href=doc.source_url;
  $('#pdf-warning').textContent=`PDF subject: ${doc.subject||'Subject unavailable'}. ${doc.extraction.warnings.join(' ')}`;
}
function collectRecord() {
  const out={outcome:$('#outcome').value,claims:[],notes:$('#notes').value,previous_owners:[],current_owners:[],subsequent_owners:[],previous_directors:[],current_directors:[],subsequent_directors:[]};
  const fact = row => {const value=$('.fact-value',row).value.trim();return value?{value,document_id:$('.fact-doc',row).value,page:Number($('.fact-page',row).value)}:null;};
  $$('[data-fact]').forEach(row=>out[row.dataset.fact]=fact(row));
  $$('.ownership-row').forEach(row=>{const f=fact(row);if(f){f.din=$('.fact-din',row).value.trim()||null;f.cin=$('.fact-cin',row).value.trim().toUpperCase()||null;out[row.dataset.array].push(f);}});
  $$('#claims .claim-card').forEach(card=>{
    const creditor=$('.claim-creditor',card).value.trim(); if(!creditor)return;
    const number=selector=> {const value=$(selector,card).value.trim();return value===''?null:value;};
    out.claims.push({creditor,category:$('.claim-category',card).value,scope:$('.claim-scope',card).value,claimed:number('.claim-claimed'),admitted:number('.claim-admitted'),plan_amount:number('.claim-plan'),actual_paid:number('.claim-paid'),document_id:$('.claim-doc',card).value,page:Number($('.claim-page',card).value),notes:$('.claim-notes',card).value});
  });
  out.plans=$$('.plan-card').map(card=>({applicant:$('.plan-applicant',card).value.trim(),amount:$('.plan-amount',card).value||null,admitted:$('.plan-admitted',card).value||null,document_id:$('.plan-doc',card).value,page:Number($('.plan-page',card).value)}));
  return out;
}
async function saveDraft() {
  const button=$('#save');setBusy(button,true);
  try {const detail=await api(`/api/cases/${state.detail.id}`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({version:state.detail.version,record:collectRecord()})});state.detail=detail;renderDetail();message('Draft saved. Review status is now draft.');}
  catch(error){message(error.message,true);}finally{setBusy(button,false);}
}
async function approve() {
  const reviewer=$('#reviewer').value.trim(),confirmed=$('#confirmed').checked;
  if(!validateApprovalFields())return;
  const button=$('#approve');setBusy(button,true);
  try {
    state.detail=await api(`/api/cases/${state.detail.id}`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({version:state.detail.version,record:collectRecord()})});
    state.detail=await api(`/api/cases/${state.detail.id}/approve`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({version:state.detail.version,reviewer,confirmed})});
    $('#status').value='all';
    $('#search').value='';
    await showExplorer();message('Record approved and included in analytics.');
  } catch(error){message(error.message,true);}
  finally{setBusy(button,false);}
}
async function openCase(id) {try{state.detail=await api(`/api/cases/${id}`);state.activeDoc=state.detail.documents[0]?.id;state.page=1;history.replaceState({},'',`/#case=${id}`);renderDetail();}catch(error){message(error.message,true);}}
async function refillFields(){
  const button=$('#refill-fields'), docId=state.activeDoc;
  if(!docId)return;
  setBusy(button,true);
  try{
    const result=await api(`/api/documents/${encodeURIComponent(docId)}/extract-facts`,{method:'POST'});
    state.detail=await api(`/api/cases/${result.case_id}`);
    renderDetail();
    message(result.evidence_count?`PDF checked: ${result.evidence_count} evidence items found. Missing draft fields were filled where supported.`:'No supported facts were found in this PDF.');
  }catch(error){message(error.message,true);setBusy(button,false);}
}
async function importSample() {const button=$('#import-sample');setBusy(button,true);try{const result=await api('/api/sample/import',{method:'POST'});await openCase(result.case_id);message(result.duplicate?'Sample already loaded.':'Official sample loaded as a draft.');}catch(error){message(error.message,true);}finally{setBusy(button,false);}}
function showDuplicateDialog(result, uploadedFile) {
  let dialog=$('#duplicate-dialog');
  if(!dialog){document.body.insertAdjacentHTML('beforeend','<dialog id="duplicate-dialog" aria-labelledby="duplicate-title"></dialog>');dialog=$('#duplicate-dialog');}
  const original=result.original||{};
  const preview=URL.createObjectURL(uploadedFile);
  dialog.innerHTML=`<h2 id="duplicate-title">This PDF is already stored</h2><p>The uploaded PDF has the same SHA-256 file hash as the original. No second document was created.</p><div class="duplicate-comparison"><div><h3>Selected file</h3><p><strong>${esc(uploadedFile.name)}</strong></p><a href="${esc(preview)}" target="_blank" rel="noopener">Open selected PDF</a></div><div><h3>Stored original</h3><p><strong>${esc(original.filename||'Stored PDF')}</strong><br>Subject: ${esc(original.subject||'Not recorded')}<br>Added: ${esc(createdTime(original.created_at))}<br>Court / authority: ${esc(original.order_kind||'Unknown')}<br>Source: ${esc(original.source_url||'Manual upload')}</p><a href="/api/documents/${encodeURIComponent(result.document_id)}/pdf" target="_blank" rel="noopener">Open stored PDF</a></div></div><div class="actions"><button class="primary" type="button" id="review-duplicate">Review existing case</button><button class="secondary" type="button" id="close-duplicate">Close</button></div>`;
  dialog.onclose=()=>URL.revokeObjectURL(preview);
  $('#close-duplicate').onclick=()=>dialog.close();
  $('#review-duplicate').onclick=async()=>{dialog.close();await openCase(result.case_id);};
  dialog.showModal();
}
async function upload(event) {event.preventDefault();const button=$('#upload-form button[type="submit"]');setBusy(button,true);try{const form=new FormData(event.target);const uploadedFile=form.get('file');const result=await api('/api/documents',{method:'POST',body:form});$('#upload-dialog').close();event.target.reset();if(result.duplicate){showDuplicateDialog(result,uploadedFile);return;}await openCase(result.case_id);const doc=state.detail?.documents.find(d=>d.id===result.document_id);const evidence=doc?.extraction?.case_facts?.evidence?.length||0;message(evidence?`PDF uploaded. ${evidence} evidence items were used to fill supported fields; review them before approval.`:'PDF uploaded, but no supported case facts were found. Review the PDF for fields this form needs.');}catch(error){message(error.message,true);}finally{setBusy(button,false);}}
async function refreshOrders() {
  try {
    const filters=new FormData($('#order-filters'));
    const params=new URLSearchParams([...filters.entries()].filter(([,v])=>v));
    params.set('page',state.orderPage);
    const [result,stats,options,summary]=await Promise.all([api(`/api/catalogue?${params}`),api('/api/orders/stats'),api('/api/orders/filters'),api('/api/orders/summary')]);
    state.orderTotal=result.total;
    $('#order-stats').innerHTML=[['CATALOGUE ROWS',result.total,'Matching current filters'],['IBBI ORDERS',stats.orders,'Official listing entries'],['IBBI PDFS STORED',stats.pdfs_downloaded,'Downloaded public orders'],['PAGES SYNCED',stats.pages_fetched,`${stats.pages_expected||'?'} reported · ${stats.pages_failed} failed`]].map(([title,value,note])=>`<div class="stat"><div class="stat-title">${title}</div><div class="stat-value">${esc(value)}</div><div class="stat-note">${esc(note)}</div></div>`).join('');
    const chosenBench=$('#order-bench').value,chosenRemark=$('#order-remark').value; const categorySelect=$('#order-category'),chosenCategory=categorySelect.value;categorySelect.innerHTML='<option value="">All categories</option>'+(options.categories||[]).map(c=>`<option value="${esc(c)}">${esc(c.toUpperCase())}</option>`).join('');categorySelect.value=chosenCategory;
    $('#order-bench').innerHTML='<option value="">All benches</option>'+options.benches.map(x=>`<option value="${esc(x.key)}">${esc(x.label)}</option>`).join('');
    $('#order-remark').innerHTML='<option value="">All remarks</option>'+options.remarks.map(x=>`<option value="${esc(x)}">${esc(x)}</option>`).join('');
    $('#order-bench').value=chosenBench;$('#order-remark').value=chosenRemark;
    $('#order-empty').hidden=result.total>0;
    $('#order-list').innerHTML=result.items.map(row=>`<tr>
      <td>${esc((row.categories?.length?row.categories:[row.order_kind||'unknown']).join(', ').toUpperCase())}</td>
      <td>${esc(row.order_date||'—')}</td>
      <td><strong>${esc(row.subject||'Subject unavailable')}</strong>${row.pdf_subject&&row.pdf_subject!==row.subject?`<small>PDF title: ${esc(row.pdf_subject)}</small>`:''}</td>
      <td class="order-remark">${esc(row.remarks||'—')}<small>${esc(row.bench_name||'')}</small></td>
      <td><span class="badge ${row.pdf_status==='downloaded'?'approved':'draft'}">${esc(row.pdf_status)}</span>${row.pdf_error?`<small>${esc(row.pdf_error)}</small>`:''}</td>
      <td>${row.filename?`<strong>${esc(row.imported_filename||row.filename)}</strong>`:'PDF pending'}<small>${esc(row.source_url||'Local upload')}</small></td>
      <td>${row.document_id?duplicateCell(row):'—'}</td>
      <td>${esc(createdTime(row.created_at))}${row.imported_at&&row.imported_at!==row.created_at?`<small>Imported: ${esc(createdTime(row.imported_at))}</small>`:''}</td>
      <td>${row.document_id?`<button class="text-button catalogue-content" data-doc="${esc(row.document_id)}">View content</button>`:'—'}</td>
      <td>${row.document_id?`<a href="/api/documents/${encodeURIComponent(row.document_id)}/pdf" target="_blank" rel="noopener">Open PDF</a><br><a href="/api/documents/${encodeURIComponent(row.document_id)}/pdf" download="${esc(row.filename)}">Download PDF</a>`:''}${row.official_pdf_url?`${row.document_id?'<br>':''}<a href="${esc(row.official_pdf_url)}" target="_blank" rel="noopener">Official PDF ↗</a>`:''}${!row.document_id&&!row.official_pdf_url?'—':''}</td>
      <td>${row.case_id?`<button class="text-button catalogue-case" data-case="${esc(row.case_id)}">Review case</button>`:row.source_order_id?`<button class="text-button review-order" data-id="${esc(row.source_order_id)}">Import for review</button>`:'—'}</td>
      </tr>`).join('');
    $$('.review-order').forEach(button=>button.onclick=()=>reviewOrder(button));
    $$('.catalogue-content').forEach(button=>button.onclick=()=>showFileContent(button.dataset.doc));
    $$('.catalogue-case').forEach(button=>button.onclick=()=>openCase(button.dataset.case));
    $('#orders-page').textContent=`Page ${result.page} of ${Math.max(1,Math.ceil(result.total/result.page_size))}`;
    $('#orders-prev').disabled=state.orderPage===1;$('#orders-next').disabled=state.orderPage*result.page_size>=result.total;
    const bars=(items,name)=>{const max=Math.max(1,...items.map(x=>x.orders));return items.map(x=>`<div class="source-bar"><span title="${esc(x[name])}">${esc(x[name])}</span><span class="track"><i style="width:${Math.round(100*x.orders/max)}%"></i></span><strong>${esc(x.orders)}</strong></div>`).join('')||'<p>Nothing imported yet.</p>';};
    $('#order-summary').innerHTML=`<div><h3>By year</h3>${bars(summary.by_year,'year')}</div><div><h3>Top order remarks</h3>${bars(summary.top_remarks,'remarks')}</div>`;
  } catch(error){message(error.message,true);}
}
async function reviewOrder(button){setBusy(button,true);try{const result=await api(`/api/orders/${button.dataset.id}/review`,{method:'POST'});await openCase(result.case_id);message(result.duplicate?'Existing review case opened.':'IBBI PDF loaded into a draft for review.');}catch(error){message(error.message,true);}finally{setBusy(button,false);}}
$('#nav-cases').onclick=showExplorer;$('#nav-orders').onclick=showCatalogue;$('#refresh-explorer').onclick=async()=>{const button=$('#refresh-explorer');setBusy(button,true);try{if(await refresh())message('Case explorer refreshed.');}finally{setBusy(button,false);}};$('#add-document').onclick=showUpload;$('#close-upload').onclick=()=>$('#upload-dialog').close();$('#upload-form').onsubmit=upload;$('#import-sample').onclick=importSample;$('#search').oninput=renderList;$('#status').onchange=renderList;
$('#order-filters').onsubmit=event=>{event.preventDefault();state.orderPage=1;refreshOrders();};
$('#order-filters').onreset=()=>{state.orderPage=1;setTimeout(refreshOrders,0);};
$('#orders-prev').onclick=()=>{state.orderPage--;refreshOrders();};$('#orders-next').onclick=()=>{state.orderPage++;refreshOrders();};
refresh().then(()=>{const match=location.hash.match(/^#case=([a-f0-9]{32})$/);if(match)openCase(match[1]);else if(location.hash==='#orders')showCatalogue();else if(location.hash==='#files')showCatalogue();});

function planCard(p,docs){return `<div class="plan-card claim-card"><label>Applicant<input class="plan-applicant" value="${esc(p.applicant)}"></label><label>Plan amount (INR)<input class="plan-amount" type="number" min="0" step="0.01" value="${esc(p.amount)}"></label><label>Admitted amount covered by this plan (INR)<input class="plan-admitted" type="number" min="0.01" step="0.01" value="${esc(p.admitted)}"></label><label>Document<select class="plan-doc">${docOptions(docs,p.document_id)}</select></label><label>Page<input class="plan-page" type="number" min="1" value="${esc(p.page||1)}"></label><button type="button" class="text-button remove-row">Remove plan</button></div>`;}
function renderResearch(stats){
  const table=(headers,rows)=>rows.length?`<div class="table-scroll"><table><thead><tr>${headers.map(h=>`<th>${esc(h)}</th>`).join('')}</tr></thead><tbody>${rows.map(row=>`<tr>${row.map(v=>`<td>${esc(v)}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`:'<p>No reviewed data available yet.</p>';
  const period=stats.period||{},coverage=stats.coverage||{};
  const professionals=stats.professional_haircuts||[];
  const professionalTable=professionals.length?`<div class="table-scroll"><table><thead><tr><th>ROLE</th><th>PROFESSIONAL</th><th>CASES</th><th>AVERAGE HAIRCUT</th><th>WEIGHTED HAIRCUT</th><th>50%+ CASES</th><th>PATTERN</th><th>CASE EVIDENCE</th></tr></thead><tbody>${professionals.map(x=>`<tr><td>${x.role==='irp'?'IRP':'RP'}</td><td>${esc(x.professional)}</td><td>${esc(x.cases)}</td><td>${esc(x.average_haircut_percent)}%</td><td>${x.weighted_haircut_percent==null?'—':`${esc(x.weighted_haircut_percent)}%`}</td><td>${esc(x.high_haircut_cases)}</td><td>${x.repeated_high_haircuts?'Repeated 50%+':'—'}</td><td>${(x.case_details||[]).map(item=>`<button class="text-button research-case" data-case="${esc(item.case_id)}">${esc(item.company||item.case_number||'Case')} · ${esc(item.haircut_percent)}%</button>`).join('<br>')}</td></tr>`).join('')}</tbody></table></div>`:'<p>No approved resolution cases with calculable haircuts in this period.</p>';
  $('#research-analytics').innerHTML=`<p>${period.years?`${esc(period.from_date)} to ${esc(period.through_date)}`:'All dated cases'} · ${esc(period.reviewed_in_period||0)} approved cases in period · ${esc(period.reviewed_without_date||0)} approved cases lack a usable date and are excluded.</p>`+
    '<h3>Evidence coverage</h3>'+table(['Approved resolutions','Party-wise claims','Admitted claims','Plans','Calculable haircut','Actual payments','Before/after people','Confirmed DIN/CIN overlap'],[[coverage.resolution_cases||0,coverage.with_party_claims||0,coverage.with_admitted_claims||0,coverage.with_plans||0,coverage.with_haircut||0,coverage.with_actual_payments||0,coverage.with_before_after_people||0,coverage.with_identifier_confirmed_overlap||0]])+
    '<h3>Party-wise claims</h3><p>Filed and admitted INR totals use individually named creditors only; category totals are kept separate to prevent double counting.</p>'+table(['Creditor type','Parties','Filed values','Admitted values','Filed INR','Admitted INR'],(stats.party_claims_by_type||[]).map(x=>[x.category.replaceAll('_',' '),x.party_rows,x.filed_rows,x.admitted_rows,fmt(x.filed_inr),fmt(x.admitted_inr)]))+
    '<h3>IRP / RP associated haircuts</h3><p>These are associations with reviewed cases; the resolution applicant submits a plan. Names without registration IDs may refer to different people.</p>'+professionalTable+
    '<h3>Bench timelines</h3>'+table(['Bench','Cases','Average days','Minimum','Maximum'],(stats.bench_timelines||[]).map(x=>[x.bench,x.cases,x.average_days,x.min_days,x.max_days]))+`<p>${esc(stats.missing_timeline_cases||0)} cases in the period lack complete timeline dates.</p>`;
  const evidenceLink=fact=>fact?.document_id&&fact?.page?`<a href="/api/documents/${encodeURIComponent(fact.document_id)}/pdf#page=${encodeURIComponent(fact.page)}" target="_blank" rel="noopener">PDF p. ${esc(fact.page)}</a>`:'Source page unavailable';
  const people=facts=>(facts||[]).length?facts.map(f=>`<div><strong>${esc(f.value)}</strong>${f.din?` · DIN ${esc(f.din)}`:''}${f.cin?` · CIN ${esc(f.cin)}`:''}<small>${evidenceLink(f)}</small></div>`).join(''):'Not documented';
  const profiles=stats.irp_profiles||[];
  const comparisons=stats.ownership_comparisons||[];
  const irpHtml=profiles.length?profiles.map(profile=>`<details class="research-profile"><summary><strong>${esc(profile.name)}</strong> · ${esc(profile.cases)} approved case${profile.cases===1?'':'s'} · ${esc(profile.approved_resolutions)} approved resolution${profile.approved_resolutions===1?'':'s'} · ${profile.average_haircut_percent==null?'Haircut unavailable':`Average recorded haircut ${esc(profile.average_haircut_percent)}% (${esc(profile.haircut_cases)} case${profile.haircut_cases===1?'':'s'})`}</summary><div class="table-scroll"><table><thead><tr><th>CASE</th><th>ORDER / RESOLUTION DATE</th><th>OUTCOME</th><th>HAIRCUT</th><th>IRP EVIDENCE</th></tr></thead><tbody>${profile.case_details.map(item=>`<tr><td><button class="text-button research-case" data-case="${esc(item.case_id)}">${esc(item.company)}</button><small>${esc(item.case_number||'Case number not recorded')}</small></td><td>${esc(item.order_date||'Not recorded')}</td><td>${esc(label(item.outcome))}</td><td>${item.haircut_percent==null?'—':`${esc(item.haircut_percent)}%`}</td><td>${evidenceLink(item.evidence)}</td></tr>`).join('')}</tbody></table></div></details>`).join(''):'<p>No approved case has a documented IRP name yet.</p>';
  const ownershipHtml=comparisons.length?`<div class="table-scroll"><table><thead><tr><th>CASE</th><th>ROLE</th><th>BEFORE</th><th>CURRENT</th><th>AFTER</th><th>COMPARISON</th></tr></thead><tbody>${comparisons.flatMap(item=>item.roles.map(role=>`<tr><td><button class="text-button research-case" data-case="${esc(item.case_id)}">${esc(item.company)}</button><small>${esc(item.case_number||'')}</small></td><td>${esc(role.role)}</td><td>${people(role.before)}</td><td>${people(role.current)}</td><td>${people(role.after)}</td><td>${role.confirmed.length?`Confirmed DIN/CIN overlap: ${esc(role.confirmed.map(x=>`${x.before} / ${x.after}`).join('; '))}`:role.name_candidates.length?`Possible name-only overlap: ${esc(role.name_candidates.map(x=>`${x.before} / ${x.after}`).join('; '))}`:!role.before.length||!role.after.length?'Incomplete: one side not documented':'No matching identifier or name documented'}</td></tr>`)).join('')}</tbody></table></div>`:'<p>No approved case has documented ownership or director names yet.</p>';
  $('#research-analytics').insertAdjacentHTML('beforeend',`<h3>IRP profiles</h3><p>All approved cases, including undated cases. Profiles group exact normalized names; a shared name does not prove the same person. Open a case or source page to verify it.</p>${irpHtml}<h3>Before / after ownership and directors</h3><p>All approved cases with at least one documented side. CIN/DIN matches are confirmed identifier overlaps; name-only matches require review. Missing names do not establish a change in ownership.</p>${ownershipHtml}`);
  $$('.research-case').forEach(button=>button.onclick=()=>openCase(button.dataset.case));
}
$('#analytics-years').onchange=refresh;
$('#entity-form').onsubmit=async event=>{event.preventDefault();try{const values=[...new FormData(event.target)].filter(([,v])=>v.trim());const matches=await api(`/api/entities/match?${new URLSearchParams(values)}`);$('#entity-results').innerHTML=matches.length?matches.map(m=>`<p>${esc(m.name)} · ${esc(m.identifier||'No identifier')} · ${esc(m.match)} · ${esc(m.score)}</p>`).join(''):'No matches found.';}catch(e){message(e.message,true);}};

const pageLimit=$('#import-form input[name="max_pages"]');
pageLimit.required=false;pageLimit.removeAttribute('max');pageLimit.value='';pageLimit.placeholder='Blank = all listing pages';
pageLimit.parentElement.insertAdjacentHTML('beforeend','<small>Leave blank to follow pagination until the PDF file limit is reached.</small>');

let importHistoryPage=1, selectedImport=null, jobFilesPage=1, importHistoryTimer;
function hideImportScreen(){clearTimeout(importHistoryTimer);$('#import-screen').hidden=true;$('#nav-import').classList.remove('active');}
function createdTime(value){if(!value)return 'Not recorded';const date=new Date(value);return Number.isNaN(date.getTime())?'Not recorded':date.toLocaleString(undefined,{dateStyle:'medium',timeStyle:'medium'});}
async function showImportScreen(){['explorer','catalogue','detail'].forEach(id=>$('#'+id).hidden=true);['nav-cases','nav-orders'].forEach(id=>$('#'+id).classList.remove('active'));$('#import-screen').hidden=false;$('#nav-import').classList.add('active');history.replaceState({},'', '/#imports');await refreshImportHistory();}
async function refreshImportHistory(){
  clearTimeout(importHistoryTimer);
  try{
    await refreshScheduler();
    const result=await api(`/api/import-history?page=${importHistoryPage}`);
    $('#import-job-list').innerHTML=result.items.map(j=>`<tr data-job="${esc(j.id)}" tabindex="0" title="Double-click to view files"><td>${esc(j.url)}</td><td>${esc((j.subjects||[]).join(' · ')||'Not extracted yet')}</td><td>${esc(createdTime(j.created_at))}</td><td><span class="badge">${esc(j.status)}</span></td><td>${j.pages}</td><td>${j.discovered}</td><td>${j.imported}</td><td>${j.duplicates}</td><td>${j.failed}</td><td>${j.errors}</td><td><button class="text-button open-import-job" data-job="${esc(j.id)}">View files</button></td></tr>`).join('');
    $('#imports-empty').hidden=result.total!==0;$('#imports-page').textContent=`Page ${importHistoryPage} of ${Math.max(1,Math.ceil(result.total/result.page_size))} · ${result.total} tasks`;$('#imports-prev').disabled=importHistoryPage===1;$('#imports-next').disabled=importHistoryPage*result.page_size>=result.total;
    $$('#import-job-list tr').forEach(row=>{row.ondblclick=()=>selectImport(row.dataset.job);row.onkeydown=e=>{if(e.key==='Enter')selectImport(row.dataset.job);};});
    $$('.open-import-job').forEach(b=>b.onclick=()=>selectImport(b.dataset.job));
    if(selectedImport)await refreshJobFiles();
  }catch(e){message(e.message,true);}
  if(!$('#import-screen').hidden)importHistoryTimer=setTimeout(refreshImportHistory,3000);
}
async function selectImport(id){selectedImport=id;jobFilesPage=1;$('#import-job-detail').hidden=false;await refreshJobFiles();$('#import-job-detail').scrollIntoView({behavior:'smooth',block:'start'});}
async function refreshJobFiles(){
  const id=selectedImport;
  try{
    const [job,files]=await Promise.all([api(`/api/imports/${id}`),api(`/api/import-files?job_id=${encodeURIComponent(id)}&page=${jobFilesPage}`)]);
    if(id!==selectedImport)return;
    $('#import-job-title').textContent=`Files for ${job.url} (${job.status})`;
    $('#import-job-errors').innerHTML=`${job.result.limits_reached?'<p>Configured import limit reached.</p>':''}${job.result.error?`<p>${esc(job.result.error)}</p>`:''}${(job.result.errors||[]).length?'<table><thead><tr><th>FAILED URL</th><th>ERROR</th></tr></thead><tbody>'+job.result.errors.map(e=>`<tr><td>${esc(e.url)}</td><td>${esc(e.error)}</td></tr>`).join('')+'</tbody></table>':''}`;
    $('#import-job-files').innerHTML=files.items.map(f=>`<tr><td>${esc((f.categories?.length?f.categories:[f.order_kind||'unknown']).join(', ').toUpperCase())}</td><td>${esc(f.subject||'Subject unavailable')}</td><td><strong>${esc(f.imported_filename||f.filename)}</strong><small>${esc(f.source_url)}</small></td><td>${duplicateCell(f)}</td><td>${esc(createdTime(f.created_at))}</td><td><button class="text-button job-content" data-doc="${esc(f.document_id)}">View content</button><br><button class="text-button job-review" data-case="${esc(f.case_id)}">Review case</button></td><td><a href="/api/documents/${encodeURIComponent(f.document_id)}/pdf" target="_blank" rel="noopener">Open PDF</a><br><a href="/api/documents/${encodeURIComponent(f.document_id)}/pdf" download="${esc(f.filename)}">Download PDF</a></td></tr>`).join('');
    $('#import-job-empty').hidden=files.total!==0;$('#job-files-page').textContent=`Page ${jobFilesPage} of ${Math.max(1,Math.ceil(files.total/files.page_size))} · ${files.total} files`;$('#job-files-prev').disabled=jobFilesPage===1;$('#job-files-next').disabled=jobFilesPage*files.page_size>=files.total;
    $$('.job-content').forEach(b=>b.onclick=()=>showFileContent(b.dataset.doc));$$('.job-review').forEach(b=>b.onclick=()=>openCase(b.dataset.case));
  }catch(e){message(e.message,true);}
}
$('#nav-import').onclick=showImportScreen;$('#refresh-imports').onclick=refreshImportHistory;
$('#imports-prev').onclick=()=>{importHistoryPage--;refreshImportHistory();};$('#imports-next').onclick=()=>{importHistoryPage++;refreshImportHistory();};
$('#job-files-prev').onclick=()=>{jobFilesPage--;refreshJobFiles();};$('#job-files-next').onclick=()=>{jobFilesPage++;refreshJobFiles();};
$('#import-form').onsubmit=async event=>{event.preventDefault();const button=$('button',event.target);setBusy(button,true);try{const values=Object.fromEntries(new FormData(event.target));values.max_files=Number(values.max_files);values.max_pages=values.max_pages.trim()===''?null:Number(values.max_pages);const job=await api('/api/imports',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(values)});$('#import-progress').textContent='Import queued. Its counts and status will update in the task table.';importHistoryPage=1;await refreshImportHistory();await selectImport(job.id);}catch(e){$('#import-progress').textContent=e.message;}finally{setBusy(button,false);}};
if(location.hash==='#imports')showImportScreen();
function duplicateCell(file){
  if(!file.duplicate){
    const related=(file.possible_matches||[]).map(m=>`<small>Possible match: ${esc(m.filename)} (${esc(m.reasons.map(reason=>reason.replaceAll('_',' ')).join(', '))})</small><a href="/api/documents/${encodeURIComponent(m.document_id)}/pdf" download="${esc(m.filename)}">Download candidate PDF</a>`).join('');
    return '<span class="badge approved">No exact duplicate</span>'+related;
  }
  const original=file.duplicate_of||{document_id:file.document_id,filename:file.filename};
  return `<span class="badge draft">Yes</span><small>Duplicate of: ${esc(original.filename)}</small><small>Original source: ${esc(original.source_url||'Local upload')}</small><small>Document ID: ${esc(original.document_id)}</small><a href="/api/documents/${encodeURIComponent(original.document_id)}/pdf" download="${esc(original.filename)}">Download matching PDF</a>`;
}
async function showFileContent(id){try{message('Reading document content. Scanned pages may take a few seconds for OCR.');const doc=await api(`/api/documents/${id}/content`);let dialog=$('#content-dialog');if(!dialog){document.body.insertAdjacentHTML('beforeend','<dialog id="content-dialog"><button class="secondary" id="close-content">Close</button><h2 id="content-title"></h2><p id="content-warning"></p><pre id="content-text" class="page-text"></pre></dialog>');dialog=$('#content-dialog');$('#close-content').onclick=()=>dialog.close();}$('#content-title').textContent=doc.subject||doc.filename;$('#content-warning').textContent=doc.warnings.join(' ');$('#content-text').textContent=doc.pages.map(p=>`Page ${p.page}\n${p.text||'[No extractable text]'}`).join('\n\n');dialog.showModal();}catch(e){message(e.message,true);}}

let currentSchedule=null;
async function refreshScheduler(){
  currentSchedule=await api('/api/scheduler');
  const select=$('#sync-category');
  if(!select.dataset.initialized){
    select.innerHTML='<option value="all">All categories</option>'+currentSchedule.sources.map(s=>`<option value="${esc(s.category)}">${esc(s.category.toUpperCase())}</option>`).join('');
    select.value=currentSchedule.category||'all';select.dataset.initialized='true';
  }
  $('#toggle-scheduler').textContent=currentSchedule.enabled?'Disable scheduler':'Enable scheduler';
  $('#sync-now').disabled=currentSchedule.running;
  $('#scheduler-status').textContent=`${currentSchedule.enabled?'Scheduled collection enabled':'Scheduled collection disabled'} · ${currentSchedule.running?'Sync running':'Idle'} · Next run: ${currentSchedule.enabled&&currentSchedule.next_run?createdTime(currentSchedule.next_run):'—'}`;
  if(document.activeElement!==$('#schedule-interval'))$('#schedule-interval').value=currentSchedule.interval_minutes;
  $('#scheduler-sources').innerHTML=currentSchedule.sources.map(s=>`<tr><td>${esc(s.category.toUpperCase())}</td><td>${esc(createdTime(s.created_at))}</td><td>${esc(s.last_sync?createdTime(s.last_sync):'Not synced yet')}</td><td>${esc(s.status)}</td><td>${esc(s.next_page)}</td><td>${esc(s.error||'')}</td></tr>`).join('');
}
async function saveScheduler(enabled){try{const interval=Number($('#schedule-interval').value);await api('/api/scheduler',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled,interval_minutes:interval,category:$('#sync-category').value})});await refreshScheduler();message(enabled?'Scheduler enabled. The first run collects historical orders; later runs skip stored orders.':'Scheduler disabled. A currently running sync will finish.');}catch(e){message(e.message,true);}}
$('#toggle-scheduler').onclick=()=>saveScheduler(!currentSchedule?.enabled);
$('#save-schedule').onclick=()=>saveScheduler(Boolean(currentSchedule?.enabled));
$('#sync-now').onclick=async()=>{try{const job=await api(`/api/scheduler/sync?category=${encodeURIComponent($('#sync-category').value)}`,{method:'POST'});importHistoryPage=1;await refreshImportHistory();await selectImport(job.id);}catch(e){message(e.message,true);}};
