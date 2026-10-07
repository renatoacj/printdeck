// Visualizador 3D do G-code: desenha a peça e pinta a parte já impressa conforme a posição
// no arquivo informada pelo OctoPrint (filepos, em bytes).
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";

const MAX_SEGMENTOS = 3_000_000;

// Lê o G-code direto dos bytes para que cada segmento saiba em que byte do arquivo termina.
function lerGcode(bytes) {
  const pos = [];          // x1,y1,z1,x2,y2,z2 (já no sistema do three: y = altura)
  const fim = [];          // byte final da linha de cada segmento
  let x = 0, y = 0, z = 0, e = 0;
  let absXYZ = true, absE = true;
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity, maxZ = 0;
  let inicioPeca = -1;     // 1º segmento depois de ";LAYER:" (antes disso é a purga do início)
  const n = bytes.length;
  let i = 0;
  const num = (j) => {     // lê número a partir de j; devolve [valor, próximo índice]
    let k = j, s = "";
    while (k < n) {
      const c = bytes[k];
      if ((c >= 48 && c <= 57) || c === 46 || c === 45 || c === 43) { s += String.fromCharCode(c); k++; } else break;
    }
    return [s ? parseFloat(s) : NaN, k];
  };
  while (i < n) {
    let fimLinha = bytes.indexOf(10, i);
    if (fimLinha === -1) fimLinha = n;
    let j = i;
    while (j < fimLinha && (bytes[j] === 32 || bytes[j] === 9)) j++;
    if (inicioPeca < 0 && bytes[j] === 59 && bytes[j + 1] === 76 && bytes[j + 2] === 65 && bytes[j + 3] === 89
        && bytes[j + 4] === 69 && bytes[j + 5] === 82 && bytes[j + 6] === 58) inicioPeca = fim.length; // ";LAYER:"
    const c0 = bytes[j] & 0xDF; // maiúscula
    if (c0 === 71 || c0 === 77) { // G ou M
      const [cod, k] = num(j + 1);
      if (c0 === 71 && (cod === 0 || cod === 1 || cod === 2 || cod === 3 || cod === 92 || cod === 28)) {
        let nx = NaN, ny = NaN, nz = NaN, ne = NaN;
        let p = k;
        while (p < fimLinha && bytes[p] !== 59) { // até ';'
          const L = bytes[p] & 0xDF;
          if (L === 88 || L === 89 || L === 90 || L === 69) {
            const [v, q] = num(p + 1);
            if (L === 88) nx = v; else if (L === 89) ny = v; else if (L === 90) nz = v; else ne = v;
            p = q;
          } else p++;
        }
        if (cod === 92) { if (!isNaN(nx)) x = nx; if (!isNaN(ny)) y = ny; if (!isNaN(nz)) z = nz; if (!isNaN(ne)) e = ne; }
        else if (cod === 28) { const todos = isNaN(nx) && isNaN(ny) && isNaN(nz); if (todos || !isNaN(nx)) x = 0; if (todos || !isNaN(ny)) y = 0; if (todos || !isNaN(nz)) z = 0; }
        else {
          const tx = isNaN(nx) ? x : (absXYZ ? nx : x + nx);
          const ty = isNaN(ny) ? y : (absXYZ ? ny : y + ny);
          const tz = isNaN(nz) ? z : (absXYZ ? nz : z + nz);
          let extrudindo = false;
          if (!isNaN(ne)) {
            if (absE) { extrudindo = ne > e + 1e-5; e = ne; } else { extrudindo = ne > 1e-5; e += ne; }
          }
          if (extrudindo && (tx !== x || ty !== y) && fim.length < MAX_SEGMENTOS) {
            pos.push(x, z, -y, tx, tz, -ty);
            fim.push(fimLinha);
            if (tx < minX) minX = tx; if (tx > maxX) maxX = tx; if (x < minX) minX = x; if (x > maxX) maxX = x;
            if (ty < minY) minY = ty; if (ty > maxY) maxY = ty; if (y < minY) minY = y; if (y > maxY) maxY = y;
            if (tz > maxZ) maxZ = tz;
          }
          x = tx; y = ty; z = tz;
        }
      } else if (c0 === 71 && cod === 90) { absXYZ = true; absE = true; }
      else if (c0 === 71 && cod === 91) { absXYZ = false; absE = false; }
      else if (c0 === 77 && cod === 82) { absE = true; }
      else if (c0 === 77 && cod === 83) { absE = false; }
    }
    i = fimLinha + 1;
  }
  return { pos: new Float32Array(pos), fim: new Float64Array(fim), caixa: enquadrar(pos, Math.max(0, inicioPeca), { minX, minY, maxX, maxY, maxZ }) };
}

// Caixa só da peça: sem a purga do início e sem a 1ª camada, que ficam longe e estragam o zoom.
function enquadrar(pos, inicio, tudo) {
  let zMin = Infinity;
  for (let k = inicio * 6 + 1; k < pos.length; k += 3) if (pos[k] < zMin) zMin = pos[k];
  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity, achou = false;
  for (let k = inicio * 6; k < pos.length; k += 3) {
    if (pos[k + 1] <= zMin + 0.05) continue;  // sem a 1ª camada (a saia fica longe da peça)
    achou = true;
    const x = pos[k], y = -pos[k + 2];
    if (x < minX) minX = x; if (x > maxX) maxX = x; if (y < minY) minY = y; if (y > maxY) maxY = y;
  }
  return achou ? { minX, maxX, minY, maxY, maxZ: tudo.maxZ } : tudo;
}

// primeiro segmento cujo byte final passa de 'byte' (busca binária)
function segmentosAte(fim, byte) {
  let lo = 0, hi = fim.length;
  while (lo < hi) { const m = (lo + hi) >> 1; if (fim[m] <= byte) lo = m + 1; else hi = m; }
  return lo;
}

export async function criarVisualizador(el, urlGcode, cores) {
  el.innerHTML = '<div class="viz-msg">Carregando o modelo…</div>';
  const resp = await fetch(urlGcode);
  if (!resp.ok) throw new Error("não consegui baixar o G-code");
  const bytes = new Uint8Array(await resp.arrayBuffer());
  el.querySelector(".viz-msg").textContent = "Montando o modelo 3D…";
  await new Promise(r => setTimeout(r, 30));
  const g = lerGcode(bytes);
  if (!g.fim.length) { el.innerHTML = '<div class="viz-msg">Não encontrei movimentos de impressão nesse arquivo.</div>'; return null; }

  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
  el.innerHTML = "";
  el.appendChild(renderer.domElement);
  const cena = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(40, 1, 1, 5000);

  // mesa da Ender 3 (220 × 220) como referência
  const mesa = new THREE.GridHelper(220, 11, cores.grade, cores.grade);
  mesa.position.set(110, 0, -110);
  mesa.material.transparent = true; mesa.material.opacity = 0.5;
  cena.add(mesa);

  const geo = new THREE.BufferGeometry();
  geo.setAttribute("position", new THREE.BufferAttribute(g.pos, 3));
  const impresso = new THREE.LineSegments(geo, new THREE.LineBasicMaterial({ color: cores.impresso }));
  const falta = new THREE.LineSegments(geo, new THREE.LineBasicMaterial({ color: cores.falta, transparent: true, opacity: 0.18 }));
  cena.add(falta, impresso);
  const bico = new THREE.Mesh(new THREE.SphereGeometry(1.6, 16, 12), new THREE.MeshBasicMaterial({ color: cores.bico }));
  cena.add(bico);

  const c = g.caixa;
  const centro = new THREE.Vector3((c.minX + c.maxX) / 2, c.maxZ / 2, -(c.minY + c.maxY) / 2);
  const raio = Math.max(c.maxX - c.minX, c.maxY - c.minY, c.maxZ, 20);
  camera.position.set(centro.x + raio * 1.1, centro.y + raio * 0.9, centro.z + raio * 1.3);
  const controles = new OrbitControls(camera, renderer.domElement);
  controles.target.copy(centro);
  controles.enableDamping = true;
  // gira sozinho só por alguns segundos ao abrir; depois fica parado para não gastar a placa de vídeo
  controles.autoRotate = true;
  controles.autoRotateSpeed = 0.6;
  renderer.domElement.addEventListener("pointerdown", () => { controles.autoRotate = false; });
  setTimeout(() => { controles.autoRotate = false; }, 8000);

  // desenha só quando algo muda (câmera, progresso, tamanho), em vez de 60 vezes por segundo o tempo todo
  let pedido = false;
  function desenhar() {
    if (pedido) return;
    pedido = true;
    requestAnimationFrame(() => { pedido = false; controles.update(); renderer.render(cena, camera); });
  }
  controles.addEventListener("change", desenhar);

  const total = g.fim.length;
  function progresso(byte) {
    const k = byte == null ? 0 : segmentosAte(g.fim, byte);
    impresso.geometry.setDrawRange(0, k * 2);   // mesma geometria, dois recortes
    falta.visible = k < total;
    falta.onBeforeRender = () => geo.setDrawRange(k * 2, (total - k) * 2);
    impresso.onBeforeRender = () => geo.setDrawRange(0, k * 2);
    const v = Math.max(0, Math.min(total - 1, k - 1)) * 6 + 3;
    bico.position.set(g.pos[v], g.pos[v + 1], g.pos[v + 2]);
    bico.visible = k > 0 && k < total;
    desenhar();
  }

  function tamanho() {
    const w = el.clientWidth, h = el.clientHeight;
    renderer.setSize(w, h, false);
    camera.aspect = w / h; camera.updateProjectionMatrix();
    desenhar();
  }
  new ResizeObserver(tamanho).observe(el);
  tamanho();
  desenhar();

  return { progresso, camadas: Math.round(c.maxZ * 100) / 100 };
}
