import { expect, test } from 'vitest';
import { parseExactJson, stringifyExactJson } from './exact-integer';

test('reads unsafe JSON integers exactly without rewriting quoted text or safe numbers', () => {
  const payload = parseExactJson('{"cap":9223372036854775807,"safe":9007199254740991,"negative":-9007199254740992,"text":"\\\"cap\\\":9223372036854775807","fraction":1.25}');
  expect(payload).toEqual({ cap: '9223372036854775807', safe: Number.MAX_SAFE_INTEGER,
    negative: '-9007199254740992', text: '"cap":9223372036854775807', fraction: 1.25 });
});

test('writes supported signed64 values as numeric JSON and rejects unsafe JS numbers', () => {
  expect(stringifyExactJson({ hard_token_cap: '9223372036854775807', manual_estimate_tokens: '9007199254740992', unrelated: '9223372036854775807' }))
    .toBe('{"hard_token_cap":9223372036854775807,"manual_estimate_tokens":9007199254740992,"unrelated":"9223372036854775807"}');
  expect(() => stringifyExactJson({ hard_token_cap: 9007199254740992 })).toThrow('unsafe');
  expect(() => stringifyExactJson({ hard_token_cap: '9223372036854775808' })).toThrow('signed 64');
  expect(() => stringifyExactJson({ hard_token_cap: '1.5' })).toThrow('integer');
});

test('never rounds an unsafe integral decimal or exponent token', () => {
  expect(parseExactJson('{"cap":9.007199254740993e15}'))
    .toEqual({ cap: '9007199254740993' });
  expect(parseExactJson('{"cap":9007199254740993.0}'))
    .toEqual({ cap: '9007199254740993' });
  expect(parseExactJson('{"fraction":1.25e2,"nested":{"text":"\\\"v\\\":9.007199254740993e15"}}'))
    .toEqual({ fraction: 125, nested: { text: '"v":9.007199254740993e15' } });
  expect(() => parseExactJson('{"cap":9007199254740992.1}')).toThrow('unsafe');
});
