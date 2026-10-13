/*
 * Galaxy permission prompt: a glass modal for every Android action. Nothing runs before the user decides here.
 *
 * The server keeps the executable action and returns it only with an approved decision. This file opens only what the
 * server approved, and only allowlisted destinations (the same rules as galaxy/android_intents.py). The pure helpers are
 * tested in Node (tests/js/permission_modal.test.js); the DOM controller follows them.
 */
(function (root) {
  'use strict';

  var DECISIONS = ['once', 'always', 'deny'];
  // BEGIN GENERATED ALLOWLIST (galaxy/platforms.py). Do not edit by hand; a test checks this block.
  var HOSTS = ['music.apple.com', 'music.youtube.com', 'open.spotify.com', 'wa.me', 'web.whatsapp.com', 'www.hotstar.com', 'www.jiosaavn.com', 'www.netflix.com', 'www.primevideo.com', 'www.youtube.com', 'wynk.in'];
  var PACKAGES = ['com.amazon.avod.thirdpartyclient', 'com.apple.android.music', 'com.bsbportal.music', 'com.google.android.apps.youtube.music', 'com.google.android.contacts', 'com.google.android.dialer', 'com.google.android.youtube', 'com.jio.media.jiobeats', 'com.netflix.mediaclient', 'com.spotify.music', 'com.whatsapp', 'in.startv.hotstar'];
  var PAIRS = ['music.apple.com|com.apple.android.music', 'music.youtube.com|com.google.android.apps.youtube.music', 'open.spotify.com|com.spotify.music', 'web.whatsapp.com|com.whatsapp', 'www.hotstar.com|in.startv.hotstar', 'www.jiosaavn.com|com.jio.media.jiobeats', 'www.netflix.com|com.netflix.mediaclient', 'www.primevideo.com|com.amazon.avod.thirdpartyclient', 'www.youtube.com|com.google.android.youtube', 'wynk.in|com.bsbportal.music'];
  var SETTINGS = ['android.settings.BLUETOOTH_SETTINGS', 'android.settings.DISPLAY_SETTINGS', 'android.settings.SOUND_SETTINGS', 'android.settings.WIFI_SETTINGS'];
  // END GENERATED ALLOWLIST
  var BADGE = { youtube: '▶', spotify: '♪', whatsapp: 'W', call: '✆', camera: '◉', settings: '⚙', notify: '✦' };
  var TONE = { idle: 'grey', pending: 'amber', submitting: 'blue', done: 'green', denied: 'red', expired: 'grey', error: 'red' };
  var STATUS_TEXT = {
    pending: 'Aapki permission ka intezaar hai.',
    submitting: 'Bhej raha hoon…',
    done: 'Ho gaya.',
    denied: 'Band kar diya gaya. Kuch nahi hua.',
    expired: 'Request expire ho gayi. Dobara bolein.',
    error: 'Kuch gadbad hui.'
  };
  var TERMINAL = ['done', 'denied', 'expired', 'error'];

  // ---- pure helpers (tested in Node) ----
  function isSafeLaunchUrl(url) {
    var s = String(url || ''), m, fallback;
    if (s.length > 2000) return false;
    if (/^tel:\+?\d{8,15}$/.test(s)) return true;
    m = /^https:\/\/([a-z0-9.-]+)(\/[^\s]*)?$/.exec(s);
    if (m) return HOSTS.indexOf(m[1]) !== -1;
    m = /^intent:\/\/([a-z0-9.-]+)(\/[^#\s;]*)#Intent;scheme=https;package=([a-z0-9.]+);S\.browser_fallback_url=([^;\s]+);end$/.exec(s);
    if (m) {
      if (PAIRS.indexOf(m[1] + '|' + m[3]) === -1) return false;   // a host and a package belong to one platform
      try {
        fallback = /^https:\/\/([a-z0-9.-]+)(\/[^\s]*)?$/.exec(decodeURIComponent(m[4]));
      } catch (e) {
        return false;
      }
      return !!fallback && fallback[1] === m[1];
    }
    m = /^intent:#Intent;package=([a-z0-9.]+);end$/.exec(s);
    if (m) return PACKAGES.indexOf(m[1]) !== -1;
    m = /^intent:#Intent;action=(android\.settings\.[A-Z_]+);end$/.exec(s);
    return !!m && SETTINGS.indexOf(m[1]) !== -1;
  }

  // What the modal shows: the platform badge, the exact action, and the destination. Pure, so Node can check it.
  function describeAction(request) {
    var req = request || {};
    return {
      platform: req.platform || '',
      verb: req.verb || '',
      destination: req.destination || '',
      badge: req.badge || BADGE[req.kind] || 'G',
      title: req.title || ''
    };
  }

  function decisionBody(requestId, decision) {
    if (!/^[0-9a-f]{32}$/.test(String(requestId || '')) || DECISIONS.indexOf(decision) === -1) return null;
    return { request_id: requestId, decision: decision };
  }

  function buttonsFor(request) {
    var req = request || {}, canAlways;
    if (req.mode === 'streamlined') {
      return { revoke: req.can_always === true, buttons: [
        { id: 'once', label: 'Continue', enabled: true, primary: true },
        { id: 'deny', label: 'Deny', enabled: true, primary: false }] };
    }
    canAlways = req.can_always === true && req.risk !== 'high';
    return { revoke: false, buttons: [
      { id: 'once', label: 'Allow Once', enabled: true, primary: true },
      { id: 'always', label: 'Always Allow', enabled: canAlways, primary: false,
        hint: canAlways ? '' : 'Not offered for calls or messages' },
      { id: 'deny', label: 'Deny', enabled: true, primary: false }] };
  }

  function reduce(state, event) {
    var s = state || { phase: 'idle' };
    switch (event.type) {
      case 'open':
        return s.phase === 'idle' ? { phase: 'pending', request: event.request } : s;
      case 'decide':
        return (s.phase === 'pending' && decisionBody(s.request.request_id, event.decision))
          ? { phase: 'submitting', request: s.request, decision: event.decision } : s;
      case 'result':
        if (s.phase !== 'submitting') return s;
        if (event.status === 'approved') return { phase: 'done', request: s.request, action: event.action };
        if (event.status === 'denied') return { phase: 'denied', request: s.request };
        if (event.status === 'expired') return { phase: 'expired', request: s.request };
        return { phase: 'error', request: s.request, message: event.message || 'refused' };
      case 'fail':
        return s.phase === 'submitting' ? { phase: 'error', request: s.request, message: event.message || 'network' } : s;
      case 'expire':
        return s.phase === 'pending' ? { phase: 'expired', request: s.request } : s;
      case 'close':
        return { phase: 'idle' };
      default:
        return s;
    }
  }

  function countdownLabel(ms) {
    var total = Math.max(0, Math.floor(ms / 1000));
    return Math.floor(total / 60) + ':' + String(total % 60).padStart(2, '0');
  }

  function statusTone(phase) {
    return TONE[phase] || 'grey';
  }

  function actionPlan(action) {
    if (!action) return null;
    if (action.type === 'open_url' || action.type === 'dial' || action.type === 'settings') {
      return isSafeLaunchUrl(action.url) ? { kind: 'url', url: action.url } : null;
    }
    if (action.type === 'camera') return { kind: 'camera' };
    if (action.type === 'notify') {
      return { kind: 'notify', title: String(action.title || 'Galaxy AI').slice(0, 60),
               body: String(action.body || '').slice(0, 140) };
    }
    return null;
  }

  // ---- DOM controller ----
  var refs = null, state = { phase: 'idle' }, ticker = null, expiresAt = 0;

  function $(id) { return document.getElementById(id); }

  function getRefs() {
    if (!refs) {
      refs = { modal: $('gvPermModal'), title: $('gvPermTitle'), dest: $('gvPermDest'), why: $('gvPermWhy'),
               preview: $('gvPermPreview'), meta: $('gvPermMeta'), status: $('gvPermStatus'),
               actions: $('gvPermActions'), dot: $('gvPermDot'), badge: $('gvPermBadge'), revoke: $('gvPermRevoke') };
    }
    return refs;
  }

  function dispatch(event) {
    state = reduce(state, event);
    render();
  }

  function render() {
    var r = getRefs(), req = state.request || {}, view = buttonsFor(req), terminal = TERMINAL.indexOf(state.phase) !== -1;
    if (!r.modal) return;
    r.dot.setAttribute('data-tone', statusTone(state.phase));
    r.badge.setAttribute('data-kind', req.kind || '');
    r.badge.textContent = req.badge || BADGE[req.kind] || 'G';
    r.title.textContent = req.title || '';
    r.dest.textContent = [req.platform, req.destination].filter(Boolean).join(' · ');
    r.why.textContent = req.why || '';
    r.preview.textContent = req.preview || '';
    r.preview.hidden = !req.preview;
    var parts = [req.verb ? 'Action: ' + req.verb : '', req.risk === 'high' ? 'High risk: asks every time' : ''];
    if (state.phase === 'pending') parts.push('Expires in ' + countdownLabel(expiresAt - Date.now()));
    r.meta.textContent = parts.filter(Boolean).join(' · ');
    r.status.textContent = (STATUS_TEXT[state.phase] || '') +
      (state.phase === 'error' && state.message ? ' (' + state.message + ')' : '');
    r.revoke.hidden = !(view.revoke && state.phase === 'pending');
    r.actions.textContent = '';   // clears the children without markup
    var buttons = terminal ? [{ id: 'close', label: 'Close', enabled: true, primary: true }] : view.buttons;
    buttons.forEach(function (b) {
      var el = document.createElement('button');
      el.type = 'button';
      el.className = 'perm-btn' + (b.primary ? ' primary' : '');
      el.textContent = b.label;
      el.setAttribute('data-decision', b.id);
      el.disabled = !(terminal || state.phase === 'pending') || !b.enabled;
      if (b.hint) el.title = b.hint;
      el.onclick = function () { if (b.id === 'close') close(); else decide(b.id); };
      r.actions.appendChild(el);
    });
  }

  function decide(decision) {
    var body = decisionBody(state.request && state.request.request_id, decision);
    if (!body || state.phase !== 'pending') return;
    dispatch({ type: 'decide', decision: decision });
    var payload = { request_id: body.request_id, decision: body.decision,
                    user_id: root.galaxyUser ? root.galaxyUser() : '' };
    var headers = root.galaxyHeaders ? root.galaxyHeaders() : { 'Content-Type': 'application/json' };
    fetch('/api/android/decision', { method: 'POST', headers: headers, body: JSON.stringify(payload) })
      .then(function (res) { return res.json(); })
      .then(function (data) {
        dispatch({ type: 'result', status: data.status, action: data.action, message: data.reason });
        if (state.phase === 'done') {
          stopTicker();
          execute(state.action);
          setTimeout(close, 900);
        }
      })
      .catch(function () { dispatch({ type: 'fail', message: 'network' }); });
  }

  // Runs only after the server approved the decision. Browsers may still block pop-ups or the camera picker without a
  // fresh user gesture; the status line then stays on "Ho gaya" and the user can open the link from the chat.
  function execute(action) {
    var plan = actionPlan(action), picker;
    if (!plan) return;
    if (plan.kind === 'url') {
      if (plan.url.indexOf('https://') === 0) root.open(plan.url, '_blank', 'noopener');
      else root.location.href = plan.url;
    } else if (plan.kind === 'camera') {
      picker = document.createElement('input');
      picker.type = 'file';
      picker.accept = 'image/*';
      picker.setAttribute('capture', 'environment');
      picker.click();
    } else if (plan.kind === 'notify' && root.Notification && typeof root.Notification.requestPermission === 'function') {
      root.Notification.requestPermission().then(function (permission) {
        if (permission === 'granted') new root.Notification(plan.title, { body: plan.body });
      });
    }
  }

  function revokeAlways() {
    var headers, payload, r = getRefs();
    if (!state.request) return;
    headers = root.galaxyHeaders ? root.galaxyHeaders() : { 'Content-Type': 'application/json' };
    payload = { kind: state.request.kind, user_id: root.galaxyUser ? root.galaxyUser() : '' };
    fetch('/api/android/revoke', { method: 'POST', headers: headers, body: JSON.stringify(payload) })
      .then(function (res) { return res.json(); })
      .then(function (data) {
        r.status.textContent = data.status === 'revoked' ? 'Always Allow hata diya gaya.' : 'Koi Always Allow nahi mila.';
      })
      .catch(function () { r.status.textContent = 'Network error.'; });
  }

  function stopTicker() {
    if (ticker) clearInterval(ticker);
    ticker = null;
  }

  function tick() {
    if (state.phase !== 'pending') { stopTicker(); return; }
    if (Date.now() >= expiresAt) { dispatch({ type: 'expire' }); stopTicker(); return; }
    render();
  }

  function open(request) {
    var r;
    if (!request || !request.request_id || !$('gvPermModal')) return;
    stopTicker();
    expiresAt = Date.now() + (Number(request.expires_in_s) || 120) * 1000;
    state = { phase: 'idle' };
    dispatch({ type: 'open', request: request });
    r = getRefs();
    r.revoke.onclick = revokeAlways;
    r.modal.classList.add('open');
    r.modal.setAttribute('aria-hidden', 'false');
    ticker = setInterval(tick, 1000);
    var first = r.actions.querySelector('button:not([disabled])');
    if (first) first.focus();
  }

  function close() {
    var r = getRefs();
    stopTicker();
    dispatch({ type: 'close' });
    if (r.modal) {
      r.modal.classList.remove('open');
      r.modal.setAttribute('aria-hidden', 'true');
    }
  }

  if (typeof document !== 'undefined') {
    document.addEventListener('keydown', function (e) {
      if (e.key !== 'Escape') return;
      if (state.phase === 'pending') decide('deny');          // Escape is the safe default: deny
      else if (state.phase !== 'submitting' && state.phase !== 'idle') close();
    });
  }

  var pure = {
    isSafeLaunchUrl: isSafeLaunchUrl, decisionBody: decisionBody, buttonsFor: buttonsFor, reduce: reduce,
    countdownLabel: countdownLabel, statusTone: statusTone, actionPlan: actionPlan, describeAction: describeAction
  };
  root.GalaxyPermission = { open: open, close: close, pure: pure };
  if (typeof module !== 'undefined' && module.exports) module.exports = pure;
})(typeof window !== 'undefined' ? window : globalThis);
