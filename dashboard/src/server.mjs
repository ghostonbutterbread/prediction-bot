import { createServer } from 'node:http';
import { createReadStream, existsSync, statSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { buildDashboardData, buildTradeRows, runMonitorSnapshot } from './data.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PUBLIC_DIR = path.resolve(__dirname, '..', 'public');
const PORT = Number(process.env.PORT || 4173);

const contentTypes = {
  '.html': 'text/html; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.js': 'application/javascript; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml',
};

const server = createServer(async (request, response) => {
  const url = new URL(request.url ?? '/', `http://${request.headers.host ?? 'localhost'}`);

  try {
    if (url.pathname === '/api/dashboard') {
      return sendJson(response, buildDashboardData());
    }
    if (url.pathname === '/api/monitor') {
      return sendJson(response, runMonitorSnapshot());
    }
    if (url.pathname === '/api/rows') {
      return sendJson(response, await buildTradeRows({
        lanes: url.searchParams.getAll('lane'),
        preset: url.searchParams.get('preset') ?? 'latest',
        search: url.searchParams.get('search') ?? '',
        limit: Number(url.searchParams.get('limit') ?? 100),
      }));
    }
  } catch (error) {
    response.writeHead(500, { 'content-type': 'application/json; charset=utf-8' });
    response.end(JSON.stringify({ error: error.message }));
    return;
  }

  const filePath = resolvePublicFile(url.pathname);
  if (!filePath) {
    response.writeHead(404, { 'content-type': 'text/plain; charset=utf-8' });
    response.end('Not found');
    return;
  }

  streamStaticFile(response, filePath);
});

server.listen(PORT, '127.0.0.1', () => {
  console.log(`Prediction Bot dashboard: http://127.0.0.1:${PORT}`);
});

function resolvePublicFile(rawPath) {
  const pathname = rawPath === '/' ? '/index.html' : rawPath;
  const resolved = path.resolve(PUBLIC_DIR, `.${pathname}`);
  if (!resolved.startsWith(PUBLIC_DIR)) return null;
  if (!existsSync(resolved) || !statSync(resolved).isFile()) return null;
  return resolved;
}

function sendJson(response, payload) {
  response.writeHead(200, {
    'content-type': 'application/json; charset=utf-8',
    'cache-control': 'no-store',
  });
  response.end(JSON.stringify(payload));
}

function streamStaticFile(response, filePath) {
  const extension = path.extname(filePath);
  const stream = createReadStream(filePath);

  stream.once('open', () => {
    response.writeHead(200, { 'content-type': contentTypes[extension] ?? 'application/octet-stream' });
    stream.pipe(response);
  });

  stream.once('error', (error) => {
    if (response.headersSent) {
      response.destroy(error);
      return;
    }
    const status = error.code === 'ENOENT' ? 404 : 500;
    response.writeHead(status, { 'content-type': 'text/plain; charset=utf-8' });
    response.end(status === 404 ? 'Not found' : 'Unable to read static file');
  });
}
