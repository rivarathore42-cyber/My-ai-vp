/*
 * Galaxy chat page. It opens straight into the assistant: no pairing, no password. Pure helpers are in app_logic.js,
 * permission cards in device_permissions.js, voice in voice_ui.js. Replies are rendered with textContent only.
 */
(function () {
  'use strict';

  var L = window.GalaxyLogic;
  var D = window.GalaxyDevice;
  var NO_NETWORK = 'Server nahi mil raha. Free hosting par server so raha ho sakta hai: 1 minute baad dobara try karein.';
  var busy = false;
  var currentLocation = null;
  var liveWatch = null;
  var noticeTimer = null;

  window.galaxyUser = function () { return ''; };   // the identity comes from the server session, never from the page
  window.galaxyHeaders = function () { return { 'Content-Type': 'application/json' }; };

  function $(id) { return document.getElementById(id); }

  function setStatus(kind, text) {
    $('status').setAttribute('data-kind', kind);
    $('status').querySelector('.status-text').textContent = text;
  }

  function notify(message, tone) {
    var box = $('notice');
    box.textContent = message;
    box.setAttribute('data-tone', tone || 'info');
    box.hidden = false;
    clearTimeout(noticeTimer);
    noticeTimer = setTimeout(function () { box.hidden = true; }, 4500);
  }
  window.galaxyNotify = notify;

  function bubble(role, text) {
    $('chat').classList.add('has-messages');
    var row = document.createElement('div');
    row.className = 'msg ' + role;
    var body = document.createElement('div');
    body.className = 'bubble';
    body.textContent = text;
    row.appendChild(body);
    var list = $('messages');
    list.appendChild(row);
    list.scrollTop = list.scrollHeight;
    return row;
  }

  function setBusy(value) {
    busy = value;
    $('send').disabled = value;
  }

  // Every request has a timeout, and nothing in it can throw into the page: the result is always an object.
  function request(path, options) {
    var opts = options || {};
    var init = { method: opts.method || 'GET', credentials: 'same-origin', headers: window.galaxyHeaders() };
    if (opts.body !== undefined) { init.body = JSON.stringify(opts.body); }
    var controller = typeof AbortController === 'function' ? new AbortController() : null;
    if (controller) { init.signal = controller.signal; }
    var timer = setTimeout(function () { if (controller) { controller.abort(); } }, L.REQUEST_TIMEOUT_MS);
    return fetch(path, init).then(function (res) {
      return res.text().then(function (raw) {
        clearTimeout(timer);
        var parsed = L.parseJson(raw);
        if (!parsed.ok) { return { parseError: true, status: res.status }; }
        return { ok: res.ok, status: res.status, body: parsed.value };
      });
    }).catch(function (err) {
      clearTimeout(timer);
      if (err && err.name === 'AbortError') { return { timeout: true }; }
      return { networkError: true };
    });
  }

  function connect() {
    setStatus('idle', 'Connecting');
    return request('/api/me').then(function (r) {
      if (r.ok && r.body && r.body.ok === true) {
        setStatus('ready', 'Ready');
        return true;
      }
      setStatus('offline', 'Offline');
      notify(r.networkError ? NO_NETWORK : L.describeFailure(r), 'warn');
      return false;
    });
  }

  function sendMessage(text, source) {
    if (busy) {
      notify('Pehle wala jawab aane dijiye.', 'info');
      return false;
    }
    var built = L.buildChatPayload(text, { source: source, location: currentLocation });
    if (built.error) {
      notify(built.error, 'warn');
      return false;
    }
    setBusy(true);
    bubble('user', built.payload.message);
    var waiting = bubble('bot pending', 'Soch raha hoon…');
    request('/api/chat', { method: 'POST', body: built.payload }).then(function (r) {
      waiting.remove();
      if (r.status === 401) { setStatus('offline', 'Offline'); }
      if (L.isReply(r)) {
        bubble(r.body.ok === false ? 'bot error' : 'bot', r.body.text);
        if (r.body.action && window.galaxyOnAction) { window.galaxyOnAction(r.body.action); }
        if (window.galaxyOnReply) { window.galaxyOnReply(r.body.text); }
      } else {
        var message = r.networkError ? NO_NETWORK : L.describeFailure(r);
        bubble('bot error', message);
        if (window.galaxyOnReply) { window.galaxyOnReply(message); }
      }
    }).catch(function () {
      bubble('bot error', 'Kuch gadbad hui. Dobara try karein.');
    }).then(function () {
      setBusy(false);
    });
    return true;
  }

  // The voice flow sends its recognised text through this hook.
  window.galaxyVoiceSend = function (text) { return sendMessage(text, 'voice'); };

  function readAsDataUrl(file) {
    return new Promise(function (resolve, reject) {
      var reader = new FileReader();
      reader.onload = function () { resolve(String(reader.result)); };
      reader.onerror = function () { reject(reader.error || new Error('read failed')); };
      reader.readAsDataURL(file);
    });
  }

  function sendFile(file) {
    var check = L.checkFile(file);
    if (check.error) {
      notify(check.error, 'warn');
      return;
    }
    if (busy) {
      notify('Pehle wala kaam chal raha hai.', 'info');
      return;
    }
    setBusy(true);
    var label = L.safeName(file.name);
    bubble('user', label);
    var waiting = bubble('bot pending', 'File check ho rahi hai…');
    readAsDataUrl(file).then(function (dataUrl) {
      return request('/api/upload', { method: 'POST', body: { filename: label, mime: file.type, data: dataUrl } });
    }).then(function (r) {
      waiting.remove();
      if (L.isReply(r)) {
        bubble(r.body.ok ? 'bot' : 'bot error', r.body.text);
      } else {
        bubble('bot error', r.networkError ? NO_NETWORK : L.describeFailure(r));
      }
    }, function () {
      waiting.remove();
      bubble('bot error', 'File padhi nahi ja saki. Dobara chunein.');
    }).then(function () {
      setBusy(false);
    }, function () {
      setBusy(false);
    });
  }

  function showLocation() {
    var chip = $('locationChip');
    if (currentLocation) {
      $('locationText').textContent = L.formatLocation(currentLocation);
      chip.hidden = false;
    } else {
      chip.hidden = true;
    }
  }

  function stopLive() {
    if (liveWatch !== null) { D.stopLive(liveWatch); }
    liveWatch = null;
    $('btnLiveLocation').textContent = 'Live';
  }

  function clearLocation() {
    currentLocation = null;
    stopLive();
    showLocation();
  }

  $('send').onclick = function () {
    var box = $('input');
    if (sendMessage(box.value, 'text')) {
      box.value = '';
      box.style.height = '';
    }
  };
  $('input').addEventListener('keydown', function (event) {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      $('send').click();
    }
  });
  $('input').addEventListener('input', function () {
    this.style.height = 'auto';
    this.style.height = Math.min(this.scrollHeight, 140) + 'px';
  });

  $('btnFile').onclick = function () {
    D.pickFile('image/*,application/pdf', null).then(function (file) {
      if (file) { sendFile(file); }
    });
  };
  $('btnCamera').onclick = function () {
    D.camera().then(function (answer) {
      if (answer.state !== 'granted') {
        notify(answer.message, 'info');
        return null;
      }
      return D.pickFile('image/*', 'environment');
    }).then(function (file) {
      if (file) { sendFile(file); }
    });
  };

  $('btnLocation').onclick = function () {
    D.location().then(function (result) {
      if (result.state === 'granted') {
        currentLocation = result.location;
        showLocation();
      } else {
        notify(result.message, result.state === 'declined' ? 'info' : 'warn');
      }
    });
  };
  $('btnLiveLocation').onclick = function () {
    if (liveWatch !== null) {
      stopLive();
      notify('Live location band.', 'info');
      return;
    }
    D.location().then(function (result) {
      if (result.state !== 'granted') {
        notify(result.message, 'warn');
        return;
      }
      currentLocation = result.location;
      showLocation();
      var id = D.startLive(function (loc) {
        currentLocation = loc;
        showLocation();
      }, function (err) {
        notify(err.message, 'warn');
        stopLive();
      });
      if (id === null) {
        notify('Is device par live location nahi chal sakti.', 'warn');
        return;
      }
      liveWatch = id;
      $('btnLiveLocation').textContent = 'Stop';
    });
  };
  $('btnClearLocation').onclick = clearLocation;

  connect();
})();
