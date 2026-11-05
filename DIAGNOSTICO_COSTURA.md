# Diagnostico de los 93 movimientos sin altura valida

## Resultado

La reproduccion numerica del modelo da exactamente 93 bloqueos antes de corregir la alineacion de costura y cero despues de buscar la costura en XY. Se mantienen 100 puntos por vuelta, tol_xy=3 mm, h_nom=30 mm, v=200 mm/s y limites 50-350 mm/s. La geometria original no se ha publicado en este repositorio.

## Metodo y limites

Se descomprimio el archivo .gh sin ejecutar sus componentes y se leyeron sus tres referencias de geometria. Las tres existen en el .3dm recibido, cuyo documento usa milimetros. La pared es una extrusion vertical de un contorno compuesto por 14 caras; la superficie inferior es horizontal y la superior es un plano inclinado.

Se leyeron las NURBS con rhino3dm y se reconstruyeron los cortes con los planos intermedios, conservando la correspondencia UV del laminador. Se evaluo el contorno por intervalos de nudos, se localizo la costura por proyeccion sobre sus segmentos y se muestreo por longitud 3D. Esta es una reconstruccion numerica con polilineas densas, no una ejecucion de BrepBrep ni de GhPython. Con 80 y 240 muestras por intervalo de nudos, se obtienen los mismos 93 bloqueos antes y cero despues. Coinciden las primeras 15 lineas y estados reportados por el usuario.

## Antes de la correccion

| Estado | Movimientos bloqueados |
| --- | ---: |
| SOPORTE_B:SIN_SOPORTE | 29 |
| SOPORTE_A:SIN_SOPORTE | 29 |
| SOPORTE_A:AMBIGUO | 14 |
| SOPORTE_B:FUERA_DE_VENTANA | 14 |
| SOPORTE_A:SIN_SOPORTE/B:SIN_SOPORTE | 7 |
| Total | 93 |

La costura se desplaza en XY entre 0.43 y 10.13 mm por capa, acumulando aproximadamente 109.81 mm respecto a la primera. No es un movimiento lateral de la pared: surge al minimizar distancia 3D a la costura de la capa anterior sobre una capa inclinada.

Los errores interiores en las lineas 647-648, 696-697 y 748-749 y los errores periodicos de costura en 812/814, 913/915 y 1014/1016 aparecen en la reproduccion. Esto permite atribuir el patron al cambio de fase de las muestras, sin ampliar tolerancias para hacerlo pasar.

## Correccion minima

En slice_layers, _align_seam_xy duplica el contorno y proyecta la copia a WorldXY. Busca el punto de referencia con Z=0 en esa copia y aplica el parametro encontrado como costura de la curva 3D original. La proyeccion afin conserva el parametro; no se usa la geometria proyectada para imprimir.

Se conservan el muestreo por longitud 3D, el sentido por area XY, build_helix, write_gcode, match_pass y la formula de h_eff. Las proyecciones o cambios de costura fallidos provocan un error explicito. No hay soporte artificial ni velocidades de reserva para tramos bloqueados.

## Despues de la correccion

En la reconstruccion con los parametros nominales:

- 21 vueltas, 2101 destinos.
- 2000 tramos con altura calculada contra la vuelta inferior; cero bloqueados.
- 101 destinos iniciales bajo el supuesto explicito de base validada, que el algoritmo no mide.
- Velocidad compensada aproximada: 152.976 a 350 mm/s; 94 tramos limitados a v_max.
- h_eff aproximada: 13.113 a 39.222 mm.
- Desvio XY maximo aproximado: 2.410 mm, inferior a tol_xy=3 mm.
- Al invertir el sentido del contorno y mantener costura XY tambien se obtienen cero bloqueos.

El limite de 350 mm/s implica que en esos 94 tramos no se alcanza la compensacion ideal de caudal/altura. La formula respeta el limite solicitado; el ancho real de cordon requiere calibracion del proceso.

## Comprobaciones y pasos pendientes

python3 verificar.py comprueba circulos, altura inclinada, limites de velocidad y bloqueo de base no validada/deposicion sin apoyo. python3 verificar_costura.py reproduce deriva por distancia 3D y comprueba la correccion con curvas sinteticas y una interfaz simulada de Rhino. La simulacion de interfaz no acredita ejecucion en RhinoCommon.

En Rhino 7: reemplaza el archivo C:\MIGA\Miga_Laminador_Diagnostico.py con el actualizado; conserva el cargador, entradas y salidas. Recalcula el componente y comprueba que resumen incluye "Costura alineada en XY". Mantener tol_xy=3 mm y primera_vuelta_validada segun la comprobacion real de la base. La llamada PlanarProjection/ClosestPoint y la previsualizacion se deben verificar en el GhPython real.

El archivo .gh recibido contiene un fragmento anterior de script en el laminador; abrirlo no reemplaza el archivo local corregido. Esta entrega actualiza el .py y el ZIP, no modifica el .gh/.3dm originales.

RobotStudio/IRC5, herramienta, workobject, orientaciones, alcance, colisiones, redondeo, velocidades efectivas y sincronizacion de bomba siguen pendientes. Esta entrega no es un programa de robot validado.
