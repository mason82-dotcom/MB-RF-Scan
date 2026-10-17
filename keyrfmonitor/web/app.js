'use strict';
const $ = id => document.getElementById(id);
const token = document.querySelector('meta[name="rf-token"]').content;
const statusText = {stopped:'Gestoppt',starting:'Kalibriert',running:'Empfang aktiv',stopping:'Beendet',error:'Fehler'};
function fmtTime(s){try{return new Date(s).toLocaleString('de-DE',{dateStyle:'short',timeStyle:'medium'})}catch{return '–'}}

let sessionsLastFetched=0;
let recentScanSessions=[];
let detailSelectionLast=null;

function clearSessionDetails(message){
 $('session-detail-status').textContent=message;
 $('detail-bursts').textContent='–';$('detail-pairs').textContent='–';
 $('detail-timeline').replaceChildren();
 $('detail-snr').replaceChildren();$('detail-duration').replaceChildren();
 $('session-detail-warning').textContent='';
 $('detail-timeline').setAttribute('aria-label',message);
}
function chooseSessionDetail(items){
 const choice=$('session-detail-choice');
 const previous=choice.value;
 choice.replaceChildren();
 if(!items.length){
  choice.disabled=true;
  const o=document.createElement('option');o.value='';o.textContent='Keine Sitzungen verfügbar';choice.append(o);
  clearSessionDetails('Noch keine Sitzung aufgezeichnet.');
  detailSelectionLast=null;return;
 }
 choice.disabled=false;
 for(const session of items){
  const option=document.createElement('option');
  option.value=String(session.id);
  option.textContent='#'+session.id+' · '+fmtTime(session.started_at);
  choice.append(option);
 }
 choice.value=items.some(item=>String(item.id)===previous)?previous:String(items[0].id);
 if(choice.value!==detailSelectionLast){
  clearSessionDetails('Lade Sitzung #'+choice.value+' …');
  detailSelectionLast=null;
 }
}
function renderDetailHistogram(elementId,items){
 const root=$(elementId);root.replaceChildren();
 const max=Math.max(1,...items.map(item=>item.count));
 for(const item of items){
  const line=document.createElement('div');line.className='detail-histogram-line';
  const label=document.createElement('span');label.textContent=item.label;
  const barTrack=document.createElement('div');barTrack.className='detail-histogram-track';
  const bar=document.createElement('span');bar.className='detail-histogram-fill';
  bar.style.width=100*item.count/max+'%';barTrack.append(bar);
  const count=document.createElement('b');count.textContent=String(item.count);
  line.append(label,barTrack,count);root.append(line);
 }
}
function renderSessionDetail(data){
 if(!data.timeline_available){
  clearSessionDetails('Diese ältere Sitzung hat keine eindeutig zugeordneten Burst-Metadaten. Keine Rückrechnung möglich.');
  $('detail-step').textContent='–';
  return;
 }
 const root=$('detail-timeline');root.replaceChildren();
 const max=Math.max(1,...data.buckets.map(b=>Math.max(b.bursts,b.pairs)));
 const totalBursts=data.included_bursts, totalPairs=data.included_pairs;
 $('detail-bursts').textContent=String(totalBursts);
 $('detail-pairs').textContent=String(totalPairs);
 $('detail-step').textContent=data.bucket_seconds+' s';
 for(const bucket of data.buckets){
  const cell=document.createElement('div');cell.className='detail-timeline-cell';
  const bars=document.createElement('div');bars.className='detail-timeline-bars';
  for(const [kind,value] of [['burst',bucket.bursts],['pair',bucket.pairs]]){
   const bar=document.createElement('span');bar.className='detail-timeline-bar '+kind;
   bar.style.height=value?Math.max(4,100*value/max)+'%':'0%';bars.append(bar);
  }
  const tick=document.createElement('span');tick.className='detail-timeline-tick';
  tick.textContent=String(bucket.from_s)+'s';
  const annotation=[
   bucket.from_s+'–'+bucket.to_s+' s',
   bucket.bursts+' Bursts',
   bucket.pairs+' Paare',
   'Ø SNR: '+fmtNumber(bucket.avg_snr_db,'dB'),
   'Ø Dauer: '+fmtNumber(bucket.avg_duration_ms,'ms'),
  ];
  cell.title=annotation.join(' | ');
  cell.append(bars,tick);root.append(cell);
 }
 root.setAttribute('aria-label',totalBursts+' Bursts und '+totalPairs+' Paare in '+data.bucket_seconds+'-Sekunden-Buckets');
 renderDetailHistogram('detail-snr',data.snr_distribution);
 renderDetailHistogram('detail-duration',data.duration_distribution);
 $('session-detail-status').textContent='Scan #'+data.session_id+' · '+fmtTime(data.started_at)
    +' · '+data.result+' · ADS-B: '+data.restoration;
 const warnings=[];
 if(data.truncated)warnings.push('Mehr als 10.000 Ereignisse zugeordnet: Verlauf ist gekürzt.');
 if(data.ignored_events)warnings.push(data.ignored_events+' Ereignisse außerhalb der Auswertezeit oder ohne gültige Zeitinformation.');
 if(!totalBursts)warnings.push('Keine Burst-Metadaten im ausgewählten Verlauf.');
 $('session-detail-warning').textContent=warnings.join(' ');
}
async function refreshSessionDetail(){
 const choice=$('session-detail-choice').value;
 if(!choice)return;
 const response=await fetch('/api/session-detail?id='+encodeURIComponent(choice)+'&bucket=5',{cache:'no-store'});
 if(!response.ok)throw Error('HTTP '+response.status);
 const detail=await response.json();
 // Ignore responses for a selection the user changed while loading.
 if(choice!==$('session-detail-choice').value)return;
 renderSessionDetail(detail);
 detailSelectionLast=choice;
}

const sessionDiagnoses={
 adsb_restore_failed:'ADS-B-Wiederherstellung beim Scan fehlgeschlagen',
 unfinished_scan:'Abgelaufene Sitzung ohne Abschlussprotokoll',
 no_bursts:'Keine Bursts erkannt',
 no_pairs:'Keine Burst-Paare erkannt',
 snr_missing:'SNR-Messwerte fehlen',
};
const comparisonUnits={
 burst_rate:'Bursts/min',pair_rate:'Paare/min',
 avg_snr_db:'dB',avg_duration_ms:'ms',avg_rise_db:'dB',
};
function comparisonValue(item,metric){
 if(metric==='burst_rate'||metric==='pair_rate'){
  const duration=item.elapsed_seconds>0?item.elapsed_seconds:item.planned_seconds;
  return duration>0?(metric==='burst_rate'?item.burst_count:item.pair_count)*60/duration:null;
 }
 return item.metrics_available&&Number.isFinite(item[metric])?item[metric]:null;
}
function renderComparison(items){
 const metric=$('comparison-metric').value;
 const root=$('comparison-chart');root.replaceChildren();
 const eligible=items.filter(i=>i.result==='completed'&&i.restoration==='verified').slice(0,12).reverse();
 const numbers=eligible.map(i=>comparisonValue(i,metric));
 const max=Math.max(1,...numbers.filter(Number.isFinite));
 if(!numbers.some(Number.isFinite)){
  const p=document.createElement('p');p.className='empty';
  p.textContent=eligible.length?'Für diese Kennzahl liegen noch keine Sitzungsdaten ab v0.6.0 vor.':'Noch keine abgeschlossenen Scan-Sitzungen vorhanden.';
  root.append(p);root.setAttribute('aria-label',p.textContent);return;
 }
 for(const [index,item] of eligible.entries()){
  const value=numbers[index];
  const element=document.createElement('div');element.className='comparison-item';
  const valueText=Number.isFinite(value)?value.toFixed(1):'–';
  const num=document.createElement('span');num.className='comparison-value';num.textContent=valueText;
  const barArea=document.createElement('div');barArea.className='comparison-bar-area';
  const bar=document.createElement('div');bar.className='comparison-bar';
  bar.style.height=Number.isFinite(value)&&value>0?Math.max(5,100*value/max)+'%':'0%';
  barArea.append(bar);
  const label=document.createElement('span');label.className='comparison-label';label.textContent='#'+item.id;
  element.title='Scan #'+item.id+', '+fmtTime(item.started_at)+': '+valueText+' '+comparisonUnits[metric];
  element.append(num,barArea,label);root.append(element);
 }
 root.setAttribute('aria-label',eligible.length+' Scan-Sitzungen im Vergleich: '+comparisonUnits[metric]);
}
function renderSessionDiagnostics(items){
 const root=$('session-diagnostics');
 const problems=[];
 for(const s of items){
  for(const code of s.diagnostics||[]){
   if(sessionDiagnoses[code])problems.push('Scan #'+s.id+': '+sessionDiagnoses[code]);
  }
 }
 if(!problems.length){root.textContent='Keine regelbasierten Hinweise in den letzten '+items.length+' Sitzungen.';return}
 root.textContent=problems.slice(0,5).join(' · ')+(problems.length>5?' · Weitere Hinweise in der Tabelle.':'');
}
function renderSessions(items){
 const body=$('session-body');body.replaceChildren();
 if(!items.length){
  const tr=document.createElement('tr');const td=document.createElement('td');
  td.colSpan=9;td.className='empty';td.textContent='Noch keine Scan-Sitzungen protokolliert';
  tr.append(td);body.append(tr);
  renderComparison(items);renderSessionDiagnostics(items);return;
 }
 const states={running:'Läuft / unvollständig',completed:'Beendet',interrupted:'Unterbrochen',error:'Fehler'};
 const recovery={pending:'Nicht bestätigt',verified:'Ja, geprüft',failed:'Nein / Fehler',not_required:'Nicht erforderlich'};
 for(const item of items){
  const tr=document.createElement('tr');
  const state=item.stale?'Unvollständig – prüfen':states[item.result]||'Unbekannt';
  const notes=(item.diagnostics||[]).map(code=>sessionDiagnoses[code]||code);
  const values=[
   fmtTime(item.started_at),
   item.planned_seconds+' s / '+(item.finished_at?item.elapsed_seconds+' s':'läuft / offen'),
   state,
   recovery[item.restoration]||'Unbekannt',
   item.burst_count+' / '+item.pair_count,
   item.metrics_available?fmtNumber(item.avg_duration_ms,'ms'):'Nicht erfasst',
   item.metrics_available?fmtNumber(item.avg_snr_db,'dB'):'Nicht erfasst',
   item.metrics_available?fmtNumber(item.peak_snr_db,'dB'):'Nicht erfasst',
   notes.length?notes.join('; '):'–',
  ];
  for(const value of values){const td=document.createElement('td');td.textContent=value;tr.append(td)}
  if(item.restoration==='failed'||item.stale)tr.className='session-warning';
  body.append(tr);
 }
 renderComparison(items);renderSessionDiagnostics(items);
}
async function refreshSessions(){
 const res=await fetch('/api/sessions',{cache:'no-store'});
 if(!res.ok)throw Error('HTTP '+res.status);
 const result=await res.json();
 recentScanSessions=result.sessions;
 renderSessions(recentScanSessions);
 chooseSessionDetail(recentScanSessions);
 try{await refreshSessionDetail()}
 catch(e){$('session-detail-status').textContent='Sitzungsdetails nicht erreichbar: '+e.message}
 $('sessions-error').textContent='';
 $('sessions-updated').textContent='Aktualisiert '+new Date().toLocaleTimeString('de-DE');
 sessionsLastFetched=Date.now();
}

const levelSamples=[];
let oldState='stopped', historyFetched=0, polling=false;
function fmtNumber(n,unit){return Number.isFinite(n)?n.toFixed(1)+' '+unit:'–'}
function renderTrend(){
 const root=$('signal-trend');root.replaceChildren();
 if(!levelSamples.length){
  const p=document.createElement('p');p.className='empty';
  p.textContent='Nur aktive Web-Messungen liefern Livepegel; Terminal-Scans erscheinen im Verlauf.';
  root.append(p);root.setAttribute('aria-label','Keine Live-Pegel');return;
 }
 for(const sample of levelSamples){
  const bar=document.createElement('span');bar.className='trend-bin';
  bar.style.height=Math.min(100,Math.max(3,sample*2.5))+'%';
  bar.title=sample.toFixed(1)+' dB';root.append(bar);
 }
 root.setAttribute('aria-label',levelSamples.length+' Pegelwerte; letzter Wert '+levelSamples.at(-1).toFixed(1)+' dB');
}
function renderSummary(s){
 $('history-bursts').textContent=String(s.burst_count);
 $('history-pairs').textContent=String(s.pair_count);
 $('history-duration').textContent=fmtNumber(s.avg_duration_ms,'ms');
 $('history-snr').textContent=fmtNumber(s.avg_snr_db,'dB');
 $('history-last').textContent=s.last_event_at?fmtTime(s.last_event_at):'Keine Ereignisse';
 const root=$('history-chart');root.replaceChildren();
 const max=Math.max(1,...s.buckets.map(b=>Math.max(b.bursts,b.pairs)));
 for(const b of s.buckets){
  const hour=new Date(b.hour);
  const label=hour.toLocaleString('de-DE',{dateStyle:'short',timeStyle:'short'});
  const column=document.createElement('div');
  column.className='history-bin'+(s.buckets.length>30?' compact':'');
  column.title=label+': '+b.bursts+' Bursts, '+b.pairs+' Paare';
  const bars=document.createElement('div');bars.className='history-bars';
  for(const [kind,value] of [['burst',b.bursts],['pair',b.pairs]]){
   const bar=document.createElement('span');bar.className='history-bar '+kind;
   bar.style.height=(value?Math.max(5,100*value/max):0)+'%';bars.append(bar);
  }
  const tick=document.createElement('span');tick.className='history-tick';
  tick.textContent=s.buckets.length>30?hour.toLocaleDateString('de-DE',{day:'2-digit',month:'2-digit'}):hour.toLocaleTimeString('de-DE',{hour:'2-digit',minute:'2-digit'});
  column.append(bars,tick);root.append(column);
 }
 root.setAttribute('aria-label',s.burst_count+' Bursts und '+s.pair_count+' Paare, '+s.window_hours+' Stunden');
 $('history-error').textContent='';
}
async function refreshHistory(){
 const hours=$('history-window').value;
 const res=await fetch('/api/summary?hours='+encodeURIComponent(hours),{cache:'no-store'});
 if(!res.ok)throw Error('HTTP '+res.status);
 renderSummary(await res.json());historyFetched=Date.now();
}
function updateUI(s){
 const active=['starting','running'].includes(s.state);
 const stopping=s.state==='stopping';
 $('status-pill').textContent=statusText[s.state]||'Unbekannt';
 $('status-pill').className='pill '+(active?'running':s.adsb_active?'blocked':'neutral');
 $('start').disabled=active||stopping||s.adsb_active||s.external_scan_active;
 $('stop').disabled=!active;
 $('monitor-status').textContent=statusText[s.state]||s.state;
 $('adsb-status').textContent=s.adsb_active?'Aktiv (SDR belegt)':'Inaktiv';
 $('external-scan').textContent=s.external_scan_active?'Aktiv (SDR belegt)':'Inaktiv';
 $('cal-status').textContent=s.baseline_ready?'Abgeschlossen':active?'Läuft':'Nicht gestartet';
 $('bursts').textContent=String(s.burst_count||0);
 $('pairs').textContent=String(s.pair_count||0);
 $('receive-state').textContent=s.signal?.active?'Funksignal erkannt':active?'Bereit':'Inaktiv';
 const rise=s.signal?.rise_db, snr=s.signal?.snr_db;
 $('rise').textContent=Number.isFinite(rise)?`${rise.toFixed(1)} dB`:'–';
 $('snr').textContent=Number.isFinite(snr)?`${snr.toFixed(1)} dB`:'–';
 $('rise-bar').style.width=`${Math.max(0,Math.min(100,(rise||0)*2.3))}%`;
 $('snr-bar').style.width=`${Math.max(0,Math.min(100,(snr||0)*2.0))}%`;
 if(s.state!==oldState){levelSamples.length=0;oldState=s.state}
 if(s.state==='running'&&Number.isFinite(s.signal?.rise_db)){
  levelSamples.push(Math.max(0,s.signal.rise_db));
  if(levelSamples.length>40)levelSamples.shift();
 }
 renderTrend();
 $('backend-error').hidden=!s.error;
 $('backend-error').textContent=s.error||'';
 if(s.state==='error') $('notice').textContent=s.error||'Der RTL-SDR konnte nicht geöffnet werden.';
 else if(s.external_scan_active) $('notice').textContent='Der befristete Konsolenscan verwendet derzeit den RTL-SDR. Die Ereignisse erscheinen in der Historie.';
 else if(s.adsb_active) $('notice').textContent='ADS-B ist aktiv. Für eine neue Messung dump1090.service bewusst stoppen; der Monitor übernimmt keine Dienste.';
 else if(active) $('notice').textContent=s.baseline_ready?'Empfang läuft. Nur technische Metadaten werden gespeichert.':'Ruhekalibrierung läuft: jetzt noch keine Taste drücken.';
 else $('notice').textContent='Bereit. Starten Sie die passive Messung oder schalten Sie ADS-B wieder ein.';
}
function renderEvents(items){
 const body=$('event-body');body.replaceChildren();
 if(!items.length){const tr=document.createElement('tr');const td=document.createElement('td');td.colSpan=6;td.className='empty';td.textContent='Noch keine Ereignisse';tr.append(td);body.append(tr);return;}
 for(const event of items){
  const tr=document.createElement('tr');
  const cells=[fmtTime(event.at),event.simulated?'Simulation':'RTL-SDR',event.kind==='pair'?'Burst-Paar':'Burst',
   event.kind==='pair'?`${event.start_gap_ms} ms Abstand`:`${event.duration_ms} ms`,
   event.kind==='burst'?`${event.peak_rise_db} dB`:'–',
   event.kind==='burst'?`${event.peak_snr_db} dB`:'–'];
  for(const v of cells){const td=document.createElement('td');td.textContent=v;tr.append(td)}
  body.append(tr);
 }
}
async function refresh(){
 if(polling)return;polling=true;
 try{
  const [a,b]=await Promise.all([fetch('/api/status',{cache:'no-store'}),fetch('/api/events',{cache:'no-store'})]);
  if(!a.ok||!b.ok)throw Error('HTTP '+(a.ok?b.status:a.status));
  updateUI(await a.json());renderEvents((await b.json()).events);
  if(Date.now()-historyFetched>8000){
   try{await refreshHistory()}catch(e){$('history-error').textContent='Verlauf nicht erreichbar: '+e.message}
  }
  if(Date.now()-sessionsLastFetched>8000){
   try{await refreshSessions()}catch(e){$('sessions-error').textContent='Sitzungsübersicht nicht erreichbar: '+e.message}
  }
 }catch(e){$('notice').textContent='Backend nicht erreichbar: '+e.message}
 finally{polling=false}
}
async function action(path){
 $('start').disabled=true;$('stop').disabled=true;
 try{const res=await fetch(path,{method:'POST',headers:{'X-RFMonitor-Token':token}});const body=await res.json();$('notice').textContent=body.message||'Aktion ausgeführt';if(!res.ok)throw Error(body.message||`HTTP ${res.status}`)}catch(e){$('notice').textContent='Aktion nicht ausgeführt: '+e.message}await refresh();
}
$('start').addEventListener('click',()=>action('/api/start'));
$('stop').addEventListener('click',()=>action('/api/stop'));
$('copy-scan').addEventListener('click',()=>{
 navigator.clipboard.writeText($('scan-command').textContent)
  .then(()=>{$('notice').textContent='Konsolenbefehl kopiert.';})
  .catch(()=>{$('notice').textContent='Bitte Befehl manuell aus dem Kasten kopieren.';});
});
$('history-window').addEventListener('change',()=>{historyFetched=0;refresh()});
$('comparison-metric').addEventListener('change',()=>renderComparison(recentScanSessions));
$('session-detail-choice').addEventListener('change',()=>{
  detailSelectionLast=null;
  refreshSessionDetail().catch(e=>{
    $('session-detail-status').textContent='Sitzungsdetails nicht erreichbar: '+e.message;
  });
});
refresh();setInterval(refresh,1500);


/* Automatically refreshed, receive-only real CU8 traces for manual scans. */
let iqTraces=[];
let iqCurrent=null;
let iqPageStart=0;
let iqRefreshToken=0;
let iqInventoryBusy=false;
let iqLastInventoryIds='';
const IQ_REFRESH_INTERVAL_MS=4000;

async function iqFetch(path){
 const result=await fetch(path,{cache:'no-store'});
 if(!result.ok)throw new Error('HTTP '+result.status);
 return await result.json();
}
function iqEmptyCanvas(canvas,caption){
 const ctx=canvas.getContext('2d');
 ctx.clearRect(0,0,canvas.width,canvas.height);
 ctx.fillStyle='#9eafc7';ctx.font='14px sans-serif';
 ctx.fillText(caption,20,canvas.height/2);
}
function iqChartFrame(ctx,w,h,caption){
 ctx.fillStyle='#101d2b';ctx.fillRect(0,0,w,h);
 ctx.strokeStyle='#415269';ctx.lineWidth=1;ctx.strokeRect(32,12,w-45,h-38);
 ctx.fillStyle='#9eafc7';ctx.font='11px sans-serif';ctx.fillText(caption,38,h-8);
}
function iqLine(ctx,values,w,h,color,minVal,maxVal){
 if(!values.length)return;
 ctx.strokeStyle=color;ctx.lineWidth=1;ctx.beginPath();
 const height=h-52;
 const range=Math.max(.01,maxVal-minVal);
 for(let j=0;j<values.length;j++){
  const x=32+(w-45)*j/Math.max(1,values.length-1);
  const y=12+height*(1-(values[j]-minVal)/range);
  if(j===0)ctx.moveTo(x,y);else ctx.lineTo(x,y);
 }
 ctx.stroke();
}
function drawIQ(page){
 const wave=$('iq-waveform'),wctx=wave.getContext('2d');
 iqChartFrame(wctx,wave.width,wave.height,'I = cyan, Q = amber · Rohwerte 0–255');
 wctx.strokeStyle='#425268';wctx.beginPath();wctx.moveTo(32,12+(wave.height-52)/2);wctx.lineTo(wave.width-13,12+(wave.height-52)/2);wctx.stroke();
 iqLine(wctx,page.i,wave.width,wave.height,'#71ded1',0,255);
 iqLine(wctx,page.q,wave.width,wave.height,'#edb96f',0,255);
 const scatter=$('iq-constellation'),cctx=scatter.getContext('2d');
 iqChartFrame(cctx,scatter.width,scatter.height,'I × Q · Nullpunkt 127,5');
 const cX=scatter.width/2,cY=(scatter.height-14)/2;
 cctx.fillStyle='#83dbc8';
 const step=Math.max(1,Math.ceil(page.count/1800));
 for(let i=0;i<page.count;i+=step){
  const x=cX+(page.i[i]-127.5)*.56;
  const y=cY-(page.q[i]-127.5)*.56;
  cctx.fillRect(x,y,2,2);
 }
 const spec=$('iq-spectrum'),sctx=spec.getContext('2d');
 iqChartFrame(sctx,spec.width,spec.height,'Relative Frequenz zum Empfängerzentrum [kHz]');
 const vals=page.spectrum.map(p=>p.db);
 if(vals.length){
  const min=Math.min(...vals),max=Math.max(...vals);
  iqLine(sctx,vals,spec.width,spec.height,'#8ccfdf',min-1,max+1);
 }
}
function renderIQBits(trace){
 const decoder=trace.decoder||{};
 const bits=decoder.bits||'';
 iqCurrent=trace;
 $('iq-bits').textContent=bits?(bits.match(/.{1,8}/g)||[]).join(' '):'Keine Symbolentscheidungen ableitbar';
 $('iq-copy-bits').disabled=!bits.length;
 $('iq-bitcount').textContent=String(bits.length);
 $('iq-baud').textContent=decoder.baud_nominal?decoder.baud_nominal+' Bd':'–';
 $('iq-duration').textContent=Number.isFinite(trace.duration_ms)?trace.duration_ms.toFixed(1)+' ms':'–';
 $('iq-total').textContent=trace.sample_count.toLocaleString('de-DE');
 $('iq-snr').textContent=Number.isFinite(trace.peak_snr_db)?trace.peak_snr_db.toFixed(1)+' dB':'–';
 const unknown=(bits.match(/\?/g)||[]).length;
 $('iq-decoder-status').textContent='Nicht verifizierte '+decoder.method+' · Timingphase '+decoder.timing_offset_samples+' Samples · '+unknown+' unsichere Symbole von '+bits.length;
 const max=Math.max(0,trace.sample_count-Number($('iq-pagesize').value));
 $('iq-offset').max=String(max);
 iqPageStart=Math.min(iqPageStart,max);
 $('iq-offset').value=String(iqPageStart);
}
async function loadIQPage(){
 if(!iqCurrent)return;
 const current=iqCurrent.id;
 const count=Number($('iq-pagesize').value);
 const max=Math.max(0,iqCurrent.sample_count-count);
 iqPageStart=Math.max(0,Math.min(max,iqPageStart));
 $('iq-offset').max=String(max);
 $('iq-offset').value=String(iqPageStart);
 const query='/api/iq/samples?id='+encodeURIComponent(current)+'&start='+iqPageStart+'&count='+count;
 const stamp=++iqRefreshToken;
 const page=await iqFetch(query);
 if(stamp!==iqRefreshToken||!iqCurrent||iqCurrent.id!==current)return;
 drawIQ(page);
 $('iq-range-label').textContent=(page.start+1).toLocaleString('de-DE')+'–'+(page.start+page.count).toLocaleString('de-DE')+' / '+page.total.toLocaleString('de-DE');
 $('iq-prev').disabled=iqPageStart===0;
 $('iq-next').disabled=iqPageStart+page.count>=page.total;
 $('iq-offset').disabled=max===0;
}
async function selectIQTrace(){
 const id=$('iq-choice').value;
 if(!id){
  iqCurrent=null;$('iq-copy-bits').disabled=true;
  $('iq-status').textContent='Noch keine echte IQ-Aufnahme gespeichert.';
  return;
 }
 iqPageStart=0;
 const query='/api/iq/trace?id='+encodeURIComponent(id);
 const result=await iqFetch(query);
 if($('iq-choice').value!==id)return;
 renderIQBits(result.trace);
 await loadIQPage();
 $('iq-status').textContent='Echter RTL-SDR-Burst vom '+fmtTime(result.trace.event_at)+' · nur lokal gespeichert';
}
async function refreshIQTraces(){
 // Periodic discovery must not reset a manually chosen trace or sample offset.
 if(iqInventoryBusy)return;
 iqInventoryBusy=true;
 try{
  const data=await iqFetch('/api/iq/traces');
  const incoming=data.traces;
  const signature=incoming.map(t=>t.id).join(',');
  if(signature===iqLastInventoryIds && (iqCurrent || !incoming.length))return;
  const wasLatest=iqTraces.length?iqTraces[0].id:null;
  const choice=$('iq-choice'),previous=choice.value;
  iqTraces=incoming;
  iqLastInventoryIds=signature;
  choice.replaceChildren();
  if(!incoming.length){
   choice.disabled=true;
   const option=document.createElement('option');option.value='';option.textContent='Keine IQ-Traces gespeichert';
   choice.append(option);
   $('iq-recordings-status').textContent='Keine Aufnahmen';
   $('iq-status').textContent='Neue IQ-/Bitaufnahmen erscheinen hier automatisch nach einem Scan.';
   $('iq-bits').textContent='–';
   iqCurrent=null;
   return;
  }
  choice.disabled=false;
  for(const trace of incoming){
   const option=document.createElement('option');option.value=trace.id;
   option.textContent=fmtTime(trace.event_at)+' · '+trace.duration_ms+' ms · '+trace.symbol_count+' Symbole';
   choice.append(option);
  }
  const hasNewLatest=wasLatest!==incoming[0].id;
  const stillAvailable=incoming.some(t=>t.id===previous);
  // Always show a newly received burst immediately; never reload a stable
  // selection merely because the periodic status poll ran.
  choice.value=hasNewLatest||!stillAvailable?incoming[0].id:previous;
  $('iq-recordings-status').textContent=incoming.length+' Aufnahmen vorhanden';
  if(!iqCurrent || choice.value!==iqCurrent.id){
   await selectIQTrace();
  }
 }finally{
  iqInventoryBusy=false;
 }
}
function iqReportError(err){$('iq-status').textContent='IQ-Diagnose nicht erreichbar: '+err.message}
$('iq-choice').addEventListener('change',()=>selectIQTrace().catch(iqReportError));
$('iq-refresh').addEventListener('click',()=>refreshIQTraces().catch(iqReportError));
$('iq-pagesize').addEventListener('change',()=>{iqPageStart=0;loadIQPage().catch(iqReportError)});
$('iq-offset').addEventListener('change',value=>{
 iqPageStart=Number(value.target.value);loadIQPage().catch(iqReportError);
});
$('iq-prev').addEventListener('click',()=>{
 iqPageStart=Math.max(0,iqPageStart-Number($('iq-pagesize').value));
 loadIQPage().catch(iqReportError);
});
$('iq-next').addEventListener('click',()=>{
 iqPageStart+=Number($('iq-pagesize').value);
 loadIQPage().catch(iqReportError);
});
$('iq-copy-bits').addEventListener('click',()=>{
 if(!iqCurrent)return;
 navigator.clipboard.writeText(iqCurrent.decoder.bits)
  .then(()=>{$('iq-status').textContent='Unverifizierte FSK-Bitentscheidungen kopiert.'})
  .catch(()=>{$('iq-status').textContent='Kopieren nicht verfügbar.'});
});
$('copy-iq').addEventListener('click',()=>{
 navigator.clipboard.writeText($('iq-command').textContent)
  .then(()=>{$('iq-status').textContent='IQ-Scanbefehl kopiert.'})
  .catch(()=>{$('iq-status').textContent='Befehl manuell kopieren.'});
});
refreshIQTraces().catch(iqReportError);
setInterval(()=>refreshIQTraces().catch(iqReportError),IQ_REFRESH_INTERVAL_MS);
