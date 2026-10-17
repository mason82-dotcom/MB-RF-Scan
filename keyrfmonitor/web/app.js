'use strict';
const $ = id => document.getElementById(id);
const token = document.querySelector('meta[name="rf-token"]').content;
const statusText = {stopped:'Gestoppt',starting:'Kalibriert',running:'Empfang aktiv',stopping:'Beendet',error:'Fehler'};
function fmtTime(s){try{return new Date(s).toLocaleString('de-DE',{dateStyle:'short',timeStyle:'medium'})}catch{return '–'}}

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
refresh();setInterval(refresh,1500);
