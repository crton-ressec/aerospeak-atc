(() => {
  const $ = id => document.getElementById(id);
  const ptt = $('ptt'), meter = $('meter'), rx = $('rx'), logEl = $('log'), state = $('state');
  const gear = $('gear'), settings = $('settings'), simbrief = $('simbrief'), callsign = $('callsign'), saveBtn = $('save'), closeBtn = $('close'), saved = $('saved');
  $('ver').textContent = 'v0.2.0';
  let mediaRecorder = null, recording = false, ctx = null, analyser = null, raf = null, stream = null;

  function log(who, text) {
    const d = document.createElement('div');
    d.className = 'msg ' + who;
    d.innerHTML = `<span class="who">${who === 'pilot' ? 'YOU' : who === 'atc' ? 'ATC' : 'SYS'}</span>${text}`;
    logEl.appendChild(d); logEl.scrollTop = logEl.scrollHeight;
  }
  function setState(s) { state.textContent = s; }

  async function ensureMic() {
    if (stream) return;
    stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
    ctx = new (window.AudioContext || window.webkitAudioContext)();
    analyser = ctx.createAnalyser(); analyser.fftSize = 512;
    ctx.createMediaStreamSource(stream).connect(analyser);
    startMeter();
  }

  function startMeter() {
    const buf = new Uint8Array(analyser.frequencyBinCount);
    const tick = () => {
      if (recording && analyser) {
        analyser.getByteTimeDomainData(buf);
        let sum = 0; for (let i=0;i<buf.length;i++){ const v=(buf[i]-128)/128; sum+=v*v; }
        meter.style.width = Math.min(100, Math.sqrt(sum/buf.length)*240) + '%';
      }
      raf = requestAnimationFrame(tick);
    };
    tick();
  }

  async function startRecording() {
    try { await ensureMic(); } catch (e) { log('sys', 'Mic error: ' + e.message); setState('mic blocked'); return; }
    if (ctx.state === 'suspended') await ctx.resume();
    const chunks = [];
    const rec = new MediaRecorder(stream);
    rec.ondataavailable = e => { if (e.data.size) chunks.push(e.data); };
    rec.onstop = async () => {
      const blob = new Blob(chunks, { type: rec.mimeType || 'audio/webm' });
      setState('sending…');
      await sendAudio(blob);
    };
    rec.start();
    mediaRecorder = rec;
    recording = true;
    rx.classList.add('live'); state.textContent = 'TX'; ptt.classList.add('hold');
  }

  function stopRecording() {
    if (!recording) return;
    recording = false;
    rx.classList.remove('live'); ptt.classList.remove('hold'); meter.style.width = '0%';
    if (mediaRecorder && mediaRecorder.state !== 'inactive') mediaRecorder.stop();
  }

  // Unlock iOS audio on first user gesture (PTT pointerdown).
  let audioUnlocked = false;
  function unlockAudio() {
    if (audioUnlocked) return;
    try {
      const silent = new Audio('data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEARKwAAIhYAQACABAAZGF0YQAAAAA=');
      silent.play().then(() => { silent.pause(); }).catch(() => {});
      audioUnlocked = true;
    } catch (e) {}
  }

  async function sendAudio(blob) {
    const fd = new FormData(); fd.append('audio', blob, 'tx.wav');
    if (callsign.value.trim()) fd.append('callsign', callsign.value.trim());
    setState('transmitting…');
    try {
      const resp = await fetch('/api/chat', { method: 'POST', body: fd });
      const data = await resp.json();
      if (!resp.ok) {
        if (data.error === 'transcribe_failed') {
          log('sys', "Didn't catch you — press and hold & speak again.");
        } else if (data.error === 'brain_unavailable') {
          log('sys', 'ATC brain glitched — try again.');
        } else {
          log('sys', 'Error: ' + (data.error || resp.status));
        }
        setState('error'); setTimeout(() => setState('ready'), 1500);
        return;
      }
      log('atc', data.text || '');
      if (data.audio) {
        const abs = data.audio.startsWith('http') ? data.audio : location.origin + data.audio;
        // Try autoplay; for strict browsers (Aloha/Safari) show a tappable play button.
        playAudio(abs);
        addPlayButton(abs);
      }
      setState('ready');
    } catch (e) { log('sys', 'Network error: ' + e.message); setState('error'); setTimeout(() => setState('ready'), 1500); }
  }

  function playAudio(url) {
    const abs = url.startsWith('http') ? url : location.origin + url;
    const audio = new Audio(abs);
    audio.play().catch(() => {}); // strict browsers: rely on the play button below
  }

  function addPlayButton(url) {
    const abs = url.startsWith('http') ? url : location.origin + url;
    const row = document.createElement('div');
    row.className = 'msg playrow';
    row.innerHTML = `<button class="playbtn" type="button">▶ Play ATC audio</button>`;
    row.querySelector('.playbtn').addEventListener('click', () => {
      const a = new Audio(abs);
      a.play().catch(() => log('sys', 'Still blocked — tap volume or open in Safari'));
    });
    logEl.appendChild(row); logEl.scrollTop = logEl.scrollHeight;
  }

  ptt.addEventListener('pointerdown', e => { e.preventDefault(); unlockAudio(); startRecording(); });

  // ---- airport + frequency ----
  let curIcao = '', curFreqType = 'GND', curFreqs = {};
  const freqLabel = { 'ATIS':'ATIS', 'CLD':'CLNCE', 'GND':'GND', 'TWR':'TWR', 'APP':'APPROACH', 'DEP':'DEPARTURE' };
  const apsearch = $('apsearch'), apresults = $('apresults'), freqrow = $('freqrow'), freqBadge = $('freqBadge');

  function setFreq(type) {
    curFreqType = type;
    const f = curFreqs[type];
    freqBadge.innerHTML = f ? `FREQ <b>${f.toFixed(3)}</b>` : (type === 'ATIS' ? 'ATIS · <b>press to play</b>' : 'FREQ —');
    renderFreqs();
  }
  function renderFreqs() {
    freqrow.innerHTML = '';
    ['ATIS','CLD','GND','TWR','APP','DEP'].forEach(t => {
      const f = curFreqs[t];
      const b = document.createElement('button');
      b.className = 'fbt' + (t === curFreqType ? ' active' : '');
      b.innerHTML = `<span class="type">${freqLabel[t]}</span>${f ? f.toFixed(3) : '—'}`;
      b.onclick = () => {
        if (t === 'ATIS') { playAtis(); return; }
        setFreq(t);
      };
      freqrow.appendChild(b);
    });
  }
  async function loadFreqs(icao) {
    curIcao = icao; curFreqs = {};
    try {
      const r = await fetch('/api/frequencies/' + icao);
      if (r.ok) { const d = await r.json(); curFreqs = d.freqs || {}; }
    } catch (e) {}
    // Show GND (or TWR if no GND, else CLD) by default
    if (curFreqs.GND) setFreq('GND');
    else if (curFreqs.TWR) setFreq('TWR');
    else setFreq('ATIS');
    if (curFreqs.ATIS) setFreq('ATIS');
  }
  async function selectAirport(icao) {
    apsearch.value = icao;
    apresults.classList.remove('open');
    await loadSettings({ icao });
    await playFreqs(icao);
  }
  async function playFreqs(icao) {
    curIcao = icao; curFreqs = {};
    try {
      const r = await fetch('/api/frequencies/' + icao);
      if (r.ok) { const d = await r.json(); curFreqs = d.freqs || {}; }
    } catch (e) {}
    // Default to TWR if available, else GND, else ATIS
    if (curFreqs.TWR) setFreq('TWR');
    else if (curFreqs.GND) setFreq('GND');
    else if (curFreqs.ATIS) setFreq('ATIS');
    else setFreq('CLD');
  }
  async function playAtis() {
    if (!curIcao) { log('sys', 'Select an airport first'); return; }
    setState('getting ATIS…');
    try {
      const r = await fetch('/api/atis/' + curIcao);
      const d = await r.json();
      if (!r.ok) { log('sys', 'ATIS: ' + (d.error || 'unavailable')); setState('ready'); return; }
      log('atc', '📻 ' + d.atis);
      setFreq('ATIS');
      setState('ready');
      // Play ATIS via TTS if an audio URL is provided
      if (d.audio) {
        const abs = d.audio.startsWith('http') ? d.audio : location.origin + d.audio;
        playAudio(abs); addPlayButton(abs);
      }
    } catch (e) { log('sys', 'ATIS error: ' + e.message); setState('ready'); }
  }
  async function searchAirports(q) {
    if (!q || q.length < 2) { apresults.classList.remove('open'); return; }
    try {
      const r = await fetch('/api/airports?q=' + encodeURIComponent(q));
      const d = await r.json();
      apresults.innerHTML = '';
      (d.airports || []).forEach(a => {
        const row = document.createElement('div');
        row.className = 'ap';
        row.innerHTML = `<span><b>${a.icao}</b> ${a.name} · ${a.city} ${a.country}</span><span class="dim">${(a.freqs?.GND || a.freqs?.TWR || '').toFixed(3) || ''}</span>`;
        row.onclick = () => { selectAirport(a.icao); };
        apresults.appendChild(row);
      });
      if (d.airports.length) apresults.classList.add('open'); else apresults.classList.remove('open');
    } catch (e) {}
  }
  apsearch.addEventListener('input', () => searchAirports(apsearch.value.trim()));
  apsearch.addEventListener('focus', () => { if (apsearch.value.trim().length >= 2) searchAirports(apsearch.value.trim()); });
  document.addEventListener('click', e => { if (!e.target.closest('.aprow')) apresults.classList.remove('open'); });

  // ---- settings panel ----
  async function loadSettings({ icao } = {}) {
    try {
      const r = await fetch('/api/settings');
      const d = await r.json();
      if (icao) {
        await fetch('/api/settings', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ airport: icao })
        });
      }
      if (d.airport) { playFreqs(d.airport); }
      if (d.simbrief_id) simbrief.value = d.simbrief_id;
      if (d.callsign) callsign.value = d.callsign;
    } catch (e) {}
  }
  gear.addEventListener('click', () => { settings.classList.toggle('open'); });
  closeBtn.addEventListener('click', () => settings.classList.remove('open'));
  saveBtn.addEventListener('click', async () => {
    saveBtn.disabled = true;
    try {
      const r = await fetch('/api/settings', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ simbrief_id: simbrief.value.trim(), callsign: callsign.value.trim(), airport: curIcao })
      });
      if (r.ok) { saved.textContent = 'Saved ✓'; setTimeout(() => { saved.textContent=''; }, 2000); }
      else saved.textContent = 'Save failed';
    } catch (e) { saved.textContent = 'Save failed'; }
    finally { saveBtn.disabled = false; }
  });
  loadSettings();
  ptt.addEventListener('pointerup', e => { e.preventDefault(); stopRecording(); });
  ptt.addEventListener('pointerleave', stopRecording);
  document.addEventListener('keydown', e => { if (e.code === 'Space' && !e.repeat) { e.preventDefault(); startRecording(); } });
  document.addEventListener('keyup', e => { if (e.code === 'Space') stopRecording(); });
})();
