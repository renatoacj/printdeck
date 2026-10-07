// Serviço do aplicativo instalado (Android/iPhone/computador). Não guarda páginas: a fila precisa ser sempre
// a de agora. Só serve para o app ser instalável e para mostrar uma tela amigável quando o PrintDeck está
// desligado no computador do dono (em vez do erro do navegador).
const FORA_DO_AR = `<!doctype html><html lang="pt-BR"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>PrintDeck fora do ar</title>
<body style="margin:0;min-height:100vh;display:grid;place-items:center;background:#111110;color:#eceae4;font-family:system-ui,sans-serif;text-align:center">
<div style="padding:28px;max-width:340px"><div style="font-size:54px">🖨️</div>
<h1 style="font-size:20px;margin:14px 0 8px">O PrintDeck está desligado</h1>
<p style="color:#a8a59c;font-size:15px;line-height:1.5;margin:0 0 20px">O programa precisa estar aberto no computador do dono da impressora (ou você está sem internet).</p>
<button onclick="location.reload()" style="font:inherit;font-weight:600;padding:11px 22px;border:0;border-radius:9px;background:#3987e5;color:#fff">Tentar de novo</button></div></body></html>`;
const tela = () => new Response(FORA_DO_AR, { status: 503, headers: { "Content-Type": "text/html; charset=utf-8" } });

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", e => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", e => {
  if (e.request.mode !== "navigate") return;            // imagens, dados etc. seguem o caminho normal
  e.respondWith(fetch(e.request).then(r => (r.status === 502 || r.status === 504) ? tela() : r).catch(tela));
});
