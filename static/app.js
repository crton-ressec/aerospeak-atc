(() => {
  const $ = id => document.getElementById(id);
  const ptt = $('ptt'), meter = $('meter'), rx = $('rx'), logEl = $('log'), state = $('state');
  $('ver').textContent = 'v0.1';
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

  async function sendAudio(blob) {
    const fd = new FormData(); fd.append('audio', blob, 'tx.wav');
    try {
      const resp = await fetch('/api/chat', { method: 'POST', body: fd });
      const data = await resp.json();
      if (!resp.ok) { log('sys', 'Error: ' + (data.error || resp.status)); setState('error'); return; }
      log('atc', data.text || '');
      playAudio(data.audio); setState('ready');
    } catch (e) { log('sys', 'Network error: ' + e.message); setState('error'); }
  }

  function playAudio(url) {
    const audio = new Audio(url);
    audio.play().catch(() => log('sys', 'Tap to enable audio'));
  }

  ptt.addEventListener('pointerdown', e => { e.preventDefault(); startRecording(); });
  ptt.addEventListener('pointerup', e => { e.preventDefault(); stopRecording(); });
  ptt.addEventListener('pointerleave', stopRecording);
  document.addEventListener('keydown', e => { if (e.code === 'Space' && !e.repeat) { e.preventDefault(); startRecording(); } });
  document.addEventListener('keyup', e => { if (e.code === 'Space') stopRecording(); });
})();
