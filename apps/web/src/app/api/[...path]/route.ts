import { proxyApi } from '@/server/api-proxy';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

async function handle(request: Request, context: { params: Promise<{ path: string[] }> }) {
  return proxyApi(request, (await context.params).path, {
    webOrigin: process.env.FORGE_WEB_ORIGIN ?? 'http://127.0.0.1:3000',
    internalOrigin: process.env.FORGE_API_INTERNAL_ORIGIN ?? 'http://127.0.0.1:8000',
  });
}
export { handle as GET, handle as POST, handle as PUT, handle as PATCH, handle as DELETE, handle as HEAD, handle as OPTIONS };
