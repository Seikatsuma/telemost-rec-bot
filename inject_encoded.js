// Запись встречи без декодирования: перехват уже сжатых кадров Opus.
//
// Чем отличается от inject.js: там входящие дорожки разворачивались в WebAudio,
// смешивались и заново сжимались MediaRecorder'ом — три лишние работы. Здесь кадры
// забираются такими, какими пришли по сети (`RTCRtpReceiver.createEncodedStreams`)
// и уходят в Python как есть. В файле остаются те же байты, что прислал сервер встречи.
//
// Учёт участников (сигнальный WebSocket) повторяет inject.js намеренно: он нужен и для
// подсчёта людей в звонке, и для `telemost.join`, который признаёт вход состоявшимся
// только по снимку `window.__rec.snapshot()`. Без него бот входит, но сам об этом не знает —
// проверено 18.09: 1618 кадров поймано, а заход отчитался «failed».
//
// Настройки со стороны Python (add_init_script):
//   window.__recStripVideo = true  — вырезать видео из согласования. ПО УМОЛЧАНИЮ ВЫКЛЮЧЕНО:
//       замер 18.09 показал, что Телемост на такую правку отвечает «Произошла ошибка» и
//       во встречу не пускает. Оставлено только для опытов.
//   window.__recFrame(ssrc, rtpTimestamp, atMs, base64) — приёмник кадров
//   window.__recEvent(kind, payload) — события для журнала хронологии
(() => {
  if (window.__rec) return;
  const R = (window.__rec = {
    participants: new Map(),   // id -> имя
    speaking: new Map(),       // id -> говорит ли сейчас (признак vad из раздачи слотов)
    selfId: null,
    signalingOpen: false,
    signalingEverOpen: false,
    lastSignalAt: 0,
    tracks: 0,
    liveTracks: 0,
    state: 'encoded',          // этот способ пишет всегда, пока есть кадры
    frames: 0,
    bytes: 0,
    level: 0,
    errors: [],
  });
  const err = (where, e) => {
    R.errors.push(where + ': ' + (e && e.message ? e.message : String(e)));
    if (R.errors.length > 20) R.errors.shift();
  };
  const emit = (kind, payload) => {
    try { if (window.__recEvent) window.__recEvent(kind, payload); } catch (e) {}
  };

  // ---------- Участники по сигнальному протоколу (как в inject.js) ----------
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
        emit('participants', [...R.participants.entries()]);
      }
      if (m.upsertDescription) {
        for (const d of m.upsertDescription.description || []) R.participants.set(d.id, nameOf(d));
        emit('participants', [...R.participants.entries()]);
      }
      // Кто говорит прямо сейчас. Телемост смешивает звук всех в один поток (проверено
      // 18.09: один ssrc, один csrc, уровней громкости нет), но в раздаче слотов у каждого
      // участника стоит признак речевой активности vad. Это единственный доступный источник
      // хронологии — по нему и размечаем реплики.
      if (m.slotsConfig && Array.isArray(m.slotsConfig.slots)) {
        for (const s of m.slotsConfig.slots) {
          const pid = s.participant && s.participant.participantId;
          if (!pid) continue;
          const now = !!s.vad;
          if (R.speaking.get(pid) === now) continue;
          R.speaking.set(pid, now);
          emit('vad', {id: pid, name: R.participants.get(pid) || '', speaking: now,
                       at: Date.now()});
        }
      }
      if (m.removeDescription) {
        for (const id of m.removeDescription.descriptionId || []) R.participants.delete(id);
        emit('participants', [...R.participants.entries()]);
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
          }
        } catch (e) {}
      }
      return origSend(data);
    };
    ws.addEventListener('message', onSignal);
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
    frames: R.frames,
    bytes: R.bytes,
    speaking: [...R.speaking.entries()].filter(([, v]) => v).map(([k]) => R.participants.get(k) || k),
    errors: R.errors.slice(-5),
  });

  // ---------- Перехват сжатых кадров ----------
  // Вырезание видео из SDP оставлено выключенным: Телемост на него отвечает ошибкой входа.
  function stripVideo(sdp) {
    if (!sdp) return sdp;
    return sdp.split(/(?=^m=)/m).map((p) => p.startsWith('m=video')
      ? p.replace(/^a=(sendrecv|sendonly|recvonly)\s*$/m, 'a=inactive') : p).join('');
  }

  const Orig = window.RTCPeerConnection;
  if (!Orig) return;

  R.pcs = [];
  // Отказ от видео ШТАТНЫМ способом, без правки SDP (та ломает вход — замер 18.09).
  // Пробуем по очереди: погасить направление трансивера, затем остановить его.
  R.dropVideo = (mode) => {
    let touched = 0;
    for (const pc of R.pcs) {
      let trs = [];
      try { trs = pc.getTransceivers(); } catch (e) { continue; }
      for (const tr of trs) {
        const kind = (tr.receiver && tr.receiver.track && tr.receiver.track.kind)
          || (tr.sender && tr.sender.track && tr.sender.track.kind);
        if (kind !== 'video') continue;
        try {
          if (mode === 'stop') { tr.stop(); } else { tr.direction = 'inactive'; }
          touched += 1;
        } catch (e) { err('dropVideo', e); }
      }
    }
    return touched;
  };

  function wrap(pc) {
    R.pcs.push(pc);
    if (window.__recStripVideo) {
      const origSetRemote = pc.setRemoteDescription.bind(pc);
      pc.setRemoteDescription = (desc, ...rest) => {
        try {
          if (desc && desc.sdp) desc = {type: desc.type, sdp: stripVideo(desc.sdp)};
        } catch (e) { err('stripVideo', e); }
        return origSetRemote(desc, ...rest);
      };
    }

    pc.addEventListener('track', (ev) => {
      if (ev.track.kind !== 'audio') return;
      R.tracks += 1;
      R.liveTracks += 1;
      emit('track_added', {id: ev.track.id, streams: (ev.streams || []).map((s) => s.id)});
      ev.track.addEventListener('ended', () => {
        R.liveTracks = Math.max(0, R.liveTracks - 1);
        emit('track_ended', {id: ev.track.id});
      });

      if (window.__recNoCapture) return;   // режим замера: только учёт участников, без записи
      let streams;
      try {
        streams = ev.receiver.createEncodedStreams();
      } catch (e) {
        err('createEncodedStreams', e);
        emit('encoded_unavailable', String(e));   // Python откатится на прежний способ
        return;
      }
      const reader = streams.readable.getReader();
      const writer = streams.writable.getWriter();
      (async () => {
        for (;;) {
          let chunk;
          try { chunk = await reader.read(); } catch (e) { break; }
          if (chunk.done) break;
          const frame = chunk.value;
          const md = frame.getMetadata ? frame.getMetadata() : {};
          const b = new Uint8Array(frame.data);
          let s = '';
          for (let i = 0; i < b.length; i += 0x8000) {
            s += String.fromCharCode.apply(null, b.subarray(i, i + 0x8000));
          }
          R.frames += 1;
          R.bytes += b.length;
          try {
            await window.__recFrame(md.synchronizationSource || 0, frame.timestamp || 0,
                                    performance.now(), btoa(s));
          } catch (e) { /* биндинг пропал — страница закрывается */ }
          try { await writer.write(frame); } catch (e) { break; }
        }
      })();
    });
    return pc;
  }

  function Patched(...args) { return wrap(new Orig(...args)); }
  Patched.prototype = Orig.prototype;
  Object.setPrototypeOf(Patched, Orig);
  window.RTCPeerConnection = Patched;

  // ---------- Бот не звучит ----------
  const md = navigator.mediaDevices;
  if (md && md.getUserMedia && !window.__recKeepMic) {
    const orig = md.getUserMedia.bind(md);
    md.getUserMedia = async (c) => {
      // Обработка микрофона боту не нужна (он не говорит), а стоит ~19 пунктов процессора.
      if (c && c.audio) {
        c = Object.assign({}, c, {audio: {echoCancellation: false, noiseSuppression: false,
                                          autoGainControl: false}});
      }
      const s = await orig(c);
      s.getAudioTracks().forEach((t) => { t.enabled = false; });
      return s;
    };
  }
})();
