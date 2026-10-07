"""Leitura dos metadados de um arquivo G-code (Cura e, como bônus, PrusaSlicer/OrcaSlicer).

O Cura grava um cabeçalho como:
    ;FLAVOR:Marlin
    ;TIME:6397
    ;Filament used: 2.34567m
    ;Layer height: 0.2
    ;TARGET_MACHINE.name:Creality Ender-3
e, se o plugin/script de miniatura estiver ativo:
    ; thumbnail begin 300x300 12345
    ; <base64>
    ; thumbnail end
"""
import math
import re

_DURATION_RE = re.compile(r"(?:(\d+)\s*d)?\s*(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?\s*(?:(\d+)\s*s)?")


def _parse_duration(text):
    """Converte '1d 2h 3m 4s' em segundos."""
    m = _DURATION_RE.search(text.strip())
    if not m or not any(m.groups()):
        return None
    d, h, mi, s = (int(g) if g else 0 for g in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def _first_float(text):
    m = re.search(r"[-+]?\d*\.?\d+", text)
    return float(m.group()) if m else None


def _sum_floats(text):
    """'1.23m, 0.5m' -> 1.73 (impressoras com mais de um extrusor)."""
    return sum(float(x) for x in re.findall(r"\d*\.?\d+", text))


_MOV_E = re.compile(rb"^[ \t]*(G[01]|G92|M82|M83)\b([^;\n]*)", re.M)
_VALOR_E = re.compile(rb"E(-?[\d.]+)")


def filamento_extrudado(path, ate_byte=None, diametro_mm=1.75, densidade=1.24):
    """Gramas de filamento que a impressora empurrou do começo do arquivo até o byte `ate_byte`.

    Conta só o avanço líquido: o E só vale quando passa do maior valor já atingido. Assim retrações
    e o "desretrair" não são contados em dobro, mesmo com um G92 no meio de uma retração."""
    with open(path, "rb") as f:
        dados = f.read() if ate_byte is None else f.read(max(0, int(ate_byte)))
    total = e = topo = 0.0
    absoluto = True
    for m in _MOV_E.finditer(dados):
        cmd = m.group(1)
        if cmd == b"M82":
            absoluto = True
            continue
        if cmd == b"M83":
            absoluto = False
            continue
        v = _VALOR_E.search(m.group(2))
        if not v:
            continue
        v = float(v.group(1))
        if cmd == b"G92":
            # zerar o E não muda o estado físico: se estava recolhido (retração), continua recolhido
            topo = v + (topo - e)
            e = v
            continue
        e = v if absoluto else e + v
        if e > topo:
            total += e - topo
            topo = e
    area_mm2 = math.pi * (diametro_mm / 2) ** 2
    return total * area_mm2 / 1000 * densidade


def indice_extrusao(path, passo=2048):
    """Índice para consultar rápido o filamento gasto até qualquer byte do arquivo.

    Retorna (bytes, mm): pontos a cada ~`passo` bytes com o filamento líquido acumulado (mm),
    usando a mesma regra de `filamento_extrudado`. Montado uma vez por arquivo."""
    with open(path, "rb") as f:
        dados = f.read()
    offs, acum = [0], [0.0]
    total = e = topo = 0.0
    absoluto = True
    for m in _MOV_E.finditer(dados):
        cmd = m.group(1)
        if cmd == b"M82":
            absoluto = True
            continue
        if cmd == b"M83":
            absoluto = False
            continue
        v = _VALOR_E.search(m.group(2))
        if not v:
            continue
        v = float(v.group(1))
        if cmd == b"G92":
            # zerar o E não muda o estado físico: se estava recolhido (retração), continua recolhido
            topo = v + (topo - e)
            e = v
            continue
        e = v if absoluto else e + v
        if e > topo:
            total += e - topo
            topo = e
        fim = m.end()
        if fim - offs[-1] >= passo:
            offs.append(fim)
            acum.append(total)
    offs.append(len(dados))
    acum.append(total)
    return offs, acum


def mm_ate(indice, byte):
    """Filamento (mm) gasto até `byte`, interpolando no índice de `indice_extrusao`."""
    import bisect
    offs, acum = indice
    i = bisect.bisect_right(offs, byte)
    if i <= 0:
        return 0.0
    if i >= len(offs):
        return acum[-1]
    o0, o1 = offs[i - 1], offs[i]
    return acum[i - 1] + (acum[i] - acum[i - 1]) * (byte - o0) / max(1, o1 - o0)


def parse_gcode(path):
    info = {
        "time_s": None,       # tempo estimado em segundos
        "filament_m": None,   # metros de filamento
        "filament_g": None,   # gramas (só quando o fatiador informa)
        "layer_height": None,
        "machine": None,
        "slicer": None,
        "thumbnail": None,    # PNG em base64
    }
    thumb_size = 0
    thumb_buf = None
    thumb_px = 0

    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            if not line.startswith(";"):
                continue
            body = line[1:].strip()

            # --- miniatura ---
            if thumb_buf is not None:
                if body.startswith("thumbnail end") or body.startswith("thumbnail_PNG end"):
                    if thumb_px >= thumb_size:
                        info["thumbnail"] = "".join(thumb_buf)
                        thumb_size = thumb_px
                    thumb_buf = None
                else:
                    thumb_buf.append(body)
                continue
            m = re.match(r"thumbnail(?:_PNG)? begin (\d+)x(\d+)", body)
            if m:
                thumb_buf = []
                thumb_px = int(m.group(1)) * int(m.group(2))
                continue

            # --- Cura ---
            if body.startswith("TIME:"):
                info["time_s"] = info["time_s"] or int(float(body[5:]))
            elif body.startswith("PRINT.TIME:"):
                info["time_s"] = info["time_s"] or int(float(body[11:]))
            elif body.startswith("Filament used:"):
                info["filament_m"] = _sum_floats(body[14:])
            elif body.startswith("Layer height:"):
                info["layer_height"] = _first_float(body[13:])
            elif body.upper().startswith("TARGET_MACHINE.NAME:"):
                info["machine"] = body[20:].strip()
            elif body.startswith("Generated with"):
                info["slicer"] = body[len("Generated with"):].strip()

            # --- PrusaSlicer / OrcaSlicer (bônus) ---
            elif body.startswith("estimated printing time (normal mode)"):
                info["time_s"] = info["time_s"] or _parse_duration(body.split("=", 1)[1])
            elif "total estimated time:" in body:
                info["time_s"] = info["time_s"] or _parse_duration(body.split("total estimated time:", 1)[1])
            elif body.startswith("filament used [mm]"):
                info["filament_m"] = info["filament_m"] or _sum_floats(body.split("=", 1)[1]) / 1000
            elif body.startswith("filament used [g]") or body.startswith("total filament used [g]"):
                info["filament_g"] = info["filament_g"] or _sum_floats(body.split("=", 1)[1])
            elif body.startswith("layer_height ="):
                info["layer_height"] = info["layer_height"] or _first_float(body.split("=", 1)[1])
            elif body.startswith("generated by"):
                info["slicer"] = info["slicer"] or body[len("generated by"):].strip()

    return info
