'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { MomentsService } = require('../src/services/moments');

test('unconfirmed automatic like stops the batch and stays an attempt', async (t) => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'moments-receipt-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const now = () => Date.UTC(2026, 10, 26, 4);
  const config = { moments: { autoLikeMaxPerDay: 3 } };
  const service = new MomentsService({ dir, now, config });
  const rows = ['first', 'next'].map(id => ({
    id, nickname: id, text: '这是一条合成的开心分享', likedByMe: false,
  }));
  const claims = [];
  service.judgeLikes = async () => rows;
  service.waitForJob = async (id) => {
    const job = service.claim('fixture-account');
    assert.equal(job.id, id);
    claims.push(job.query.publisher);
    const persisted = new MomentsService({ dir, now, config });
    assert.ok(Object.hasOwn(persisted.state.attempted, job.query.publisher));
    return service.complete({ id, account: 'fixture-account', performed: true });
  };

  await service.autoLike({ account: 'fixture-account', items: rows });
  assert.deepEqual(claims, ['first']);
  assert.equal(service.job.status, 'unconfirmed');
  const restored = new MomentsService({ dir, now, config });
  assert.equal(restored.state.count, 1);
  assert.equal(restored.state.liked.size, 0);
  assert.equal(restored.reserveAutoLikeAttempt('first'), false);
  assert.equal(restored.reserveAutoLikeAttempt('next'), true);
});

test('scheduled Moments operations cannot be claimed after Beijing midnight', () => {
  const account = 'fixture-account';
  let timestamp = Date.UTC(2026, 10, 26, 15, 59, 59);
  const svc = new MomentsService({
    now: () => timestamp, config: { moments: { autoReadEveryHours: 4, nightSilence: false } },
  });
  const scheduled = svc.request({ account }, { scheduled: true });
  timestamp += 1000;
  assert.equal(svc.claim(account), null);
  assert.equal(svc.result(scheduled.id).status, 'failed');
  assert.throws(() => svc.request({ account }, { scheduled: true }), { statusCode: 409 });
  const manual = svc.request({ account });
  assert.equal(svc.claim(account).id, manual.id);
  svc.complete({ id: manual.id, account, items: [] });

  timestamp = Date.UTC(2026, 10, 26, 15, 59, 59);
  const like = svc.action({ action: 'like', publisher: 'fixture', keyword: 'fixture', account },
                          { automatic: true });
  timestamp += 1000;
  assert.equal(svc.claim(account), null);
  assert.equal(svc.result(like.id).status, 'failed');
});
