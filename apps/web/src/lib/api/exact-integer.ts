/** Preserve integer tokens that JSON.parse would otherwise round in JavaScript. */
const MAX_SAFE = BigInt(Number.MAX_SAFE_INTEGER);
export const MAX_SIGNED_64 = 9223372036854775807n;
const exactFields = new Set([
  'hard_token_cap', 'hard_context_cap', 'manual_estimate_tokens',
  'history_limit', 'minimum_comparables', 'window_days',
  'max_input_tokens', 'max_output_tokens',
]);

function exactInteger(token: string): bigint | null {
  const match = /^(-?)(0|[1-9][0-9]*)(?:\.([0-9]+))?(?:[eE]([+-]?[0-9]+))?$/.exec(token);
  if (!match) return null;
  const fraction = match[3] ?? '';
  const exponent = Number(match[4] ?? 0);
  const shift = exponent - fraction.length;
  if (!Number.isSafeInteger(shift) || shift > 1000) throw new TypeError('unsafe JSON integer exponent');
  let digits = match[2] + fraction;
  if (shift < 0) {
    if (-shift > digits.length || !/^0*$/.test(digits.slice(shift))) return null;
    digits = digits.slice(0, shift);
  } else {
    digits += '0'.repeat(shift);
  }
  if (digits.length > 1000) throw new TypeError('unsafe JSON integer length');
  return BigInt((match[1] || '') + (digits || '0'));
}

export function parseExactJson(source: string): unknown {
  let output = '';
  for (let index = 0; index < source.length;) {
    const char = source[index];
    if (char === '"') {
      const start = index++;
      while (index < source.length) {
        if (source[index] === '\\') { index += 2; continue; }
        if (source[index++] === '"') break;
      }
      output += source.slice(start, index);
      continue;
    }
    if (char === '-' || (char >= '0' && char <= '9')) {
      const start = index;
      while (index < source.length && /[0-9eE+.-]/.test(source[index])) index++;
      const token = source.slice(start, index);
      const integer = exactInteger(token);
      if (integer !== null && (integer > MAX_SAFE || integer < -MAX_SAFE)) {
        output += JSON.stringify(integer.toString());
      } else {
        if (integer === null && Math.abs(Number(token)) >= Number.MAX_SAFE_INTEGER) {
          throw new TypeError('unsafe nonintegral JSON number');
        }
        output += token;
      }
      continue;
    }
    output += char;
    index++;
  }
  return JSON.parse(output);
}

/** Serialize supported exact decimal strings as JSON integers for the numeric API. */
export function stringifyExactJson(value: unknown): string {
  const replacements: string[] = [];
  const nonce = crypto.randomUUID();
  const encoded = JSON.stringify(value, (key, item: unknown) => {
    if (!exactFields.has(key) || item == null) return item;
    if (typeof item === 'number') {
      if (!Number.isSafeInteger(item)) throw new TypeError(`${key} is an unsafe JavaScript integer`);
      return item;
    }
    if (typeof item !== 'string' || !/^(0|[1-9][0-9]*)$/.test(item)) {
      throw new TypeError(`${key} must be a canonical integer`);
    }
    if (BigInt(item) > MAX_SIGNED_64) throw new TypeError(`${key} exceeds signed 64-bit range`);
    replacements.push(item);
    return `__forge_exact_integer_${nonce}_${replacements.length - 1}__`;
  });
  if (encoded === undefined) throw new TypeError('JSON body is unavailable');
  return encoded.replace(new RegExp(`"__forge_exact_integer_${nonce}_([0-9]+)__"`, 'g'),
    (_match, index: string) => replacements[Number(index)]);
}
