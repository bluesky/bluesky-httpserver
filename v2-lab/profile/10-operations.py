"""The lab's plans, described for both front ends: ``@operation`` makes the v2 controller's catalog, and
``@parameter_annotation_decorator`` gives bluesky-httpserver v1 (through RE Manager) the same types, ranges and
device lists, so that compare.py runs the same plans on both, each validated as well as it can be.

Runs after 00-tst-sim-tools.py in the same namespace, which provides ``tpw``, ``dcm_c1`` and ``dcm_c2``.

Clients name devices by the ids in each ``devices`` map; the controller replaces them with these namespace names,
and RE Worker replaces the names with the devices listed for the parameter in its annotation.
"""

from bluesky.plans import count
from bluesky_queueserver import operation, parameter_annotation_decorator
from tst_sim_tools.plans.bmm import change_energy_stub, scan_energy

_DETECTOR_NAMES = ["diag1", "diag2", "nano_bpm", "xrd_det", "xas_det"]
_CRYSTAL_NAMES = ["si111", "si311"]
_DETECTORS = {name: name for name in _DETECTOR_NAMES}
_CRYSTALS = {name: name for name in _CRYSTAL_NAMES}
# BMM Mode C, the only range the XRT model and change_energy_stub accept.
_ENERGY_MIN, _ENERGY_MAX = 6000, 8000
_ENERGY_EV = {"type": "number", "minimum": _ENERGY_MIN, "maximum": _ENERGY_MAX}
_BAND_EV = {"type": "number", "exclusiveMinimum": 0}
_DEVICE_LIST = {"type": "array", "items": {"type": "string"}, "minItems": 1}
# Bounds on how long one operation may hold the instrument: a count takes at most _COUNT_MAX x _DELAY_MAX s
# (about 17 min), a scan visits at most _ENERGIES_MAX energies.
_COUNT_MAX, _DELAY_MAX, _ENERGIES_MAX = 100, 10, 50

# The same contract for RE Manager. Its ranges are inclusive, so the band's lower bound is only approximate.
_V1_DETECTORS = {"annotation": "typing.List[Detectors]", "devices": {"Detectors": _DETECTOR_NAMES}}
_V1_CRYSTAL = {"annotation": "Crystals", "devices": {"Crystals": _CRYSTAL_NAMES}}
_V1_ENERGY = {"annotation": "float", "min": _ENERGY_MIN, "max": _ENERGY_MAX}
_V1_BAND = {"annotation": "float", "min": 0}


@operation(
    id="tst.count",
    version="1",
    request_model={
        "type": "object",
        "properties": {
            "detectors": _DEVICE_LIST,
            "num": {"type": "integer", "minimum": 1, "maximum": _COUNT_MAX},
            "delay": {"type": "number", "minimum": 0, "maximum": _DELAY_MAX},
        },
        "required": ["detectors"],
        "additionalProperties": False,
    },
    devices={"detectors": _DETECTORS},
)
@parameter_annotation_decorator(
    {
        "parameters": {
            "detectors": _V1_DETECTORS,
            "num": {"annotation": "int", "min": 1, "max": _COUNT_MAX},
            "delay": {"annotation": "float", "min": 0, "max": _DELAY_MAX},
        }
    }
)
def count_operation(detectors, num=1, delay=0):
    """Read ``detectors`` ``num`` times, ``delay`` seconds apart."""
    yield from count(detectors, num=num, delay=delay)


@operation(
    id="bmm.change_energy",
    version="1",
    request_model={
        "type": "object",
        "properties": {"energy": _ENERGY_EV, "crystal": {"type": "string"}, "band": _BAND_EV},
        "required": ["energy", "crystal"],
        "additionalProperties": False,
    },
    devices={"crystal": _CRYSTALS},
)
@parameter_annotation_decorator({"parameters": {"energy": _V1_ENERGY, "crystal": _V1_CRYSTAL, "band": _V1_BAND}})
def change_energy_operation(energy, crystal, band=1.0):
    """Tune the wiggler band and the fixed-exit DCM to ``energy`` eV with ``crystal``; opens no run."""
    yield from change_energy_stub(tpw, dcm_c1, dcm_c2, crystal, energy, band=band)  # noqa: F821


@operation(
    id="bmm.scan_energy",
    version="1",
    request_model={
        "type": "object",
        "properties": {
            "detectors": _DEVICE_LIST,
            "energies": {"type": "array", "items": _ENERGY_EV, "minItems": 1, "maxItems": _ENERGIES_MAX},
            "crystal": {"type": "string"},
            "band": _BAND_EV,
        },
        "required": ["detectors", "energies", "crystal"],
        "additionalProperties": False,
    },
    devices={"detectors": _DETECTORS, "crystal": _CRYSTALS},
)
@parameter_annotation_decorator(
    {
        "parameters": {
            "detectors": _V1_DETECTORS,
            "energies": {"annotation": "typing.List[float]", "min": _ENERGY_MIN, "max": _ENERGY_MAX},
            "crystal": _V1_CRYSTAL,
            "band": _V1_BAND,
        }
    }
)
def scan_energy_operation(detectors, energies, crystal, band=1.0):
    """Tune to each of ``energies`` (eV) in turn and read ``detectors`` there, in one run."""
    yield from scan_energy(detectors, tpw, dcm_c1, dcm_c2, crystal, energies, band=band)  # noqa: F821
