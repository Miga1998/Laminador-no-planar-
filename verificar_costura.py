# -*- coding: utf-8 -*-
"""Seam regression checks without Rhino; run with python3 verificar_costura.py.

The parameterized circle fixture models the small API used by _align_seam_xy.
These tests exercise the real numerical slicer, but do NOT run RhinoCommon.
"""
from __future__ import division, print_function
import math
import unittest
import Miga_Laminador_Diagnostico as m


class Point(object):
    def __init__(self, x, y, z):
        self.X, self.Y, self.Z = x, y, z

    def xyz(self):
        return (self.X, self.Y, self.Z)


class FakeGeometry(object):
    Point3d = Point

    class Plane(object):
        WorldXY = 'world_xy'

    class Transform(object):
        @staticmethod
        def PlanarProjection(plane):
            if plane != 'world_xy':
                raise ValueError('Unexpected projection plane')
            return 'project_xy'


class TiltedCircle(object):
    """x=R*cos(t), y=R*sin(t), z=height+amplitude*sin(t)."""
    def __init__(self, height=0.0, radius=100.0, amplitude=20.0, failure=None):
        self.height, self.radius, self.amplitude = height, radius, amplitude
        self.failure = failure
        self.projected = False
        self.seam = 0.0

    def DuplicateCurve(self):
        other = TiltedCircle(self.height, self.radius, self.amplitude, self.failure)
        other.projected, other.seam = self.projected, self.seam
        return other

    def Transform(self, transform):
        if transform != 'project_xy':
            raise ValueError('Unexpected transform')
        if self.failure == 'transform':
            return False
        self.projected = True
        return True

    def GetLength(self):
        if self.failure == 'length':
            return 0.0
        # The helper only requests projected length; this is exact for that case.
        return 2 * math.pi * self.radius

    def PointAt(self, t):
        return Point(self.radius * math.cos(t), self.radius * math.sin(t),
                     0.0 if self.projected else self.height + self.amplitude * math.sin(t))

    @property
    def PointAtStart(self):
        return self.PointAt(self.seam)

    def ClosestPoint(self, reference):
        if self.failure == 'closest':
            return False, 0.0
        if self.projected:
            return True, math.atan2(reference.Y, reference.X) % (2 * math.pi)
        # Numerical 3D nearest point; no substitute geometry/support matching.
        def distance(t):
            p = self.PointAt(t)
            return ((p.X - reference.X)**2 + (p.Y - reference.Y)**2 +
                    (p.Z - reference.Z)**2)
        step = 2 * math.pi / 512
        center = min(range(512), key=lambda i: distance(i * step)) * step
        lo, hi = center - step, center + step
        for unused in range(70):
            a, b = lo + (hi - lo) / 3, hi - (hi - lo) / 3
            if distance(a) <= distance(b):
                hi = b
            else:
                lo = a
        return True, ((lo + hi) / 2) % (2 * math.pi)

    def ChangeClosedCurveSeam(self, parameter):
        if self.failure == 'seam':
            return False
        self.seam = parameter
        return True


def synthetic_contours(use_xy):
    """Uniform angular samples isolate seam selection, not Rhino arc division."""
    contours, reference = [], Point(100.0, 0.0, 0.0)
    for i in range(14):
        curve = TiltedCircle(height=30.0 * i)
        if use_xy:
            m._align_seam_xy(curve, reference, FakeGeometry, str(i + 1))
        else:
            ok, parameter = curve.ClosestPoint(reference)
            if not ok:
                raise AssertionError('Fixture closest-point calculation failed')
            curve.ChangeClosedCurveSeam(parameter)
        reference = curve.PointAtStart
        contours.append([curve.PointAt(curve.seam + 2 * math.pi * k / 100).xyz()
                         for k in range(100)])
    return contours


class SeamTests(unittest.TestCase):
    def test_height_of_reference_does_not_move_xy_seam(self):
        theta = 0.35
        for ref_z in (-1000.0, 0.0, 1000.0):
            curve = TiltedCircle(height=250.0)
            reference = Point(100 * math.cos(theta), 100 * math.sin(theta), ref_z)
            m._align_seam_xy(curve, reference, FakeGeometry, 'reference Z')
            self.assertAlmostEqual(curve.seam, theta, places=10)
            self.assertAlmostEqual(curve.PointAtStart.X, reference.X, places=9)
            self.assertAlmostEqual(curve.PointAtStart.Y, reference.Y, places=9)

    def test_projection_does_not_flatten_original_geometry(self):
        curve = TiltedCircle(height=250.0)
        reference = Point(0.0, 100.0, -700.0)
        m._align_seam_xy(curve, reference, FakeGeometry, 'original preserved')
        self.assertFalse(curve.projected)
        self.assertAlmostEqual(curve.PointAtStart.Z, 270.0, places=9)

    def test_projection_failure_blocks(self):
        self.assert_failure('transform')

    def test_degenerate_projected_curve_blocks(self):
        self.assert_failure('length')

    def test_closest_point_failure_blocks(self):
        self.assert_failure('closest')

    def test_seam_change_failure_blocks(self):
        self.assert_failure('seam')

    def assert_failure(self, mode):
        with self.assertRaises(ValueError):
            m._align_seam_xy(TiltedCircle(failure=mode), Point(100, 0, 30),
                             FakeGeometry, 'failure ' + mode)

    def test_3d_nearest_seam_drift_reproduces_support_failure(self):
        contours = synthetic_contours(False)
        self.assertGreater(m.dxy(contours[0][0], contours[1][0]), 3.0)
        with self.assertRaises(ValueError) as caught:
            m.write_gcode(m.build_helix(contours), tol_xy=3.0, salida=False,
                          primera_vuelta_validada=True)
        rows = caught.exception.geometry['rows']
        self.assertTrue(any(row['block'] and 'FUERA_DE_VENTANA' in row['status']
                            for row in rows))

    def test_xy_seam_keeps_real_support_validation_and_formula(self):
        result = m.write_gcode(m.build_helix(synthetic_contours(True)), tol_xy=3.0,
                               salida=False, primera_vuelta_validada=True)
        self.assertFalse(any(row['block'] for row in result['rows']))
        calculated = [row for row in result['rows'] if row['calc']]
        self.assertEqual(len(calculated), 1200)
        for row in calculated:
            self.assertAlmostEqual(row['h_a'], 30.0, places=3)
            self.assertAlmostEqual(row['h_b'], 30.0, places=3)
            self.assertEqual(row['v_cmd'], m.speed_from_height(
                row['h_eff'], 200.0, 30.0, 50.0, 350.0)[1])


if __name__ == '__main__':
    unittest.main(verbosity=2)
