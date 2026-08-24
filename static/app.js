(() => {
  const $ = id => document.getElementById(id);
  const ptt = $('ptt'), meter = $('meter'), rx = $('rx'), logEl = $('log'), state = $('state');
  const gear = $('gear'), settings = $('settings'), simbrief = $('simbrief'), callsign = $('callsign'), gate = $('gate'), arrivalGate = $('arrivalGate'), scenario = $('scenario');
  const saveBtn = $('save'), closeBtn = $('close'), syncPlan = $('syncPlan'), validatePlan = $('validatePlan'), saved = $('saved'), planstatus = $('planstatus');
  const accessBlock = $('accessBlock'), accessCode = $('accessCode'), unlock = $('unlock');
  const apsearch = $('apsearch'), apresults = $('apresults'), freqrow = $('freqrow'), freqBadge = $('freqBadge');
  $('ver').textContent = 'v0.4.0';

  let mediaRecorder = null, recording = false, stream = null, ctx = null, analyser = null;
  let curIcao = '', curFreqType = 'GND', curFreqs = {};
  const freqLabel = { ATIS:'ATIS', CLD:'CLNCE', GND:'GND', TWR:'TWR', APP:'APPROACH', DEP:'DEPARTURE' };

  function log(kind, text) {
    const row = document.createElement('div');
    row.className = 'msg ' + kind;
    const who = document.createElement('span');
    who.className = 'who';
    who.textContent = kind === 'atc' ? 'ATC' : 'SYS';
    row.append(who, document.createTextNode(text));
    logEl.appendChild(row); logEl.scrollTop = logEl.scrollHeight;
  }
  function setState(text) { state.textContent = text; }

  function isArrivalScenario() { return scenario.value.startsWith('arrival_'); }

  function renderPlanStatus(plan, validation) {
    if (!plan || !plan.origin) { planstatus.textContent = 'No flight plan synchronized yet.'; validatePlan.disabled = true; return; }
    const departureGate = plan.gate ? `Departure gate/stand ${plan.gate}` : 'Departure gate/stand not supplied';
    const arrivalGateText = plan.arrival_gate ? `Arrival gate/stand ${plan.arrival_gate}` : 'Arrival gate/stand not supplied';
    const missing = validation && validation.missing && validation.missing.length ? `\nNeeds attention: ${validation.missing.join(', ')}.` : '';
    const arrivalMode = isArrivalScenario() ? '\nArrival mode: destination METAR, ATIS, runway, and frequencies are active. Position reports are required; no radar vectors are simulated.' : '';
    planstatus.textContent = `Synced: ${plan.callsign || 'callsign unavailable'} · ${plan.origin} → ${plan.destination}\n${departureGate} · ${arrivalGateText} · ${plan.aircraft || 'aircraft unavailable'} · cruise ${plan.cruise_altitude || 'not specified'}\nLive METAR/ATIS refresh automatically during radio use.${arrivalMode}${missing}`;
    validatePlan.disabled = false;
  }

  async function ensureMic() {
    if (stream) return;
    stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
    ctx = new (window.AudioContext || window.webkitAudioContext)();
    analyser = ctx.createAnalyser(); analyser.fftSize = 512;
    ctx.createMediaStreamSource(stream).connect(analyser);
    const samples = new Uint8Array(analyser.frequencyBinCount);
    const tick = () => {
      if (recording && analyser) {
        analyser.getByteTimeDomainData(samples);
        let total = 0;
        for (let i = 0; i < samples.length; i++) { const v = (samples[i] - 128) / 128; total += v * v; }
        meter.style.width = Math.min(100, Math.sqrt(total / samples.length) * 240) + '%';
      }
      requestAnimationFrame(tick);
    };
    tick();
  }

  function unlockAudio() {
    try {
      const silent = new Audio('data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEARKwAAIhYAQACABAAZGF0YQAAAAA=');
      silent.play().then(() => silent.pause()).catch(() => {});
    } catch (_) {}
  }

  async function startRecording() {
    try { await ensureMic(); } catch (error) { log('sys', 'Microphone unavailable: ' + error.message); setState('mic blocked'); return; }
    if (ctx.state === 'suspended') await ctx.resume();
    const chunks = [];
    const recorder = new MediaRecorder(stream);
    recorder.ondataavailable = event => { if (event.data.size) chunks.push(event.data); };
    recorder.onstop = () => sendAudio(new Blob(chunks, { type: recorder.mimeType || 'audio/webm' }));
    recorder.start(); mediaRecorder = recorder; recording = true;
    rx.classList.add('live'); ptt.classList.add('hold'); setState('TX');
  }

  function stopRecording() {
    if (!recording) return;
    recording = false; rx.classList.remove('live'); ptt.classList.remove('hold'); meter.style.width = '0%';
    if (mediaRecorder && mediaRecorder.state !== 'inactive') mediaRecorder.stop();
  }

  function playAudio(url) {
    const audio = new Audio(url.startsWith('http') ? url : location.origin + url);
    audio.play().catch(() => addPlayButton(url));
  }

  function addPlayButton(url) {
    const row = document.createElement('div'); row.className = 'msg';
    const button = document.createElement('button'); button.className = 'playbtn'; button.type = 'button'; button.textContent = '▶ Play ATC audio';
    button.addEventListener('click', () => new Audio(url.startsWith('http') ? url : location.origin + url).play().catch(() => log('sys', 'Playback was blocked by this browser.')));
    row.appendChild(button); logEl.appendChild(row); logEl.scrollTop = logEl.scrollHeight;
  }

  async function sendAudio(blob) {
    if (!blob.size) { setState('ready'); return; }
    const form = new FormData(); form.append('audio', blob, 'transmission.webm');
    setState('transmitting…');
    try {
      const response = await fetch('/api/chat', { method: 'POST', body: form });
      const data = await response.json();
      if (!response.ok) {
        log('sys', data.detail || data.error || 'Radio request failed.');
        setState('error'); setTimeout(() => setState('ready'), 1600); return;
      }
      if (data.text) log('atc', data.text);
      if (data.audio) playAudio(data.audio);
      if (data.context_refreshed) planstatus.textContent = planstatus.textContent.replace('Live METAR/ATIS refresh automatically during radio use.', 'Live METAR/ATIS refreshed for this transmission.');
      setState('ready');
    } catch (error) { log('sys', 'Network error: ' + error.message); setState('error'); setTimeout(() => setState('ready'), 1600); }
  }

  function setFreq(type) {
    curFreqType = type; const frequency = curFreqs[type];
    freqBadge.innerHTML = frequency ? `FREQ <b>${frequency.toFixed(3)}</b>` : (type === 'ATIS' ? 'ATIS · <b>play in Settings</b>' : 'FREQ —');
    renderFreqs();
  }

  function renderFreqs() {
    freqrow.textContent = '';
    ['ATIS','CLD','GND','TWR','APP','DEP'].forEach(type => {
      const frequency = curFreqs[type]; const button = document.createElement('button');
      button.className = 'fbt' + (type === curFreqType ? ' active' : '');
      button.innerHTML = `<span class="type">${freqLabel[type]}</span>${frequency ? Number(frequency).toFixed(3) : '—'}`;
      button.addEventListener('click', () => type === 'ATIS' ? playAtis() : setFreq(type));
      freqrow.appendChild(button);
    });
  }

  async function loadFreqs(icao) {
    curIcao = icao; curFreqs = {};
    try { const response = await fetch('/api/frequencies/' + encodeURIComponent(icao)); if (response.ok) curFreqs = (await response.json()).freqs || {}; } catch (_) {}
    if (curFreqs.TWR) setFreq('TWR'); else if (curFreqs.GND) setFreq('GND'); else setFreq('ATIS');
  }

  async function playAtis() {
    if (!curIcao) { log('sys', 'Sync a flight plan or choose an airport in Settings first.'); return; }
    setState('getting ATIS…');
    try {
      const response = await fetch('/api/atis/' + encodeURIComponent(curIcao)); const data = await response.json();
      if (!response.ok) { log('sys', 'ATIS unavailable: ' + (data.error || 'unknown error')); setState('ready'); return; }
      log('atc', data.atis); if (data.audio) playAudio(data.audio); setFreq('ATIS');
    } catch (error) { log('sys', 'ATIS error: ' + error.message); }
    setState('ready');
  }

  async function searchAirports(query) {
    if (query.length < 2) { apresults.classList.remove('open'); return; }
    try {
      const response = await fetch('/api/airports?q=' + encodeURIComponent(query)); const data = await response.json();
      apresults.textContent = '';
      (data.airports || []).forEach(airport => {
        const row = document.createElement('div'); row.className = 'ap';
        const left = document.createElement('span'); const code = document.createElement('b'); code.textContent = airport.icao;
        left.append(code, document.createTextNode(` ${airport.name || ''} · ${airport.city || ''} ${airport.country || ''}`)); row.appendChild(left);
        row.addEventListener('click', async () => { apsearch.value = airport.icao; apresults.classList.remove('open'); await saveSettings({ airport: airport.icao }, false); await loadFreqs(airport.icao); });
        apresults.appendChild(row);
      });
      apresults.classList.toggle('open', Boolean((data.airports || []).length));
    } catch (_) { apresults.classList.remove('open'); }
  }

  async function saveSettings(extra = {}, announce = true) {
    const payload = { simbrief_id: simbrief.value.trim(), callsign: callsign.value.trim(), gate: gate.value.trim(), arrival_gate: arrivalGate.value.trim(), scenario: scenario.value, airport: curIcao, ...extra };
    const response = await fetch('/api/settings', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload) });
    if (!response.ok) throw new Error('Settings could not be saved.');
    syncPlan.disabled = !simbrief.value.trim();
    if (announce) saved.textContent = simbrief.value.trim() ? 'Saved. Sync your latest flight plan when ready.' : 'Saved. Add a SimBrief Pilot ID to enable sync.';
    return response.json();
  }

  async function syncFlightPlan() {
    syncPlan.disabled = true; syncPlan.textContent = 'Syncing Flight Plan…'; planstatus.textContent = 'Retrieving your latest briefing and live departure context…';
    try {
      const response = await fetch('/api/flight-plan/sync', { method:'POST' }); const data = await response.json();
      if (!response.ok) throw new Error(data.detail || 'Flight-plan sync failed.');
      const plan = data.flight_plan || {}; if (plan.callsign) callsign.value = plan.callsign; if (plan.gate) gate.value = plan.gate; if (plan.arrival_gate) arrivalGate.value = plan.arrival_gate;
      const activeAirport = isArrivalScenario() ? plan.destination : plan.origin;
      if (activeAirport) { apsearch.value = activeAirport; await loadFreqs(activeAirport); }
      renderPlanStatus(plan, data.validation); saved.textContent = 'Flight plan synchronized.';
    } catch (error) { planstatus.textContent = error.message; }
    finally { syncPlan.disabled = !simbrief.value.trim(); syncPlan.textContent = 'Sync Flight Plan'; }
  }

  async function validateFlightPlan() {
    validatePlan.disabled = true; validatePlan.textContent = 'Refreshing…';
    try {
      const response = await fetch('/api/flight-plan/validate', { method:'POST' }); const data = await response.json();
      renderPlanStatus(data.flight_plan, data.validation); saved.textContent = data.context_refreshed ? 'Live flight details refreshed.' : 'No synchronized plan to refresh.';
    } catch (error) { planstatus.textContent = 'Refresh failed: ' + error.message; }
    finally { validatePlan.textContent = 'Refresh Live Details'; validatePlan.disabled = false; }
  }

  async function loadSettings() {
    try {
      const response = await fetch('/api/settings'); const data = await response.json();
      simbrief.value = data.simbrief_id || ''; callsign.value = data.callsign || ''; gate.value = data.gate || ''; arrivalGate.value = data.arrival_gate || ''; scenario.value = data.scenario || 'ifr_clearance';
      accessBlock.hidden = !(data.access_required && !data.authorized);
      const plan = data.flight_plan || {}; const airport = (isArrivalScenario() ? plan.destination : plan.origin) || data.airport || '';
      syncPlan.disabled = !simbrief.value.trim(); renderPlanStatus(plan, data.validation);
      if (airport) { curIcao = airport; apsearch.value = airport; await loadFreqs(airport); }
    } catch (_) { planstatus.textContent = 'Could not restore this browser session.'; }
  }

  ptt.addEventListener('pointerdown', event => { event.preventDefault(); unlockAudio(); startRecording(); });
  ptt.addEventListener('pointerup', event => { event.preventDefault(); stopRecording(); });
  ptt.addEventListener('pointerleave', stopRecording);
  document.addEventListener('keydown', event => { if (event.code === 'Space' && !event.repeat && !settings.classList.contains('open')) { event.preventDefault(); unlockAudio(); startRecording(); } });
  document.addEventListener('keyup', event => { if (event.code === 'Space') stopRecording(); });
  apsearch.addEventListener('input', () => searchAirports(apsearch.value.trim()));
  document.addEventListener('click', event => { if (!event.target.closest('.aprow')) apresults.classList.remove('open'); });
  gear.addEventListener('click', () => settings.classList.toggle('open'));
  closeBtn.addEventListener('click', () => settings.classList.remove('open'));
  saveBtn.addEventListener('click', async () => { saveBtn.disabled = true; try { await saveSettings(); } catch (error) { saved.textContent = error.message; } finally { saveBtn.disabled = false; } });
  syncPlan.addEventListener('click', syncFlightPlan);
  validatePlan.addEventListener('click', validateFlightPlan);
  unlock.addEventListener('click', async () => {
    unlock.disabled = true;
    try {
      const response = await fetch('/api/access', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({ code: accessCode.value }) });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || 'Access was not accepted.');
      accessCode.value = ''; accessBlock.hidden = true; saved.textContent = 'Live services unlocked for this browser session.';
    } catch (error) { saved.textContent = error.message; }
    finally { unlock.disabled = false; }
  });
  loadSettings();
})();
