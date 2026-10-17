'use strict';
const $ = id => document.getElementById(id);
const token = document.querySelector('meta[name="rf-token"]').content;
const statusText = {stopped:'Gestoppt',starting:'Kalibriert',running:'Empfang aktiv',stopping:'Beendet',error:'Fehler'};
function fmtTime(s){try{return new Date(s).toLocaleString('de-DE',{dateStyle:'short',timeStyle:'medium'})}catch{return '–'}}
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
 if(!items.length){const tr=document.createElement('tr');const td=document.createElement('td');td.colSpan=5;td.className='empty';td.textContent='Noch keine Ereignisse';tr.append(td);body.append(tr);return;}
 for(const event of items){
  const tr=document.createElement('tr');
  const cells=[fmtTime(event.at),event.kind==='pair'?'Burst-Paar':'Burst',
   event.kind==='pair'?`${event.start_gap_ms} ms Abstand`:`${event.duration_ms} ms`,
   event.kind==='burst'?`${event.peak_rise_db} dB`:'–',
   event.kind==='burst'?`${event.peak_snr_db} dB`:'–'];
  for(const v of cells){const td=document.createElement('td');td.textContent=v;tr.append(td)}
  body.append(tr);
 }
}
async function refresh(){try{const [a,b]=await Promise.all([fetch('/api/status',{cache:'no-store'}),fetch('/api/events',{cache:'no-store'})]);if(!a.ok||!b.ok)throw Error('HTTP '+a.status);updateUI(await a.json());renderEvents((await b.json()).events)}catch(e){$('notice').textContent='Backend nicht erreichbar: '+e.message}}
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
refresh();setInterval(refresh,1500);
