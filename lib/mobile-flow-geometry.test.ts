import { describe, expect, it } from 'vitest';
import { mobileFlowSegments, MOBILE_FLOW_HEIGHT } from './mobile-flow-geometry';

describe('mobileFlowSegments', () => {
  it.each([258, 298, 328, 368])('connects four card edges with long curved paths at %i px', (width) => {
    const branches = mobileFlowSegments(width);
    expect(branches).toHaveLength(4);
    for (const branch of branches) {
      const length = Math.hypot(branch.card[0] - branch.hub[0], branch.card[1] - branch.hub[1]);
      expect(length).toBeGreaterThan(80);
      expect(branch.card[0]).toBeGreaterThan(0);
      expect(branch.card[0]).toBeLessThan(width);
      expect(branch.hub).toEqual([width / 2, MOBILE_FLOW_HEIGHT / 2]);
      expect(branch.controls[0]).not.toEqual(branch.card);
      expect(branch.controls[1]).not.toEqual(branch.hub);
    }
    expect(branches[0].card[1]).toBe(110);
    expect(branches[1].card[1]).toBe(110);
    expect(branches[2].card[1]).toBe(222);
    expect(branches[3].card[1]).toBe(222);
  });

  it('keeps upper and lower curved branches separate until the invisible junction', () => {
    const s = mobileFlowSegments(328);
    expect(s[0].controls[1][1]).toBeLessThan(MOBILE_FLOW_HEIGHT / 2);
    expect(s[1].controls[1][1]).toBeLessThan(MOBILE_FLOW_HEIGHT / 2);
    expect(s[2].controls[1][1]).toBeGreaterThan(MOBILE_FLOW_HEIGHT / 2);
    expect(s[3].controls[1][1]).toBeGreaterThan(MOBILE_FLOW_HEIGHT / 2);
    expect(s[0].controls[1][0]).toBeLessThan(328 / 2);
    expect(s[1].controls[1][0]).toBeGreaterThan(328 / 2);
  });
});
