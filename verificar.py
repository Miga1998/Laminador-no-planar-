# -*- coding: utf-8 -*-
from __future__ import print_function
import math
import Miga_Laminador_Diagnostico as m

def contours(radius, n=100):
    return [[(radius*math.cos(2*math.pi*k/n), radius*math.sin(2*math.pi*k/n), 30*i)
             for k in range(n)] for i in range(6)]

for radius in (30, 100, 500):
    laps=m.build_helix(contours(radius))
    r=m.write_gcode(laps,tol_xy=3,primera_vuelta_validada=True)
    calc=[x for x in r['rows'] if x['calc']]
    assert len(calc)==400
    assert all(x['status'] in ('OK','CLAMP_VMAX','CLAMP_VMIN') for x in calc)
    assert all(abs(x['h_a']-30)<0.002 and abs(x['h_b']-30)<0.002 for x in calc)
    assert all(199 < x['v_cmd'] < 203 for x in calc)
    assert [p['line_start'] for p in m.geometry_analysis(m.parse_gcode('\n'.join(r['gcode'])),200,30,50,350,3)['passes']] # markers survive output
    print('PASS circle radius',radius,'400 compensated segments; 30 mm endpoint gaps')
try:
    m.write_gcode(m.build_helix(contours(100)),tol_xy=3)
    raise AssertionError('Unvalidated base accepted')
except ValueError as e:
    assert 'BASE_PRIMERA_VUELTA_NO_VALIDADA' in str(e)
    assert all(x['v_cmd'] is None for x in e.geometry['rows'] if x['block'])
    print('PASS unvalidated base blocks output')
c=contours(100)
c[2:]=[[(x+20,y,z) for x,y,z in lap] for lap in c[2:]]
try:
    m.write_gcode(m.build_helix(c),tol_xy=3,primera_vuelta_validada=True)
    raise AssertionError('Unsupported deposition accepted')
except ValueError as e:
    bad=[x for x in e.geometry['rows'] if x['block']]
    assert any(x['status'].startswith('SOPORTE_') for x in bad)
    assert all(x['v_cmd'] is None for x in bad)
    print('PASS unsupported deposition blocks; diagnostic rows retained:',len(bad))
a,b,lxy,l3,h=m.segment_height((0,0,60),(3,4,65),30,35)
assert abs(h-30*5/math.sqrt(50))<1e-9
assert m.speed_from_height(0.2,200,30,50,350)[1]==350
assert m.speed_from_height(1000,200,30,50,350)[1]==50
print('PASS effective height formula and both speed clamps')
