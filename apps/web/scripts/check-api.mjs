import { readFile, writeFile, unlink } from 'node:fs/promises';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { randomUUID } from 'node:crypto';
import { renderApiTypes } from './generate-api.mjs';

const temporary = join(tmpdir(), `forge-api-${randomUUID()}.d.ts`);
try {
  await writeFile(temporary, await renderApiTypes());
  const generated = await readFile(temporary);
  const checkedIn = await readFile(new URL('../src/lib/api/schema.d.ts', import.meta.url));
  if (!generated.equals(checkedIn)) {
    throw new Error('API types are stale. Run npm run api:generate.');
  }
  console.log('API types match the frozen OpenAPI schema.');
} finally {
  await unlink(temporary).catch(error => { if (error.code !== 'ENOENT') throw error; });
}
