# -*- coding: utf-8 -*-
import os

ruta = r"C:\MIGA\Miga_Laminador_Diagnostico.py"
if not os.path.isfile(ruta):
    raise IOError("No existe el archivo: " + ruta)

execfile(ruta, globals())
