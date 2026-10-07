// Atualiza o quadro "Imprimindo agora" sem recarregar a página e controla o visualizador 3D.
(function () {
  const quadro = document.getElementById("impressora");
  if (!quadro) return;
  const cfg = window.AO_VIVO || {};
  const set = (nome, v) => quadro.querySelectorAll(`[data-live="${nome}"]`).forEach(el => { el.textContent = v; });
  let viz = null, vizJob = null, ultimo = null;

  // recarrega só se ninguém estiver no meio de preencher algo ou com um menu aberto
  let digitou = false;
  document.addEventListener("input", () => { digitou = true; });
  function podeRecarregar() {
    if (digitou || document.querySelector("details.mais[open]")) return false;
    const el = document.activeElement;
    return !(el && /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName));
  }

  async function atualizar() {
    const d = await fetch(cfg.status, { cache: "no-store" }).then(r => r.ok ? r.json() : null).catch(() => null);
    if (!d) return;
    ultimo = d;
    const i = d.impressora, a = d.atual;
    const trocouImpressao = String(a ? a.id : "") !== quadro.dataset.job
      || (i.configurada && String(!!i.ocupada) !== quadro.dataset.ocupada)
      || (i.configurada && String(!!i.conectada) !== quadro.dataset.conectada)
      || (i.configurada && String(!!i.pausada) !== quadro.dataset.pausada);
    const mudouFila = cfg.fila && JSON.stringify(d.fila) !== JSON.stringify(cfg.fila);
    const vendo3D = viz !== null && !trocouImpressao;  // não interrompe quem está olhando o 3D
    if ((trocouImpressao || (mudouFila && !vendo3D)) && podeRecarregar()) { location.reload(); return; }

    // totais do mês (contando a impressão em andamento) e horários da fila acompanham o relógio
    if (d.mes) for (const k in d.mes) if (d.mes[k] != null)
      document.querySelectorAll(`[data-mes="${k}"]`).forEach(el => { el.textContent = d.mes[k]; });
    for (const id in (d.horarios || {})) {
      document.querySelectorAll(`[data-hora-ini="${id}"]`).forEach(el => { el.textContent = d.horarios[id][0]; });
      document.querySelectorAll(`[data-hora-fim="${id}"]`).forEach(el => { el.textContent = d.horarios[id][1]; });
    }
    if (d.fila_fim) document.querySelectorAll("[data-fila-fim]").forEach(el => { el.textContent = d.fila_fim; });

    if (i.configurada) {
      set("texto", i.online === false ? "Sem contato com o OctoPrint" : i.texto);
      if (i.temp_bico != null) set("bico", Math.round(i.temp_bico));
      if (i.temp_mesa != null) set("mesa", Math.round(i.temp_mesa));
    }
    if (a) {
      set("pct", Math.floor(a.progresso));
      set("restante", a.restante);
      set("fim", a.fim);
      if (a.gramas_agora_txt) set("gagora", a.gramas_agora_txt);
      quadro.querySelectorAll('[data-live="barra"]').forEach(el => { el.style.width = a.progresso + "%"; });
      if (viz && vizJob === a.id) viz.progresso(a.filepos);
    }
  }
  // com a janela escondida (bandeja, aba em segundo plano) a página não fica consultando nem redesenhando nada
  setInterval(() => { if (!document.hidden) atualizar(); }, cfg.intervalo || 5000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) atualizar(); });

  // visualizador 3D: só carrega quando alguém abre (o G-code pode ser grande)
  const caixa = document.getElementById("viz");
  const botao = document.getElementById("viz-abrir");
  if (caixa && botao) {
    botao.addEventListener("click", async () => {
      botao.hidden = true;
      caixa.hidden = false;
      caixa.classList.add("aberto");
      const job = Number(quadro.dataset.job);
      const css = getComputedStyle(document.documentElement);
      const cor = n => css.getPropertyValue(n).trim();
      try {
        const { criarVisualizador } = await import(cfg.visualizador);
        viz = await criarVisualizador(caixa, `${cfg.gcode}${job}.gcode`, {
          impresso: cor("--filamento"), falta: cor("--muted"), bico: cor("--err"), grade: cor("--border"),
        });
        vizJob = job;
        if (viz) { if (!ultimo) await atualizar(); if (ultimo?.atual) viz.progresso(ultimo.atual.filepos); }
      } catch (e) {
        caixa.innerHTML = `<div class="viz-msg">Não consegui mostrar o modelo 3D (${e.message}).</div>`;
      }
    });
    // em tela grande (computador) o 3D já abre sozinho; no celular só se tocar, para não gastar dados
    if (matchMedia("(min-width: 1100px)").matches) botao.click();
  }

  // foto da webcam (painel do dono)
  const cam = document.getElementById("cam"), img = document.getElementById("cam-img");
  if (cam && img && cfg.webcam) {
    const foto = () => { if (document.hidden) { setTimeout(foto, 5000); return; } img.src = cfg.webcam + "?t=" + Date.now(); };
    img.onload = () => { cam.hidden = false; setTimeout(foto, 5000); };
    img.onerror = () => { cam.hidden = true; setTimeout(foto, 30000); };
    foto();
  }
})();
