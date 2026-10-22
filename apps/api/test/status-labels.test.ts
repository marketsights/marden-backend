import { describe, expect, it } from 'vitest';
import { chainStatusCell, linkStatusWord } from '../src/lib/status-labels.js';
import { cellValue } from '../src/lib/xlsx.js';

// Export cells show the display words (At risk / Watch / No flags / Not started; Red flag / Warning /
// No flags / No data); the served values stay the contract enums.
describe('status display words in exports', () => {
  it('relabels a chain status in a status column and leaves other columns and values alone', () => {
    expect(chainStatusCell('status', 'Broken')).toBe('At risk');
    expect(chainStatusCell('chainStatus', 'Strained')).toBe('Watch');
    expect(chainStatusCell('status', 'Holding')).toBe('No flags');
    expect(chainStatusCell('status', 'No build')).toBe('Not started');
    expect(chainStatusCell('status', 'Operating')).toBe('Operating');
    expect(chainStatusCell('name', 'Broken')).toBe('Broken');
    expect(chainStatusCell('status', null)).toBeNull();
  });

  it('writes each chain link with its party and its link word', () => {
    expect(linkStatusWord('red')).toBe('Red flag');
    expect(linkStatusWord('Signed')).toBe('Signed');
    const chain = [
      { role: 'end_user', party: 'OpenAI', status: 'amber' },
      { role: 'tenant', party: 'Oracle', status: 'red' },
      { role: 'developer', party: 'STACK', status: 'green' },
      { role: 'lenders', party: '—', status: 'none' },
    ];
    expect(cellValue(chain)).toBe('OpenAI (Warning) → Oracle (Red flag) → STACK (No flags) → — (No data)');
  });
});
