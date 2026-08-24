(() => {
  const $ = id => document.getElementById(id);
  const ptt = $('ptt'), meter = $('meter'), rx = $('rx'), logEl = $('log'), state = $('state');
  const gear = $('gear'), settings = $('settings'), simbrief = $('simbrief'), callsign = $('callsign'), gate = $('gate'), arrivalGate = $('arrivalGate');
  const saveBtn = $('save'), closeBtn = $('close'), syncPlan = $('syncPlan'), validatePlan = $('validatePlan'), saved = $('saved'), planstatus = $('planstatus'), controllerStatus = $('controllerStatus');
  const accessBlock = $('accessBlock'), accessCode = $('accessCode'), unlock = $('unlock');
  const apsearch = $('apsearch'), apresults = $('apresults'), freqrow = $('freqrow'), freqBadge = $('freqBadge');
  const departureMode = $('departureMode'), arrivalMode = $('arrivalMode');
  const modeChip = $('modeChip'), phaseChip = $('phaseChip'), readbackChip = $('readbackChip'), opAirport = $('opAirport'), opController = $('opController'), opRunway = $('opRunway'), opStand = $('opStand'), opLabel = $('opLabel'), opDetail = $('opDetail');
  $('ver').textContent = 'v0.7.0';

  let mediaRecorder = null, recording = false, stream = null, ctx = null, analyser = null;
  let curIcao = '', curController = '', curFreqs = {}, curOperation = {};
  const controllerNames = { ATIS:'ATIS', CLD:'CLEARANCE', GND:'GROUND', TWR:'TOWER', APP:'APPROACH', DEP:'DEPARTURE' };
  const controllerOrder = ['ATIS', 'CLD', 'GND', 'TWR', 'APP', 'DEP'];

  function log(kind, text) {
    const row = document.createElement('div'); row.className = 'msg ' + kind;
    const who = document.createElement('span'); who.className = 'who'; who.textContent = kind === 'atc' ? 'ATC' : 'SYS';
    row.append(who, document.createTextNode(text)); logEl.appendChild(row); logEl.scrollTop = logEl.scrollHeight;
  }
  function setState(text) { state.textContent = text; }
  function controllerFrequency(type) { return curFreqs[type] ? Number(curFreqs[type]).toFixed(3) : ''; }
  function stationLabel(type = curController) { const frequency = controllerFrequency(type); return type ? `${controllerNames[type]}${frequency ? ' · ' + frequency : ''}` : 'No controller selected'; }
  function stationName(type) { return controllerNames[type] || type || 'Not selected'; }

  function renderOperation(operation = {}) {
    curOperation = operation || {};
    const pending = curOperation.pending_readback || {};
    modeChip.textContent = curOperation.mode || 'DEPARTURE';
    phaseChip.textContent = (curOperation.phase_label || 'PRE-FLIGHT').toUpperCase();
    phaseChip.className = 'chip ' + (curOperation.emergency ? 'danger' : 'live');
    readbackChip.textContent = pending.description ? 'READBACK REQUIRED' : 'NO READBACK';
    readbackChip.className = 'chip ' + (pending.description ? 'warn' : '');
    opAirport.textContent = curOperation.airport || curIcao || 'Not selected';
    opController.textContent = curOperation.controller ? `${stationName(curOperation.controller)}${curOperation.frequency ? ' · ' + Number(curOperation.frequency).toFixed(3) : ''}` : 'Not selected';
    opRunway.textContent = curOperation.planned_runway || '—';
    opStand.textContent = curOperation.stand || '—';
    const route = curOperation.taxi_route || {};
    const routeText = route.ok ? `Verified taxi route: ${route.taxiways.join(' → ')} to runway ${route.runway}.` : '';
    opLabel.textContent = pending.description ? 'Readback:' : (routeText ? 'Taxi route:' : 'Status:');
    opDetail.textContent = pending.description || routeText || curOperation.last_transition || 'Select an airport and controller to begin.';
    departureMode.classList.toggle('active', (curOperation.mode || 'DEPARTURE') === 'DEPARTURE');
    arrivalMode.classList.toggle('active', curOperation.mode === 'ARRIVAL');
  }

  function renderPlanStatus(plan, validation, station = {}) {
    if (!plan || !plan.origin) { planstatus.textContent = curIcao ? `Active airport: ${curIcao}. ${stationLabel()}.` : 'No SimBrief flight plan synchronized.'; validatePlan.disabled = true; return; }
    const departureGate = plan.gate ? `Departure stand ${plan.gate}` : 'Departure stand not supplied';
    const arrivalGateText = plan.arrival_gate ? `Arrival stand ${plan.arrival_gate}` : 'Arrival stand not supplied';
    const missing = validation && validation.missing && validation.missing.length ? `\nBriefing fields unavailable: ${validation.missing.join(', ')}.` : '';
    planstatus.textContent = `Flight plan: ${plan.callsign || 'callsign unavailable'} · ${plan.origin} → ${plan.destination}\n${departureGate} · ${arrivalGateText}\nActive station: ${station.icao || curIcao || 'not selected'} · ${stationLabel()}${missing}`;
    validatePlan.disabled = false;
  }

  function renderFreqs() {
    freqrow.textContent = '';
    controllerOrder.forEach(type => {
      const frequency = controllerFrequency(type); const available = type === 'ATIS' ? Boolean(curIcao) : Boolean(frequency);
      const button = document.createElement('button'); button.type = 'button'; button.className = 'fbt' + (type === curController ? ' active' : ''); button.disabled = !available;
      button.textContent = `${controllerNames[type]}${frequency ? ' ' + frequency : type === 'ATIS' && curIcao ? '' : ' —'}`;
      button.addEventListener('click', () => selectController(type, true)); freqrow.appendChild(button);
    });
  }

  async function persistController(announce = false) {
    const payload = { simbrief_id: simbrief.value.trim(), callsign: callsign.value.trim(), gate: gate.value.trim(), arrival_gate: arrivalGate.value.trim(), airport: curIcao, controller_type: curController, controller_frequency: controllerFrequency(curController) };
    const response = await fetch('/api/settings', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload) }); const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'Settings could not be saved.');
    if (announce) saved.textContent = 'Airport and controller selection saved.';
    if (data.operation) renderOperation(data.operation);
    return data;
  }

  async function selectController(type, persist) {
    if (!curIcao) { controllerStatus.textContent = 'Select an airport first.'; return; }
    if (type !== 'ATIS' && !curFreqs[type]) { controllerStatus.textContent = `${controllerNames[type]} is not listed for ${curIcao}.`; return; }
    curController = type; const frequency = controllerFrequency(type); freqBadge.innerHTML = type === 'ATIS' ? 'ATIS' : `${controllerNames[type]} <b>${frequency}</b>`;
    controllerStatus.textContent = `Active voice station: ${stationLabel()}. AeroSpeak will answer only as this controller.`; renderFreqs();
    if (persist) { try { const data = await persistController(false); if (data.station) renderOperation({ ...curOperation, airport:data.station.icao || curIcao, controller:curController, frequency:frequency }); } catch (error) { controllerStatus.textContent = error.message; return; } }
    if (type === 'ATIS') playAtis();
  }

  async function loadFreqs(icao, preferredType = '', persistSelection = false) {
    curIcao = icao; curFreqs = {};
    try { const response = await fetch('/api/frequencies/' + encodeURIComponent(icao)); if (response.ok) curFreqs = (await response.json()).freqs || {}; } catch (_) {}
    const chosen = (preferredType && (preferredType === 'ATIS' || curFreqs[preferredType])) ? preferredType : ['CLD', 'GND', 'TWR', 'APP', 'DEP', 'ATIS'].find(type => type === 'ATIS' || curFreqs[type]);
    if (chosen) await selectController(chosen, persistSelection); else { curController = ''; freqBadge.textContent = 'FREQ —'; controllerStatus.textContent = `No controller frequencies are listed for ${icao}.`; renderFreqs(); }
  }

  async function setOperation(mode) {
    try {
      const response = await fetch('/api/operation', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({ mode }) }); const data = await response.json();
      if (!response.ok) throw new Error(data.detail || 'Operation mode could not be changed.');
      renderOperation(data.operation); curController = ''; freqBadge.textContent = 'FREQ —';
      const airport = (data.station || {}).icao || curIcao; if (airport) { apsearch.value = airport; await loadFreqs(airport, '', false); await persistController(false); }
      controllerStatus.textContent = mode === 'ARRIVAL' ? 'Arrival operation active. Select Approach, Tower, or Ground as appropriate.' : 'Departure operation active. Select ATIS, Clearance, Ground, or Tower as appropriate.';
    } catch (error) { saved.textContent = error.message; }
  }

  async function ensureMic() {
    if (stream) return;
    stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } }); ctx = new (window.AudioContext || window.webkitAudioContext)(); analyser = ctx.createAnalyser(); analyser.fftSize = 512; ctx.createMediaStreamSource(stream).connect(analyser);
    const samples = new Uint8Array(analyser.frequencyBinCount); const tick = () => { if (recording && analyser) { analyser.getByteTimeDomainData(samples); let total = 0; for (let i = 0; i < samples.length; i++) { const value = (samples[i] - 128) / 128; total += value * value; } meter.style.width = Math.min(100, Math.sqrt(total / samples.length) * 240) + '%'; } requestAnimationFrame(tick); }; tick();
  }
  function unlockAudio() { try { const silent = new Audio('data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEARKwAAIhYAQACABAAZGF0YQAAAAA='); silent.play().then(() => silent.pause()).catch(() => {}); } catch (_) {} }
  async function startRecording() { if (!curIcao || !curController) { log('sys', 'Open Settings and select an airport plus the controller frequency you are calling.'); setState('station required'); return; } try { await ensureMic(); } catch (error) { log('sys', 'Microphone unavailable: ' + error.message); setState('mic blocked'); return; } if (ctx.state === 'suspended') await ctx.resume(); const chunks = []; const recorder = new MediaRecorder(stream); recorder.ondataavailable = event => { if (event.data.size) chunks.push(event.data); }; recorder.onstop = () => sendAudio(new Blob(chunks, { type: recorder.mimeType || 'audio/webm' })); recorder.start(); mediaRecorder = recorder; recording = true; rx.classList.add('live'); ptt.classList.add('hold'); setState('TX ' + stationLabel()); }
  function stopRecording() { if (!recording) return; recording = false; rx.classList.remove('live'); ptt.classList.remove('hold'); meter.style.width = '0%'; if (mediaRecorder && mediaRecorder.state !== 'inactive') mediaRecorder.stop(); }
  function addPlayButton(url) { const row = document.createElement('div'); row.className = 'msg'; const button = document.createElement('button'); button.className = 'playbtn'; button.type = 'button'; button.textContent = '▶ Play ATC audio'; button.addEventListener('click', () => new Audio(url.startsWith('http') ? url : location.origin + url).play().catch(() => log('sys', 'Playback was blocked by this browser.'))); row.appendChild(button); logEl.appendChild(row); logEl.scrollTop = logEl.scrollHeight; }
  function playAudio(url) { new Audio(url.startsWith('http') ? url : location.origin + url).play().catch(() => addPlayButton(url)); }
  async function sendAudio(blob) { if (!blob.size) { setState('ready'); return; } const form = new FormData(); form.append('audio', blob, 'transmission.webm'); setState('transmitting…'); try { const response = await fetch('/api/chat', { method:'POST', body: form }); const data = await response.json(); if (!response.ok) { log('sys', data.detail || data.error || 'Radio request failed.'); setState('error'); setTimeout(() => setState('ready'), 1600); return; } if (data.text) log('atc', data.text); if (data.audio) playAudio(data.audio); if (data.operation) renderOperation(data.operation); if (data.context_refreshed) saved.textContent = `Live ${curIcao} METAR and ATIS refreshed.`; setState('ready'); } catch (error) { log('sys', 'Network error: ' + error.message); setState('error'); setTimeout(() => setState('ready'), 1600); } }
  async function playAtis() { if (!curIcao) return; setState('getting ATIS…'); try { const response = await fetch('/api/atis/' + encodeURIComponent(curIcao)); const data = await response.json(); if (!response.ok) { log('sys', 'ATIS unavailable: ' + (data.error || 'unknown error')); return; } log('atc', data.atis); if (data.audio) playAudio(data.audio); } catch (error) { log('sys', 'ATIS error: ' + error.message); } finally { setState('ready'); } }
  async function searchAirports(query) { if (query.length < 2) { apresults.classList.remove('open'); return; } try { const response = await fetch('/api/airports?q=' + encodeURIComponent(query)); const data = await response.json(); apresults.textContent = ''; (data.airports || []).forEach(airport => { const row = document.createElement('div'); row.className = 'ap'; const left = document.createElement('span'); const code = document.createElement('b'); code.textContent = airport.icao; left.append(code, document.createTextNode(` ${airport.name || ''} · ${airport.city || ''} ${airport.country || ''}`)); row.appendChild(left); row.addEventListener('click', async () => { apsearch.value = airport.icao; apresults.classList.remove('open'); await loadFreqs(airport.icao, '', true); renderOperation({ ...curOperation, airport:airport.icao }); }); apresults.appendChild(row); }); apresults.classList.toggle('open', Boolean((data.airports || []).length)); } catch (_) { apresults.classList.remove('open'); } }
  async function saveSettings(announce = true) { const data = await persistController(announce); syncPlan.disabled = !simbrief.value.trim(); renderPlanStatus(data.flight_plan || {}, data.validation, data.station || {}); }
  async function syncFlightPlan() { syncPlan.disabled = true; syncPlan.textContent = 'Syncing Flight Plan…'; planstatus.textContent = 'Retrieving your latest SimBrief briefing…'; try { const response = await fetch('/api/flight-plan/sync', { method:'POST' }); const data = await response.json(); if (!response.ok) throw new Error(data.detail || 'Flight-plan sync failed.'); const plan = data.flight_plan || {}; if (plan.callsign) callsign.value = plan.callsign; if (plan.gate) gate.value = plan.gate; if (plan.arrival_gate) arrivalGate.value = plan.arrival_gate; const airport = curIcao || plan.origin; if (airport) { apsearch.value = airport; await loadFreqs(airport, curController, false); } renderPlanStatus(plan, data.validation, data.station || {}); if (data.operation) renderOperation(data.operation); saved.textContent = 'Flight plan synchronized. Select the controller frequency you are calling.'; } catch (error) { planstatus.textContent = error.message; } finally { syncPlan.disabled = !simbrief.value.trim(); syncPlan.textContent = 'Sync Flight Plan'; } }
  async function validateFlightPlan() { validatePlan.disabled = true; validatePlan.textContent = 'Refreshing…'; try { const response = await fetch('/api/flight-plan/validate', { method:'POST' }); const data = await response.json(); renderPlanStatus(data.flight_plan, data.validation, data.station || {}); saved.textContent = data.context_refreshed ? 'Live airport details refreshed.' : 'No active airport to refresh.'; } catch (error) { planstatus.textContent = 'Refresh failed: ' + error.message; } finally { validatePlan.textContent = 'Refresh Live Details'; validatePlan.disabled = false; } }
  async function loadSettings() { try { const response = await fetch('/api/settings'); const data = await response.json(); simbrief.value = data.simbrief_id || ''; callsign.value = data.callsign || ''; gate.value = data.gate || ''; arrivalGate.value = data.arrival_gate || ''; accessBlock.hidden = !(data.access_required && !data.authorized); syncPlan.disabled = !simbrief.value.trim(); renderOperation(data.operation || {}); const airport = data.airport || data.flight_plan?.origin || ''; if (airport) { apsearch.value = airport; await loadFreqs(airport, data.controller_type || '', false); } renderPlanStatus(data.flight_plan || {}, data.validation, data.station || {}); if (!airport) controllerStatus.textContent = 'Select an airport, then choose the controller frequency you are calling.'; } catch (_) { planstatus.textContent = 'Could not restore this browser session.'; } }

  ptt.addEventListener('pointerdown', event => { event.preventDefault(); unlockAudio(); startRecording(); }); ptt.addEventListener('pointerup', event => { event.preventDefault(); stopRecording(); }); ptt.addEventListener('pointerleave', stopRecording);
  document.addEventListener('keydown', event => { if (event.code === 'Space' && !event.repeat && !settings.classList.contains('open')) { event.preventDefault(); unlockAudio(); startRecording(); } }); document.addEventListener('keyup', event => { if (event.code === 'Space') stopRecording(); });
  apsearch.addEventListener('input', () => searchAirports(apsearch.value.trim())); document.addEventListener('click', event => { if (!event.target.closest('.aprow')) apresults.classList.remove('open'); }); gear.addEventListener('click', () => settings.classList.toggle('open')); closeBtn.addEventListener('click', () => settings.classList.remove('open'));
  saveBtn.addEventListener('click', async () => { saveBtn.disabled = true; try { await saveSettings(true); } catch (error) { saved.textContent = error.message; } finally { saveBtn.disabled = false; } }); syncPlan.addEventListener('click', syncFlightPlan); validatePlan.addEventListener('click', validateFlightPlan); departureMode.addEventListener('click', () => setOperation('DEPARTURE')); arrivalMode.addEventListener('click', () => setOperation('ARRIVAL'));
  unlock.addEventListener('click', async () => { unlock.disabled = true; try { const response = await fetch('/api/access', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({ code: accessCode.value }) }); const data = await response.json(); if (!response.ok) throw new Error(data.detail || 'Access was not accepted.'); accessCode.value = ''; accessBlock.hidden = true; saved.textContent = 'Live services unlocked for this browser session.'; } catch (error) { saved.textContent = error.message; } finally { unlock.disabled = false; } });
  loadSettings();
})();
