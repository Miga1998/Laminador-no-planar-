# -*- coding: utf-8 -*-
"""LAMINADOR NO PLANAR MIGA (GhPython / IronPython 2.7): pared + 2 superficies -> G-code.
Incluye el nucleo del conversor G-code -> RAPID para calcular F con la misma formula.

Modos de velocidad:
  constante  : v en todos los MoveL (comportamiento original).
  feed       : G1 a F/60, G0 a v (comportamiento original con usar_F=True).
  geometria  : G1 de deposicion con v_cmd = clamp(v_min, v_max, v*h_nom/h_eff),
               h_eff medido contra la pasada anterior (estaciones locales).
El mismo archivo se pega en el componente GhPython y se importa para pruebas.
BORRADOR DE SIMULACION: no validado en controlador ni en celda.
"""
from __future__ import division, print_function
import io
import math
import os
import re

# CONFIGURACION DE LA PLANTILLA. No son calibraciones calculadas por el script.
NOMBRE_MODULO = "Module1"
NOMBRE_HERRAMIENTA = "tool0"
WOBJ_MOVE = '[FALSE,TRUE,"",[[1000,0,15],[1,0,0,0]],[[0,0,0],[1,0,0,0]]]'
CUATERNION = (0.00281313, -0.711436, 0.702744, 0.00128046)
CONFIGURACION = (0, 0, -1, 0)
EJES_EXTERNOS = '[9E+09,9E+09,9E+09,9E+09,9E+09,9E+09]'

# Parametros fijos del modo geometria (documentados en el resumen).
H_EFF_MIN = 0.1        # mm; h_eff <= H_EFF_MIN bloquea
L_XY_MIN = 0.001       # mm; longitud XY inutil por debajo (segmento vertical)
VENTANA_TOL = 10.0     # semiancho de ventana de estacion = VENTANA_PASO*ds + VENTANA_TOL*tol_xy
VENTANA_PASO = 0.5
DEC_V = 3              # v_cmd se redondea a 0.001 mm/s
# tol_xy usado por el componente cuando la entrada tol_xy no existe o esta desconectada.
# Ajustado a 3 mm para non_planar_slicing_2: las capas se desplazan ~1 mm en XY entre si
# (cordon de 25 mm). Conecta una entrada tol_xy para cambiarlo sin editar el codigo.
TOL_XY_DEFECTO = 3.0
MODOS = ('geometria', 'feed', 'constante')

try:
    TEXT_TYPE = unicode
    STRING_TYPES = (basestring,)
except NameError:
    TEXT_TYPE = str
    STRING_TYPES = (str, bytes)

# E se interpreta como palabra de extrusion, no como exponente de otro numero.
WORD = re.compile(r'([A-Za-z])\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+))')
ALLOWED_WORDS = set('GMNXYZFE')
G_GROUPS = {0: 'movimiento', 1: 'movimiento', 20: 'unidades', 21: 'unidades',
            90: 'distancia', 91: 'distancia', 94: 'avance'}
ZONES = set(['fine'] + ['z' + str(n) for n in
            (0, 1, 5, 10, 15, 20, 30, 40, 50, 60, 80, 100, 150, 200)])
# Marcadores de pasada reconocidos (linea completa de comentario).
MARKER = re.compile(r'^\s*;\s*(PASS|LAYER)\s*:\s*([-+]?\d+)\s*$', re.I)
# Metadatos de deposicion para G1 (linea completa de comentario).
DEP_META = re.compile(r'^\s*;\s*(DEPOSITION|DEPOSICION)\s*:\s*(ON|OFF)\s*$', re.I)
# Comentarios de salida/purga final: lo que sigue es traslado sin deposicion (conserva su F/v).
SALIDA_META = re.compile(r'^\s*;\s*(?:SALIDA FINAL|PURGA FINAL|RETIRADA FINAL|MIGA_PURGA)\b', re.I)


def as_text(value):
    if isinstance(value, bytes) and not isinstance(value, TEXT_TYPE):
        return value.decode('utf-8-sig')
    return value if isinstance(value, TEXT_TYPE) else TEXT_TYPE(value)


def finite(value):
    return not (math.isnan(value) or math.isinf(value))


def positive(value, label):
    try:
        number = float(value)
    except (ValueError, TypeError):
        raise ValueError(label + ' debe ser un numero en mm/s.')
    if not finite(number) or number <= 0:
        raise ValueError(label + ' debe ser finito y mayor que cero.')
    return number


def positive_num(value, label, unit):
    try:
        number = float(value)
    except (ValueError, TypeError):
        raise ValueError(label + ' debe ser un numero en ' + unit + '.')
    if not finite(number) or number <= 0:
        raise ValueError(label + ' debe ser finito y mayor que cero.')
    return number


def fmt(value):
    value = float(value)
    if not finite(value):
        raise ValueError('Numero no finito en la exportacion.')
    return ('{:.9f}'.format(value)).rstrip('0').rstrip('.') if abs(value) >= 0.0000000005 else '0'


def fmt3(value):
    return '-' if value is None else '{:.3f}'.format(value)


def vector(values):
    return '[' + ','.join(fmt(value) for value in values) + ']'


def panel_text(value):
    """Acepta un texto multilinea o una lista ordenada de textos del Panel."""
    if value is None:
        raise ValueError('Conecta el Panel a gcode: List Access, str y Flatten.')
    if hasattr(value, 'Branches'):
        raise ValueError('gcode debe usar List Access y Flatten, no Tree Access.')
    if isinstance(value, STRING_TYPES):
        result = as_text(value)
    else:
        try:
            items = list(value)
        except TypeError:
            raise ValueError('gcode debe ser texto o una lista de textos.')
        converted = []
        for item in items:
            if hasattr(item, 'Value'):
                item = item.Value
            if not isinstance(item, STRING_TYPES):
                raise ValueError('Hay datos sin formato de texto en gcode. Usa Type hint str.')
            converted.append(as_text(item))
        result = u'\n'.join(converted)
    result = result.lstrip(u'\ufeff')
    if not result.strip():
        raise ValueError('El Panel de G-code esta vacio.')
    return result


def clean_line(source, line_number):
    result, depth = [], 0
    for char in source:
        if char == ';' and depth == 0:
            break
        if char == '(':
            depth += 1
        elif char == ')':
            depth -= 1
            if depth < 0:
                raise ValueError('Linea {}: comentario con parentesis incorrectos.'.format(line_number))
        elif depth == 0:
            result.append(char)
    if depth:
        raise ValueError('Linea {}: comentario sin cerrar.'.format(line_number))
    return ''.join(result).strip()


def parse_gcode(value, require_xyz=True):
    """Parser del script base. Anade, sin cambiar registros ni errores:
    marcadores ;PASS:n/;LAYER:n, metadatos ;DEPOSITION:ON/OFF e incremento de E."""
    source = panel_text(value)
    position, mode, scale, absolute, feed = None, None, 1.0, True, None
    records, notes = [], []
    explicit_units, explicit_distance = False, False
    saw_f, saw_e, g0_count, ended = False, False, 0, False
    markers, pending, dep_state = [], [], None
    e_absolute, e_pos = True, 0.0
    for line_number, raw in enumerate(source.splitlines(), 1):
        line = clean_line(raw, line_number)
        if not line or line == '%':
            if not line:
                found = MARKER.match(raw)
                if found:
                    marker = {'line': line_number, 'kind': found.group(1).upper(),
                              'n': int(found.group(2)), 'idx': None}
                    markers.append(marker)
                    pending.append(marker)
                found = DEP_META.match(raw)
                if found:
                    dep_state = (found.group(2).upper() == 'ON')
                if records and SALIDA_META.match(raw):
                    dep_state = False
            continue
        if ended:
            raise ValueError('Linea {}: hay instrucciones despues de M2/M30.'.format(line_number))
        matches = list(WORD.finditer(line))
        residue = WORD.sub('', line).strip()
        if residue or not matches:
            raise ValueError('Linea {}: texto no reconocido o valor incompleto: {}.'.format(
                line_number, raw.strip()[:100]))
        words, g_codes, m_codes = {}, [], []
        for match in matches:
            key, number = match.group(1).upper(), float(match.group(2))
            if key not in ALLOWED_WORDS or not finite(number):
                raise ValueError('Linea {}: palabra {} no admitida.'.format(line_number, key))
            if key in 'GMN' and number != int(number):
                raise ValueError('Linea {}: codigo {}{} no admitido.'.format(line_number, key, fmt(number)))
            if key == 'G':
                g_codes.append(int(number))
            elif key == 'M':
                m_codes.append(int(number))
            elif key in words:
                raise ValueError('Linea {}: palabra {} repetida.'.format(line_number, key))
            else:
                words[key] = number
        if any(code in (2, 3) for code in g_codes):
            raise ValueError('Linea {}: G2/G3 son arcos; conviertelos a G1 antes de usar este componente.'.format(line_number))
        if 92 in g_codes:
            if g_codes != [92] or set(words) - set('NE') or 'E' not in words or m_codes:
                raise ValueError('Linea {}: solo se permite G92 E; no se modifican origenes XYZ.'.format(line_number))
            saw_e = True
            e_pos = words['E']
            continue
        if m_codes:
            if len(m_codes) != 1 or g_codes or set(words) - set('N'):
                raise ValueError('Linea {}: usa M82/M83/M2/M30 en un bloque independiente.'.format(line_number))
            if m_codes[0] in (82, 83):
                saw_e = True
                e_absolute = (m_codes[0] == 82)
                continue
            if m_codes[0] in (2, 30):
                ended = True
                continue
            raise ValueError('Linea {}: M{} no se traduce automaticamente.'.format(line_number, m_codes[0]))
        groups = set()
        for code in g_codes:
            if code not in G_GROUPS:
                raise ValueError('Linea {}: G{} no admitido; no se omite ni aproxima.'.format(line_number, code))
            group = G_GROUPS[code]
            if group in groups:
                raise ValueError('Linea {}: hay dos codigos del grupo {}.'.format(line_number, group))
            groups.add(group)
        for code in g_codes:
            if code in (20, 21):
                scale, explicit_units = (25.4 if code == 20 else 1.0), True
            elif code in (90, 91):
                absolute, explicit_distance = (code == 90), True
            elif code in (0, 1):
                mode = code
        if 'F' in words:
            feed, saw_f = positive(words['F'] * scale, 'F en linea ' + str(line_number)), True
        saw_e = saw_e or ('E' in words)
        e_delta = None
        if 'E' in words:
            e_delta = (words['E'] - e_pos) if e_absolute else words['E']
            e_pos = words['E'] if e_absolute else e_pos + words['E']
        axes = [axis for axis in 'XYZ' if axis in words]
        if not axes:
            continue
        if mode is None:
            raise ValueError('Linea {}: indica G0 o G1 antes de las coordenadas.'.format(line_number))
        if require_xyz and len(axes) != 3:
            missing = ','.join(axis for axis in 'XYZ' if axis not in words)
            raise ValueError('Linea {}: falta {}. Con exigir_xyz=True cada movimiento necesita XYZ completos. '
                             'Si tu codigo omite ejes intencionalmente, conecta exigir_xyz=False.'.format(line_number, missing))
        if position is None and (not absolute or len(axes) != 3):
            raise ValueError('Linea {}: el primer movimiento necesita XYZ absolutos; no se conoce el inicio.'.format(line_number))
        current = list(position) if position is not None else [None, None, None]
        for i, axis in enumerate('XYZ'):
            if axis in words:
                value_mm = words[axis] * scale
                current[i] = value_mm if absolute else current[i] + value_mm
            if current[i] is None or not finite(current[i]):
                raise ValueError('Linea {}: coordenada {} indefinida o no finita.'.format(line_number, axis))
        position = tuple(current)
        g0_count += int(mode == 0)
        for marker in pending:
            marker['idx'] = len(records)
        pending = []
        records.append({'line': line_number, 'p': position, 'g': mode, 'f_mm_min': feed,
                        'dep_meta': dep_state, 'e_delta': e_delta})
    if not records:
        raise ValueError('No se encontraron movimientos XYZ validos.')
    if not explicit_units:
        notes.append('Sin G20/G21: se han supuesto milimetros.')
    if not explicit_distance:
        notes.append('Sin G90/G91: se han supuesto coordenadas absolutas.')
    if saw_e:
        notes.append('E, G92 E y M82/M83 no generan comandos para la bomba.')
    if g0_count:
        notes.append('{} movimientos G0 pasan a MoveL a la velocidad v; no a MoveJ.'.format(g0_count))
    return {'records': records, 'notes': notes, 'has_f': saw_f, 'source': source,
            'markers': markers}


# --------------------------------------------------------------------------
# Geometria: utilidades
# --------------------------------------------------------------------------
def dxy(p, q):
    return math.hypot(q[0] - p[0], q[1] - p[1])


def d3(p, q):
    return math.sqrt((q[0] - p[0]) ** 2 + (q[1] - p[1]) ** 2 + (q[2] - p[2]) ** 2)


def speed_from_height(h_eff, v, h_nom, v_min, v_max):
    """v_raw = v*h_nom/h_eff; v_cmd = clamp. h_eff ya incluye L_xy/L_3d (una vez)."""
    v_raw = v * h_nom / h_eff
    v_cmd = round(max(v_min, min(v_max, v_raw)), DEC_V)
    return v_raw, v_cmd


def segment_height(a, b, za_sup, zb_sup):
    """h_A, h_B, L_xy, L_3d, h_eff de un segmento A->B contra el soporte."""
    h_a, h_b = a[2] - za_sup, b[2] - zb_sup
    l_xy = dxy(a, b)
    l_3d = d3(a, b)
    h_eff = (h_a + h_b) / 2.0 * l_xy / l_3d if l_3d > 0 else None
    return h_a, h_b, l_xy, l_3d, h_eff


def classify(records):
    """travel (G0), nodep (G1 declarado sin deposicion), dep (G1), ambiguous."""
    uses_e = any(r.get('e_delta') is not None for r in records)
    kinds = []
    for r in records:
        if r['g'] == 0:
            kinds.append('travel')
        elif r.get('dep_meta') is False:
            kinds.append('nodep')
        elif r.get('dep_meta') is True or not uses_e:
            kinds.append('dep')
        elif r.get('e_delta') is not None and r['e_delta'] > 0:
            kinds.append('dep')
        else:
            kinds.append('ambiguous')
    return kinds


def signed_area(points):
    area = 0.0
    for i in range(len(points)):
        x0, y0 = points[i][0], points[i][1]
        x1, y1 = points[(i + 1) % len(points)][0], points[(i + 1) % len(points)][1]
        area += x0 * y1 - x1 * y0
    return area / 2.0


# --------------------------------------------------------------------------
# Identificacion de pasadas (solo analisis: no modifica movimientos)
# --------------------------------------------------------------------------
def passes_from_starts(lines_to_idx, manual, records):
    problems, starts = [], []
    try:
        values = [int(round(float(x))) for x in manual]
    except (TypeError, ValueError):
        return None, [(None, 'inicios_pasada debe contener numeros de linea enteros.')]
    for x in manual:
        if abs(float(x) - round(float(x))) > 1e-9:
            problems.append((None, 'inicios_pasada: {} no es entero.'.format(x)))
    for value in values:
        if value not in lines_to_idx:
            problems.append((value, 'inicios_pasada: la linea {} no es un movimiento XYZ.'.format(value)))
        else:
            starts.append(lines_to_idx[value])
    if starts != sorted(set(starts)) or len(starts) != len(values):
        problems.append((None, 'inicios_pasada debe ser una lista estrictamente creciente sin repetidos.'))
    return (starts if not problems else None), problems


def passes_from_markers(markers):
    problems = []
    kind = 'PASS' if any(m['kind'] == 'PASS' for m in markers) else 'LAYER'
    used = [m for m in markers if m['kind'] == kind]
    starts, ids, last_n = [], [], None
    for m in used:
        if m['idx'] is None:
            problems.append((m['line'], 'Marcador ;{}:{} sin movimiento posterior.'.format(m['kind'], m['n'])))
            continue
        if last_n is not None and m['n'] <= last_n:
            problems.append((m['line'], 'Marcador ;{}:{} no es creciente.'.format(m['kind'], m['n'])))
        if starts and m['idx'] == starts[-1]:
            problems.append((m['line'], 'Dos marcadores sin movimientos entre ellos.'))
        starts.append(m['idx'])
        ids.append(m['n'])
        last_n = m['n']
    return (starts if not problems else None), ids, problems, kind


def _runs(kinds):
    """Inicios de tramos delimitados por G0: el ultimo G0 de cada bloque de traslados."""
    n = len(kinds)
    nodep = ('travel', 'nodep')
    starts = []
    if kinds[0] not in nodep:
        starts.append(0)
    for i in range(n):
        if kinds[i] in nodep and i + 1 < n and kinds[i + 1] not in nodep:
            starts.append(i)
    return starts


def _continuous_laps(records, kinds, a, b, tol):
    """Vueltas de una trayectoria continua (sin G0 entre capas).
    Una vuelta termina cuando, tras alejarse de su costura, la trayectoria vuelve en XY
    a menos de tol_xy de ella. Se confirma con area no nula, mismo sentido y longitud
    de vuelta coherente. No usa un numero fijo de puntos ni cambios de Z."""
    starts = [a]
    seam = records[a]['p']
    lap_start, far, lengths, areas = a, 0.0, [], []
    run_len = 0.0
    j = a + 1
    while j <= b:
        p = records[j]['p']
        run_len += dxy(records[j - 1]['p'], p)
        far = max(far, dxy(p, seam))
        if far > 4 * tol and dxy(p, seam) <= tol:
            choice, dist, look = j, dxy(p, seam), j + 1
            while look <= b and dxy(records[look]['p'], seam) <= tol:
                if dxy(records[look]['p'], seam) < dist:
                    choice, dist = look, dxy(records[look]['p'], seam)
                look += 1
            poly = [records[q]['p'] for q in range(lap_start, choice + 1)]
            area = signed_area(poly)
            length = sum(dxy(poly[q - 1], poly[q]) for q in range(1, len(poly)))
            if abs(area) <= (4 * tol) ** 2:
                return None, (records[choice]['line'], 'la trayectoria vuelve a la costura sin encerrar '
                              'area (ida y vuelta o zigzag)')
            if areas and area * areas[0] < 0:
                return None, (records[choice]['line'], 'la vuelta cambia de sentido respecto a la primera')
            if lengths and not (0.67 * lengths[0] <= length <= 1.5 * lengths[0]):
                return None, (records[choice]['line'], 'la longitud de la vuelta no es coherente con la '
                              'primera; la costura puede repetirse a mitad de vuelta')
            areas.append(area)
            lengths.append(length)
            if choice + 1 > b:
                break
            starts.append(choice + 1)
            nxt = records[choice + 1]['p']
            seam = nxt if dxy(nxt, records[choice]['p']) <= tol else records[choice]['p']
            lap_start, far, j = choice, 0.0, choice + 1
            run_len = 0.0
        j += 1
    if not lengths:
        return None, (records[a]['line'], 'no se detecta ninguna vuelta cerrada')
    return starts, None


def mark_transitions(records, kinds, starts, tol):
    """G1 al inicio de una pasada que sube casi en vertical (desplazamiento XY <= tol_xy):
    cambio de capa. Se conserva tal cual a velocidad v y no se usa como soporte."""
    found = []
    for s in starts[1:]:
        if s > 0 and kinds[s] in ('dep', 'ambiguous'):
            prev, cur = records[s - 1]['p'], records[s]['p']
            if dxy(prev, cur) <= tol and cur[2] - prev[2] > 0:
                kinds[s] = 'transition'
                found.append(records[s]['line'])
    return found


def infer_passes(records, kinds, tol):
    """Sin marcadores: tramos G0 confirmados por contornos cerrados, o vueltas continuas."""
    runs = _runs(kinds)
    if not runs:
        return None, [(records[0]['line'], 'no hay movimientos de deposicion')], ''
    n = len(records)
    info = []
    for r, a in enumerate(runs):
        b = (runs[r + 1] - 1) if r + 1 < len(runs) else n - 1
        dep = [j for j in range(a + 1, b + 1) if kinds[j] != 'travel' and kinds[j] != 'nodep'
               and dxy(records[j - 1]['p'], records[j]['p']) > 1e-9]
        pts = [records[a]['p']] + [records[j]['p'] for j in dep]
        closed = len(dep) >= 3 and dxy(records[dep[-1]]['p'], records[a]['p']) <= tol
        info.append({'a': a, 'b': b, 'dep': dep, 'closed': closed,
                     'area': signed_area(pts) if len(pts) >= 3 else 0.0})
    if len(runs) >= 2:
        problems = []
        for r, it in enumerate(info):
            line = records[it['a']]['line']
            if not it['closed']:
                problems.append((line, 'el tramo que empieza aqui no es un contorno cerrado'))
            elif abs(it['area']) <= tol * tol:
                problems.append((line, 'contorno de area nula'))
            elif r and it['area'] * info[0]['area'] < 0:
                problems.append((line, 'el sentido del contorno no coincide con la primera pasada'))
            if r and dxy(records[it['a']]['p'], records[info[r - 1]['a']]['p']) > tol:
                problems.append((line, 'la costura (inicio XY) no coincide con la pasada anterior'))
        if problems:
            return None, problems, ''
        return runs, [], 'tramos G0 con contornos cerrados'
    it = info[0]
    starts, problem = _continuous_laps(records, kinds, it['a'], it['b'], tol)
    if problem:
        return None, [problem], ''
    return starts, [], 'vueltas continuas con costura repetida'


# --------------------------------------------------------------------------
# Soporte y emparejamiento por estaciones locales
# --------------------------------------------------------------------------
def pass_path(records, kinds, start, end):
    """Ancla + objetivos de la pasada, con estacion XY acumulada."""
    anchor = start if (start == 0 or kinds[start] in ('travel', 'nodep', 'transition')) else start - 1
    idxs = list(range(anchor, end + 1))
    s = [0.0]
    for k in range(1, len(idxs)):
        s.append(s[-1] + dxy(records[idxs[k - 1]]['p'], records[idxs[k]]['p']))
    return {'anchor': anchor, 'idxs': idxs, 's': s, 'start': start, 'end': end}


def build_support(records, kinds, path, tol):
    segs = []
    for k in range(1, len(path['idxs'])):
        j = path['idxs'][k]
        if kinds[j] != 'dep':
            continue
        a, b = records[path['idxs'][k - 1]]['p'], records[j]['p']
        if dxy(a, b) <= 1e-9:
            continue
        segs.append({'a': a, 'b': b, 's0': path['s'][k - 1], 's1': path['s'][k]})
    if not segs:
        return None
    base = segs[0]['s0']
    for sg in segs:
        sg['s0'] -= base
        sg['s1'] -= base
    closed = len(segs) >= 3 and dxy(segs[0]['a'], segs[-1]['b']) <= tol
    last = len(segs) - 1
    for i, sg in enumerate(segs):
        sg['open_start'] = (i == 0 and not closed) or (i > 0 and dxy(segs[i - 1]['b'], sg['a']) > 1e-6)
        sg['open_end'] = (i == last and not closed) or (i < last and dxy(segs[i + 1]['a'], sg['b']) > 1e-6)
    return {'segs': segs, 'closed': closed, 'L': segs[-1]['s1']}


def cdiff(sup, a, b):
    d = a - b
    if sup['closed'] and sup['L'] > 0:
        span = sup['L']
        d = (d + span / 2.0) % span - span / 2.0
    return d


def project(sup, p, tol, center=None, half=None):
    """Proyeccion XY sobre los segmentos depositados (opcionalmente dentro de una ventana)."""
    cands, extrap = [], False
    px, py = p[0], p[1]
    for sg in sup['segs']:
        if center is not None:
            mid = (sg['s0'] + sg['s1']) / 2.0
            if abs(cdiff(sup, mid, center)) > half + (sg['s1'] - sg['s0']) / 2.0 + tol:
                continue
        ax, ay, az = sg['a']
        bx, by, bz = sg['b']
        dx, dy = bx - ax, by - ay
        ll = dx * dx + dy * dy
        t = ((px - ax) * dx + (py - ay) * dy) / ll
        tc = min(1.0, max(0.0, t))
        dist = math.hypot(px - (ax + tc * dx), py - (ay + tc * dy))
        if dist > tol:
            continue
        seglen = math.sqrt(ll)
        if (t < 0 and sg['open_start'] and -t * seglen > 1e-6) or \
           (t > 1 and sg['open_end'] and (t - 1) * seglen > 1e-6):
            extrap = True
            continue
        st = sg['s0'] + tc * (sg['s1'] - sg['s0'])
        if center is not None and abs(cdiff(sup, st, center)) > half:
            continue
        cands.append((st, az + tc * (bz - az), dist, dx / seglen, dy / seglen))
    return cands, extrap


def clusters(sup, cands, tol):
    if not cands:
        return []
    cs = sorted(cands)
    groups = [[cs[0]]]
    for c in cs[1:]:
        if abs(cdiff(sup, c[0], groups[-1][-1][0])) <= 2 * tol:
            groups[-1].append(c)
        else:
            groups.append([c])
    if len(groups) > 1 and sup['closed'] and abs(cdiff(sup, groups[0][0][0], groups[-1][-1][0])) <= 2 * tol:
        groups[0] = groups[-1] + groups[0]
        groups.pop()
    return groups


def match_pass(records, path, sup, tol):
    """Seguimiento de estaciones: cada punto se busca en una ventana local alrededor de
    la estacion esperada; nunca se compara A con B ni se usa el punto XYZ mas cercano."""
    res, prev_k, prev_st, direction = {}, None, None, 0
    for k, j in enumerate(path['idxs']):
        p = records[j]['p']
        err, group = None, None
        center, half = None, None
        if prev_k is None:
            cands, ex = project(sup, p, tol)
            groups = clusters(sup, cands, tol)
            if len(groups) == 1:
                group = groups[0]
            elif not groups:
                err = 'EXTRAPOLACION' if ex else 'SIN_SOPORTE'
            else:
                err = 'AMBIGUO'
        else:
            ds = path['s'][k] - path['s'][prev_k]
            if direction == 0 and ds > 1e-9:
                q = records[path['idxs'][prev_k]]['p']
                ux, uy = (p[0] - q[0]) / ds, (p[1] - q[1]) / ds
                if dxy(q, p) > 0.5 * ds:
                    best = max(res[prev_k]['cands'], key=lambda c: abs(c[3] * ux + c[4] * uy))
                    dot = best[3] * ux + best[4] * uy
                    if abs(dot) >= 0.5:
                        direction = 1 if dot > 0 else -1
            if direction:
                center = prev_st + direction * ds
                half = VENTANA_PASO * ds + VENTANA_TOL * tol
            else:
                center = prev_st
                half = 1.5 * ds + VENTANA_TOL * tol
            if sup['closed']:
                center %= sup['L']
            cands, ex = project(sup, p, tol, center, half)
            groups = clusters(sup, cands, tol)
            if len(groups) > 1 and direction:
                # Varios tramos del soporte dentro de la ventana (p. ej. los dos brazos de un
                # giro cerrado): se acepta el de estacion esperada solo si es claramente el
                # mas cercano en estacion; si no, sigue siendo AMBIGUO.
                ranked = sorted(groups, key=lambda gr: abs(cdiff(sup, min(gr, key=lambda c: c[2])[0], center)))
                d0 = abs(cdiff(sup, min(ranked[0], key=lambda c: c[2])[0], center))
                d1 = abs(cdiff(sup, min(ranked[1], key=lambda c: c[2])[0], center))
                if d0 <= 0.5 * ds + 2 * tol and d1 >= 2.0 * d0 + 2 * tol:
                    groups = [ranked[0]]
            if len(groups) == 1:
                group = groups[0]
            elif not groups:
                allc, ex2 = project(sup, p, tol)
                err = 'FUERA_DE_VENTANA' if allc else ('EXTRAPOLACION' if (ex or ex2) else 'SIN_SOPORTE')
            else:
                err = 'AMBIGUO'
        if group is not None:
            rep = min(group, key=lambda c: c[2])[0]
            if prev_k is not None:
                diff = cdiff(sup, rep, prev_st)
                if direction == 0 and abs(diff) > 2 * tol:
                    direction = 1 if diff > 0 else -1
                elif direction and direction * diff < -2 * tol:
                    err, group = 'NO_MONOTONO', None
        if group is not None:
            res[k] = {'cands': group, 'st': rep}
            prev_k, prev_st = k, rep
        else:
            res[k] = {'err': err, 'expected': center, 'half': half,
                      'station_path': path['s'][k], 'point': p}
    return res, direction


def pick_pair(sup, ca, cb, expected, half):
    """Elige el par de candidatos de soporte (A,B) coherente con el sentido del segmento.
    Resuelve la costura de un soporte helicoidal (estacion 0 frente a estacion L)."""
    best = None
    for a in ca:
        for b in cb:
            score = abs((b[0] - a[0]) - expected)
            key = (round(score, 9), a[2] + b[2])
            if best is None or key < best[0]:
                best = (key, a, b)
    if best[0][0] > half:
        best = None
        for a in ca:
            for b in cb:
                score = abs(cdiff(sup, b[0], a[0]) - expected)
                key = (round(score, 9), a[2] + b[2])
                if best is None or key < best[0]:
                    best = (key, a, b)
    return best[1], best[2]


# --------------------------------------------------------------------------
# Modo geometria
# --------------------------------------------------------------------------
BLOQUEANTES = set(['SIN_PASADA', 'DEPOSICION_AMBIGUA', 'PASADAS_NO_RESUELTAS', 'SIN_SOPORTE_PASADA_PREVIA',
                   'GAP_NO_POSITIVO', 'LONGITUD_XY_INUTIL', 'H_EFF_BAJO'])


def geometry_analysis(parsed, v, h_nom, v_min, v_max, tol, manual_starts=None):
    records = parsed['records']
    n = len(records)
    kinds = classify(records)
    problems, source, ids = [], '', None
    lines_to_idx = dict((r['line'], i) for i, r in enumerate(records))
    starts = None
    if manual_starts:
        starts, problems = passes_from_starts(lines_to_idx, manual_starts, records)
        source = 'inicios_pasada (manual)'
        if parsed['markers']:
            problems = list(problems)
    elif parsed['markers']:
        starts, ids, problems, kind = passes_from_markers(parsed['markers'])
        source = 'marcadores ;' + kind + ':n'
    else:
        starts, problems, source = infer_passes(records, kinds, tol)
        source = 'inferencia: ' + source if source else 'inferencia'
    passes = []
    # G1 remains deposition unless explicitly declared otherwise.
    # tol_xy is a support tolerance, not a travel/deposition classifier.
    transitions = []
    if starts:
        for m, a in enumerate(starts):
            b = (starts[m + 1] - 1) if m + 1 < len(starts) else n - 1
            path = pass_path(records, kinds, a, b)
            passes.append({'id': ids[m] if ids else m + 1, 'start': a, 'end': b, 'path': path,
                           'line_start': records[a]['line'], 'line_end': records[b]['line'],
                           'line_anchor': records[path['anchor']]['line']})
    pass_of = [None] * n
    for m, ps in enumerate(passes):
        for i in range(ps['start'], ps['end'] + 1):
            pass_of[i] = m
    supports = [build_support(records, kinds, ps['path'], tol) for ps in passes]
    matches = {}
    for m in range(1, len(passes)):
        if supports[m - 1] is not None:
            matches[m] = match_pass(records, passes[m]['path'], supports[m - 1], tol)
    rows = []
    for i, r in enumerate(records):
        row = {'line': r['line'], 'target': 'p3dcp_{:04d}'.format(i + 1), 'g': r['g'],
               'pass': passes[pass_of[i]]['id'] if pass_of[i] is not None else None,
               'h_a': None, 'h_b': None, 'h_eff': None, 'f': r['f_mm_min'], 'v_raw': None,
               'v_cmd': None, 'status': '', 'calc': False, 'kind': kinds[i]}
        rows.append(row)
        prev = records[i - 1]['p'] if i else None
        if i == 0:
            row['v_cmd'] = v
            row['status'] = 'INICIAL_ORIGEN_DESCONOCIDO'
            continue
        if kinds[i] == 'travel':
            row['status'] = 'G0_TRASLADO'
            continue
        if kinds[i] == 'nodep':
            row['status'] = 'G1_SIN_DEPOSICION'
            continue
        if kinds[i] == 'transition':
            row['status'] = 'TRANSICION_ENTRE_PASADAS'
            continue
        if d3(prev, r['p']) <= 1e-9:
            row['status'] = 'LONGITUD_CERO'
            continue
        if kinds[i] == 'ambiguous':
            row['status'] = 'DEPOSICION_AMBIGUA'
            continue
        if not passes:
            row['status'] = 'PASADAS_NO_RESUELTAS'
            continue
        m = pass_of[i]
        if m is None:
            row['status'] = 'SIN_PASADA'
            continue
        if m == 0:
            row['v_cmd'] = v
            row['status'] = 'SUPUESTO_PRIMERA_PASADA'
            continue
        if supports[m - 1] is None:
            row['status'] = 'SIN_SOPORTE_PASADA_PREVIA'
            continue
        path = passes[m]['path']
        res, direction = matches[m]
        k = path['idxs'].index(i)
        ra, rb = res[k - 1], res[k]
        if 'err' in ra or 'err' in rb:
            parts = []
            if 'err' in ra:
                parts.append('A:' + ra['err'])
            if 'err' in rb:
                parts.append('B:' + rb['err'])
            row['diagnostico_A'], row['diagnostico_B'] = ra, rb
            row['status'] = 'SOPORTE_' + '/'.join(parts)
            row['block'] = True
            continue
        ds = path['s'][k] - path['s'][k - 1]
        sup = supports[m - 1]
        dirn = direction or (1 if cdiff(sup, rb['st'], ra['st']) >= 0 else -1)
        ca, cb = pick_pair(sup, ra['cands'], rb['cands'], dirn * ds, VENTANA_PASO * ds + VENTANA_TOL * tol)
        h_a, h_b, l_xy, l_3d, h_eff = segment_height(prev, r['p'], ca[1], cb[1])
        row['h_a'], row['h_b'] = h_a, h_b
        row['e_xy'] = max(ca[2], cb[2])
        if l_xy <= L_XY_MIN:
            row['status'] = 'LONGITUD_XY_INUTIL'
            continue
        row['h_eff'] = h_eff
        if h_a <= 0 or h_b <= 0:
            row['status'] = 'GAP_NO_POSITIVO'
            continue
        if h_eff <= H_EFF_MIN:
            row['status'] = 'H_EFF_BAJO'
            continue
        v_raw, v_cmd = speed_from_height(h_eff, v, h_nom, v_min, v_max)
        row['v_raw'], row['v_cmd'], row['calc'] = v_raw, v_cmd, True
        row['status'] = 'CLAMP_VMAX' if v_raw > v_max else ('CLAMP_VMIN' if v_raw < v_min else 'OK')
    for row in rows:
        if row['v_cmd'] is None and row['status'] in ('G0_TRASLADO', 'G1_SIN_DEPOSICION', 'TRANSICION_ENTRE_PASADAS', 'LONGITUD_CERO'):
            row['v_cmd'] = v
        row['block'] = row.get('block', False) or row['status'] in BLOQUEANTES
    return {'rows': rows, 'passes': passes, 'problems': problems, 'source': source, 'kinds': kinds,
            'transitions': transitions, 'tol': tol}


def audit_lines(rows):
    out = ['linea;objetivo;G;pasada;h_A;h_B;h_eff;F_mm_min;v_raw;v_cmd;estado']
    for r in rows:
        out.append(';'.join([str(r['line']), r['target'], 'G' + str(r['g']),
                             '-' if r['pass'] is None else str(r['pass']),
                             fmt3(r['h_a']), fmt3(r['h_b']), fmt3(r['h_eff']),
                             fmt3(r['f']), fmt3(r['v_raw']), fmt3(r['v_cmd']),
                             r['status'] + (' [BLOQUEA]' if r.get('block') else '')]))
    return out


def resolve_mode(mode, use_f):
    if mode is None or not as_text(mode).strip():
        return 'feed' if use_f else 'constante'
    text = as_text(mode).strip().lower().replace(u'\u00ed', u'i')
    if text not in MODOS:
        raise ValueError('modo_velocidad debe ser geometria, feed o constante.')
    return str(text)


# --------------------------------------------------------------------------
# Exportador RAPID (estilo del script base)
# --------------------------------------------------------------------------
def build_rapid(value, speed=200.0, use_f=False, require_xyz=True, zone='z20', mode=None,
                h_nom=30.0, v_min=50.0, v_max=350.0, tol_xy=1.0, pass_starts=None):
    speed = positive(speed, 'v')
    zone = as_text(zone).strip().lower()
    if zone not in ZONES:
        raise ValueError('zona debe ser una zona ABB predefinida, por ejemplo z20, z1 o fine.')
    for name in (NOMBRE_MODULO, NOMBRE_HERRAMIENTA):
        if not re.match(r'^[A-Za-z][A-Za-z0-9_]{0,31}$', name):
            raise ValueError('Nombre RAPID no valido: ' + name)
    if len(CUATERNION) != 4 or not all(finite(float(n)) for n in CUATERNION):
        raise ValueError('CUATERNION debe tener cuatro numeros finitos.')
    norm = math.sqrt(sum(float(n) ** 2 for n in CUATERNION))
    if abs(norm - 1.0) > 0.001:
        raise ValueError('CUATERNION debe ser unitario; no se normaliza sin revisar la configuracion.')
    if len(CONFIGURACION) != 4 or any(not finite(float(n)) or float(n) != int(n) for n in CONFIGURACION):
        raise ValueError('CONFIGURACION debe tener cuatro enteros.')
    explicit_mode = mode is not None and as_text(mode).strip() != ''
    mode = resolve_mode(mode, use_f)
    feed_mode = (mode == 'feed')
    geo = None
    if mode == 'geometria':
        h_nom = positive_num(h_nom, 'h_nom', 'mm')
        v_min = positive(v_min, 'v_min')
        v_max = positive(v_max, 'v_max')
        tol_xy = positive_num(tol_xy, 'tol_xy', 'mm')
        if not (v_min <= speed <= v_max):
            raise ValueError('Se requiere 0 < v_min <= v <= v_max (v_min={}, v={}, v_max={}).'.format(
                fmt(v_min), fmt(speed), fmt(v_max)))
    result = parse_gcode(value, require_xyz)
    records = result['records']
    if mode == 'geometria':
        geo = geometry_analysis(result, speed, h_nom, v_min, v_max, tol_xy, pass_starts)
    target_lines, move_lines, speeds = [], [], []
    for index, record in enumerate(records):
        name = 'p3dcp_{:04d}'.format(index + 1)
        target = '[' + vector(record['p']) + ',' + vector(CUATERNION) + ',' + vector(CONFIGURACION) + ',' + EJES_EXTERNOS + ']'
        target_lines.append('    CONST robtarget ' + name + ':=' + target + ';')
        current_speed = speed
        speed_argument = 'velocidad'
        if feed_mode and record['g'] == 1:
            if record['f_mm_min'] is None:
                raise ValueError('Linea {}: usar_F=True, pero aun no se ha definido F. '
                                 'Anade F o usa velocidad constante con usar_F=False.'.format(record['line']))
            current_speed = positive(record['f_mm_min'] / 60.0, 'F/60')
        if geo is not None and geo['rows'][index]['calc']:
            current_speed = geo['rows'][index]['v_cmd']
            speed_argument += '\\V:=' + fmt(current_speed)
        elif abs(current_speed - 200.0) > 0.0000000001:
            speed_argument += '\\V:=' + fmt(current_speed)
        current_zone = 'fine' if index in (0, len(records) - 1) else zone
        move_lines.append('        MoveL ' + name + ',' + speed_argument + ',' + current_zone + ',' +
                          NOMBRE_HERRAMIENTA + '\\WObj:=MOVE;')
        speeds.append(current_speed)
    header = ['MODULE ' + NOMBRE_MODULO,
              '    ! G-code del Panel. Orientacion fija tomada del RAPID de referencia.',
              '    ! G0/G1 -> MoveL. Sin comandos de bomba ni aproximacion inicial.']
    if geo is not None:
        header += ['    ! BORRADOR DE SIMULACION. Velocidad por geometria: \\V = clamp(v_min,v_max,v*h_nom/h_eff).',
                   '    ! v={} h_nom={} v_min={} v_max={} mm/s|mm; tol_xy={} mm; h_eff_min={} mm.'.format(
                       fmt(speed), fmt(h_nom), fmt(v_min), fmt(v_max), fmt(tol_xy), fmt(H_EFF_MIN))]
    lines = header + ['    ! Definicion del objeto de trabajo',
                      '    PERS wobjdata MOVE:=' + WOBJ_MOVE + ';', '',
                      '    ! Definicion de puntos'] + target_lines + [
        '', '    CONST speeddata velocidad:=v200;', '', '    PROC main()'] + move_lines + [
        '    ENDPROC', 'ENDMODULE', '']
    notes = list(result['notes'])
    if explicit_mode and use_f and mode != 'feed':
        notes.append('usar_F=True ignorado: modo_velocidad={} tiene prioridad.'.format(mode))
    if mode == 'geometria':
        if result['has_f']:
            notes.append('F del G-code: solo informativo (columna F_mm_min de la auditoria).')
    elif result['has_f'] and not use_f and mode == 'constante':
        notes.append('Hay F en el G-code; no se usa porque usar_F=False.')
    if mode == 'constante':
        notes.append('Velocidad constante: {} mm/s. No se calcula compensacion entre capas.'.format(fmt(speed)))
    elif mode == 'feed':
        notes.append('G1: velocidad F/60. G0: velocidad v.')
    notes.append('Primero y ultimo: fine. Zona intermedia: {}.'.format(zone))
    notes.append('Con z20 u otra zona de paso el robot puede redondear la trayectoria.')
    notes.append('Orientacion/confdata heredadas; TCP=tool0 y MOVE requieren revision en RobotStudio.')
    summary = '{} puntos y {} MoveL; una sola declaracion por punto.\n'.format(len(records), len(move_lines))
    blocked = False
    audit = None
    if geo is None:
        summary += 'Velocidad solicitada: {} a {} mm/s.\n'.format(fmt(min(speeds)), fmt(max(speeds)))
        audit = ['linea;objetivo;G;pasada;h_A;h_B;h_eff;F_mm_min;v_raw;v_cmd;estado']
        for index, record in enumerate(records):
            audit.append(';'.join([str(record['line']), 'p3dcp_{:04d}'.format(index + 1), 'G' + str(record['g']),
                                   '-', '-', '-', '-', fmt3(record['f_mm_min']), '-', fmt3(speeds[index]),
                                   mode.upper()]))
    else:
        summary += geometry_summary(geo, records, speed)
        audit = audit_lines(geo['rows'])
        blocked = bool(geo['problems']) or any(r['block'] for r in geo['rows'])
    summary += '\n'.join(notes)
    out = {'rapid': '\r\n'.join(lines), 'puntos': '\r\n'.join(target_lines),
           'movimientos': '\r\n'.join(move_lines), 'resumen': summary,
           'records': records, 'speeds': speeds, 'mode': mode, 'auditoria': audit,
           'bloqueado': blocked, 'geo': geo}
    if blocked:
        out['rapid'] = out['puntos'] = out['movimientos'] = None
        out['resumen'] = ('ERROR: modo geometria bloqueado; no se genera el modulo.\n' + summary +
                          '\nResuelve las lineas indicadas o elige explicitamente modo_velocidad=feed o constante.')
    return out


def geometry_summary(geo, records, v):
    rows = geo['rows']
    text = 'Modo geometria. Pasadas: {} ({}).\n'.format(len(geo['passes']), geo['source'])
    for ps in geo['passes']:
        text += '  Pasada {}: lineas {}..{} (ancla linea {}).\n'.format(
            ps['id'], ps['line_start'], ps['line_end'], ps['line_anchor'])
    for line, msg in geo['problems']:
        text += '  PASADAS: ' + ('linea {}: '.format(line) if line else '') + msg + '.\n'
    if geo['problems']:
        text += '  Anade ;PASS:n antes del primer movimiento de cada pasada o conecta inicios_pasada.\n'
    calc = [r for r in rows if r['calc']]
    first = [r for r in rows if r['status'] == 'SUPUESTO_PRIMERA_PASADA']
    travel = [r for r in rows if r['status'] in ('G0_TRASLADO', 'INICIAL_ORIGEN_DESCONOCIDO', 'G1_SIN_DEPOSICION',
                                                  'TRANSICION_ENTRE_PASADAS')]
    errs = [r['e_xy'] for r in rows if r.get('e_xy') is not None]
    if errs:
        text += 'Desvio XY maximo respecto al soporte: {:.3f} mm (tol_xy = {} mm).\n'.format(max(errs), fmt(geo['tol']))
    if geo['transitions']:
        text += 'Cambios de capa casi verticales (G1, a v, sin calculo): {} (lineas {}).\n'.format(
            len(geo['transitions']), ', '.join(str(x) for x in geo['transitions'][:20]) +
            (' ...' if len(geo['transitions']) > 20 else ''))
    if calc:
        text += 'Impresion calculada: {} MoveL, v_cmd {} a {} mm/s.\n'.format(
            len(calc), fmt(min(r['v_cmd'] for r in calc)), fmt(max(r['v_cmd'] for r in calc)))
    else:
        text += 'Impresion calculada: 0 MoveL.\n'
    text += 'Limitadas: {} a v_max, {} a v_min.\n'.format(
        sum(1 for r in calc if r['status'] == 'CLAMP_VMAX'), sum(1 for r in calc if r['status'] == 'CLAMP_VMIN'))
    text += 'Primera pasada a velocidad nominal v={} mm/s (SUPUESTO, sin geometria de soporte): {} ({} MoveL).\n'.format(
        fmt(v), 'si' if first else 'no', len(first))
    text += 'Traslados/no deposicion a v={} mm/s: {} MoveL (incluye el primer movimiento, origen desconocido).\n'.format(
        fmt(v), len(travel))
    zero = [r['line'] for r in rows if r['status'] == 'LONGITUD_CERO']
    if zero:
        text += 'Longitud cero (conservados a v, sin calculo): lineas {}.\n'.format(', '.join(str(x) for x in zero))
    bad = [r for r in rows if r['block']]
    sup_fail = [r for r in bad if r['status'].startswith('SOPORTE_')]
    if sup_fail:
        text += ('SOPORTE NO ENCONTRADO en {} tramos: el punto esta a mas de tol_xy={} mm en XY de la '
                 'pasada anterior, o hay dos tramos posibles. En esquinas cerradas los puntos de cada capa '
                 'cortan la esquina distinto: sube tol_xy (p. ej. 6 mm con cordon de 25 mm) o los puntos por '
                 'vuelta.\n').format(len(sup_fail), fmt(geo['tol']))
    below = [r for r in bad if r['status'] == 'GAP_NO_POSITIVO' and r['h_a'] is not None
             and r['h_a'] < 0 and r['h_b'] < 0]
    if bad and len(below) >= 0.5 * len(bad):
        text += ('ORDEN INVERTIDO: {} tramos quedan POR DEBAJO de la pasada anterior (h hasta {:.1f} mm). '
                 'El G-code baja en Z: la definicion genera las capas de arriba hacia abajo. '
                 'Revisa BSrf (debe ser la superficie inferior) y TSrf (la superior).\n').format(
                     len(below), min(min(r['h_a'], r['h_b']) for r in below))
    if bad:
        text += 'NO RESUELTOS: {} movimientos. Lineas: {}.\n'.format(
            len(bad), ', '.join('{}({})'.format(r['line'], r['status']) for r in bad[:40]) +
            (' ...' if len(bad) > 40 else ''))
    return text


def save_module(path, text):
    if path is None or not as_text(path).strip():
        raise ValueError('Para guardar conecta ruta, por ejemplo C:/3DCP/Prueba_01.mod.')
    path = os.path.abspath(os.path.expanduser(as_text(path).strip().strip('"')))
    if os.path.splitext(path)[1].lower() != '.mod':
        raise ValueError('ruta debe terminar en .mod.')
    if not os.path.isdir(os.path.dirname(path)):
        raise ValueError('La carpeta de ruta no existe. Creala antes de guardar.')
    payload = text.encode('ascii')
    if os.path.exists(path):
        with io.open(path, 'rb') as existing:
            if existing.read() == payload:
                return path
        raise ValueError('Ya existe un archivo diferente en ruta. Elige otro nombre para guardarlo.')
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, 'wb') as destination:
        destination.write(payload)
    return path


def _optional(scope, name, default):
    value = scope.get(name)
    return default if value is None else value


def run_component(scope):
    """Adaptador GhPython; el nucleo anterior tambien se puede comprobar fuera de Rhino."""
    scope.update({'rapid': None, 'a': None, 'puntos': None, 'movimientos': None,
                  'resumen': '', 'trayectoria': None, 'archivo_guardado': None, 'auditoria': None})
    value = scope.get('gcode', scope.get('x'))
    base = scope.get('v', scope.get('y'))
    base = 200.0 if base is None else base
    use_f = scope.get('usar_F')
    use_f = False if use_f is None else bool(use_f)
    require_xyz = scope.get('exigir_xyz')
    require_xyz = True if require_xyz is None else bool(require_xyz)
    zone = scope.get('zona') or 'z20'
    starts = scope.get('inicios_pasada')
    starts = [s for s in (list(starts) if starts is not None else []) if s is not None]
    try:
        environment = scope.get('ghenv')
        if environment is not None:
            for parameter in environment.Component.Params.Input:
                if parameter.NickName in ('gcode', 'x'):
                    if as_text(parameter.Access).lower() != 'list':
                        raise ValueError('Clic derecho en gcode: selecciona List Access para convertir todo el Panel una sola vez.')
                    if parameter.VolatileData.PathCount > 1:
                        raise ValueError('Activa Flatten en gcode: hay varias ramas y se necesita una sola secuencia ordenada.')
                if parameter.NickName == 'inicios_pasada' and parameter.SourceCount > 0:
                    if as_text(parameter.Access).lower() != 'list':
                        raise ValueError('Clic derecho en inicios_pasada: selecciona List Access.')
        result = build_rapid(value, base, use_f, require_xyz, zone, scope.get('modo_velocidad'),
                             _optional(scope, 'h_nom', 30.0), _optional(scope, 'v_min', 50.0),
                             _optional(scope, 'v_max', 350.0), _optional(scope, 'tol_xy', TOL_XY_DEFECTO), starts)
    except Exception as exc:
        scope['resumen'] = 'ERROR: ' + as_text(exc) + '\nNo se genera ni se guarda un modulo parcial.'
        _runtime_message(scope, 'Error', scope['resumen'])
        return
    scope['auditoria'] = result['auditoria']
    scope['resumen'] = result['resumen']
    if result['bloqueado']:
        detail = [l.strip() for l in result['resumen'].split('\n')
                  if l.strip().startswith(('ORDEN INVERTIDO', 'SOPORTE NO ENCONTRADO', 'NO RESUELTOS', 'PASADAS:'))]
        _runtime_message(scope, 'Error', 'Modo geometria bloqueado. ' +
                         (detail[0][:300] if detail else 'Revisa resumen y auditoria.'))
        return
    for key in ('rapid', 'puntos', 'movimientos'):
        scope[key] = result[key]
    scope['a'] = result['rapid']
    try:
        import Rhino.Geometry as rg
        vertices = [rg.Point3d(*record['p']) for record in result['records']]
        scope['trayectoria'] = rg.PolylineCurve(vertices) if len(vertices) > 1 else vertices[0]
    except ImportError:
        pass
    if scope.get('guardar'):
        try:
            scope['archivo_guardado'] = save_module(scope.get('ruta'), result['rapid'])
            scope['resumen'] += '\nGuardado: ' + scope['archivo_guardado']
        except Exception as exc:
            scope['resumen'] += '\nERROR AL GUARDAR: ' + as_text(exc)
            _runtime_message(scope, 'Warning', as_text(exc))


def _runtime_message(scope, level, text):
    environment = scope.get('ghenv')
    if environment is not None:
        try:
            from Grasshopper.Kernel import GH_RuntimeMessageLevel
            environment.Component.AddRuntimeMessage(getattr(GH_RuntimeMessageLevel, level), text)
        except (ImportError, AttributeError):
            pass




# ==========================================================================
# ESCRITOR DE G-CODE (componente GhPython independiente)
# Entrada: arbol de puntos, UNA RAMA POR VUELTA, en orden de impresion.
# Salida: G-code con F = 60*v por movimiento (misma formula que el conversor),
# marcadores ;PASS:n, sin movimientos verticales entre vueltas y salida final.
# ==========================================================================
def _xyz(p):
    if hasattr(p, 'Value') and not hasattr(p, 'X'):
        p = p.Value
    if hasattr(p, 'X'):
        return (float(p.X), float(p.Y), float(p.Z))
    q = tuple(float(c) for c in p)
    if len(q) != 3:
        raise ValueError('Cada punto necesita X, Y y Z.')
    return q


def laps_from(value):
    """DataTree de Grasshopper (Tree Access) o lista de listas (pruebas)."""
    if value is None:
        raise ValueError('Conecta los puntos de la trayectoria a puntos (Tree Access, Point3d).')
    if hasattr(value, 'BranchCount'):
        laps = [[_xyz(p) for p in value.Branch(i) if p is not None] for i in range(value.BranchCount)]
    else:
        items = list(value)
        if items and isinstance(items[0], (list, tuple)) and items[0] and \
                isinstance(items[0][0], (list, tuple)):
            laps = [[_xyz(p) for p in lap] for lap in items]
        else:
            raise ValueError('puntos debe usar Tree Access: una rama por vuelta. Sin ramas no se '
                             'pueden separar las vueltas sin suponer un numero fijo de puntos.')
    laps = [lap for lap in laps if lap]
    if len(laps) < 2:
        raise ValueError('Se necesitan al menos dos vueltas (dos ramas) para calcular alturas.')
    return laps


def _num(v):
    return ('{:.6f}'.format(v)).rstrip('0').rstrip('.') if abs(v) >= 5e-7 else '0'


def write_gcode(laps, v=200.0, h_nom=30.0, v_min=50.0, v_max=350.0, tol_xy=3.0, dz=0.0,
                salida=True, dz_salida=40.0, f_salida=6000.0, omitir_ultima=False,
                primera_vuelta_validada=False):
    v = positive(v, 'v'); v_min = positive(v_min, 'v_min'); v_max = positive(v_max, 'v_max')
    h_nom = positive_num(h_nom, 'h_nom', 'mm'); tol_xy = positive_num(tol_xy, 'tol_xy', 'mm')
    f_salida = positive(f_salida, 'f_salida')
    dz = float(dz)
    if not (v_min <= v <= v_max):
        raise ValueError('Se requiere 0 < v_min <= v <= v_max.')
    laps = [list(l) for l in laps]
    notes = []
    if omitir_ultima:
        laps = laps[:-1]
        notes.append('Ultima rama omitida (omitir_ultima=True): era la vuelta extrapolada sobre TSrf.')
    laps = [[(x, y, z + dz) for x, y, z in lap] for lap in laps]
    means = [sum(p[2] for p in lap) / len(lap) for lap in laps]
    for i in range(1, len(laps)):
        if means[i] <= means[i - 1]:
            raise ValueError('La vuelta {} esta por debajo de la vuelta {} (Z media {:.1f} <= {:.1f}). '
                             'El orden esta invertido: revisa BSrf (inferior) y TSrf (superior).'.format(
                                 i + 1, i, means[i], means[i - 1]))
    for i in range(1, len(laps)):
        a, b = laps[i - 1][-1], laps[i][0]
        if dxy(a, b) <= 1e-6 and abs(b[2] - a[2]) > 1e-6:
            notes.append('Vuelta {}: empieza con una subida vertical; se conserva tal cual.'.format(i + 1))
    # Limite de pasada en la costura: si la rama no repite su primer punto, la vuelta se
    # cierra con el primer punto de la rama siguiente y la pasada nueva empieza despues.
    body = []
    for i, lap in enumerate(laps):
        cut = 0
        if i > 0:
            prev = laps[i - 1]
            closed_prev = len(prev) > 2 and dxy(prev[0], prev[-1]) <= 1e-6
            cut = 0 if closed_prev or len(lap) < 2 else 1
        for j, (x, y, z) in enumerate(lap):
            if j == cut:
                body.append(';PASS:{}'.format(i + 1))
            body.append('G1 X{} Y{} Z{}'.format(_num(x), _num(y), _num(z)))
    probe = '\n'.join(['G21', 'G90', 'G94'] + body + ['M2'])
    parsed = parse_gcode(probe, True)
    geo = geometry_analysis(parsed, v, h_nom, v_min, v_max, tol_xy)
    rows = geo['rows']
    if not primera_vuelta_validada:
        for row in rows:
            if row['status'] in ('SUPUESTO_PRIMERA_PASADA', 'INICIAL_ORIGEN_DESCONOCIDO'):
                row['status'] = 'BASE_PRIMERA_VUELTA_NO_VALIDADA'
                row['v_cmd'], row['block'] = None, True
    bad = [r for r in rows if r['block']]
    if geo['problems'] or bad:
        detail = '; '.join('{}({})'.format(r['line'], r['status']) for r in bad[:15])
        hint = ''
        if any(r['status'].startswith('SOPORTE_') for r in bad):
            hint = ' -> Revisar tramos_error y estaciones_error: costura, limites PASS y estaciones antes de cambiar tol_xy.'
        if any(r['status'] == 'GAP_NO_POSITIVO' for r in bad):
            hint += ' -> Hay tramos por debajo de la vuelta anterior: revisa el orden de las capas.'
        exc = ValueError('No se genera el G-code: {} movimientos sin altura valida. {}{}{}'.format(
            len(bad), detail, ' ...' if len(bad) > 15 else '', hint))
        exc.geometry = geo
        exc.records = parsed['records']
        raise exc
    allp = [p for lap in laps for p in lap]
    zmax = max(p[2] for p in allp)
    cx = (min(p[0] for p in allp) + max(p[0] for p in allp)) / 2.0
    cy = (min(p[1] for p in allp) + max(p[1] for p in allp)) / 2.0
    calc = [r for r in rows if r['calc']]
    n_first = sum(1 for r in rows if r['status'] in ('SUPUESTO_PRIMERA_PASADA', 'INICIAL_ORIGEN_DESCONOCIDO'))
    clamp_hi = sum(1 for r in calc if r['status'] == 'CLAMP_VMAX')
    clamp_lo = sum(1 for r in calc if r['status'] == 'CLAMP_VMIN')
    head = [
        '; G-CODE CONTINUO GENERADO EN GRASSHOPPER (MIGA). BORRADOR DE SIMULACION.',
        '; {} VUELTAS, {} DESTINOS, SIN MOVIMIENTOS VERTICALES ENTRE VUELTAS.'.format(len(laps), len(allp)),
        '; DESPLAZAMIENTO Z APLICADO: {} MM. NO APLICAR OTRO AL CONVERTIR.'.format(_num(dz)),
        '; VELOCIDAD TCP CON COMPENSACION DE ALTURA Y LONGITUD REAL 3D DEL TRAMO.',
        '; H_EF=H_VERTICAL_MEDIA*L_XY/L_3D; v=LIMITAR({}*{}/H_EF,{},{}).'.format(_num(v), _num(h_nom), _num(v_min), _num(v_max)),
        '; F EN MM/MIN, F=60*v; EN EL CONVERSOR USAR modo_velocidad=feed (o usar_F=True).',
        '; PRIMEROS {} DESTINOS A {} MM/S: PRIMERA VUELTA, ALTURA DE APOYO NO CONOCIDA.'.format(n_first, _num(v)),
        '; ALTURA MEDIDA CONTRA LA VUELTA ANTERIOR POR ESTACIONES LOCALES (TOL XY {} MM).'.format(_num(tol_xy)),
        '; ;PASS:n MARCA EL INICIO DE CADA VUELTA: CONTINUAR SIN PARADA.',
        '; EL G-CODE NO CONTROLA LA BOMBA NI DEFINE LAS ZONAS DE REDONDEO DE RAPID.',
        'G21', 'G90', 'G94']
    out, k = [], 0
    for line in body:
        if line.startswith(';'):
            out.append(line)
            continue
        r = rows[k]; k += 1
        out.append('{} F{}'.format(line, _num(60.0 * r['v_cmd'])))
    tail = []
    if salida:
        tail = ['; SALIDA FINAL DIAGONAL: CENTRO XY APROXIMADO, Z MAXIMO + {} MM'.format(_num(dz_salida)),
                'G1 X{} Y{} Z{} F{}'.format(_num(cx), _num(cy), _num(zmax + dz_salida), _num(f_salida))]
    lines = head + out + tail + ['M2']
    vs = [r['v_cmd'] for r in calc]
    summary = ['{} vueltas, {} destinos.'.format(len(laps), len(allp)),
               'Primera vuelta: {} destinos a {} mm/s (supuesto, sin apoyo).'.format(n_first, _num(v)),
               'Velocidad compensada: {} a {} mm/s en {} tramos; {} a v_max, {} a v_min.'.format(
                   _num(min(vs)) if vs else 'N/A', _num(max(vs)) if vs else 'N/A', len(vs), clamp_hi, clamp_lo)]
    errs = [r['e_xy'] for r in rows if r.get('e_xy') is not None]
    if errs:
        summary.append('Desvio XY maximo respecto a la vuelta anterior: {:.3f} mm (tol_xy {} mm).'.format(max(errs), _num(tol_xy)))
    if salida:
        summary.append('Salida final a ({}, {}, {}) con F{}.'.format(_num(cx), _num(cy), _num(zmax + dz_salida), _num(f_salida)))
    summary += notes
    return {'gcode': lines, 'resumen': '\n'.join(summary), 'auditoria': audit_lines(rows), 'rows': rows}


def run_writer(scope):
    scope.update({'gcode': None, 'resumen': '', 'auditoria': None})
    try:
        res = write_gcode(laps_from(scope.get('puntos')),
                          _optional(scope, 'v', 200.0), _optional(scope, 'h_nom', 30.0),
                          _optional(scope, 'v_min', 50.0), _optional(scope, 'v_max', 350.0),
                          _optional(scope, 'tol_xy', TOL_XY_DEFECTO), _optional(scope, 'dz', 0.0),
                          _optional(scope, 'salida', True), _optional(scope, 'dz_salida', 40.0),
                          _optional(scope, 'f_salida', 6000.0), bool(_optional(scope, 'omitir_ultima', False)))
    except Exception as exc:
        scope['resumen'] = 'ERROR: ' + as_text(exc) + '\nNo se genera G-code.'
        _runtime_message(scope, 'Error', scope['resumen'])
        return
    scope['gcode'], scope['resumen'], scope['auditoria'] = res['gcode'], res['resumen'], res['auditoria']




# ==========================================================================
# LAMINADOR NO PLANAR (componente GhPython): superficie/pared -> G-code
# Entradas: la pared (Brep) y las dos superficies que limitan las capas.
# Crea capas intermedias interpolando las dos superficies, corta la pared,
# alinea costura y sentido, divide en N puntos y arma una helice continua.
# ==========================================================================
def build_helix(contours):
    """contours: capas ordenadas de abajo arriba, N puntos cada una, costura y sentido
    alineados. Vuelta i: P_i(k) + (k/N)(P_{i+1}(k) - P_i(k)). Termina en P_n(0)."""
    n = len(contours[0])
    if any(len(c) != n for c in contours):
        raise ValueError('Todas las capas deben tener el mismo numero de puntos.')
    laps = []
    for i in range(len(contours) - 1):
        a, b = contours[i], contours[i + 1]
        lap = []
        for k in range(n):
            t = float(k) / n
            lap.append(tuple(a[k][j] + t * (b[k][j] - a[k][j]) for j in range(3)))
        laps.append(lap)
    laps[-1].append(tuple(contours[-1][0]))
    return laps


def best_uv_mapping(corners_a, corner_fn_b):
    """Elige la correspondencia UV de la superficie B (8 variantes) que mejor casa
    las esquinas con A en XY, para que la interpolacion no se retuerza."""
    best = None
    for swap in (False, True):
        for fu in (False, True):
            for fv in (False, True):
                cb = [corner_fn_b(u, v, fu, fv, swap) for (u, v) in ((0, 0), (1, 0), (0, 1), (1, 1))]
                d = sum(dxy(p, q) for p, q in zip(corners_a, cb))
                if best is None or d < best[0]:
                    best = (d, (fu, fv, swap))
    return best[1]


def _rhino():
    import Rhino.Geometry as rg
    import Rhino.Geometry.Intersect as rgi
    from System.Collections.Generic import List
    return rg, rgi, List


def _as_surface(geom, label):
    rg, rgi, List = _rhino()
    if geom is None:
        raise ValueError('Conecta ' + label + '.')
    if isinstance(geom, rg.Brep):
        if geom.Faces.Count != 1:
            raise ValueError(label + ' debe ser una superficie de una sola cara.')
        return geom.Faces[0]
    if isinstance(geom, rg.Surface):
        return geom
    raise ValueError(label + ' debe ser una superficie (type hint Surface).')


def _as_brep(geom):
    rg, rgi, List = _rhino()
    if isinstance(geom, rg.Brep):
        return geom
    if isinstance(geom, rg.Extrusion):
        return geom.ToBrep()
    if isinstance(geom, rg.Surface):
        return rg.Brep.CreateFromSurface(geom)
    if isinstance(geom, rg.Mesh):
        raise ValueError('pared es una malla; conviertela a Brep/superficie.')
    raise ValueError('Conecta la pared a pared (type hint Brep).')


def _eval(srf, u, v, fu=False, fv=False, swap=False):
    if swap:
        u, v = v, u
    if fu:
        u = 1.0 - u
    if fv:
        v = 1.0 - v
    p = srf.PointAt(srf.Domain(0).ParameterAt(u), srf.Domain(1).ParameterAt(v))
    return (p.X, p.Y, p.Z)


def slice_layers(pared, sup_a, sup_b, n_puntos=100, capas=None, h_nom=30.0, costura=None,
                 grid=25, tol=None):
    """Devuelve (contornos de abajo arriba [N puntos], curvas de capa, notas)."""
    rg, rgi, List = _rhino()
    brep = _as_brep(pared)
    sa, sb = _as_surface(sup_a, 'superficie_1'), _as_surface(sup_b, 'superficie_2')
    if tol is None:
        try:
            import Rhino
            tol = Rhino.RhinoDoc.ActiveDoc.ModelAbsoluteTolerance
        except Exception:
            tol = 0.01
    n_puntos = int(n_puntos)
    if n_puntos < 8:
        raise ValueError('puntos_por_vuelta debe ser al menos 8.')
    corners_a = [_eval(sa, u, v) for (u, v) in ((0, 0), (1, 0), (0, 1), (1, 1))]
    fu, fv, sw = best_uv_mapping(corners_a, lambda u, v, a, b, c: _eval(sb, u, v, a, b, c))
    ga = [[_eval(sa, i / float(grid - 1), j / float(grid - 1)) for j in range(grid)] for i in range(grid)]
    gb = [[_eval(sb, i / float(grid - 1), j / float(grid - 1), fu, fv, sw) for j in range(grid)] for i in range(grid)]

    def tween(t):
        pts = List[rg.Point3d]()
        for i in range(grid):
            for j in range(grid):
                a, b = ga[i][j], gb[i][j]
                pts.Add(rg.Point3d(a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]), a[2] + t * (b[2] - a[2])))
        srf = rg.NurbsSurface.CreateThroughPoints(pts, grid, grid, 3, 3, False, False)
        if srf is None:
            raise ValueError('No se pudo crear la superficie intermedia t={:.3f}.'.format(t))
        return rg.Brep.CreateFromSurface(srf)

    def contour(t, label):
        ok, crvs, pts = rgi.Intersection.BrepBrep(brep, tween(t), tol)
        if not ok or crvs is None or len(crvs) == 0:
            raise ValueError('La capa {} no corta la pared. Revisa que las dos superficies '
                             'atraviesen la pared completa.'.format(label))
        joined = rg.Curve.JoinCurves(crvs, 10 * tol)
        closed = []
        for c in joined:
            if not c.IsClosed:
                c.MakeClosed(10 * tol)
            if c.IsClosed:
                closed.append(c)
        if len(closed) != 1:
            raise ValueError('La capa {} da {} contornos cerrados ({} curvas). Se necesita un solo '
                             'contorno cerrado por capa.'.format(label, len(closed), len(joined)))
        return closed[0]

    def mean_z(c):
        params = c.DivideByCount(32, True)
        return sum(c.PointAt(t).Z for t in params) / float(len(params))

    c0, c1 = contour(0.0, 'inicial (superficie_1)'), contour(1.0, 'final (superficie_2)')
    flip = mean_z(c0) > mean_z(c1)
    notes = []
    if flip:
        notes.append('superficie_1 estaba arriba: se ordenan las capas de abajo arriba automaticamente.')
    if capas is None or int(capas) < 1:
        rise = abs(mean_z(c1) - mean_z(c0))
        capas = max(1, int(round(rise / h_nom)))
        notes.append('capas automatico: {} (altura media {:.1f} mm / h_nom {} mm).'.format(capas, rise, fmt(h_nom)))
    capas = int(capas)
    curves, contours = [], []
    seam_ref, sign_ref = None, None
    for i in range(capas + 1):
        t = float(i) / capas
        c = contour(1.0 - t if flip else t, str(i + 1))
        c = c.DuplicateCurve()
        if seam_ref is None:
            seam_ref = rg.Point3d(*costura) if costura is not None else c.PointAtStart
        ok, tc = c.ClosestPoint(seam_ref)
        if ok:
            c.ChangeClosedCurveSeam(tc)
        params = c.DivideByCount(n_puntos, True)
        pts = [(c.PointAt(p).X, c.PointAt(p).Y, c.PointAt(p).Z) for p in params]
        if len(pts) == n_puntos + 1 and d3(pts[0], pts[-1]) <= 1e-6:
            pts.pop()
        sign = 1 if signed_area(pts) > 0 else -1
        if sign_ref is None:
            sign_ref = sign
        elif sign != sign_ref:
            c.Reverse()
            params = c.DivideByCount(n_puntos, True)
            pts = [(c.PointAt(p).X, c.PointAt(p).Y, c.PointAt(p).Z) for p in params]
        if len(pts) == n_puntos + 1 and d3(pts[0], pts[-1]) <= 1e-6:
            pts.pop()
        if len(pts) != n_puntos:
            raise ValueError('La capa {} dio {} puntos en lugar de {}.'.format(i + 1, len(pts), n_puntos))
        seam_ref = c.PointAtStart
        curves.append(c)
        contours.append(pts)
    return contours, curves, notes


def run_slicer(scope):
    scope.update({'gcode': None, 'resumen': '', 'auditoria': None, 'capas_curvas': None,
                  'trayectoria': None, 'tramos_error': [], 'estaciones_error': []})
    try:
        cost = scope.get('costura')
        if cost is not None:
            cost = (cost.X, cost.Y, cost.Z)
        contours, curves, notes = slice_layers(
            scope.get('pared'), scope.get('superficie_1'), scope.get('superficie_2'),
            _optional(scope, 'puntos_por_vuelta', 100), scope.get('capas'),
            float(_optional(scope, 'h_nom', 30.0)), cost)
        laps = build_helix(contours)
        import Rhino.Geometry as rg
        from System.Collections.Generic import List
        preview = List[rg.Point3d]()
        for lap in laps:
            for p in lap:
                preview.Add(rg.Point3d(*p))
        scope['capas_curvas'] = curves
        scope['trayectoria'] = rg.PolylineCurve(preview)
        res = write_gcode(laps, _optional(scope, 'v', 200.0), _optional(scope, 'h_nom', 30.0),
                          _optional(scope, 'v_min', 50.0), _optional(scope, 'v_max', 350.0),
                          _optional(scope, 'tol_xy', TOL_XY_DEFECTO), _optional(scope, 'dz', 0.0),
                          _optional(scope, 'salida', True), _optional(scope, 'dz_salida', 40.0),
                          _optional(scope, 'f_salida', 6000.0), False,
                          bool(_optional(scope, 'primera_vuelta_validada', False)))
    except Exception as exc:
        if hasattr(exc, 'geometry'):
            scope['auditoria'] = audit_lines(exc.geometry['rows'])
            for i, row in enumerate(exc.geometry['rows']):
                if not row.get('block'):
                    continue
                if i > 0:
                    pa, pb = exc.records[i - 1]['p'], exc.records[i]['p']
                    scope['tramos_error'].append(rg.LineCurve(rg.Point3d(*pa), rg.Point3d(*pb)))
                for end in ('A', 'B'):
                    info = row.get('diagnostico_' + end, {})
                    scope['estaciones_error'].append(
                        'linea_interna={}; pasada={}; extremo={}; estado={}; estacion_trayectoria={}; esperada={}; semiventana={}'.format(
                            row['line'], row['pass'], end, info.get('err', 'OK'),
                            info.get('station_path'), info.get('expected'), info.get('half')))
        scope['resumen'] = 'ERROR: ' + as_text(exc) + '\nNo se genera G-code.'
        _runtime_message(scope, 'Error', scope['resumen'])
        return
    scope['gcode'], scope['auditoria'] = res['gcode'], res['auditoria']
    scope['resumen'] = '{} capas de corte, {} vueltas de helice, {} puntos por vuelta.\n'.format(
        len(contours), len(laps), len(contours[0])) + res['resumen'] + ('\n' + '\n'.join(notes) if notes else '')
    try:
        import Rhino.Geometry as rg
        from System.Collections.Generic import List
        pl = List[rg.Point3d]()
        for lap in laps:
            for p in lap:
                pl.Add(rg.Point3d(p[0], p[1], p[2]))
        scope['capas_curvas'] = curves
        scope['trayectoria'] = rg.PolylineCurve(pl)
    except ImportError:
        pass


if 'ghenv' in globals():
    _scope = globals()
    run_slicer(_scope)
    gcode, resumen, auditoria = _scope['gcode'], _scope['resumen'], _scope['auditoria']
    capas_curvas, trayectoria = _scope['capas_curvas'], _scope['trayectoria']
    tramos_error = _scope['tramos_error']
    estaciones_error = _scope['estaciones_error']
