import { describe, expect, it } from 'vitest';

import { zTakeEvent } from './generated/zod.gen.js';

describe('take review events', () => {
  it('accepts the durable ready snapshot carried over the event stream', () => {
    const event = zTakeEvent.parse({
      type: 'take:review_status',
      take_id: 't-review',
      piece_id: 'chopin_op11',
      movement: 2,
      state: 'ready',
      job_id: 'job-review',
      midi_url: '/api/takes/t-review/review/midi',
    });

    expect(event.type).toBe('take:review_status');
  });

  it('rejects a review event without its discriminator', () => {
    expect(() =>
      zTakeEvent.parse({
        take_id: 't-review',
        piece_id: 'chopin_op11',
        movement: 2,
        state: 'ready',
        job_id: 'job-review',
      }),
    ).toThrow();
  });
});
