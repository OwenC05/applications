import test from 'node:test';
import assert from 'node:assert/strict';
import { parseListings } from '../src/listings.mjs';
const item = { company: 'Example', role: 'Engineer', country: 'UK', sector: 'tech', url: 'https://careers.example.com/role' };

test('unverified upstream shapes are not guessed', () => {
  assert.deepEqual(parseListings([item]), []);
  assert.deepEqual(parseListings({ listings: [item] }), []);
  assert.deepEqual(parseListings(null), []);
});

test('normalized listings filter UK tech, validate HTTPS, deduplicate, bound and carry provenance', () => {
  const data = { schema: 'copilot-listings-v1', listings: [item, item, { ...item, sector: 'finance' }, { ...item, country: 'US' }, { ...item, url: 'javascript:alert(1)' }, { ...item, url: 'https://user:secret@example.com/' }, ...Array.from({ length: 150 }, (_, i) => ({ ...item, url: `https://careers.example.com/${i}` }))] };
  const parsed = parseListings(data);
  assert.equal(parsed.length, 100);
  assert.equal(parsed[0].sector, 'tech');
  assert.match(parsed[0].provenance.source, /not verified/);
});
