// Node tests for the permission modal's pure logic (galaxy/web/permission_modal.js).
'use strict';
const assert = require('assert');
const path = require('path');
const PM = require(path.join(__dirname, '..', '..', 'galaxy', 'web', 'permission_modal.js'));
const ID = 'a'.repeat(32);

const tests = [];
function test(label, fn) { tests.push({ label, fn }); }

test('URL allowlist accepts the shapes the server produces', () => {
  const ok = [
    'https://www.youtube.com/results?search_query=free%20fire',
    'https://wa.me/919876543210?text=hi%20there',
    'https://web.whatsapp.com/',
    'tel:+919876543210',
    'intent://open.spotify.com/search/lofi%20mix#Intent;scheme=https;package=com.spotify.music;' +
      'S.browser_fallback_url=https%3A%2F%2Fopen.spotify.com%2Fsearch%2Flofi%2520mix;end',
    'intent:#Intent;action=android.settings.WIFI_SETTINGS;end'
  ];
  ok.forEach((u) => assert.ok(PM.isSafeLaunchUrl(u), 'should allow ' + u));
});

test('URL allowlist refuses hostile and off-list shapes', () => {
  const bad = [
    'javascript:alert(1)',
    'https://evil.example/',
    'https://open.spotify.com.evil.example/',
    'intent://open.spotify.com/#Intent;scheme=https;package=com.evil.app;S.browser_fallback_url=https%3A%2F%2Fopen.spotify.com%2F;end',
    'intent://open.spotify.com/#Intent;scheme=https;package=com.spotify.music;S.browser_fallback_url=https%3A%2F%2Fevil.example%2F;end',
    'intent:#Intent;action=android.settings.DEVELOPMENT_SETTINGS;end',
    'tel:abc', 'tel:+91', '', undefined
  ];
  bad.forEach((u) => assert.ok(!PM.isSafeLaunchUrl(u), 'should refuse ' + u));
});

test('decision body accepts only a well-formed id and a known decision', () => {
  assert.deepStrictEqual(PM.decisionBody(ID, 'once'), { request_id: ID, decision: 'once' });
  assert.strictEqual(PM.decisionBody('not-an-id', 'once'), null);
  assert.strictEqual(PM.decisionBody(ID, 'yes please'), null);
});

test('full prompt offers Always Allow only for low-risk requests', () => {
  const low = PM.buttonsFor({ mode: 'full', risk: 'low', can_always: true });
  assert.deepStrictEqual(low.buttons.map((b) => b.id), ['once', 'always', 'deny']);
  assert.strictEqual(low.buttons[1].enabled, true);
  const high = PM.buttonsFor({ mode: 'full', risk: 'high', can_always: false });
  assert.strictEqual(high.buttons[1].enabled, false);
  assert.ok(high.buttons[1].hint.length > 0);
});

test('streamlined mode is Continue / Deny, with revoke when always-allowed', () => {
  const view = PM.buttonsFor({ mode: 'streamlined', risk: 'low', can_always: true });
  assert.deepStrictEqual(view.buttons.map((b) => b.label), ['Continue', 'Deny']);
  assert.strictEqual(view.revoke, true);
});

test('reducer: the happy path, and a second decision while submitting is ignored', () => {
  const req = { request_id: ID, kind: 'call', risk: 'high', mode: 'full', can_always: false };
  let s = PM.reduce(undefined, { type: 'open', request: req });
  assert.strictEqual(s.phase, 'pending');
  s = PM.reduce(s, { type: 'decide', decision: 'once' });
  assert.strictEqual(s.phase, 'submitting');
  assert.strictEqual(PM.reduce(s, { type: 'decide', decision: 'always' }).phase, 'submitting');
  s = PM.reduce(s, { type: 'result', status: 'approved', action: { type: 'dial', url: 'tel:+919876543210' } });
  assert.strictEqual(s.phase, 'done');
  assert.strictEqual(s.action.url, 'tel:+919876543210');
  assert.strictEqual(PM.reduce(s, { type: 'decide', decision: 'once' }).phase, 'done');
});

test('reducer: a malformed request id never reaches the server', () => {
  const s = PM.reduce(undefined, { type: 'open', request: { request_id: 'zz', kind: 'call' } });
  assert.strictEqual(PM.reduce(s, { type: 'decide', decision: 'once' }).phase, 'pending');
});

test('reducer: refusals, expiry and network failure end in a visible phase', () => {
  const req = { request_id: ID, kind: 'call' };
  const pending = PM.reduce(undefined, { type: 'open', request: req });
  const submitting = PM.reduce(pending, { type: 'decide', decision: 'once' });
  const refused = PM.reduce(submitting, { type: 'result', status: 'refused', message: 'already decided' });
  assert.strictEqual(refused.phase, 'error');
  assert.strictEqual(refused.message, 'already decided');
  assert.strictEqual(PM.reduce(submitting, { type: 'result', status: 'denied' }).phase, 'denied');
  assert.strictEqual(PM.reduce(submitting, { type: 'result', status: 'expired' }).phase, 'expired');
  assert.strictEqual(PM.reduce(submitting, { type: 'fail', message: 'network' }).message, 'network');
  assert.strictEqual(PM.reduce(pending, { type: 'expire' }).phase, 'expired');
});

test('countdown label and status tones', () => {
  assert.strictEqual(PM.countdownLabel(125000), '2:05');
  assert.strictEqual(PM.countdownLabel(9000), '0:09');
  assert.strictEqual(PM.countdownLabel(-5), '0:00');
  assert.strictEqual(PM.statusTone('pending'), 'amber');
  assert.strictEqual(PM.statusTone('done'), 'green');
  assert.strictEqual(PM.statusTone('nonsense'), 'grey');
});

test('action plans: only safe URLs, camera and notification are performed', () => {
  assert.deepStrictEqual(PM.actionPlan({ type: 'open_url', url: 'https://wa.me/919876543210' }),
    { kind: 'url', url: 'https://wa.me/919876543210' });
  assert.strictEqual(PM.actionPlan({ type: 'open_url', url: 'https://evil.example/' }), null);
  assert.deepStrictEqual(PM.actionPlan({ type: 'camera' }), { kind: 'camera' });
  const note = PM.actionPlan({ type: 'notify', title: 'Galaxy AI', body: 'x'.repeat(500) });
  assert.strictEqual(note.body.length, 140);
  assert.strictEqual(PM.actionPlan({ type: 'mystery' }), null);
  assert.strictEqual(PM.actionPlan(null), null);
});

test('cross-platform links are refused in the browser too (a host and a package belong to one platform)', () => {
  const cross = [
    'intent://open.spotify.com/#Intent;scheme=https;package=com.netflix.mediaclient;S.browser_fallback_url=https%3A%2F%2Fopen.spotify.com%2F;end',
    'intent://www.netflix.com/#Intent;scheme=https;package=com.spotify.music;S.browser_fallback_url=https%3A%2F%2Fwww.netflix.com%2F;end',
    'intent://open.spotify.com/#Intent;scheme=https;package=com.spotify.music;S.browser_fallback_url=https%3A%2F%2Fwww.netflix.com%2F;end'
  ];
  cross.forEach((u) => assert.ok(!PM.isSafeLaunchUrl(u), 'should refuse ' + u));
  assert.ok(PM.isSafeLaunchUrl('intent:#Intent;package=com.netflix.mediaclient;end'), 'a registered app launch is allowed');
  assert.ok(!PM.isSafeLaunchUrl('intent:#Intent;package=com.evil.app;end'), 'an unregistered app launch is refused');
});

test('describeAction shows the platform badge, the exact action and the destination', () => {
  const view = PM.describeAction({ platform: 'Netflix', verb: 'Search', destination: 'www.netflix.com', badge: 'N', title: 'x' });
  assert.deepStrictEqual(view, { platform: 'Netflix', verb: 'Search', destination: 'www.netflix.com', badge: 'N', title: 'x' });
  assert.strictEqual(PM.describeAction({ kind: 'not_a_platform' }).badge, 'G', 'unknown kinds get a neutral badge');
});

(() => {
  let passed = 0, failed = 0;
  for (const t of tests) {
    try { t.fn(); passed += 1; }
    catch (e) { failed += 1; console.error('FAIL', t.label, '\n  ', e.message); }
  }
  console.log(`${passed} permission modal checks passed` + (failed ? ` (${failed} failed)` : ''));
  if (failed) process.exitCode = 1;
})();
