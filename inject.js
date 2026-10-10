// Встраивается в страницу Телемоста до её собственных скриптов (add_init_script).
// Три задачи, все без звуковой карты и без разметки экрана:
//  1. Считать участников по сигнальному WebSocket Телемоста.
//     Протокол (разобран по main.ru.*.js, 16.09.26): JSON-сообщения вида {uid, <событие>: данные}.
//     updateDescription {description:[...]} — полный список, upsertDescription — добавить/обновить,
//     removeDescription {descriptionId:[...]} — убрать. Свой id бот узнаёт из исходящего {hello:{participantId}}.
//  2. Снимать звук всех участников из входящих WebRTC-дорожек (приём MiBot, webrtc-capture.ts),
//     смешивать в один поток и писать MediaRecorder'ом кусками по 5 с — куски сразу уходят на диск через __recChunk.
//  3. Не дать боту звучать и светиться: свои микрофон и камера отдаются выключенными дорожками.
(() => {
  if (window.__rec) return;
  const R = (window.__rec = {
    participants: new Map(), // id -> имя
    speaking: new Map(),     // id -> говорит ли сейчас (признак vad из раздачи слотов)
    selfId: null,
    signalingOpen: false,
    signalingEverOpen: false,
    lastSignalAt: 0,
    tracks: 0,
    liveTracks: 0,
    state: 'idle', // idle | recording | paused
    part: null,
    level: 0, // пик громкости за последние ~2 с, 0..1
    errors: [],
  });
  const err = (where, e) => {
    R.errors.push(where + ': ' + (e && e.message ? e.message : String(e)));
    if (R.errors.length > 20) R.errors.shift();
  };
  // События для журнала «кто когда говорил» (timeline.jsonl в папке записи).
  // part — адресат папки; биндинга может не быть (selftest, стенды) — тогда просто не пишем.
  const emit = (kind, payload) => {
    try {
      if (!window.__recEvent) return;
      const p = Object.assign({ part: R.part || '', at: Date.now() }, payload);
      window.__recEvent(kind, p);
    } catch (e) {}
  };

  // ---------- 1. Участники по сигналингу ----------
  const nameOf = (d) => {
    if (!d || typeof d !== 'object') return '';
    const m = d.meta || d.participantMeta || d.participantAttributes || {};
    return String(m.name || d.name || d.displayName || '');
  };
  const onSignal = (ev) => {
    if (typeof ev.data !== 'string' || ev.data.charCodeAt(0) !== 123) return;
    let m;
    try { m = JSON.parse(ev.data); } catch (e) { return; }
    R.lastSignalAt = Date.now();
    try {
      if (m.updateDescription) {
        R.participants = new Map((m.updateDescription.description || []).map((d) => [d.id, nameOf(d)]));
        emit('participants', { people: [...R.participants.entries()] });
      }
      if (m.upsertDescription) {
        for (const d of m.upsertDescription.description || []) R.participants.set(d.id, nameOf(d));
        emit('participants', { people: [...R.participants.entries()] });
      }
      // Кто говорит прямо сейчас. Телемост смешивает звук всех в один поток (замер 18.09:
      // один ssrc, уровней громкости нет), но в раздаче слотов у каждого участника есть
      // признак речевой активности vad — тот самый, от которого в окне «загорается» плитка.
      // По нему и пишем журнал «кто когда говорил» — по аккаунту, не по голосу.
      if (m.slotsConfig && Array.isArray(m.slotsConfig.slots)) {
        for (const s of m.slotsConfig.slots) {
          const pid = s.participant && s.participant.participantId;
          if (!pid) continue;
          const now = !!s.vad;
          if (!now && !R.speaking.has(pid)) continue; // первое «молчит» по неизвестному — шум
          if (R.speaking.get(pid) === now) continue;
          R.speaking.set(pid, now);
          emit('vad', { id: pid, name: R.participants.get(pid) || '', speaking: now, at: Date.now() });
        }
      }
      if (m.removeDescription) {
        for (const id of m.removeDescription.descriptionId || []) R.participants.delete(id);
        emit('participants', { people: [...R.participants.entries()] });
      }
    } catch (e) { err('signal', e); }
  };
  const OrigWS = window.WebSocket;
  function WrappedWS(...args) {
    const ws = new OrigWS(...args);
    let isSignaling = false;
    const origSend = ws.send.bind(ws);
    ws.send = (data) => {
      if (typeof data === 'string' && data.indexOf('"hello"') !== -1) {
        try {
          const m = JSON.parse(data);
          if (m.hello) {
            isSignaling = true;
            R.selfId = m.hello.participantId || R.selfId;
            R.signalingOpen = true;
            R.signalingEverOpen = true;
            R.participants = new Map();
            R.speaking = new Map();
          }
        } catch (e) {}
      }
      return origSend(data);
    };
    ws.addEventListener('message', (ev) => { onSignal(ev); });
    ws.addEventListener('close', () => { if (isSignaling) R.signalingOpen = false; });
    return ws;
  }
  WrappedWS.prototype = OrigWS.prototype;
  for (const k of ['CONNECTING', 'OPEN', 'CLOSING', 'CLOSED']) WrappedWS[k] = OrigWS[k];
  window.WebSocket = WrappedWS;

  R.humans = (botName) => {
    const names = [];
    for (const [id, name] of R.participants) {
      if (id === R.selfId) continue;
      if (botName && name === botName) continue;
      names.push(name);
    }
    return names;
  };

  // ---------- 3. Бот молчит (камера — картинка «Запись встречи», её включает telemost.media_off) ----------
  const md = navigator.mediaDevices;
  if (md && md.getUserMedia && !window.__recKeepMic) {
    const origGUM = md.getUserMedia.bind(md);
    md.getUserMedia = async (c) => {
      // Обработку микрофона (эхоподавление, шумодав, автоусиление) выключаем: бот не говорит,
      // а она работает непрерывно и стоит ~19 пунктов процессора (замер 18.09.26: 51 % → 32 %
      // в звонке; `--mute-audio` в аргументах уже был и к выигрышу отношения не имеет).
      if (c && c.audio) {
        c = Object.assign({}, c, {audio: {echoCancellation: false, noiseSuppression: false,
                                          autoGainControl: false}});
      }
      const s = await origGUM(c);
      s.getAudioTracks().forEach((t) => { t.enabled = false; });
      return s;
    };
  }

  // ---------- 2. Звук участников ----------
  let ctx = null, dest = null, analyser = null, recorder = null, stopResolve = null;
  const ensureCtx = () => {
    if (ctx) return;
    const Ctx = window.AudioContext || window.webkitAudioContext;
    ctx = new Ctx({ sampleRate: 48000 });
    dest = ctx.createMediaStreamDestination();
    analyser = ctx.createAnalyser();
    analyser.fftSize = 2048;
    const buf = new Float32Array(analyser.fftSize);
    let peak = 0, peakAt = 0;
    setInterval(() => {
      try {
        if (ctx.state === 'suspended') ctx.resume();
        analyser.getFloatTimeDomainData(buf);
        let p = 0;
        for (let i = 0; i < buf.length; i++) { const v = Math.abs(buf[i]); if (v > p) p = v; }
        const now = Date.now();
        if (p >= peak || now - peakAt > 2000) { peak = p; peakAt = now; }
        R.level = peak;
      } catch (e) {}
    }, 250);
  };
  const addTrack = (track) => {
    if (!track || track.kind !== 'audio' || track.__recAdded) return;
    track.__recAdded = true;
    try {
      ensureCtx();
      const stream = new MediaStream([track]);
      // Chromium не отдаёт звук удалённой дорожки в WebAudio, пока она не привязана к медиаэлементу.
      const a = new Audio();
      a.muted = true;
      a.srcObject = stream;
      a.play().catch(() => {});
      const src = ctx.createMediaStreamSource(stream);
      src.connect(dest);
      src.connect(analyser);
      R.tracks++;
      R.liveTracks++;
      track.addEventListener('ended', () => {
        R.liveTracks--;
        try { src.disconnect(); } catch (e) {}
        a.srcObject = null;
      });
    } catch (e) { err('track', e); }
  };
  const OrigPC = window.RTCPeerConnection;
  if (OrigPC) {
    function WrappedPC(...args) {
      const pc = new OrigPC(...args);
      pc.addEventListener('track', (ev) => addTrack(ev.track));
      return pc;
    }
    WrappedPC.prototype = OrigPC.prototype;
    try { Object.setPrototypeOf(WrappedPC, OrigPC); } catch (e) {}
    window.RTCPeerConnection = WrappedPC;
  }

  const toB64 = (buf) => {
    const bytes = new Uint8Array(buf);
    let s = '';
    for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    return btoa(s);
  };
  let sendChain = Promise.resolve();

  R.start = (part) => {
    ensureCtx();
    if (recorder && recorder.state !== 'inactive') return 'already';
    R.part = part;
    emit('rec', { op: 'start', part: part, at: Date.now() });
    // Состав на момент старта записи — чтобы в журнале были «вошёл» для уже сидящих в звонке.
    emit('participants', { people: [...R.participants.entries()] });
    // Кто заговорил до старта записи (vad прошёл при пустом part и в журнал не попал) —
    // повторяем открытые края, иначе начало речи теряется до следующего молчания.
    for (const [pid, on] of R.speaking) {
      if (on) emit('vad', { id: pid, name: R.participants.get(pid) || '', speaking: true, at: Date.now() });
    }
    recorder = new MediaRecorder(dest.stream, { mimeType: 'audio/webm;codecs=opus', audioBitsPerSecond: 64000 });
    recorder.ondataavailable = (e) => {
      if (!e.data || !e.data.size) return;
      const p = R.part;
      sendChain = sendChain.then(async () => {
        try { await window.__recChunk(p, toB64(await e.data.arrayBuffer())); } catch (x) { err('chunk', x); }
      });
    };
    recorder.onstop = () => {
      // R.part чистим только здесь: финальный ondataavailable читает его уже после stop().
      sendChain.then(() => { R.part = null; R.state = 'idle'; if (stopResolve) { stopResolve('stopped'); stopResolve = null; } });
    };
    recorder.start(5000);
    R.state = 'recording';
    return 'started';
  };
  R.pause = () => { if (recorder && recorder.state === 'recording') { recorder.requestData(); recorder.pause(); R.state = 'paused'; emit('rec', { op: 'pause', at: Date.now() }); } };
  R.resume = () => { if (recorder && recorder.state === 'paused') { recorder.resume(); R.state = 'recording'; emit('rec', { op: 'resume', at: Date.now() }); } };
  R.stop = () => new Promise((res) => {
    if (!recorder || recorder.state === 'inactive') { R.state = 'idle'; R.part = null; return res('idle'); }
    stopResolve = res;
    // Событие стопа — ещё с адресатом; сам R.part чистится в onstop, когда дошёл финальный кусок.
    emit('rec', { op: 'stop', at: Date.now() });
    recorder.stop();
    setTimeout(() => { if (stopResolve) { R.part = null; stopResolve('timeout'); stopResolve = null; } }, 15000);
  });
  R.snapshot = (botName) => ({
    humans: R.humans(botName),
    selfId: R.selfId,
    signalingOpen: R.signalingOpen,
    signalingEverOpen: R.signalingEverOpen,
    lastSignalAgo: R.lastSignalAt ? Date.now() - R.lastSignalAt : null,
    tracks: R.tracks,
    liveTracks: R.liveTracks,
    state: R.state,
    level: R.level,
    speaking: [...R.speaking.entries()].filter(([, v]) => v).map(([k]) => R.participants.get(k) || k),
    errors: R.errors.slice(-5),
  });
})();
