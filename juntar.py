"""Junta vários G-codes do Cura em uma mesa só, intercalando camada por camada.

Cada arquivo foi fatiado com a peça no meio da mesa. Aqui cada peça é deslocada para um canto
livre e as camadas são costuradas: camada 0 de todas, camada 1 de todas... com uma só preparação
(aquecimento, linha de purga) e uma só finalização.

Só aceita arquivos no formato do Cura (marcas ;LAYER:n, extrusão absoluta) com a mesma altura
de camada e temperaturas compatíveis; fora disso levanta JuntarErro com o motivo."""
import itertools
import re

MESA = (220.0, 220.0)                 # Ender 3 / Ender 3 Pro
AREA = (12.0, 6.0, 214.0, 214.0)      # onde as peças podem ficar (a linha de purga fica em X < 1)
FOLGA = 6.0                           # espaço entre as peças (mm)
SALTO_Z = 0.8                         # o bico sobe isso para viajar de uma peça para a outra
MAX_PECAS = 8

_COORD = re.compile(r"([XYZEF])(-?\d*\.?\d+)")
_META = re.compile(r"^;(FLAVOR|TIME|Filament used|Layer height|MIN[XYZ]|MAX[XYZ]|TARGET_MACHINE|Generated with|LAYER_COUNT)", re.I)
_MODAIS = ("M204", "M205", "M106", "M107")


class JuntarErro(Exception):
    pass


def _params(linha):
    """{'X': 1.0, ...} dos parâmetros de um G0/G1 (sem o comentário)."""
    return {k: float(v) for k, v in _COORD.findall(linha.split(";", 1)[0])}


def _eh_movimento(linha):
    return linha.startswith(("G0 ", "G1 ", "G2 ", "G3 "))


class Peca:
    """Um G-code do Cura analisado: preparação, camadas (com corte na subida do bico) e finalização."""

    def __init__(self, caminho, nome):
        self.nome = nome
        with open(caminho, "r", encoding="utf-8", errors="ignore") as f:
            linhas = f.read().split("\n")
        marcas = [i for i, l in enumerate(linhas) if re.match(r"^;LAYER:\d+\s*$", l)]
        if not marcas or linhas[marcas[0]].strip() != ";LAYER:0":
            raise JuntarErro(f"“{nome}” não tem as marcas de camada do Cura (;LAYER:n)")
        cab = "\n".join(linhas[:60])
        m = re.search(r"^;Layer height:\s*([\d.]+)", cab, re.M)
        if not m:
            raise JuntarErro(f"“{nome}” não informa a altura de camada (foi fatiado no Cura?)")
        self.altura_camada = float(m.group(1))
        m = re.search(r"^;TARGET_MACHINE\.NAME:(.*)$", cab, re.M | re.I)
        self.maquina = m.group(1).strip() if m else ""
        self.prefixo = linhas[:marcas[0]]

        # camadas: do ;LAYER:n até o próximo; a última termina no seu ;TIME_ELAPSED
        fim_ultima = next((i for i in range(marcas[-1], len(linhas)) if linhas[i].startswith(";TIME_ELAPSED:")), None)
        if fim_ultima is None:
            raise JuntarErro(f"“{nome}” não tem as marcas de tempo do Cura (;TIME_ELAPSED)")
        limites = marcas[1:] + [fim_ultima + 1]
        blocos, self.tempos = [], []
        for ini, fim in zip(marcas, limites):
            bloco = linhas[ini + 1:fim]
            t = [float(l.split(":", 1)[1]) for l in bloco if l.startswith(";TIME_ELAPSED:")]
            self.tempos.append(t[-1] if t else (self.tempos[-1] if self.tempos else 0.0))
            blocos.append([l for l in bloco if not l.startswith(";TIME_ELAPSED:")])
        self.rodape = linhas[fim_ultima + 1:]
        self.tempo_total = self.tempos[-1]

        # corta cada camada na subida do bico: o fim do bloco n (subir Z + viajar) pertence à camada n+1
        self.unidades = []
        cauda_anterior = []
        for n, bloco in enumerate(blocos):
            if n == len(blocos) - 1:
                corpo, cauda = bloco, []
            else:
                ultimo_e = max((i for i, l in enumerate(bloco) if _eh_movimento(l) and "E" in _params(l)), default=-1)
                sobe = next((i for i in range(ultimo_e + 1, len(bloco))
                             if _eh_movimento(bloco[i]) and "Z" in _params(bloco[i])), None)
                if sobe is None:
                    raise JuntarErro(f"“{nome}”: não achei a subida do bico no fim da camada {n}")
                corpo, cauda = bloco[:sobe], bloco[sobe:]
            self.unidades.append(cauda_anterior + corpo)
            cauda_anterior = cauda

        self._simular()

    def _simular(self):
        """Percorre o arquivo para saber, no fim de cada camada: E, quanto está recolhido, Z, comandos
        modais; e a área ocupada (só onde sai filamento) e a maior retração usada."""
        x = y = z = 0.0
        e = topo = total = 0.0
        avanco = None
        self.retracao = 0.0
        modais = {}
        caixa = [float("inf"), float("inf"), float("-inf"), float("-inf")]

        def andar(linha, dentro):
            nonlocal x, y, z, e, topo, total, avanco
            cod = linha.split(";", 1)[0].strip()
            if not cod:
                return
            cmd = cod.split(" ", 1)[0]
            if cmd in ("M83", "G91") and dentro:
                raise JuntarErro(f"“{self.nome}” usa movimentos relativos no meio da impressão")
            if cmd == "G92":
                p = _params(cod)
                if "E" in p:
                    topo = p["E"] + (topo - e)
                    e = p["E"]
                return
            if cmd in _MODAIS:
                modais["M106" if cmd in ("M106", "M107") else cmd] = cod
                return
            if cmd in ("G0", "G1", "G2", "G3"):
                p = _params(cod)
                if "F" in p:
                    avanco = p["F"]
                nx, ny = p.get("X", x), p.get("Y", y)
                if "Z" in p:
                    z = p["Z"]
                if "E" in p:
                    if p["E"] > topo + 1e-9 and dentro and ("X" in p or "Y" in p):
                        for px, py in ((x, y), (nx, ny)):
                            caixa[0], caixa[1] = min(caixa[0], px), min(caixa[1], py)
                            caixa[2], caixa[3] = max(caixa[2], px), max(caixa[3], py)
                    e = p["E"]
                    if e > topo:
                        total += e - topo
                        topo = e
                    self.retracao = max(self.retracao, topo - e)
                x, y = nx, ny

        for l in self.prefixo:
            andar(l, False)
        self.estado_inicial = (e, topo - e)
        self.filamento_preparo_mm = total                    # linha de purga: só sai uma vez na mesa conjunta
        self.z_camadas, self.estados, self.primeiro_xy, self.modais, self.avancos = [], [], [], [], []
        for unidade in self.unidades:
            self.modais.append(dict(modais))                 # o que valia antes desta camada
            self.avancos.append(avanco)
            primeiro = None
            for l in unidade:
                if primeiro is None and _eh_movimento(l):
                    p = _params(l)
                    if "X" in p or "Y" in p:
                        primeiro = (p.get("X", x), p.get("Y", y))
                andar(l, True)
            self.primeiro_xy.append(primeiro or (x, y))
            self.estados.append((e, topo - e))
            self.z_camadas.append(z)
        if caixa[0] == float("inf"):
            raise JuntarErro(f"“{self.nome}” não tem movimentos de impressão")
        self.caixa = tuple(caixa)
        self.largura, self.profundidade = caixa[2] - caixa[0], caixa[3] - caixa[1]
        self.filamento_mm = total
        pre = "\n".join(self.prefixo)
        self.temp_bico = max([float(v) for v in re.findall(r"^M109 S([\d.]+)", pre, re.M)] or [0])
        self.temp_mesa = max([float(v) for v in re.findall(r"^M190 S([\d.]+)", pre, re.M)] or [0])


def _num(v):
    return f"{v:.3f}".rstrip("0").rstrip(".")


def _ponto(x, y, lugar):
    """Onde um ponto do arquivo original vai parar na mesa conjunta."""
    if lugar["giro"]:
        return -y + lugar["dx"], x + lugar["dy"]
    return x + lugar["dx"], y + lugar["dy"]


def _deslocar(linha, lugar):
    if not _eh_movimento(linha):
        return linha
    cod, sep, coment = linha.partition(";")
    dx, dy, giro = lugar["dx"], lugar["dy"], lugar["giro"]

    def troca(m):
        eixo, v = m.group(1), float(m.group(2))
        if eixo == "X":
            return ("Y" + _num(v + dy)) if giro else ("X" + _num(v + dx))
        if eixo == "Y":
            return ("X" + _num(-v + dx)) if giro else ("Y" + _num(v + dy))
        return m.group(0)
    return _COORD.sub(troca, cod) + sep + coment


def _fileiras(tamanhos, ordem):
    """Arruma retângulos (w, h) em fileiras dentro da área útil. Devolve ({i: (x, y)}, largura, altura) ou None."""
    x0, y0, x1, y1 = AREA
    pos, x, y, alt, usado_x = {}, x0, y0, 0.0, 0.0
    for i in ordem:
        w, h = tamanhos[i]
        if w > x1 - x0 + 1e-6 or h > y1 - y0 + 1e-6:
            return None
        if x + w > x1 + 1e-6:                              # próxima fileira
            x, y, alt = x0, y + alt + FOLGA, 0.0
        if y + h > y1 + 1e-6:
            return None
        pos[i] = (x, y)
        x += w + FOLGA
        usado_x = max(usado_x, x - FOLGA - x0)
        alt = max(alt, h)
    return pos, usado_x, y + alt - y0


def encaixar(pecas):
    """Acha um lugar para cada peça na mesa, tentando também girar 90°. Devolve uma lista de
    {dx, dy, giro, x, y, w, h} (na ordem das peças) ou None se não couberem."""
    n = len(pecas)
    x0, y0, x1, y1 = AREA
    melhor = None
    for giros in itertools.product((False, True), repeat=n):
        tam = [(p.profundidade, p.largura) if g else (p.largura, p.profundidade) for p, g in zip(pecas, giros)]
        ordens = {tuple(sorted(range(n), key=lambda i: -tam[i][1])),
                  tuple(sorted(range(n), key=lambda i: -tam[i][0])),
                  tuple(sorted(range(n), key=lambda i: -tam[i][0] * tam[i][1]))}
        for ordem in ordens:
            r = _fileiras(tam, ordem)
            if r is None:
                continue
            pos, w, h = r
            nota = (sum(giros), w * h)                      # prefere girar menos e ocupar menos mesa
            if melhor is None or nota < melhor[0]:
                melhor = (nota, giros, tam, pos, w, h)
    if melhor is None:
        return None
    _, giros, tam, pos, w, h = melhor
    cx, cy = (x1 - x0 - w) / 2, (y1 - y0 - h) / 2            # centraliza o conjunto na área
    lugares = []
    for i, p in enumerate(pecas):
        x, y = pos[i][0] + cx, pos[i][1] + cy
        if giros[i]:                                         # (x, y) -> (-y, x): o canto de baixo vira (-maxY, minX)
            dx, dy = x + p.caixa[3], y - p.caixa[0]
        else:
            dx, dy = x - p.caixa[0], y - p.caixa[1]
        lugares.append({"dx": dx, "dy": dy, "giro": giros[i], "x": x, "y": y, "w": tam[i][0], "h": tam[i][1]})
    return lugares


def conferir_compatibilidade(pecas):
    if len(pecas) < 2:
        raise JuntarErro("escolha pelo menos duas peças para juntar")
    if len(pecas) > MAX_PECAS:
        raise JuntarErro(f"dá para juntar no máximo {MAX_PECAS} peças de uma vez")
    a = pecas[0]
    for b in pecas[1:]:
        if abs(a.altura_camada - b.altura_camada) > 1e-6:
            raise JuntarErro(f"alturas de camada diferentes: “{a.nome}” com {a.altura_camada} mm e “{b.nome}” com {b.altura_camada} mm")
        for n in range(min(len(a.z_camadas), len(b.z_camadas))):
            if abs(a.z_camadas[n] - b.z_camadas[n]) > 1e-3:
                raise JuntarErro(f"“{a.nome}” e “{b.nome}” não têm as camadas na mesma altura (camada {n}: "
                                 f"{a.z_camadas[n]} mm e {b.z_camadas[n]} mm)")
        if abs(a.temp_bico - b.temp_bico) > 5 or abs(a.temp_mesa - b.temp_mesa) > 5:
            raise JuntarErro(f"temperaturas diferentes: “{a.nome}” ({a.temp_bico:.0f}/{a.temp_mesa:.0f} °C) e "
                             f"“{b.nome}” ({b.temp_bico:.0f}/{b.temp_mesa:.0f} °C)")


def juntar(arquivos, destino):
    """arquivos: [(caminho, nome)]. Grava o G-code conjunto em `destino` e devolve um resumo.
    Levanta JuntarErro quando as peças não podem ser impressas juntas."""
    pecas = [Peca(c, n) for c, n in arquivos]
    conferir_compatibilidade(pecas)
    lugares = encaixar(pecas)
    if lugares is None:
        raise JuntarErro(f"as peças não cabem juntas na mesa de {MESA[0]:.0f} × {MESA[1]:.0f} mm")

    R = max(p.retracao for p in pecas)
    n_camadas = max(len(p.unidades) for p in pecas)
    tempo_total = sum(p.tempo_total for p in pecas)
    filamento_mm = sum(p.filamento_mm for p in pecas) - sum(p.filamento_preparo_mm for p in pecas[1:])
    base = pecas[0]

    saida = [";FLAVOR:Marlin", f";TIME:{round(tempo_total)}", f";Filament used: {filamento_mm / 1000:.6f}m",
             f";Layer height: {base.altura_camada}", f";TARGET_MACHINE.NAME:{base.maquina}",
             f";Mesa conjunta montada pelo PrintDeck: {len(pecas)} pecas"]
    for i, p in enumerate(pecas):
        saida.append(f";PECA_INFO:{i + 1} {p.nome}")
    pulando = False
    for l in base.prefixo:                               # preparação da primeira peça, sem metadados/miniatura
        if l.startswith("; thumbnail") or l.startswith(";Thumbnail"):
            pulando = "begin" in l
            continue
        if pulando or _META.match(l):
            continue
        saida.append(l)
    saida.append(f";LAYER_COUNT:{n_camadas}")

    recolhido = base.estado_inicial[1]                    # estado físico do filamento
    z_atual = 0.3
    ultima = None
    for n in range(n_camadas):
        saida.append(f";LAYER:{n}")
        for i, p in enumerate(pecas):
            if n >= len(p.unidades):
                continue
            lugar = lugares[i]
            if ultima is not None and ultima != i:        # troca de peça
                e_esp, rec_esp = p.estado_inicial if n == 0 else p.estados[n - 1]
                fx, fy = _ponto(*p.primeiro_xy[n], lugar)
                saida.append(f";PECA:{i + 1} {p.nome}")
                saida.append("G92 E0")
                e_log = 0.0
                if recolhido < R - 1e-6:                  # recolhe antes de viajar, para não deixar fio
                    e_log -= R - recolhido
                    saida.append(f"G1 F2100 E{e_log:.5f}")
                    recolhido = R
                saida.append(f"G0 F600 Z{_num(z_atual + SALTO_Z)}")
                saida.append(f"G0 F9000 X{_num(fx)} Y{_num(fy)}")
                if abs(recolhido - rec_esp) > 1e-6:       # deixa como a peça espera encontrar
                    e_log += recolhido - rec_esp
                    saida.append(f"G1 F1500 E{e_log:.5f}")
                    recolhido = rec_esp
                saida.append(f"G92 E{e_esp:.5f}")
                saida.extend(p.modais[n].values())
                if p.avancos[n]:
                    saida.append(f"G1 F{_num(p.avancos[n])}")
            elif ultima is None:
                saida.append(f";PECA:{i + 1} {p.nome}")
            saida.extend(_deslocar(l, lugar) for l in p.unidades[n])
            recolhido = p.estados[n][1]
            z_atual = p.z_camadas[n]
            ultima = i
        feito = sum(p.tempos[min(n, len(p.tempos) - 1)] for p in pecas)
        saida.append(f";TIME_ELAPSED:{feito:.6f}")

    # finalização da primeira peça, partindo do estado em que o filamento realmente está
    e_fim, rec_fim = base.estados[-1]
    saida.append(f"G92 E{e_fim + rec_fim - recolhido:.5f}")
    saida.extend(base.rodape)

    with open(destino, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(saida))
        if not saida[-1].endswith("\n"):
            f.write("\n")

    return {
        "tempo_s": round(tempo_total), "filamento_m": filamento_mm / 1000, "camadas": n_camadas,
        "altura_camada": base.altura_camada, "maquina": base.maquina,
        "layout": [{"nome": p.nome, "x": round(lugares[i]["x"], 1), "y": round(lugares[i]["y"], 1),
                    "w": round(lugares[i]["w"], 1), "h": round(lugares[i]["h"], 1), "giro": lugares[i]["giro"]}
                   for i, p in enumerate(pecas)],
        "partes": [{"nome": p.nome, "tempo_s": p.tempo_total, "filamento_mm": p.filamento_mm,
                    "camadas": len(p.unidades)} for p in pecas],
    }


def filamento_por_peca(caminho, ate_byte, n_pecas):
    """Milímetros de filamento que saíram para cada peça de uma mesa conjunta até o byte `ate_byte`
    (a preparação/linha de purga entra na conta da primeira)."""
    with open(caminho, "rb") as f:
        dados = f.read() if ate_byte is None else f.read(max(0, int(ate_byte)))
    mm = [0.0] * n_pecas
    atual = 0
    e = topo = 0.0
    for l in dados.decode("utf-8", "ignore").split("\n"):
        if l.startswith(";PECA:"):
            try:
                atual = max(0, min(n_pecas - 1, int(l[6:].split(" ")[0]) - 1))
            except ValueError:
                pass
            continue
        cod = l.split(";", 1)[0]
        if cod.startswith("G92"):
            p = _params(cod)
            if "E" in p:
                topo = p["E"] + (topo - e)
                e = p["E"]
        elif _eh_movimento(cod):
            p = _params(cod)
            if "E" in p:
                e = p["E"]
                if e > topo:
                    mm[atual] += e - topo
                    topo = e
    return mm
