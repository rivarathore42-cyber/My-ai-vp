'use strict';
// Page logic (galaxy/web/app_logic.js): input checks, payloads, error wording and timeouts. Run by section AB.
const L = require('../../galaxy/web/app_logic.js');
let passed = 0;
let failed = 0;
function check(cond, msg) {
  if (cond) { passed++; } else { failed++; console.error('FAIL ' + msg); }
}

check(L.cleanText('  a\u0000b\u0007c  ', 100) === 'abc', 'control characters are removed');
check(L.checkMessage(' hi ').value === 'hi' && L.checkMessage(' hi ').error === null, 'trimmed text is accepted');
check(L.checkMessage('x'.repeat(4001)).error !== null, '4001 characters are refused');
check(L.checkMessage('x'.repeat(4000)).error === null, '4000 characters are accepted');
check(L.checkMessage(42).error !== null, 'non-text is refused');
check(L.buildChatPayload('   ').error !== null, 'an empty message is refused with a plain sentence');

const voice = L.buildChatPayload('namaste', { source: 'voice', location: { lat: 28.6, lon: 77.2, accuracy: 9 } });
check(voice.error === null && voice.payload.source === 'voice' && voice.payload.location.lat === 28.6,
      'a voice message with a location builds a payload');
check(L.buildChatPayload('hi', { location: { lat: 'x', lon: 1 } }).error !== null, 'a text latitude is refused on the page too');
check(L.buildChatPayload('hi', { location: { lat: 100, lon: 1 } }).error !== null, 'a latitude out of range is refused');
const plain = L.buildChatPayload('hi');
check(plain.payload.source === 'text' && !('location' in plain.payload), 'no location means no location key');
check(L.checkLocation({ lat: 1.23456789, lon: 2, accuracy: 200000 }).value.accuracy === null,
      'an absurd accuracy is dropped, not sent');

check(L.checkFile({ type: 'image/png', size: 10 }).error === null, 'a small PNG is accepted');
check(L.checkFile({ type: 'application/x-msdownload', size: 10 }).error !== null, 'an executable is refused');
check(L.checkFile({ type: 'image/jpeg', size: 0 }).error !== null, 'an empty file is refused');
check(L.checkFile({ type: 'image/jpeg', size: 6 * 1024 * 1024 }).error !== null, 'a 6 MB file is refused');
check(L.checkFile(null).error !== null, 'no file is refused');
check(L.safeName('../../evil name.png') === 'evil_name.png', 'the name keeps only safe characters');
check(L.safeName('') === 'file', 'an empty name becomes file');

check(L.parseJson('{"a":1}').ok === true && L.parseJson('<html>').ok === false, 'a non-JSON reply is detected without throwing');
check(L.describeFailure({ timeout: true }).indexOf('der') !== -1, 'a timeout gets a plain sentence');
check(L.describeFailure({ networkError: true }).indexOf('Server') !== -1, 'a network error gets a plain sentence');
check(L.describeFailure({ parseError: true, status: 200 }).indexOf('kuch aur') !== -1, 'a bad reply gets a plain sentence');
check(L.describeFailure({ ok: false, status: 403, body: { text: 'nahi' } }) === 'nahi', 'a refusal with text shows its text');
check(L.describeFailure(null).length > 0, 'nothing at all still gets a sentence');
check(!L.isReply({ body: { text: '   ' } }) && L.isReply({ body: { text: 'ok' } }), 'a blank reply is not a reply');

check(L.describeMediaError('NotAllowedError').state === 'denied', 'a blocked mic is reported as denied');
check(L.describeMediaError('NotFoundError').state === 'error', 'missing hardware is reported as an error');
check(L.describeMediaError('SomethingNew').message.length > 0, 'an unknown error still has a message');
check(L.describeGeoError(1).state === 'denied' && L.describeGeoError(2).state === 'error' && L.describeGeoError(3).state === 'error',
      'geolocation codes map to states');
check(L.formatLocation({ lat: 28.61234, lon: 77.2, accuracy: 12.4 }) === '28.6123, 77.2000 ±12 m', 'the location is shown rounded');

L.withTimeout(new Promise(function () {}), 20).then(function () {
  failed++;
  console.error('FAIL withTimeout should reject a promise that never settles');
}, function () {
  passed++;
}).then(function () {
  console.log(passed + ' page logic checks passed, ' + failed + ' failed');
  process.exitCode = failed ? 1 : 0;
});
