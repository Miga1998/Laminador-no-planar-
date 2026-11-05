# MIGA: carga y diagnóstico

Archivo completo: Miga_Laminador_Diagnostico.py. Se parte del original adjunto, no de la versión de diagnóstico mencionada, que no se adjuntó. Se incluyen el script completo, el cargador y las comprobaciones numericas.

## Carga en Rhino 7 / Grasshopper

1. Guarda una copia del archivo .gh. Desactiva el Solver mientras editas.
2. Usa el mismo componente GhPython heredado (IronPython 2.7), no un componente Python 3. Abre su editor y selecciona TODO el contenido. Abre el .py en un editor de texto, selecciona TODO y copia/pega texto plano, sin cercas Markdown. Si el editor ofrece abrir/importar código, también puedes seleccionar el .py completo.
3. La primera línea debe ser `# -*- coding: utf-8 -*-`. La última es `estaciones_error = _scope['estaciones_error']`; la penúltima es `tramos_error = _scope['tramos_error']`. `groups.add(group)` no es la primera línea del archivo: pertenece al analizador de G-code. No lo arregles quitando sangría ni pegando únicamente esa función.
4. Conserva entradas con nombres exactos `pared`, `superficie_1`, `superficie_2`, con acceso Item y geometría Rhino: pared Brep y superficies convertibles a Surface/Brep. Conserva `costura` si ya existe (Point3d, Item). No uses los puertos MIGA_Pasadas / MIGA_Motor.
5. Entradas opcionales, Item: `puntos_por_vuelta`=100, `capas` entero o desconectado (automático), `h_nom`=30, `v`=200, `v_min`=50, `v_max`=350, `tol_xy`=3, `dz`=0, `salida`=False durante diagnóstico. Si activas salida: `dz_salida`=40 y `f_salida`=6000. Una entrada ya conectada prevalece sobre estos valores por defecto.
6. Añade entrada booleana `primera_vuelta_validada`=False. Se requiere True para exportar, únicamente después de comprobar externamente el apoyo inicial y aceptar la velocidad nominal de esa primera vuelta. El script no calcula su altura contra la base. Con False conserva la previsualización y bloquea el G-code; también marcará la primera vuelta en rojo si conectas la salida de errores.
7. Salidas exactas: `gcode`, `resumen`, `auditoria`, `capas_curvas`, `trayectoria`, `tramos_error`, `estaciones_error`. Conecta resumen/auditoria/estaciones_error a Panels; tramos_error a Custom Preview rojo. Oculta la previsualización normal del componente si dificulta distinguir los errores. Reactiva Solver.

## Cambios y evidencia

- Cierre de vuelta para ubicar ;PASS: coincidencia XY a 1e-6 mm, independiente de tol_xy. La vuelta abierta incluye su cierre mediante el primer punto de la siguiente rama; el siguiente ;PASS se coloca después de ese punto. pass_path conserva el punto anterior como ancla.
- Desactivada la reclasificación automática de G1 cortos ascendentes como transiciones: un G1 de deposición debe validarse. Los movimientos declarados explícitamente sin deposición siguen siendo distinguibles.
- tol_xy predeterminado 3 mm en el escritor y en run_slicer. No se amplió la ventana de match_pass ni se relajó el bloqueo por altura.
- Se conservan curvas, trayectoria, auditoría y segmentos bloqueados cuando falla el escritor. Los extremos fallidos incluyen estación acumulada, estación esperada y semiventana. Las líneas son del G-code interno de prueba, incluidos comentarios ;PASS; no son índices de puntos ni necesariamente líneas de la salida final con cabecera.
- Los tramos bloqueados no reciben v_cmd nominal en la auditoría. No se exporta G-code parcial.
- Se normaliza la posible repetición del extremo en DivideByCount para obtener N puntos únicos; se corrige el divisor de la media de Z al número real de muestras. Queda pendiente ejecutar estas llamadas RhinoCommon en Rhino 7.
- Fórmula conservada: h_eff=((h_A+h_B)/2)*L_XY/L_3D; v=clamp(200*30/h_eff,50,350); F=60*v. El ancho 25 mm no es una entrada del algoritmo; su relación con caudal/ancho real necesita calibración física.

Pruebas con Python 3.12: círculos de radios 30, 100 y 500 mm, 100 puntos y seis contornos; 400 tramos compensados por caso con h_A=h_B=30 mm y velocidad esperada. Caso desplazado 20 mm: bloqueo de 162 tramos y diagnósticos retenidos. Base no validada: bloqueo. Fórmula inclinada y ambos límites de velocidad: pasan. El original en radio 30 daba aproximadamente 102–203 mm/s y omitía tramos de compensación por la clasificación de transiciones. Gramática Python 2 comprobada con lib2to3; NO equivale a ejecución en IronPython/Rhino.

## Qué revisar en el modelo

No hay .3dm/.gh ni coordenadas de las vueltas del caso de 93 fallos. No se ha reproducido ese fallo concreto. Tampoco se recibió Miga_Laminador_Diagnostico.py anterior.

slice_layers alinea la costura por ClosestPoint 3D a la costura de la capa anterior y normaliza el sentido por área XY. Esto no garantiza correspondencia entre lóbulos en una pared compleja. build_helix interpola puntos de igual índice y debe comprobarse visualmente que no crea atajos en esquinas. match_pass sigue estaciones XY sobre la vuelta inferior con ventana local y diferencia cíclica cuando el soporte está cerrado en XY. La curva helicoidal puede tener distinta Z en los extremos de esa costura; pick_pair intenta resolverla por coherencia de avance. No se cambió ese algoritmo sin datos del fallo.

Para FUERA_DE_VENTANA compara los extremos rojos y estaciones_error con el límite ;PASS de cada vuelta: hay candidatos laterales, pero fuera de la ventana, por lo que aumentar tol_xy no es el primer diagnóstico. SIN_SOPORTE y AMBIGUO tienen causas diferentes. El período 101 coincide con 100 destinos más un comentario ;PASS, pero no demuestra por sí solo que el defecto esté en la costura.

Para continuar hace falta el .gh/.3dm con pared y superficies, o coordenadas XYZ ordenadas de cada contorno/vuelta, junto con auditoria y estaciones_error. Comprueba unidades mm, sentido, costura y ausencia de autointersecciones. No hace falta ejecutar el robot para esta fase.

RobotStudio pendiente: conversión RAPID en modo feed para respetar F/60; herramientas, workobject, orientación, configuración, alcance, singularidades, colisiones, redondeo y velocidad efectiva. No se validó en IRC5 ni la sincronización de bomba. La salida diagonal original se conserva, pero no está validada como retirada ni controla la bomba; por eso se recomienda salida=False al diagnosticar.

## Pruebas locales del nucleo numerico

Desde la carpeta de este repositorio: `python3 verificar.py`.
Las pruebas no requieren Rhino, no sustituyen la validacion en GhPython y no ejecutan el robot.

## Descarga

Usa Code > Download ZIP en GitHub o descarga MIGA_carga_completa.zip desde su pagina de archivo.
Extrae Miga_Laminador_Diagnostico.py en C:\MIGA\ y pega el contenido de Cargador_GhPython.py en el mismo componente GhPython.
El cargador usa execfile con globals para conservar las entradas y salidas del componente y cargar el archivo entero, sin copiar sus 1481 lineas.
