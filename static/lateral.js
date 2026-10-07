// Mini status da impressora no menu lateral (todas as páginas).
(function () {
  const caixa = document.getElementById("side-status");
  if (!caixa || getComputedStyle(caixa.parentElement).display === "none") return;
  const $ = id => document.getElementById(id);
  async function atualizar() {
    const d = await fetch(caixa.dataset.url, { cache: "no-store" }).then(r => r.ok ? r.json() : null).catch(() => null);
    if (!d) return;
    const i = d.impressora, a = d.atual;
    caixa.hidden = false;
    $("ss-dot").className = "dot " + ((i.configurada ? i.conectada : a) ? "on" : "off");
    $("ss-texto").textContent = i.configurada ? (i.online === false ? "Sem contato" : i.texto) : (a ? "Imprimindo" : "Impressora livre");
    $("ss-job").hidden = !a;
    if (a) {
      $("ss-nome").textContent = a.arquivo ? `${a.arquivo} · ${a.amigo}` : "";
      $("ss-barra").style.width = a.progresso + "%";
      $("ss-pct").textContent = Math.floor(a.progresso) + "%";
      $("ss-falta").textContent = "faltam " + a.restante;
    }
    const espera = d.fila.length - (a ? 1 : 0);
    $("ss-fila").textContent = espera ? `${espera} na fila` : "fila vazia";
  }
  atualizar();
  setInterval(() => { if (!document.hidden) atualizar(); }, 10000);
})();
