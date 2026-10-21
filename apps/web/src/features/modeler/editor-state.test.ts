import { mintId } from './editor-state';

// Keeper's pattern for ids: lorebase_core.model.ids (prefix, then a ULID).
const ID = /^att_[0-7][0-9A-HJKMNP-TV-Z]{25}$/;

describe('mintId', () => {
  it('makes ids Keeper accepts', () => {
    for (let i = 0; i < 500; i += 1) expect(mintId('att')).toMatch(ID);
  });

  it('makes a different id every time', () => {
    expect(new Set(Array.from({ length: 100 }, () => mintId('knd'))).size).toBe(100);
  });
});
